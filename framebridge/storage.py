"""Local state primitives. Session secrets use Windows user-scoped DPAPI."""
import contextlib
import ctypes
import json
import os
import sqlite3
from pathlib import Path


class UploaderError(Exception):
    """A message safe to display without exposing tokens or signed URLs."""


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
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
    path.parent.mkdir(parents=True, exist_ok=True)
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

    def save(self, values):
        atomic_write(self.path, _dpapi(json.dumps(values).encode(), True))

    def load(self):
        if not self.path.exists():
            raise UploaderError('No local session. Run: python -m framebridge login')
        try:
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
