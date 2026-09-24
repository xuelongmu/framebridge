import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from framebridge.api import Api
from framebridge.catalog import Catalog, public
from framebridge.commands import profile_dir
from framebridge.downloader import download, safe_name
from framebridge.storage import UploaderError


class Response:
    def __init__(self, data=b'abcd', status=206, span='bytes 0-3/8', etag='"same"'):
        self.status_code = status
        self.headers = {'Content-Range':span,'ETag':etag}
        self.data = data
        self.read = False
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def iter_content(self,*a):
        self.read = True
        yield self.data


class Downloads(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)/'sample.mp4'
        self.api = Mock()
        self.api.rendition.return_value = dict(asset_id='a',media_id='m',key='h264_360',
            filesizeInBytes=8,downloadUrl='https://stream-download.frame.io/file?secret=value')
        self.http = Mock()
    def run_download(self, **kwargs):
        with patch('framebridge.downloader.CHUNK',4):
            return download(self.api,'a','360p',self.output,http=self.http,sleep=lambda _:None,**kwargs)
    def test_range_resume_after_interrupt(self):
        self.http.get.side_effect = [Response(),KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt): self.run_download()
        self.http.get.side_effect = [Response(b'efgh',span='bytes 4-7/8')]
        result = self.run_download()
        self.assertEqual(self.output.read_bytes(),b'abcdefgh')
        self.assertEqual(result['sha256'],hashlib.sha256(b'abcdefgh').hexdigest())
        self.assertEqual(self.http.get.call_args.kwargs['headers']['Range'],'bytes=4-7')
        self.assertNotIn('Authorization',self.http.get.call_args.kwargs['headers'])
        self.assertNotIn('secret',self.output.with_name('sample.mp4.framebridge.json').read_text())
    def test_full_response_never_consumed(self):
        response = Response(status=200)
        self.http.get.return_value = response
        with self.assertRaisesRegex(UploaderError,'bounded 206'): self.run_download()
        self.assertFalse(response.read)
        self.assertFalse(self.output.exists())
    def test_refresh_expired_url(self):
        self.http.get.side_effect = [Response(status=403),Response(),Response(b'efgh',span='bytes 4-7/8')]
        self.run_download()
        self.assertEqual(self.api.rendition.call_count,2)
    def test_wrong_range_refused(self):
        self.http.get.return_value = Response(span='bytes 4-7/8')
        with self.assertRaisesRegex(UploaderError,'unexpected'): self.run_download()
    def test_changed_etag_refused(self):
        self.http.get.side_effect = [Response(),Response(b'efgh',span='bytes 4-7/8',etag='"changed"')]
        with self.assertRaisesRegex(UploaderError,'ETag'): self.run_download()
    def test_short_body_retried_bounded(self):
        self.http.get.side_effect = [Response(b'a') for _ in range(3)]
        with self.assertRaisesRegex(UploaderError,'network failure'): self.run_download()
        self.assertEqual(self.http.get.call_count,3)
    def test_no_overwrite(self):
        self.output.write_bytes(b'precious')
        with self.assertRaisesRegex(UploaderError,'already exists'): self.run_download()
        self.http.get.assert_not_called()
        self.assertEqual(self.output.read_bytes(),b'precious')
    def test_sample_bounded(self):
        self.http.get.return_value = Response()
        result = self.run_download(max_bytes=4)
        self.assertTrue(result['sample'])
        self.assertEqual(self.output.stat().st_size,4)
    def test_uncommitted_tail_removed(self):
        self.http.get.side_effect = [Response(),KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt): self.run_download()
        with self.output.with_suffix('.mp4.part').open('ab') as f: f.write(b'crash-tail')
        self.http.get.side_effect = [Response(b'efgh',span='bytes 4-7/8')]
        self.run_download()
        self.assertEqual(self.output.read_bytes(),b'abcdefgh')
    def test_corrupt_partial_refused(self):
        self.http.get.side_effect = [Response(),KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt): self.run_download()
        self.output.with_suffix('.mp4.part').write_bytes(b'xxxx')
        with self.assertRaisesRegex(UploaderError,'checksum changed'): self.run_download()
    def test_checksum_mismatch_keeps_part(self):
        self.http.get.return_value = Response()
        with self.assertRaisesRegex(UploaderError,'checksum mismatch'): self.run_download(max_bytes=4,expected_sha256='0'*64)
        self.assertFalse(self.output.exists())
        self.assertTrue(self.output.with_suffix('.mp4.part').exists())
    def test_host_allowlist(self):
        self.api.rendition.return_value['downloadUrl']='https://evil.example/file'
        with self.assertRaisesRegex(UploaderError,'host'): self.run_download()
        self.http.get.assert_not_called()
    def test_profiles_and_names(self):
        with self.assertRaises(UploaderError): profile_dir(Path('root'),'../bad')
        self.assertEqual(profile_dir(Path('root'),'default'),Path('root'))
        self.assertEqual(safe_name('CON'),'_CON')
        self.assertNotIn('/',safe_name('../danger/file'))
    def test_public_output_strips_signed_urls(self):
        self.assertEqual(public({'downloadUrl':'secret','nested':{'streamUrl':'secret','key':'k'}}),{'nested':{'key':'k'}})
    def test_disallowed_mutation_blocked(self):
        store=Mock();store.load.return_value={}
        api=Api(store,http=Mock())
        with self.assertRaisesRegex(UploaderError,'not permitted'):
            api._request('DeleteAssets','mutation DeleteAssets { deleteAssets { id } }',{})
        api.http.post.assert_not_called()
    def test_no_original_fallback(self):
        store=Mock();store.load.return_value={}
        api=Catalog(store)
        api.media=Mock(return_value={'id':'a','name':'file','media':{'id':'m','videoTranscodes':[]}})
        with self.assertRaisesRegex(UploaderError,'no automatic original'): api.rendition('a','1080p')


if __name__ == '__main__': unittest.main()
