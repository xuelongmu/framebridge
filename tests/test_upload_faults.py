"""Offline fault-injection tests against the real Framebridge upload code."""
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from framebridge.uploader import upload, PART_MIN
from framebridge.storage import Journal, UploaderError
from framebridge.api import Api, PROJECT, FOLDER


class UploadStress(unittest.TestCase):
    def test_live_project_schema_regression(self):
        store = Mock()
        store.load.return_value = {}
        api = Api(store)
        api.call = Mock(return_value={'project': {'id': 'p', 'workspace': {'account': {'id': 'a'}}}})
        self.assertEqual(api.project('p')['account']['id'], 'a')
        self.assertIn('workspace { account { id } }', PROJECT)
        self.assertIn('... on FolderAsset { permissions', FOLDER)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / 'synthetic.bin'
        self.file.write_bytes(b'a' * 1024)
        self.journal = Journal(self.root / 'journal.sqlite3')
        self.addCleanup(self.journal.close)
        self.api = Mock()
        self.api.create_batch.return_value = 'batch'
        self.api.create_asset.return_value = {'id': 'asset', 'totalPartCount': 1}
        self.api.status.return_value = 'TRANSCODED'
        self.http = Mock()
        self.http.put.return_value.status_code = 200

    def run_upload(self, **kwargs):
        return upload(self.api, self.journal, self.file, 'project', 'account', 'folder',
                      sleep=lambda _: None, **kwargs)

    def test_asset_creation_response_lost(self):
        self.api.create_asset.side_effect = UploaderError('lost response')
        with self.assertRaises(UploaderError): self.run_upload(http=self.http)
        with self.assertRaisesRegex(UploaderError, 'ambiguous'): self.run_upload(http=self.http)
        self.assertEqual(self.api.create_asset.call_count, 1)

    def test_batch_completion_response_lost(self):
        self.api.complete_batch.side_effect = [UploaderError('lost response'), None]
        with self.assertRaises(UploaderError): self.run_upload(http=self.http)
        self.assertEqual(self.run_upload(http=self.http), 'asset')
        self.assertEqual(self.api.create_asset.call_count, 1)
        self.assertEqual(self.http.put.call_count, 0)

    def test_changed_source_refused(self):
        self.run_upload(http=self.http)
        self.file.write_bytes(b'changed')
        with self.assertRaisesRegex(UploaderError, 'changed'): self.run_upload(http=self.http)

    def test_poll_timeout_resumes_without_resending_parts(self):
        self.api.status.return_value = 'UPLOADING'
        with self.assertRaisesRegex(UploaderError, 'awaiting'):
            self.run_upload(http=self.http, poll_seconds=2)
        self.api.status.return_value = 'TRANSCODED'
        self.assertEqual(self.run_upload(http=self.http), 'asset')
        self.assertEqual(self.http.put.call_count, 1)

    def test_multipart_http_bytes_and_mid_transfer_restart(self):
        # Two real HTTP PUTs, with an interruption after the first part is journaled.
        original = bytes(range(256)) * (11 * 1024 * 1024 // 256)
        self.file.write_bytes(original)
        received = {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_PUT(self):
                assert self.headers.get('Authorization') is None
                received[int(self.path[1:])] = self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.api.create_asset.return_value = {'id': 'asset', 'totalPartCount': 2}
        self.api.status.return_value = 'UPLOADING'
        def interrupted_url(asset, part):
            if part == 1: raise KeyboardInterrupt()
            return f'http://127.0.0.1:{server.server_port}/{part}'
        self.api.part_url.side_effect = interrupted_url
        with self.assertRaises(KeyboardInterrupt): self.run_upload(experimental=True)
        self.assertEqual(set(received), {0})
        self.journal.close()
        self.journal = Journal(self.root / 'journal.sqlite3')
        self.addCleanup(self.journal.close)
        self.api.part_url.side_effect = lambda asset, part: f'http://127.0.0.1:{server.server_port}/{part}'
        self.api.status.side_effect = ['UPLOADING', 'TRANSCODED']
        self.run_upload(experimental=True)
        combined = received[0] + received[1]
        self.assertEqual(hashlib.sha256(combined).digest(), hashlib.sha256(original).digest())
        self.assertEqual(self.api.create_asset.call_count, 1)
        self.assertEqual(self.api.part_url.call_count, 3)


if __name__ == '__main__': unittest.main(verbosity=2)
