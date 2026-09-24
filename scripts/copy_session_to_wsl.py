"""Run on Windows: copy a session to WSL through stdin, never argv or stdout."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from framebridge.storage import SessionStore, exclusive_lock


IMPORT = '''
import json, sys
from pathlib import Path
from framebridge.storage import SessionStore, exclusive_lock, atomic_write
try:
    state = Path(sys.argv[1])
    if not state.is_absolute():
        raise ValueError()
    with exclusive_lock(state / 'process.lock'):
        store = SessionStore(state / 'session.dpapi')
        if store.path.exists() or (state / 'identity.json').exists():
            raise ValueError()
        payload = json.load(sys.stdin)
        store.save(payload['session'])
        if payload.get('identity'):
            atomic_write(state / 'identity.json', json.dumps(payload['identity']).encode())
    print('Session copied to owner-only WSL storage; values were not displayed.')
except Exception:
    print('Import failed. Check the target directory and choose an unused profile.', file=sys.stderr)
    sys.exit(1)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-state', type=Path, required=True)
    parser.add_argument('--distro', default='Ubuntu')
    parser.add_argument('--wsl-python', required=True)
    parser.add_argument('--target-state', required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('Run this helper with Windows Python to unlock DPAPI.')
    try:
        with exclusive_lock(args.source_state / 'process.lock'):
            store = SessionStore(args.source_state / 'session.dpapi')
            identity = args.source_state / 'identity.json'
            payload = {'session':store.load(),
                       'identity':json.loads(identity.read_text()) if identity.exists() else None}
            result = subprocess.run(['wsl.exe','-d',args.distro,'--',args.wsl_python,
                '-c',IMPORT,args.target_state], input=json.dumps(payload),
                text=True, capture_output=True, timeout=45)
            if result.returncode:
                print('WSL import failed; check installation and use an unused private target directory.', file=sys.stderr)
                return 1
        print('Session copied to WSL without displaying credential values. Use only one copy at a time; refresh tokens rotate.')
        return 0
    except Exception:
        print('Session transfer failed; credential details suppressed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
