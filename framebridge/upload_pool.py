"""Bounded transfers; the calling thread exclusively owns API and journal calls."""
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from queue import Empty, Queue
import threading
import time

import requests

from .storage import UploaderError
from .backoff import retry_delay
from .uploader import PendingUpload, READY, finish_upload, upload


class _OwnerProxy:
    def __init__(self, target, queue, stopped):
        self.target, self.queue, self.stopped = target, queue, stopped

    def __getattr__(self, name):
        def invoke(*args, **kwargs):
            if self.stopped.is_set():
                raise UploaderError('Upload operation interrupted; rerun with the same journal.')
            result = Future()
            self.queue.put((self.target, name, args, kwargs, result))
            while True:
                try:
                    return result.result(timeout=0.1)
                except TimeoutError:
                    if result.done():
                        raise
                    if self.stopped.is_set():
                        raise UploaderError('Upload operation interrupted; rerun with the same journal.') from None
        return invoke


class _StorageCooldown:
    """One Retry-After barrier for storage workers in this operation."""
    def __init__(self, stopped):
        self.stopped, self.lock, self.until = stopped, threading.Lock(), 0.0
        self.attempts, self.body_bytes, self.seconds = 0, 0, 0.0

    def wait(self):
        while True:
            if self.stopped.is_set():
                raise UploaderError('Upload operation interrupted.')
            with self.lock:
                delay = self.until - time.monotonic()
            if delay <= 0:
                return
            self.stopped.wait(min(delay, 0.1))

    def note(self, response):
        if response.status_code in (429, 503):
            with self.lock:
                self.until = max(self.until, time.monotonic() + retry_delay(response.headers, 0))


class _StorageSession:
    def __init__(self, session, cooldown):
        self.session, self.cooldown = session, cooldown

    def put(self, *args, **kwargs):
        self.cooldown.wait()
        body = kwargs['data']
        remaining = body.remaining
        started = time.monotonic()
        try:
            response = self.session.put(*args, **kwargs)
        finally:
            with self.cooldown.lock:
                self.cooldown.attempts += 1
                self.cooldown.body_bytes += remaining - body.remaining
                self.cooldown.seconds += time.monotonic() - started
        self.cooldown.note(response)
        return response


