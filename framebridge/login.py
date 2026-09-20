"""User-initiated capture through a nonce-protected loopback receiver."""
import json
import re
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .storage import UploaderError, atomic_write


def session_id(output):
    match = re.search(r'\bSession (\d+) created\b', output)
    if not match:
        match = re.search(r'^\s*(\d+)\s*$', output, re.M)
    return match[1] if match else None


def login(store, state_dir):
    executable = shutil.which('npx.cmd') or shutil.which('npx')
    if not executable:
        raise UploaderError('Install Node.js (including npx) and enable the Playwriter Chrome extension first.')
    nonce, saved = secrets.token_urlsafe(32), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not secrets.compare_digest(self.headers.get('x-login-nonce', ''), nonce) or not 0 < length < 65536:
                    self.send_error(403)
                    return
                values = json.loads(self.rfile.read(length))
                for field in ('access_token', 'refresh_token', 'session_token', 'client_name', 'client_version'):
                    if not isinstance(values.get(field), str) or not values[field]:
                        raise ValueError()
                values['expires_at'] = float(values.get('expires_at', 0))
                store.save(values)
                saved.set()
                self.send_response(204)
                self.end_headers()
            except Exception:
                self.send_error(400, 'Session capture failed')

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    wrapper = Path(state_dir) / 'login-bridge.js'
    try:
        new = subprocess.run([executable, '--yes', 'playwriter@latest', 'session', 'new'], capture_output=True, text=True, timeout=45)
        identifier = session_id(new.stdout)
        if new.returncode or not identifier:
            raise UploaderError('Playwriter session unavailable. Enable the extension on a Chrome tab and retry.')
        module = Path(__file__).with_name('browser_login.mjs').as_uri()
        code = 'await (await import(' + json.dumps(module) + ')).login({context,getCDPSession,endpoint:' + json.dumps(f'http://127.0.0.1:{server.server_port}/') + ',nonce:' + json.dumps(nonce) + '});'
        atomic_write(wrapper, code.encode())
        print('Complete Frame.io sign-in in the browser if prompted. Waiting up to 2 minutes...')
        result = subprocess.run([executable, '--yes', 'playwriter@latest', '-s', identifier, '-f', str(wrapper), '--timeout', '150000'], capture_output=True, text=True, timeout=180)
        if result.returncode or not saved.is_set():
            raise UploaderError('Browser capture did not complete. Check Chrome sign-in and the Playwriter extension, then retry.')
    except subprocess.TimeoutExpired:
        raise UploaderError('Browser login timed out. Retry after signing in to Frame.io.') from None
    finally:
        wrapper.unlink(missing_ok=True)
        server.shutdown()
        server.server_close()
