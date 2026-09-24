"""Local state: Windows DPAPI or owner-only POSIX credential files."""
import contextlib
import ctypes
import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path


class UploaderError(Exception):
    """A message safe to display without exposing tokens or signed URLs."""


def default_state_dir(root):
    if os.environ.get('FRAMEBRIDGE_STATE_DIR'):
        return Path(os.environ['FRAMEBRIDGE_STATE_DIR']).expanduser()
    if os.name == 'nt' and (Path(root) / '.state').is_dir():
        return Path(root) / '.state'
    if os.name == 'nt':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'framebridge'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'framebridge'
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state')) / 'framebridge'


def source_identity(path):
    value = str(path)
    return value.casefold() if os.name == 'nt' else value


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + '.tmp')
    try:
        with open(temp, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def exclusive_lock(path):
    """Nonblocking, OS-released lock; stale files are harmless after a crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, 'a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UploaderError('Another uploader/login process owns this state directory.') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _dpapi(data, protect):
    if os.name != 'nt':
        raise UploaderError('Encrypted session storage currently requires Windows DPAPI.')

    class Blob(ctypes.Structure):
        _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # UI_FORBIDDEN; default scope is the current Windows user, not the machine.
    if protect:
        success = crypt.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        success = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    if not success:
        raise UploaderError('Windows could not protect/unprotect the session. Run login again.')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)
        if os.name != 'nt' and self.path.suffix == '.dpapi':
            self.path = self.path.with_suffix('.json')

    def _check_private(self):
        for path, directory in ((self.path.parent, True), (self.path, False)):
            if not path.exists() and not path.is_symlink():
                continue
            info = path.lstat()
            correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            if not correct_type or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise UploaderError('Session directory must be owned by you with mode 0700; session file requires mode 0600. Use a private Linux/macOS state directory.')

    def save(self, values):
        data = json.dumps(values).encode()
        if os.name == 'nt':
            atomic_write(self.path, _dpapi(data, True))
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._check_private()
        fd, name = tempfile.mkstemp(prefix='.session-', dir=self.path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def load(self):
        if not self.path.exists():
            raise UploaderError('No local session. Run: python -m framebridge login')
        try:
            if os.name != 'nt':
                self._check_private()
                return json.loads(self.path.read_bytes())
            return json.loads(_dpapi(self.path.read_bytes(), False))
        except (ValueError, OSError):
            raise UploaderError('Cannot read the local session. Run login again.') from None


class Journal:
    """Persist each external-write boundary, with explicit ambiguous states."""
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS uploads (key TEXT PRIMARY KEY, record TEXT NOT NULL)')
        self.db.commit()

    def get(self, key):
        row = self.db.execute('SELECT record FROM uploads WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, record):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO uploads VALUES (?, ?)', (key, json.dumps(record)))

    def close(self):
        self.db.close()
