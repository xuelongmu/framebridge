import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from framebridge.storage import SessionStore, UploaderError, default_state_dir, exclusive_lock, source_identity


class PlatformTests(unittest.TestCase):
    def test_explicit_state_environment(self):
        with patch.dict(os.environ, {'FRAMEBRIDGE_STATE_DIR': '/explicit/state'}):
            self.assertEqual(default_state_dir('/unused'), Path('/explicit/state'))

    def test_source_identity_preserves_posix_case(self):
        if os.name == 'nt':
            self.assertEqual(source_identity('Clip.mov'), source_identity('clip.mov'))
        else:
            self.assertNotEqual(source_identity('Clip.mov'), source_identity('clip.mov'))

    def test_lock_contention(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'process.lock'
            with exclusive_lock(path):
                with self.assertRaises(UploaderError):
                    with exclusive_lock(path):
                        pass

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions')
    def test_private_session_and_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            store = SessionStore(Path(temp)/'private'/'session.dpapi')
            store.save({'access_token':'synthetic'})
            self.assertEqual(store.path.suffix, '.json')
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(store.path.parent.stat().st_mode), 0o700)
            self.assertEqual(store.load(), {'access_token':'synthetic'})
            store.save({'access_token':'rotated'})
            self.assertEqual(store.load(), {'access_token':'rotated'})
            store.path.chmod(0o644)
            with self.assertRaises(UploaderError): store.load()
            with self.assertRaises(UploaderError): store.save({})

    @unittest.skipIf(os.name == 'nt', 'POSIX file permissions')
    def test_public_directory_and_symlink_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.chmod(0o755)
            with self.assertRaises(UploaderError): SessionStore(root/'session.dpapi').save({})
            root.chmod(0o700)
            target = root/'target'
            target.write_text('unchanged')
            (root/'session.json').symlink_to(target)
            with self.assertRaises(UploaderError): SessionStore(root/'session.dpapi').save({})
            self.assertEqual(target.read_text(), 'unchanged')
