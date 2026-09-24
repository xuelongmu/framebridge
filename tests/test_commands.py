import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from framebridge.commands import download_folder, main, upload_batch
from framebridge.storage import UploaderError


class Commands(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.walk.return_value = [dict(id='asset', name='clip.mov', __typename='VideoAsset', relative_parent='')]
        self.api.rendition.return_value = dict(key='h264_360', filesizeInBytes=8)
        self.args = argparse.Namespace(folder_id='folder', recursive=False, limit=100,
            rendition='360p', output=Path('downloads'), max_total_bytes=8, execute=False, dry_run=False)

    def test_folder_defaults_to_plan(self):
        with patch('framebridge.commands.download') as transfer:
            result = download_folder(self.api, self.args)
            self.assertTrue(result['dry_run'])
            self.assertEqual(result['total_bytes'], 8)
            transfer.assert_not_called()

    def test_folder_cap_checked_before_execution(self):
        self.args.execute = True
        self.args.max_total_bytes = 7
        with patch('framebridge.commands.download') as transfer:
            with self.assertRaises(UploaderError): download_folder(self.api, self.args)
            transfer.assert_not_called()

    def test_dry_run_overrides_execute(self):
        self.args.execute = self.args.dry_run = True
        with patch('framebridge.commands.download') as transfer:
            self.assertTrue(download_folder(self.api, self.args)['dry_run'])
            transfer.assert_not_called()

    def test_version_stack_is_reported(self):
        self.api.walk.return_value[0]['__typename'] = 'VersionStackAsset'
        result = download_folder(self.api, self.args)
        self.assertEqual(len(result['failed']), 1)
        self.assertEqual(result['plan'], [])

    def test_upload_batch_defaults_to_plan(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root/'clip.txt'
            source.write_text('test')
            manifest = root/'manifest.json'
            manifest.write_text(json.dumps([dict(path=str(source), folder_id='folder')]))
            args = argparse.Namespace(manifest=manifest, experimental_multipart=False, project='project', execute=False)
            self.api.project.return_value = {'permissions':{'canCreateAsset':True}}
            self.api.folder.return_value = {'permissions':{'canCreateChildren':True}}
            with patch('framebridge.uploader.upload') as transfer:
                self.assertTrue(upload_batch(self.api, root, args)['dry_run'])
                transfer.assert_not_called()

    def test_login_cannot_replace_existing_user(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'identity.json').write_text(json.dumps({'id':'old'}))
            with patch('framebridge.login.login'), patch('framebridge.commands.SessionStore') as store, patch('framebridge.commands.Catalog') as catalog:
                catalog.return_value.me.return_value = {'id':'new'}
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main(['--state-dir',str(root),'--json','login']), 1)
                store.return_value.save.assert_not_called()
                self.assertEqual(json.loads((root/'identity.json').read_text())['id'], 'old')
