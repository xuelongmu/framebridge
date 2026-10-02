"""Journaled S3 uploads; no automatic asset-creation retries."""
import hashlib
from contextlib import nullcontext
from dataclasses import dataclass
from concurrent.futures import FIRST_COMPLETED, wait
import mimetypes
import time
from pathlib import Path

import requests

from .storage import UploaderError, source_identity
from .backoff import retry_delay

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
    def __init__(self, stream, start, length, cancelled=None):
        self.stream, self.remaining, self.length = stream, length, length
        self.cancelled = cancelled
        self.digest = hashlib.sha256()
        stream.seek(start)

    def __len__(self):
        return self.length

    def read(self, size=-1):
        if self.cancelled is not None and self.cancelled():
            raise UploaderError('Upload operation interrupted; rerun with the same journal.')
        amount = self.remaining if size < 0 else min(size, self.remaining)
        data = self.stream.read(amount)
        self.remaining -= len(data)
        self.digest.update(data)
        return data


def fingerprint(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def upload_key(path, folder_id):
    return hashlib.sha256((source_identity(Path(path).resolve()) + '|' + folder_id).encode()).hexdigest()


def source_hashes(path, size, count, *, cancelled=None):
    """Hash the source and its planned parts in one bounded-memory pass."""
    whole, parts = hashlib.sha256(), []
    with path.open('rb') as stream:
        for index in range(count):
            _, remaining = part_bounds(size, count, index)
            part = hashlib.sha256()
            while remaining:
                if cancelled is not None and cancelled():
                    raise UploaderError('Upload operation interrupted; rerun with the same journal.')
                block = stream.read(min(4 * 1024 * 1024, remaining))
                if not block:
                    raise UploaderError('Source file truncated while hashing.')
                whole.update(block)
                part.update(block)
                remaining -= len(block)
            parts.append(part.hexdigest())
        if stream.read(1):
            raise UploaderError('Source file grew while hashing.')
    return whole.hexdigest(), parts


def _hash_source(path, size, count, gate, cancelled=None):
    with gate if gate is not None else nullcontext():
        return (source_hashes(path, size, count, cancelled=cancelled) if cancelled is not None
                else source_hashes(path, size, count))


@dataclass
class PendingUpload:
    path: Path
    key: str
    record: dict
    mark: list
    project_id: str
    folder_id: str


def finish_upload(api, journal, pending, *, hash_gate=None, cancelled=None):
    """Recheck source and remote evidence before marking the existing batch complete."""
    path, record, mark = pending.path, pending.record, pending.mark
    if fingerprint(path) != mark:
        raise UploaderError('Source file changed during upload. Reconcile the existing asset.')
    if record.get('source_sha256'):
        final_digest, _ = _hash_source(path, mark[0], 1, hash_gate, cancelled)
        if final_digest != record['source_sha256'] or fingerprint(path) != mark:
            raise UploaderError('Source content changed during upload; completion refused.')
    record['verification'] = verify_remote(api, record['asset_id'], mark[0],
                                            pending.project_id, pending.folder_id)
    journal.put(pending.key, record)
    api.complete_batch(record['batch_id'])
    record['phase'] = 'complete'
    journal.put(pending.key, record)
    return record['asset_id']


def _send_part(api, path, mark, record, index, expected_hash, transport, sleep, gate,
               initial_url=None, cancelled=None):
    if fingerprint(path) != mark:
        raise UploaderError('Source file changed during upload. Reconcile the existing asset.')
    start, length = part_bounds(mark[0], record['part_count'], index)
    for attempt in range(3):
        url = initial_url if attempt == 0 and initial_url is not None else api.part_url(record['asset_id'], index)
        delay = retry_delay({}, attempt)
        try:
            with gate if gate is not None else nullcontext():
                if cancelled is not None and cancelled():
                    raise UploaderError('Upload operation interrupted; rerun with the same journal.')
                with path.open('rb') as stream:
                    body = Slice(stream, start, length, cancelled)
                    response = transport.put(url, data=body,
                        headers={'Content-Length': str(length),
                                 'Content-Type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream',
                                 'x-amz-acl': 'private'}, timeout=(10, 120), allow_redirects=False)
                success = response.status_code == 200
                if not success:
                    delay = retry_delay(response.headers, attempt)
                response.close()
                if success and (body.remaining or body.digest.hexdigest() != expected_hash):
                    raise UploaderError('Uploaded part bytes do not match the source checksum; completion refused.')
        except requests.RequestException:
            success = False
        if success:
            if fingerprint(path) != mark:
                raise UploaderError('Source file changed during upload. Reconcile the existing asset.')
            return index
        if attempt == 2:
            raise UploaderError('Storage part upload failed; rerun to resume the existing asset.')
        sleep(delay)


def _parallel_parts(api, journal, path, mark, record, key, hashes, executor,
                    workers, session_factory, sleep, gate, cancelled=None):
    remaining = iter(index for index in range(record['part_count']) if index not in record['parts'])
    active, exhausted, failure = {}, False, None

    def send(index, url):
        return _send_part(api, path, mark, record, index, hashes[index], session_factory(), sleep,
                          gate, url, cancelled)

    while active or not exhausted:
        if failure is None and not exhausted and len(active) < workers:
            window = []
            for _ in range(workers - len(active)):
                try:
                    window.append(next(remaining))
                except StopIteration:
                    exhausted = True
                    break
            try:
                # Fetch at most four URLs just before scheduling. Resume gaps use
                # individual requests rather than fetching a large unused range.
                if window and window[-1] - window[0] + 1 == len(window):
                    urls = api.part_urls(record['asset_id'], window[0], len(window))
                    if len(urls) != len(window):
                        raise UploaderError('Upload URL window is incomplete.')
                else:
                    urls = [api.part_url(record['asset_id'], index) for index in window]
                for index, url in zip(window, urls):
                    active[executor.submit(send, index, url)] = index
            except BaseException as error:
                failure, exhausted = error, True
        if not active:
            break
        done, _ = wait(active, return_when=FIRST_COMPLETED)
        for future in done:
            active.pop(future)
            try:
                index = future.result()
                record['parts'].append(index)
                journal.put(key, record)
            except BaseException as error:
                if failure is None:
                    failure = error
                exhausted = True
        # Drain already-dispatched parts and retain successes even if one fails.
    if failure is not None:
        raise failure


def verify_remote(api, asset_id, size, project_id=None, folder_id=None):
    asset = api.upload_evidence(asset_id)
    if asset.get('id') != asset_id:
        raise UploaderError('Remote asset identity mismatch.')
    if asset.get('status') not in READY:
        raise UploaderError('Remote asset is not complete; reconcile before retrying.')
    if type(asset.get('filesize')) is not int or asset['filesize'] != size:
        raise UploaderError('Remote file size is missing or does not match the source; completion refused.')
    if project_id is not None and (asset.get('project') or {}).get('id') != project_id:
        raise UploaderError('Remote project changed; reconcile before retrying.')
    if folder_id is not None and (asset.get('parent') or {}).get('id') != folder_id:
        raise UploaderError('Remote destination folder changed; reconcile before retrying.')
    return {'level':'metadata', 'remote_size':asset['filesize'], 'remote_status':asset['status'],
            'checked_at':time.time(), 'checksum_verified':False}


def upload(api, journal, path, project_id, account_id, folder_id, *, experimental=False,
           http=None, sleep=time.sleep, poll_seconds=180, defer_completion=False, hash_gate=None,
           part_executor=None, part_workers=1, part_session_factory=None, transfer_gate=None,
           cancelled=None):
    if type(part_workers) is not int or not 1 <= part_workers <= 4:
        raise UploaderError('--part-workers must be between 1 and 4.')
    # ``experimental`` remains accepted for compatibility with existing callers.
    if part_workers != 1 and (part_executor is None or part_session_factory is None):
        raise UploaderError('Parallel parts require the coordinated upload pool.')
    path = Path(path).resolve(strict=True)
    mark = fingerprint(path)
    if not path.is_file() or mark[0] <= 0:
        raise UploaderError('Choose a nonempty regular file.')
    key = upload_key(path, folder_id)
    record = journal.get(key)
    if record:
        if record['fingerprint'] != mark or record['project_id'] != project_id:
            raise UploaderError('File or destination changed since the journal entry. Reconcile before retrying.')
        if record['phase'] in ('creating_batch', 'creating_asset'):
            raise UploaderError('Previous creation has an ambiguous outcome. Reconcile the journal with Frame.io; do not delete state and retry.')
        if record['phase'] == 'complete':
            if record.get('source_sha256'):
                digest, _ = _hash_source(path, mark[0], 1, hash_gate, cancelled)
                if digest != record['source_sha256'] or fingerprint(path) != mark:
                    raise UploaderError('Source content changed since upload; resume refused.')
            record['verification'] = verify_remote(api, record['asset_id'], mark[0], project_id, folder_id)
            journal.put(key, record)
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
    if type(count) is not int or not 1 <= count <= 10000:
        raise UploaderError('Unsupported part count; the existing asset is retained in the journal.')
    # Validate the last part before sending any bytes.
    part_bounds(mark[0], count, count - 1)
    status = api.status(record['asset_id'])
    if status in ('FAILED', 'ERROR', 'DELETED'):
        raise UploaderError('Server reports a failed asset. Reconcile before retrying.')
    if status not in READY:
        digest, part_hashes = _hash_source(path, mark[0], count, hash_gate, cancelled)
        if fingerprint(path) != mark:
            raise UploaderError('Source file changed while hashing.')
        if record.get('source_sha256') and digest != record['source_sha256']:
            raise UploaderError('Source content changed since upload; resume refused.')
        if record['parts'] and not record.get('source_sha256'):
            raise UploaderError('Legacy partial upload has no source checksum. Reconcile manually; automatic resume refused.')
        record.update(source_sha256=digest, part_sha256=part_hashes)
        journal.put(key, record)
        transport = http or requests.Session()
        if http is None:
            transport.trust_env = False
        try:
            if part_workers > 1 and count > 1:
                _parallel_parts(api, journal, path, mark, record, key, part_hashes,
                                part_executor, part_workers, part_session_factory, sleep, transfer_gate, cancelled)
            else:
                for index in range(count):
                    if index in record['parts']:
                        continue
                    _send_part(api, path, mark, record, index, part_hashes[index], transport, sleep,
                               transfer_gate, cancelled=cancelled)
                    record['parts'].append(index)
                    journal.put(key, record)
            if not defer_completion:
                for _ in range(max(1, poll_seconds // 2)):
                    status = api.status(record['asset_id'])
                    if status in READY:
                        break
                    if status in ('FAILED', 'ERROR', 'DELETED'):
                        raise UploaderError('Server reports a failed asset. Reconcile before retrying.')
                    sleep(2)
                else:
                    raise UploaderError('Upload is awaiting server completion; rerun to check the same asset.')
        finally:
            if http is None:
                transport.close()
    pending = PendingUpload(path, key, record, mark, project_id, folder_id)
    if defer_completion:
        return pending
    return finish_upload(api, journal, pending, hash_gate=hash_gate, cancelled=cancelled)
