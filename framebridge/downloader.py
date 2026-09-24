"""Bounded range downloads with durable, URL-free checkpoints."""
import hashlib
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .storage import UploaderError, atomic_write, exclusive_lock

CHUNK = 4 * 1024 * 1024


def safe_name(name):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')[:120]
    if not value or value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM'+str(i) for i in range(1,10)), *('LPT'+str(i) for i in range(1,10))}:
        value = '_' + value
    return value


def check_url(url):
    p = urlparse(url)
    if p.scheme != 'https' or p.hostname != 'stream-download.frame.io' or p.username or p.password or p.port not in (None, 443):
        raise UploaderError('Unverified download host; no request sent.')


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(CHUNK), b''):
            h.update(chunk)
    return h.hexdigest()


def download(api, asset_id, selection, output, *, max_bytes=None, expected_sha256=None,
             progress=None, http=None, sleep=time.sleep):
    output = Path(output).absolute()
    output.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(output.with_name(output.name + '.framebridge.lock')):
        return _download(api, asset_id, selection, output, max_bytes=max_bytes,
            expected_sha256=expected_sha256, progress=progress, http=http, sleep=sleep)


def _download(api, asset_id, selection, output, *, max_bytes, expected_sha256, progress, http, sleep):
    if max_bytes is not None and max_bytes <= 0:
        raise UploaderError('--max-bytes must be positive.')
    if expected_sha256 and not re.fullmatch('[0-9a-fA-F]{64}', expected_sha256):
        raise UploaderError('--sha256 must contain 64 hexadecimal characters.')
    rendition = api.rendition(asset_id, selection)
    size = rendition['filesizeInBytes']
    target = min(size, max_bytes) if max_bytes else size
    identity = {k: rendition[k] for k in ('asset_id', 'media_id', 'key', 'filesizeInBytes')}
    identity['target_bytes'] = target
    part = output.with_name(output.name + '.part')
    meta = output.with_name(output.name + '.framebridge.json')
    for path in (output, part, meta):
        if path.is_symlink():
            raise UploaderError('Refusing a symlink download target or state file.')
    record = json.loads(meta.read_text()) if meta.exists() else None
    if record and record['identity'] != identity:
        raise UploaderError('Download identity changed; choose a different output path.')
    if output.exists():
        if record and output.stat().st_size == target and sha256(output) == record.get('sha256'):
            if expected_sha256 and record['sha256'] != expected_sha256.lower():
                raise UploaderError('Expected checksum does not match the existing download.')
            return dict(path=str(output), bytes=target, sha256=record['sha256'], sample=target<size, skipped=True)
        raise UploaderError('Output already exists; it will not be overwritten.')
    if part.exists() and not record:
        raise UploaderError('Partial file has no identity checkpoint; choose another output path.')
    if not record:
        record = dict(identity=identity, offset=0, sha256=hashlib.sha256(b'').hexdigest(), etag=None)
        atomic_write(meta, json.dumps(record).encode())
        with part.open('xb'):
            pass
    elif not part.exists():
        if record['offset']:
            raise UploaderError('Checkpoint exists but partial file is missing.')
        with part.open('xb'):
            pass
    offset = record['offset']
    if not isinstance(offset, int) or not 0 <= offset <= target or part.stat().st_size < offset:
        raise UploaderError('Invalid or truncated download checkpoint.')
    # Crash after append but before checkpoint: discard only uncommitted suffix.
    with part.open('r+b') as f:
        f.truncate(offset)
    if sha256(part) != record['sha256']:
        raise UploaderError('Partial file checksum changed; resume refused.')
    digest = hashlib.sha256()
    with part.open('rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            digest.update(block)
    transport = http or requests.Session()
    own_transport = http is None
    if own_transport:
        transport.trust_env = False  # no inherited netrc credentials on signed media requests
    started, initial = time.monotonic(), offset
    try:
        while offset < target:
            end = min(target, offset + CHUNK) - 1
            for attempt in range(3):
                if attempt:
                    rendition = api.rendition(asset_id, rendition['key'])
                    if any(rendition[k] != identity[k] for k in ('asset_id','media_id','key','filesizeInBytes')):
                        raise UploaderError('Remote rendition changed during download.')
                    sleep(2 ** (attempt - 1))
                url = rendition['downloadUrl']
                check_url(url)
                try:
                    headers = {'Range': f'bytes={offset}-{end}', 'Accept-Encoding': 'identity'}
                    if record['etag']:
                        headers['If-Range'] = record['etag']
                    with transport.get(url, headers=headers, stream=True, timeout=(10,30), allow_redirects=False) as response:
                        if response.status_code in (401,403,408,429,500,502,503,504):
                            if attempt == 2:
                                raise UploaderError(f'Download HTTP {response.status_code}; partial file retained.')
                            continue
                        if response.status_code != 206:
                            raise UploaderError(f'Download HTTP {response.status_code}; a bounded 206 response is required.')
                        expected_range = f'bytes {offset}-{end}/{size}'
                        if response.headers.get('Content-Range') != expected_range:
                            raise UploaderError('Server returned an unexpected byte range.')
                        if response.headers.get('Content-Encoding', 'identity') not in ('identity', ''):
                            raise UploaderError('Compressed range response is not supported.')
                        etag = response.headers.get('ETag')
                        if record['etag'] and etag != record['etag']:
                            raise UploaderError('Remote ETag changed; resume refused.')
                        if etag and not etag.startswith('W/'):
                            record['etag'] = etag
                        data = bytearray()
                        for block in response.iter_content(64*1024):
                            if len(data) + len(block) > end-offset+1:
                                raise UploaderError('Server exceeded the requested range.')
                            data.extend(block)
                        if len(data) != end-offset+1:
                            raise requests.ConnectionError('Short range')
                    break
                except requests.RequestException:
                    if attempt == 2:
                        raise UploaderError('Download network failure; partial file retained.') from None
            with part.open('ab') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            digest.update(data)
            offset += len(data)
            record.update(offset=offset, sha256=digest.hexdigest())
            atomic_write(meta, json.dumps(record).encode())
            if progress:
                speed = (offset-initial)/max(.001,time.monotonic()-started)
                progress({'bytes':offset,'total':target,'bytes_per_second':round(speed),
                          'eta_seconds':round((target-offset)/speed) if speed else None})
        if expected_sha256 and digest.hexdigest() != expected_sha256.lower():
            raise UploaderError('Expected checksum mismatch; partial file retained.')
        if output.exists():
            raise UploaderError('Output appeared during transfer; refusing to overwrite.')
        # Hard link is atomic and fails if destination exists; both paths share a filesystem.
        os.link(part, output)
        part.unlink()
        return dict(path=str(output), bytes=target, sha256=digest.hexdigest(), sample=target<size, skipped=False)
    finally:
        if own_transport:
            transport.close()
