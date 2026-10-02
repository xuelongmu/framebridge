import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from framebridge.integrity import verify_etag
from framebridge.storage import UploaderError
from framebridge.transcripts import export_transcript


class FeatureHttp(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.write_bytes(b'abcdefgh')
        self.requests = []
        self.etag_status = 206
        self.transcript_body = b'1\n00:00:00,000 --> 00:00:01,000\nHello\n'
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                test.requests.append(dict(self.headers))
                if self.path == '/original':
                    self.send_response(test.etag_status)
                    self.send_header('Content-Length', '1')
                    self.send_header('Content-Range', 'bytes 0-0/8')
                    self.send_header('ETag', '"' + hashlib.md5(b'abcdefgh', usedforsecurity=False).hexdigest() + '"')
                    self.end_headers()
                    self.wfile.write(b'a')
                else:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(test.transcript_body)))
                    self.end_headers()
                    self.wfile.write(test.transcript_body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.api = Mock()
        self.api.rendition.return_value = dict(asset_id='a', media_id='m', key='original',
                                               filesizeInBytes=8, downloadUrl=self.origin + '/original')
        self.api.upload_evidence.return_value = dict(id='a', status='UPLOADED', filesize=8)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def test_real_http_etag_probes(self):
        with patch('framebridge.integrity.check_url'):
            result = verify_etag(self.api, 'a', self.source)
        self.assertTrue(result['etag_match'])
        self.assertEqual(len(self.requests), 2)
        for headers in self.requests:
            self.assertEqual(headers['Range'], 'bytes=0-0')
            self.assertEqual(headers['Accept-Encoding'], 'identity')
            self.assertNotIn('Authorization', headers)

    def test_real_http_ignored_range_refused(self):
        self.etag_status = 200
        with patch('framebridge.integrity.check_url'), self.assertRaisesRegex(UploaderError, '206'):
            verify_etag(self.api, 'a', self.source)
        self.assertEqual(len(self.requests), 1)

    def test_real_http_transcript_export(self):
        self.api.asset.return_value = {'project': {'id': 'p'}}
        self.api.project.return_value = {'permissions': {'canDownloadTranscription': True}}
        self.api.transcripts.return_value = [{'id': 't', 'locale': 'en-US', 'lastEditedAt': 'date',
                                             'encodeStatus': 'SUCCESS', 'srt': {'downloadUrl': self.origin + '/transcript'}}]
        output = self.root / 'captions.srt'
        with patch('framebridge.transcripts.check_url'):
            result = export_transcript(self.api, 'a', 't', 'srt', output)
        self.assertEqual(output.read_bytes(), self.transcript_body)
        self.assertEqual(result['sha256'], hashlib.sha256(self.transcript_body).hexdigest())
        self.assertNotIn('Authorization', self.requests[0])
