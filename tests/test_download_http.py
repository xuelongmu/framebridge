"""Exercise real requests streaming against a bounded local HTTP server."""
import hashlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from framebridge.downloader import download


class DownloadHTTP(unittest.TestCase):
    def test_real_ranges_resume_and_completed_skip(self):
        payload = bytes(range(256)) * 401
        ranges = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.server.test.assertIsNone(self.headers.get('Authorization'))
                start, end = map(int, self.headers['Range'][6:].split('-'))
                ranges.append((start, end))
                data = payload[start:end + 1]
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{end}/{len(payload)}')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('ETag', '"stable"')
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.test = self
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        api = Mock()
        api.rendition.return_value = dict(asset_id='a', media_id='m', key='h264_360',
            filesizeInBytes=len(payload), downloadUrl=f'http://127.0.0.1:{server.server_port}/media')

        def interrupt(event):
            raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as temp, patch('framebridge.downloader.check_url'), patch('framebridge.downloader.CHUNK', 32768):
            output = Path(temp) / 'test.mp4'
            with self.assertRaises(KeyboardInterrupt):
                download(api, 'a', '360p', output, progress=interrupt)
            result = download(api, 'a', '360p', output,
                expected_sha256=hashlib.sha256(payload).hexdigest())
            self.assertEqual(output.read_bytes(), payload)
            self.assertFalse(result['sample'])
            self.assertEqual(ranges[1][0], 32768)
            self.assertFalse(output.with_suffix('.mp4.part').exists())
            count = len(ranges)
            self.assertTrue(download(api, 'a', '360p', output)['skipped'])
            self.assertEqual(len(ranges), count)
