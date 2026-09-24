"""Installed CLI entry point."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None):
    from .commands import main as dispatch
    return dispatch(argv)
