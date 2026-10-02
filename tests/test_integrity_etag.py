import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import requests

from framebridge.integrity import original_probe, verify_etag
from framebridge.storage import UploaderError
from framebridge.uploader import part_bounds


class ETagVerification(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'source.bin'
        self.data = b'abcdefgh'
        self.path.write_bytes(self.data)
        self.api, self.http = Mock(), Mock()
        self.original = dict(asset_id='asset', media_id='media', key='original', filesizeInBytes=len(self.data),
                             downloadUrl='https://assets.frame.io/original?signature=private')
        self.api.rendition.return_value = self.original
        self.api.upload_evidence.return_value = dict(id='asset', status='UPLOADED', filesize=len(self.data))
        self.etag = '"' + hashlib.md5(self.data, usedforsecurity=False).hexdigest() + '"'
        self.responses = []
        self.http.get.side_effect = lambda *args, **kwargs: self.response()

    def response(self, status=206, body=b'a', **headers):
        result = Mock(status_code=status)
        result.headers = {'Content-Range': f'bytes 0-0/{self.original["filesizeInBytes"]}',
                          'Content-Length': '1', 'ETag': self.etag, **headers}
        result.raw = io.BytesIO(body)
        result.__enter__ = Mock(return_value=result)
        result.__exit__ = Mock(return_value=False)
        self.responses.append(result)
        return result

    def verify(self, **kwargs):
        return verify_etag(self.api, 'asset', self.path, http=self.http, **kwargs)

    def test_single_part_match_is_not_sha256_verification(self):
        result = self.verify()
        self.assertTrue(result['etag_match'])
        self.assertFalse(result['checksum_verified'])
        self.assertEqual(result['verification_level'], 'etag_md5_match')
        self.assertEqual(result['probe_bytes_consumed'], 2)
        self.assertEqual(result['local_sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(sum(r.raw.tell() for r in self.responses), 2)
        for call in self.http.get.call_args_list:
            self.assertEqual(call.kwargs['headers']['Range'], 'bytes=0-0')
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertNotIn('authorization', call.kwargs['headers'])

    def test_multipart_boundary_match(self):
        self.data = b'x' * (5 * 1024 * 1024 + 7)
        self.path.write_bytes(self.data)
        self.original['filesizeInBytes'] = len(self.data)
        self.api.upload_evidence.return_value['filesize'] = len(self.data)
        parts = []
        for i in range(2):
            start, size = part_bounds(len(self.data), 2, i)
            parts.append(hashlib.md5(self.data[start:start + size], usedforsecurity=False).digest())
        self.etag = '"' + hashlib.md5(b''.join(parts), usedforsecurity=False).hexdigest() + '-2"'
        result = self.verify(part_count=2)
        self.assertEqual(result['verification_level'], 'multipart_etag_match')

    def test_incorrect_part_count_stops_after_probe(self):
        with self.assertRaisesRegex(UploaderError, 'part count'): self.verify(part_count=35)
        self.assertEqual(self.http.get.call_count, 1)

    def test_unrecognized_weak_and_missing_etags_rejected(self):
        for value in ('', 'W/' + self.etag, '"not-md5"'):
            self.etag = value
            with self.subTest(value=value), self.assertRaisesRegex(UploaderError, 'strong'):
                self.verify()

    def test_mismatched_etag_not_claimed_as_corruption_proof(self):
        self.etag = '"' + '0' * 32 + '"'
        with self.assertRaisesRegex(UploaderError, 'semantics may differ'): self.verify()

    def test_full_get_redirect_wrong_range_encoding_and_length_not_consumed(self):
        cases = [(200, {}), (302, {}), (206, {'Content-Range': 'bytes 0-1/8'}),
                 (206, {'Content-Encoding': 'gzip'}), (206, {'Content-Length': '8'})]
        for status, headers in cases:
            response = self.response(status=status, **headers)
            self.http.get.side_effect = None
            self.http.get.return_value = response
            with self.subTest(status=status, headers=headers), self.assertRaises(UploaderError): self.verify()
            self.assertEqual(response.raw.tell(), 0)

    def test_short_probe_rejected(self):
        self.http.get.side_effect = lambda *a, **kw: self.response(body=b'')
        with self.assertRaisesRegex(UploaderError, 'truncated'): self.verify()

    def test_remote_identity_changed_during_hash(self):
        self.api.rendition.side_effect = [self.original, dict(self.original, media_id='different')]
        with self.assertRaisesRegex(UploaderError, 'Remote original changed'): self.verify()

    def test_remote_etag_changed_after_hash(self):
        first = self.response()
        second = self.response(ETag='"' + '0' * 32 + '"')
        self.http.get.side_effect = [first, second]
        with self.assertRaisesRegex(UploaderError, 'Remote original changed'): self.verify()

    def test_local_change_during_hash(self):
        def change(_): self.path.write_bytes(b'changed-longer')
        with self.assertRaisesRegex(UploaderError, 'grew|changed'):
            self.verify(progress=change)

    def test_host_size_and_status_checked_before_probe(self):
        self.original['downloadUrl'] = 'https://unverified.example/original'
        with self.assertRaisesRegex(UploaderError, 'host'): self.verify()
        self.http.get.assert_not_called()
        self.original['downloadUrl'] = 'https://assets.frame.io/original'
        self.original['filesizeInBytes'] = 9
        with self.assertRaisesRegex(UploaderError, 'size'): self.verify()
        self.http.get.assert_not_called()

    def test_network_failure_redacts_url(self):
        self.http.get.side_effect = requests.ConnectionError('https://private/?signature=secret')
        with self.assertRaises(UploaderError) as raised: self.verify()
        self.assertNotIn('signature', str(raised.exception))
