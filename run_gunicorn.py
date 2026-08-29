#!/usr/bin/env python3
"""Wrapper: fix sys.path before gunicorn loads (prevents local platform/ shadowing stdlib)"""
import sys, os

# Remove project root from sys.path temporarily so stdlib platform can be imported
_script_dir = os.path.dirname(os.path.abspath(__file__))
_saved = []
while _script_dir in sys.path:
    sys.path.remove(_script_dir)
    _saved.append(_script_dir)

# Force-load REAL stdlib platform module
import platform as _stdlib_platform
assert hasattr(_stdlib_platform, 'system'), 'FATAL: stdlib platform module not loaded!'

# Restore project root (but after stdlib entries, so it has lower priority)
for p in reversed(_saved):
    if p not in sys.path:
        sys.path.insert(0, p)

# Also remove CWD to prevent shadowing in workers
sys.path = [p for p in sys.path if p != '']

# Run gunicorn with remaining args
from gunicorn.app.wsgiapp import run
if __name__ == '__main__':
    run()
