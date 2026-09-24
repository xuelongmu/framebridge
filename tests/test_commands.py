import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from framebridge.commands import download_folder, main, upload_batch, upload_one, parser
from framebridge.storage import UploaderError


class Commands(unittest.TestCase):
    def test_obsolete_commands_removed(self):
        for command in ('remaining', 'list'):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                parser().parse_args([command])
            self.assertEqual(result.exception.code, 2)

    def test_upload_current_cli_json_and_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'sample.txt'
            source.write_text('data')
            output = io.StringIO()
            with patch('framebridge.commands.Catalog') as catalog, patch('framebridge.uploader.upload', return_value='asset') as transfer:
                catalog.return_value.project.return_value = {'account':{'id':'account'},'permissions':{'canCreateAsset':True}}
                catalog.return_value.folder.return_value = {'permissions':{'canCreateChildren':True}}
                with contextlib.redirect_stdout(output):
                    result = main(['--state-dir',str(root),'--profile','work','--json','upload',str(source),'--project','project','--folder-id','folder'])
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(output.getvalue())['asset_id'], 'asset')
                self.assertEqual(transfer.call_args.args[3:6], ('project','account','folder'))
                self.assertTrue((root/'profiles'/'work'/'uploads.sqlite3').exists())

    def test_upload_permission_denied_does_not_transfer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'sample.txt'
            source.write_text('data')
            args = argparse.Namespace(path=source,project='project',folder_id='folder',experimental_multipart=False)
            self.api.project.return_value = {'permissions':{'canCreateAsset':False}}
            with patch('framebridge.uploader.upload') as transfer:
                with self.assertRaises(UploaderError): upload_one(self.api, root, args)
                transfer.assert_not_called()

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
