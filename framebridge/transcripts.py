"""Export existing transcript resources; never request transcription generation."""
import hashlib
import os
from pathlib import Path
import tempfile

import requests

from .downloader import check_url
from .storage import UploaderError

FORMATS = {'srt', 'vtt', 'text'}


def list_transcripts(api, asset_id):
    return [dict(row, available_formats=sorted(fmt for fmt in FORMATS
                 if (row.get(fmt) or {}).get('downloadUrl'))) for row in api.transcripts(asset_id)]


def export_transcript(api, asset_id, transcription_id, format, output, *,
                      max_bytes=10 * 1024 * 1024, http=None):
    if format not in FORMATS or type(max_bytes) is not int or max_bytes <= 0:
        raise UploaderError('Choose srt, vtt, or text and a positive transcript byte cap.')
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise UploaderError('Transcript output already exists; it will not be overwritten.')
    asset = api.asset(asset_id)
    project_id = (asset.get('project') or {}).get('id')
    if not project_id or api.project(project_id).get('permissions', {}).get('canDownloadTranscription') is not True:
        raise UploaderError('Transcript download permission is missing or denied.')
    rows = [row for row in api.transcripts(asset_id) if row['id'] == transcription_id]
    if len(rows) != 1 or rows[0].get('encodeStatus') != 'SUCCESS':
        raise UploaderError('Choose an existing, ready transcription ID from transcripts.')
    transcript = rows[0]
    url = (transcript.get(format) or {}).get('downloadUrl')
    if not url:
        raise UploaderError('The selected transcript format is unavailable.')
    check_url(url)
    transport = http or requests.Session()
    if http is None:
        transport.trust_env = False
    temp = None
    try:
        with transport.get(url, headers={'Accept-Encoding': 'identity'}, stream=True,
                           allow_redirects=False, timeout=(10, 30)) as response:
            if response.status_code != 200:
                raise UploaderError(f'Transcript download HTTP {response.status_code}; no redirect is followed.')
            if response.headers.get('Content-Encoding', 'identity') not in ('', 'identity'):
                raise UploaderError('Compressed transcript responses are unsupported.')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > max_bytes):
                raise UploaderError('Transcript length is invalid or exceeds the byte cap.')
            output.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.framebridge-transcript-', dir=output.parent)
            temp = Path(name)
            size, digest = 0, hashlib.sha256()
            with os.fdopen(fd, 'wb') as stream:
                while True:
                    data = response.raw.read(min(64 * 1024, max_bytes - size + 1))
                    if not data:
                        break
                    size += len(data)
                    if size > max_bytes:
                        raise UploaderError('Transcript exceeded the byte cap; incomplete output discarded.')
                    stream.write(data)
                    digest.update(data)
                if length is not None and size != int(length):
                    raise UploaderError('Transcript response was truncated.')
                stream.flush()
                os.fsync(stream.fileno())
        current = [row for row in api.transcripts(asset_id) if row['id'] == transcription_id]
        if len(current) != 1 or any(current[0].get(k) != transcript.get(k)
                                    for k in ('id', 'locale', 'lastEditedAt', 'encodeStatus')):
            raise UploaderError('Transcript metadata changed during export; rerun the command.')
        os.link(temp, output)  # atomic publication; never overwrite a racing writer
        return {'asset_id': asset_id, 'transcription_id': transcription_id,
                'format': format, 'path': str(output), 'bytes': size, 'sha256': digest.hexdigest(),
                'checksum_verified': False}
    except requests.RequestException:
        raise UploaderError('Transcript download failed; network details suppressed.') from None
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        if http is None:
            transport.close()
