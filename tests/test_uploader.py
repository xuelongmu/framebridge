import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from frameio_uploader.api import Api
from frameio_uploader.cli import remaining_plan
from frameio_uploader.login import session_id
from frameio_uploader.storage import Journal, SessionStore, UploaderError
from frameio_uploader.uploader import Slice, part_bounds, upload


class Tests(unittest.TestCase):
    def test_playwriter_session_output(self):
        self.assertEqual(session_id('Session 42 created. Use with: playwriter -s 42 -e "..."'), '42')
        self.assertEqual(session_id('42\n'), '42')
        self.assertIsNone(session_id('Extension did not connect within timeout'))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def api(self):
        store = Mock()
        store.load.return_value = dict(access_token='secret', client_name='web', client_version='test', expires_at=9999999999)
        http = Mock()
        return Api(store, http, sleep=lambda _: None), http, store

    def test_graphql_errors_redacted(self):
        api, http, _ = self.api()
        http.post.return_value = Mock(ok=True, status_code=200)
        http.post.return_value.json.return_value = {'errors': [{'message': 'secret'}]}
        with self.assertRaises(UploaderError) as raised:
            api.me()
        self.assertNotIn('secret', str(raised.exception))

    def test_mutation_not_retried(self):
        api, http, _ = self.api()
        http.post.side_effect = requests.ConnectionError('secret')
        with self.assertRaises(UploaderError):
            api.create_batch('account', 'file')
        self.assertEqual(http.post.call_count, 1)

    def test_read_retries(self):
        api, http, _ = self.api()
        http.post.side_effect = requests.ConnectionError('secret')
        with self.assertRaises(UploaderError):
            api.me()
        self.assertEqual(http.post.call_count, 3)

    def test_refresh_saved_before_use(self):
        api, http, store = self.api()
        api.credentials['expires_at'] = 0
        first = Mock(ok=True, status_code=200)
        first.json.return_value = {'data': {'cycleRefreshToken': {'token': {'accessToken': 'new', 'expiration': 9999999999}}}}
        second = Mock(ok=True, status_code=200)
        second.json.return_value = {'data': {'me': {'id': 'user'}}}
        http.post.side_effect = [first, second]
        self.assertEqual(api.me()['id'], 'user')
        self.assertEqual(store.save.call_args.args[0]['access_token'], 'new')
        self.assertNotIn('authorization', http.post.call_args_list[0].kwargs['headers'])
        self.assertEqual(http.post.call_args_list[1].kwargs['headers']['authorization'], 'Bearer new')

    def test_slice_and_parts(self):
        stream = Slice(io.BytesIO(b'abcdef'), 2, 3)
        self.assertEqual(stream.read(2), b'cd')
        self.assertEqual(stream.read(10), b'e')
        self.assertEqual(stream.read(), b'')
        self.assertEqual(part_bounds(11 * 1024 * 1024, 2, 1), (5767168, 5767168))
        with self.assertRaises(UploaderError):
            part_bounds(10, 2, 1)

    def test_upload_resume_and_no_duplicates(self):
        path = self.root / 'test.txt'
        path.write_bytes(b'hello')
        journal = Journal(self.root / 'state.sqlite3')
        self.addCleanup(journal.close)
        api, http = Mock(), Mock()
        api.create_batch.return_value = 'batch'
        api.create_asset.return_value = {'id': 'asset', 'totalPartCount': 1}
        api.part_url.return_value = 'https://example.amazonaws.com/upload'
        api.status.side_effect = ['CREATED', 'TRANSCODED']
        http.put.side_effect = [requests.ConnectionError('secret'), Mock(status_code=200)]
        self.assertEqual(upload(api, journal, path, 'p', 'a', 'f', http=http, sleep=lambda _: None), 'asset')
        self.assertEqual(upload(api, journal, path, 'p', 'a', 'f', http=http), 'asset')
        self.assertEqual(api.create_asset.call_count, 1)
        self.assertEqual(http.put.call_count, 2)
        self.assertNotIn('authorization', http.put.call_args.kwargs['headers'])

    def test_ambiguous_creation_blocks_replay(self):
        path = self.root / 'test.txt'
        path.write_bytes(b'hello')
        location = self.root / 'journal.sqlite3'
        journal = Journal(location)
        api = Mock()
        api.create_batch.side_effect = UploaderError('network')
        with self.assertRaises(UploaderError):
            upload(api, journal, path, 'p', 'a', 'f')
        journal.close()
        journal = Journal(location)
        self.addCleanup(journal.close)
        with self.assertRaisesRegex(UploaderError, 'ambiguous'):
            upload(api, journal, path, 'p', 'a', 'f')
        self.assertEqual(api.create_batch.call_count, 1)

    def test_legacy_plan_preserved(self):
        (self.root / 'missing_files.txt').write_text('E:/folder/file.txt\nE:/folder/next.txt')
        (self.root / 'folder_ids.csv').write_text('uuid|E:\\folder')
        progress = self.root / 'upload_progress.json'
        progress.write_text(json.dumps(['E:\\folder\\file.txt']))
        before = progress.read_bytes()
        _, _, pending = remaining_plan(self.root)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1], 'uuid')
        self.assertEqual(progress.read_bytes(), before)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_roundtrip(self):
        store = SessionStore(self.root / 'session.dpapi')
        store.save({'access_token': 'secret-value'})
        self.assertEqual(store.load()['access_token'], 'secret-value')
        self.assertNotIn(b'secret-value', store.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