def upload_many(api, journal, jobs, *, workers=4, experimental=False, upload_fn=upload,
                check_source=None, metrics=None, poll_interval=2, poll_seconds=180,
                session_factory=requests.Session, part_workers=1):
    """Yield (job, asset_id, error); jobs must have distinct source/destination keys.

    Only this thread calls the real API or SQLite connection. Worker RPCs wait
    for durable acknowledgements. Cancellation stops new work and leaves the
    existing journal phases usable by the ordinary sequential resume path.
    """
    if type(workers) is not int or not 1 <= workers <= 16:
        raise UploaderError('--workers must be between 1 and 16.')
    if type(part_workers) is not int or not 1 <= part_workers <= 4:
        raise UploaderError('--part-workers must be between 1 and 4.')
    if part_workers > 1 and workers < 2:
        raise UploaderError('--part-workers above 1 requires --workers above 1.')
    metrics = metrics if metrics is not None else {}
    counts, durations = Counter(), Counter()
    metrics.update(workers=workers, part_workers=part_workers, peak_active=0, peak_pending=0)
    queue, stopped = Queue(), threading.Event()
    cooldown = _StorageCooldown(stopped)
    api_proxy = _OwnerProxy(api, queue, stopped)
    journal_proxy = _OwnerProxy(journal, queue, stopped)
    local, sessions, sessions_lock = threading.local(), [], threading.Lock()
    hash_gate = threading.Semaphore(1)
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='framebridge-upload')
    parts = (ThreadPoolExecutor(max_workers=workers, thread_name_prefix='framebridge-part')
             if part_workers > 1 else None)
    transfer_gate = threading.Semaphore(workers)
    active, pending = {}, []
    jobs, exhausted = iter(jobs), False
    started, next_poll = time.monotonic(), 0.0

    def storage_session():
        if not hasattr(local, 'session'):
            local.session = session_factory()
            local.session.trust_env = False
            with sessions_lock:
                sessions.append(local.session)
        return _StorageSession(local.session, cooldown)

    def transfer(job, waiting=None):
        if stopped.is_set():
            raise UploaderError('Upload operation interrupted.')
        if check_source:
            check_source(job)
        if waiting is not None:
            return finish_upload(api_proxy, journal_proxy, waiting, hash_gate=hash_gate,
                                 cancelled=stopped.is_set)
        return upload_fn(api_proxy, journal_proxy, job['path'], job['project_id'],
                         job['account_id'], job['folder_id'], experimental=experimental,
                         http=storage_session(), defer_completion=True, hash_gate=hash_gate,
                         sleep=lambda seconds: stopped.wait(seconds), part_executor=parts,
                         part_workers=part_workers, part_session_factory=storage_session,
                         transfer_gate=transfer_gate, cancelled=stopped.is_set)

    def service(timeout=0):
        try:
            target, name, args, kwargs, result = queue.get(timeout=timeout)
        except Empty:
            return
        begin = time.monotonic()
        kind = 'api' if target is api else 'journal'
        counts[kind + '.' + name] += 1
        try:
            result.set_result(getattr(target, name)(*args, **kwargs))
        except BaseException as error:
            result.set_exception(error)
            # A shared renewal failure is not a per-file failure to repeat N times.
            if not isinstance(error, Exception) or (
                    isinstance(error, UploaderError) and any(text in str(error) for text in
                        ('RefreshAccessToken', 'Session renewal failed'))):
                raise
        finally:
            durations[kind] += time.monotonic() - begin

    try:
        while active or pending or not exhausted:
            service()
            for future in list(active):
                if not future.done():
                    continue
                job = active.pop(future)
                try:
                    value = future.result()
                    if isinstance(value, PendingUpload):
                        pending.append({'job': job, 'upload': value,
                                        'deadline': time.monotonic() + poll_seconds, 'ready': False})
                    else:
                        yield job, value, None
                except (UploaderError, OSError) as error:
                    yield job, None, str(error) if isinstance(error, UploaderError) else 'Local I/O failure'

            # Final verification gets priority, but hashes run off the owner thread.
            for entry in list(pending):
                if entry['ready'] and len(active) < workers:
                    pending.remove(entry)
                    active[executor.submit(transfer, entry['job'], entry['upload'])] = entry['job']

            while not exhausted and len(active) < workers and len(active) + len(pending) < workers * 2:
                try:
                    job = next(jobs)
                except StopIteration:
                    exhausted = True
                    break
                if job.get('error'):
                    yield job, None, job['error']
                else:
                    active[executor.submit(transfer, job)] = job
            metrics['peak_active'] = max(metrics['peak_active'], len(active))
            metrics['peak_pending'] = max(metrics['peak_pending'], len(pending))

            now = time.monotonic()
            waiting = [entry for entry in pending if not entry['ready']]
            if waiting and now >= next_poll:
                for offset in range(0, len(waiting), 100):
                    group = waiting[offset:offset + 100]
                    ids = [entry['upload'].record['asset_id'] for entry in group]
                    begin = time.monotonic()
                    try:
                        rows = api.upload_states(ids)
                        counts['api.upload_states'] += 1
                        by_id = {row['id']: row for row in rows}
                        if len(rows) != len(ids) or set(by_id) != set(ids):
                            raise UploaderError('Upload status response does not match pending assets.')
                    except UploaderError:
                        # Preserve all records for a fresh read on the next invocation.
                        raise UploaderError('Pending upload status check failed; rerun with the same journal.') from None
                    finally:
                        durations['api'] += time.monotonic() - begin
                    for entry in group:
                        status = by_id[entry['upload'].record['asset_id']].get('status')
                        if status in READY:
                            entry['ready'] = True
                        elif status in {'FAILED', 'ERROR', 'DELETED'} or time.monotonic() >= entry['deadline']:
                            pending.remove(entry)
                            message = ('Server reports a failed asset; reconcile before retrying.'
                                       if status in {'FAILED', 'ERROR', 'DELETED'} else
                                       'Upload is awaiting server completion; rerun to check the same asset.')
                            yield entry['job'], None, message
                next_poll = time.monotonic() + poll_interval
            if active or pending:
                service(timeout=0.02)
    finally:
        stopped.set()
        for future in active:
            future.cancel()
        # Blocked RPCs observe stopped; in-flight HTTP calls retain bounded timeouts.
        executor.shutdown(wait=True, cancel_futures=True)
        if parts is not None:
            parts.shutdown(wait=True, cancel_futures=True)
        for session in sessions:
            session.close()
        metrics.update(elapsed_seconds=round(time.monotonic() - started, 3),
                       calls=dict(counts), owner_seconds={k: round(v, 3) for k, v in durations.items()},
                       storage_sessions=len(sessions), storage_attempts=cooldown.attempts,
                       body_bytes_read=cooldown.body_bytes, storage_seconds=round(cooldown.seconds, 3))
