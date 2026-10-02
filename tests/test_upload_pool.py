"""Offline concurrency, ownership, backpressure, and restart tests."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

import requests

from framebridge.api import Api
from framebridge.storage import Journal, UploaderError
from framebridge.upload_pool import upload_many
from framebridge.uploader import upload


class OwnerJournal(Journal):
    def __init__(self, path):
        super().__init__(path)
        self.owner = threading.get_ident()

    def get(self, key):
        assert threading.get_ident() == self.owner
        return super().get(key)

    def put(self, key, value):
        assert threading.get_ident() == self.owner
        return super().put(key, value)


class PoolApi:
    def __init__(self):
        self.owner = threading.get_ident()
        self.assets, self.batches, self.completed, self.polls = {}, [], [], []
        self.received = set()
        self.hold = False
        self.release_after = 0
        self.lose_create = False
        self.bad_size = False

    def check(self):
        assert threading.get_ident() == self.owner

    def create_batch(self, account, name):
        self.check()
        id = 'batch-' + str(len(self.batches))
        self.batches.append(id)
        return id

    def create_asset(self, batch, folder, name, size, mime):
        self.check()
        id = 'asset-' + str(len(self.assets))
        self.assets[id] = {'id': id, 'status': 'UPLOADING', 'filesize': size,
                           'project': {'id': 'p'}, 'parent': {'id': folder}}
        if self.lose_create:
            raise UploaderError('Creation response lost.')
        return {'id': id, 'totalPartCount': 1}

    def status(self, id):
        self.check()
        return ('UPLOADED' if id in self.received and not self.hold and
                len(self.assets) >= self.release_after else 'UPLOADING')

    def part_url(self, id, index):
        self.check()
        return id

    def part_urls(self, id, offset, count):
        self.check()
        return [self.part_url(id, index) for index in range(offset, offset + count)]

    def upload_states(self, ids):
        self.check()
        self.polls.append(list(ids))
        return [dict(self.assets[id], status=self.status(id)) for id in ids]

    def upload_evidence(self, id):
        self.check()
        row = copy.deepcopy(self.assets[id])
        row['status'] = self.status(id)
        if self.bad_size:
            row['filesize'] += 1
        return row

    def complete_batch(self, id):
        self.check()
        self.completed.append(id)


class UploadPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = OwnerJournal(self.root / 'uploads.sqlite3')
        self.addCleanup(self.journal.close)
        self.api = PoolApi()
        self.jobs = []
        for i in range(6):
            path = self.root / str(i)
            path.write_bytes(bytes([i]) * 2048)
            self.jobs.append(dict(path=str(path), project_id='p', account_id='a', folder_id='f'))
        self.lock = threading.Lock()
        self.inflight = self.peak = self.puts = 0
        self.first_pair = None
        self.sessions = []
        test = self

        class Session:
            def __init__(self):
                self.owner = threading.get_ident()
                self.closed = False
                test.sessions.append(self)

            def put(self, url, **kwargs):
                assert threading.get_ident() == self.owner
                assert 'Authorization' not in kwargs['headers']
                with test.lock:
                    test.inflight += 1
                    test.peak = max(test.peak, test.inflight)
                    test.puts += 1
                    ordinal = test.puts
                if test.first_pair is not None and ordinal <= 2:
                    test.first_pair.wait(timeout=5)
                while kwargs['data'].read(1024):
                    pass
                time.sleep(0.01)
                with test.lock:
                    test.api.received.add(url)
                    test.inflight -= 1
                return Mock(status_code=200)

            def close(self):
                self.closed = True

        self.factory = Session

    def run_pool(self, **kwargs):
        return list(upload_many(self.api, self.journal, self.jobs, workers=2,
                                session_factory=self.factory, poll_interval=0.01, **kwargs))

    def test_concurrency_owner_threads_and_connection_reuse(self):
        self.first_pair = threading.Barrier(2)
        metrics = {}
        rows = self.run_pool(metrics=metrics)
        self.assertTrue(all(error is None for _, _, error in rows))
        self.assertEqual(len(self.api.completed), 6)
        self.assertEqual(self.peak, 2)
        self.assertLessEqual(len(self.sessions), 2)
        self.assertTrue(all(s.closed for s in self.sessions))
        self.assertLessEqual(metrics['peak_pending'], 4)
        self.assertEqual(metrics['calls']['api.create_asset'], 6)
        self.assertEqual(metrics['calls']['journal.put'], 48)

    def test_completed_resume_does_not_create_or_send_again(self):
        self.run_pool()
        rows = self.run_pool()
        self.assertTrue(all(error is None for _, _, error in rows))
        self.assertEqual(self.puts, 6)
        self.assertEqual(len(self.api.assets), 6)

    def test_waiting_for_processing_does_not_hold_transfer_slots(self):
        self.api.release_after = 3
        rows = self.run_pool(poll_seconds=2)
        self.assertTrue(all(error is None for _, _, error in rows))
        self.assertTrue(any(len(group) >= 2 for group in self.api.polls))

    def test_pending_timeout_resumes_without_resending_parts(self):
        self.api.hold = True
        rows = self.run_pool(poll_seconds=0)
        self.assertTrue(all('awaiting' in error for _, _, error in rows))
        self.assertEqual(self.api.completed, [])
        self.journal.close()
        self.journal = OwnerJournal(self.root / 'uploads.sqlite3')
        self.addCleanup(self.journal.close)
        self.api.hold = False
        rows = self.run_pool()
        self.assertTrue(all(error is None for _, _, error in rows))
        self.assertEqual(self.puts, 6)
        self.assertEqual(len(self.api.assets), 6)

    def test_hashing_and_upload_stream_observe_cancellation(self):
        import io
        from framebridge.uploader import Slice, source_hashes
        with self.assertRaisesRegex(UploaderError, 'interrupted'):
            Slice(io.BytesIO(b'abc'), 0, 3, cancelled=lambda: True).read(1)
        with self.assertRaisesRegex(UploaderError, 'interrupted'):
            source_hashes(Path(self.jobs[0]['path']), 2048, 1, cancelled=lambda: True)

    def test_lost_creation_stays_ambiguous_on_resume(self):
        self.api.lose_create = True
        rows = self.run_pool()
        self.assertTrue(all(error for _, _, error in rows))
        self.api.lose_create = False
        rows = self.run_pool()
        self.assertTrue(all('ambiguous' in error for _, _, error in rows))
        self.assertEqual(len(self.api.assets), 6)
        self.assertEqual(self.puts, 0)

    def test_verification_failure_does_not_complete(self):
        self.api.bad_size = True
        rows = self.run_pool()
        self.assertTrue(all('size' in error for _, _, error in rows))
        self.assertEqual(self.api.completed, [])
        self.api.bad_size = False
        self.assertTrue(all(error is None for _, _, error in self.run_pool()))
        self.assertEqual(self.puts, 6)

    def test_source_changed_while_waiting_is_refused(self):
        original = self.api.upload_states
        def change(ids):
            for job in self.jobs:
                Path(job['path']).write_bytes(b'changed')
            return original(ids)
        self.api.upload_states = change
        rows = self.run_pool()
        self.assertTrue(any(error and 'changed' in error for _, _, error in rows))
        self.assertLess(len(self.api.completed), 6)

    def test_interrupt_releases_waiting_rpc_and_keeps_journal(self):
        def interrupted(api, journal, path, *args, **kwargs):
            if Path(path).name == '1':
                raise KeyboardInterrupt()
            return upload(api, journal, path, *args, **kwargs)
        with self.assertRaises(KeyboardInterrupt):
            self.run_pool(upload_fn=interrupted)
        self.assertTrue(all(s.closed for s in self.sessions))
        # A stopped creation response may require reconciliation, never duplication.
        rows = self.run_pool()
        self.assertTrue(all(error is None or 'ambiguous' in error for _, _, error in rows))
        self.assertLessEqual(len(self.api.assets), 6)

    def test_invalid_workers_fail_without_remote_operations(self):
        for workers in (0, 17, -1):
            with self.assertRaises(UploaderError):
                list(upload_many(self.api, self.journal, self.jobs, workers=workers))
        self.assertEqual(self.api.assets, {})

    def test_parallel_parts_require_valid_worker_settings(self):
        for options in ({'workers': 1, 'part_workers': 2},
                        {'workers': 2, 'part_workers': 5, 'experimental': True}):
            with self.assertRaises(UploaderError):
                list(upload_many(self.api, self.journal, self.jobs, **options))
        self.assertEqual(self.api.assets, {})

    def test_renewal_failure_stops_queue(self):
        def fail(*args):
            raise UploaderError('RefreshAccessToken: GraphQL error (BAD_REQUEST).')
        self.api.create_batch = fail
        with self.assertRaisesRegex(UploaderError, 'RefreshAccessToken'):
            self.run_pool()
        self.assertEqual(self.api.assets, {})

    def test_real_http_pool(self):
        test = self
        headers_seen = []
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_PUT(self):
                headers_seen.append(dict(self.headers))
                body = self.rfile.read(int(self.headers['Content-Length']))
                assert len(body) == 2048
                test.api.received.add(self.path[1:])
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.api.part_url = lambda id, index: f'http://127.0.0.1:{server.server_port}/{id}'
        rows = list(upload_many(self.api, self.journal, self.jobs, workers=2,
                                session_factory=requests.Session, poll_interval=0.01))
        self.assertTrue(all(error is None for _, _, error in rows))
        self.assertEqual(len(headers_seen), 6)
        self.assertTrue(all('Authorization' not in headers for headers in headers_seen))

    def test_parallel_parts_real_http_failure_and_sequential_resume(self):
        self.jobs = self.jobs[:2]
        payload = bytes(range(256)) * (11 * 1024 * 1024 // 256)
        for job in self.jobs:
            Path(job['path']).write_bytes(payload)
        original_create = self.api.create_asset
        def create(*args):
            row = original_create(*args)
            return dict(row, totalPartCount=2)
        self.api.create_asset = create
        test = self
        received, attempts, active = {}, {}, [0, 0]
        fail = [True]
        lock = threading.Lock()
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_PUT(self):
                asset, part = self.path[1:].split('/')
                part = int(part)
                with lock:
                    active[0] += 1
                    active[1] = max(active[1], active[0])
                    attempts[(asset, part)] = attempts.get((asset, part), 0) + 1
                data = self.rfile.read(int(self.headers['Content-Length']))
                time.sleep(0.02)
                reject = fail[0] and asset == 'asset-0' and part == 1
                with lock:
                    if not reject:
                        received[(asset, part)] = data
                        if (asset, 0) in received and (asset, 1) in received:
                            test.api.received.add(asset)
                    active[0] -= 1
                self.send_response(503 if reject else 200)
                self.send_header('Retry-After', '0')
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.api.part_url = lambda id, index: f'http://127.0.0.1:{server.server_port}/{id}/{index}'
        rows = list(upload_many(self.api, self.journal, self.jobs, workers=2, part_workers=2,
                                poll_interval=0.01))
        self.assertEqual(sum(error is not None for _, _, error in rows), 1)
        self.assertLessEqual(active[1], 2)
        self.assertEqual(attempts[('asset-0', 0)], 1)
        self.assertEqual(attempts[('asset-0', 1)], 3)
        fail[0] = False
        # The original sequential uploader consumes the concurrent journal unchanged.
        for job in self.jobs:
            upload(self.api, self.journal, job['path'], 'p', 'a', 'f')
        self.assertEqual(len(self.api.assets), 2)
        self.assertEqual(attempts[('asset-0', 0)], 1)
        self.assertEqual(attempts[('asset-0', 1)], 4)
        for asset in self.api.assets:
            self.assertEqual(received[(asset, 0)] + received[(asset, 1)], payload)


class StatusBatchTests(unittest.TestCase):
    def test_concurrent_manifest_name_collision_refused_before_api(self):
        import argparse
        import json
        from framebridge.commands import upload_batch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for sub in ('a', 'b'):
                (root / sub).mkdir()
                (root / sub / 'clip').write_bytes(b'test')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps([{'path': str(root / sub / 'clip'), 'folder_id': 'f'}
                                            for sub in ('a', 'b')]))
            api = Mock()
            args = argparse.Namespace(manifest=manifest, workers=2, part_workers=1,
                                      experimental_multipart=False, project='p', execute=True)
            with self.assertRaisesRegex(UploaderError, 'destination name'):
                upload_batch(api, root, args)
            api.project.assert_not_called()

    def test_url_windows_validate_hosts_counts_and_part_order(self):
        store = Mock()
        store.load.return_value = {}
        api = Api(store)
        valid = ['https://bucket.s3.amazonaws.com/key?partNumber=3',
                 'https://bucket.s3.amazonaws.com/key?partNumber=4']
        api.call = Mock(return_value={'asset': {'uploadUrls': valid}})
        self.assertEqual(api.part_urls('a', 2, 2), valid)
        self.assertEqual(api.call.call_args.args[2], {'assetId': 'a', 'limit': 2, 'offset': 2})
        for urls in (valid[::-1], valid[:1], [valid[0], 'https://untrusted.test/key?partNumber=4'],
                     [valid[0], 'https://bucket.s3.amazonaws.com/key']):
            api.call.return_value = {'asset': {'uploadUrls': urls}}
            with self.assertRaises(UploaderError): api.part_urls('a', 2, 2)

    def test_staged_part_metadata_numbers(self):
        store = Mock()
        store.load.return_value = {}
        api = Api(store)
        valid = [f'https://frameio-uploads-production.s3-accelerate.amazonaws.com/key{i}'
                 f'?x-amz-meta-part_number={i}&x-amz-meta-part_count=35' for i in (3, 4)]
        api.call = Mock(return_value={'asset': {'uploadUrls': valid}})
        self.assertEqual(api.part_urls('a', 2, 2), valid)
        for urls in (valid[::-1], [valid[0], valid[0]],
                     [valid[0] + '&partNumber=4', valid[1]],
                     [valid[0] + '&x-amz-meta-part_number=3', valid[1]],
                     [valid[0].replace('number=3', 'number='), valid[1]]):
            api.call.return_value = {'asset': {'uploadUrls': urls}}
            with self.assertRaises(UploaderError): api.part_urls('a', 2, 2)
        api.call.return_value = {'asset': {'uploadUrls': [valid[1]]}}
        with self.assertRaises(UploaderError): api.part_url('a', 2)

    def test_status_batch_validates_ids_and_preserves_order(self):
        store = Mock()
        store.load.return_value = {}
        api = Api(store)
        api.call = Mock(return_value={'assets': [{'id': 'b'}, {'id': 'a'}]})
        self.assertEqual([row['id'] for row in api.upload_states(['a', 'b'])], ['a', 'b'])
        for rows in ([{'id': 'a'}], [{'id': 'a'}, {'id': 'a'}], [None, {'id': 'b'}]):
            api.call.return_value = {'assets': rows}
            with self.assertRaises(UploaderError): api.upload_states(['a', 'b'])
        for ids in ([], ['a', 'a'], list(map(str, range(101)))):
            with self.assertRaises(UploaderError): api.upload_states(ids)
