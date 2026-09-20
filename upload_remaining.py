"""Compatibility entry point. Defaults to a read-only plan, never a fresh upload."""
import sys
from framebridge.cli import main

if __name__ == '__main__':
    raise SystemExit(main(['remaining', *sys.argv[1:]]))
