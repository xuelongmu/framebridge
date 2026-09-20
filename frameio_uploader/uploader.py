"""Sequential, journaled S3 uploads; no automatic asset-creation retries."""
import hashlib
import mimetypes
import time
from pathlib import Path

import requests

from .storage import UploaderError

PART_MIN = 5 * 1024 * 1024
READY = {'UPLOADED', 'TRANSCODED'}


def part_bounds(size, count, index):
    if count < 1 or not 0 <= index < count:
        raise UploaderError('Invalid multipart layout.')
    width = max(PART_MIN, (size + count - 1) // count)
    start = index * width
    length = min(width, size - start)
    if length <= 0:
        raise UploaderError('Invalid multipart layout.')
    return start, length


class Slice:
    def __init__(self, stream, start, length):
        self.stream, self.remaining, self.length = stream, length, length
        stream.seek(start)

    def __len__(self):
        return self.length

    def read(self, size=-1):
        amount = self.remaining if size < 0 else min(size, self.remaining)
        data = self.stream.read(amount)
        self.remaining -= len(data)
        return data


def fingerprint(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def upload(api, journal, path, project_id, account_id, folder_id, *, experimental=False,
           http=None, sleep=time.sleep, poll_seconds=180):
    path = Path(path).resolve(strict=True)
    mark = fingerprint(path)
    if not path.is_file() or mark[0] <= 0:
        raise UploaderError('Choose a nonempty regular file.')
    if mark[0] > PART_MIN and not experimental:
        raise UploaderError('Files larger than 5 MiB require --experimental-multipart (not live-validated).')
    key = hashlib.sha256((str(path).casefold() + '|' + folder_id).encode()).hexdigest()
    record = journal.get(key)
    if record:
        if record['fingerprint'] != mark or record['project_id'] != project_id:
            raise UploaderError('File or destination changed since the journal entry. Reconcile before retrying.')
        if record['phase'] in ('creating_batch', 'creating_asset'):
            raise UploaderError('Previous creation has an ambiguous outcome. Reconcile the journal with Frame.io; do not delete state and retry.')
        if record['phase'] == 'complete':
            return record['asset_id']
    else:
        record = dict(path=str(path), fingerprint=mark, project_id=project_id,
                      folder_id=folder_id, phase='creating_batch', parts=[])
        journal.put(key, record)
        record['batch_id'] = api.create_batch(account_id, path.name)
        record['phase'] = 'batch_created'
        journal.put(key, record)
    if record['phase'] == 'batch_created':
        record['phase'] = 'creating_asset'
        journal.put(key, record)
        asset = api.create_asset(record['batch_id'], folder_id, path.name, mark[0],
                                 mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        record.update(asset_id=asset['id'], part_count=asset['totalPartCount'], phase='uploading')
        journal.put(key, record)
    count = record['part_count']
    if not isinstance(count, int) or count < 1 or (count > 1 and not experimental):
        raise UploaderError('Unsupported part count; the existing asset is retained in the journal.')
    # Validate the last part before sending any bytes.
    part_bounds(mark[0], count, count - 1)
    if api.status(record['asset_id']) not in READY:
        transport = http or requests.Session()
        for index in range(count):
            if index in record['parts']:
                continue
            if fingerprint(path) != mark:
                raise UploaderError('Source file changed during upload. Reconcile the existing asset.')
            start, length = part_bounds(mark[0], count, index)
            for attempt in range(3):
                url = api.part_url(record['asset_id'], index)
                try:
                    with path.open('rb') as stream:
                        response = transport.put(url, data=Slice(stream, start, length),
                            headers={'Content-Length': str(length), 'Content-Type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                                     'x-amz-acl': 'private'}, timeout=(10, 120), allow_redirects=False)
                    success = response.status_code == 200
                except requests.RequestException:
                    success = False
                if success:
                    break
                if attempt == 2:
                    raise UploaderError('Storage part upload failed; rerun to resume the existing asset.')
                sleep(2 ** attempt)
            if fingerprint(path) != mark:
                raise UploaderError('Source file changed during upload. Reconcile the existing asset.')
            record['parts'].append(index)
            journal.put(key, record)
        for _ in range(max(1, poll_seconds // 2)):
            status = api.status(record['asset_id'])
            if status in READY:
                break
            if status in ('FAILED', 'ERROR', 'DELETED'):
                raise UploaderError('Server reports a failed asset. Reconcile before retrying.')
            sleep(2)
        else:
            raise UploaderError('Upload is awaiting server completion; rerun to check the same asset.')
    api.complete_batch(record['batch_id'])
    record['phase'] = 'complete'
    journal.put(key, record)
    return record['asset_id']
