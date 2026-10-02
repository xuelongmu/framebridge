"""Explicit ETag evidence, separate from full-original SHA-256 verification."""
import hashlib
import re
from pathlib import Path

import requests

from .downloader import check_url
from .storage import UploaderError
from .uploader import part_bounds, verify_remote


def original_probe(rendition, http):
    """Read at most one body byte; never follow a redirect or accept a full GET."""
    check_url(rendition['downloadUrl'])
    size = rendition['filesizeInBytes']
    try:
        with http.get(rendition['downloadUrl'], headers={'Range': 'bytes=0-0',
                      'Accept-Encoding': 'identity'}, stream=True, allow_redirects=False,
                      timeout=(10, 30)) as response:
            if response.status_code != 206:
                raise UploaderError(f'Original probe HTTP {response.status_code}; bounded 206 required.')
            if response.headers.get('Content-Range') != f'bytes 0-0/{size}':
                raise UploaderError('Original probe returned an unexpected range.')
            if response.headers.get('Content-Encoding', 'identity') not in ('', 'identity'):
                raise UploaderError('Compressed original probe is unsupported.')
            if response.headers.get('Content-Length', '1') != '1':
                raise UploaderError('Original probe returned an unexpected length.')
            etag = response.headers.get('ETag', '')
            if not re.fullmatch(r'"[0-9a-fA-F]{32}(?:-[1-9][0-9]{0,4})?"', etag):
                raise UploaderError('Original has no supported strong MD5-shaped ETag.')
            if len(response.raw.read(1)) != 1:
                raise UploaderError('Original probe body was truncated.')
            return etag
    except requests.RequestException:
        raise UploaderError('Original probe failed; network details suppressed.') from None


def _identity(rendition):
    return tuple(rendition[k] for k in ('asset_id', 'media_id', 'key', 'filesizeInBytes'))


def _mark(path):
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def verify_etag(api, asset_id, local_file, *, part_count=None, http=None, progress=None):
    path = Path(local_file)
    if path.is_symlink() or not path.is_file():
        raise UploaderError('ETag comparison requires a regular, non-symlink local file.')
    before = _mark(path)
    size = before[2]
    if size <= 0:
        raise UploaderError('ETag comparison requires a nonempty local file.')
    verify_remote(api, asset_id, size)
    original = api.rendition(asset_id, 'original')
    if original['filesizeInBytes'] != size:
        raise UploaderError('Original size does not match the local source.')
    transport = http or requests.Session()
    if http is None:
        transport.trust_env = False
    try:
        remote = original_probe(original, transport)
        value = remote.strip('"').lower()
        multipart = '-' in value
        count = int(value.rsplit('-', 1)[1]) if multipart else 1
        if count > 10000 or (part_count is not None and (type(part_count) is not int or part_count != count)):
            raise UploaderError('ETag part count is unsupported or does not match --part-count.')
        part_bounds(size, count, count - 1)
        whole, parts = hashlib.sha256(), []
        with path.open('rb') as stream:
            for index in range(count):
                _, remaining = part_bounds(size, count, index)
                digest = hashlib.md5(usedforsecurity=False)
                while remaining:
                    data = stream.read(min(4 * 1024 * 1024, remaining))
                    if not data:
                        raise UploaderError('Local source truncated during ETag comparison.')
                    digest.update(data)
                    whole.update(data)
                    remaining -= len(data)
                parts.append(digest.digest())
                if progress:
                    progress({'hashed_parts': index + 1, 'total_parts': count})
            if stream.read(1):
                raise UploaderError('Local source grew during ETag comparison.')
        if _mark(path) != before:
            raise UploaderError('Local source changed during ETag comparison.')
        candidate = (hashlib.md5(b''.join(parts), usedforsecurity=False).hexdigest() + f'-{count}'
                     if multipart else parts[0].hex())
        if candidate != value:
            raise UploaderError('ETag does not match the local source with Framebridge part boundaries; '
                                'content, part layout, or ETag semantics may differ.')
        current = api.rendition(asset_id, 'original')
        if _identity(current) != _identity(original) or original_probe(current, transport) != remote:
            raise UploaderError('Remote original changed during ETag comparison.')
        verify_remote(api, asset_id, size)
        if _mark(path) != before:
            raise UploaderError('Local source changed during ETag comparison.')
        return {'verification_level': 'multipart_etag_match' if multipart else 'etag_md5_match',
                'etag_match': True, 'checksum_verified': False, 'etag': remote,
                'part_count': count, 'local_sha256': whole.hexdigest(),
                'probe_bytes_consumed': 2,
                'assumption': 'MD5 ETag semantics; multipart uses Framebridge equal-width part boundaries.',
                'note': 'Not a full-download SHA-256 comparison or a cryptographic authenticity guarantee.'}
    finally:
        if http is None:
            transport.close()
