#!/usr/bin/env python3
"""Admin gunicorn launcher -- prevents local platform/ directory shadowing stdlib."""
import sys, os

# Remove project root from sys.path so stdlib platform can be imported
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_script_dir)
_saved = []
while _project_root in sys.path:
    sys.path.remove(_project_root)
    _saved.append(_project_root)
while '' in sys.path:
    sys.path.remove('')
    _saved.append('')

# Force-load REAL stdlib platform module (before project path can shadow it)
import platform as _stdlib_platform
assert hasattr(_stdlib_platform, 'system'), 'FATAL: stdlib platform module not loaded!'

# Restore project root to sys.path (after stdlib entries, lower priority)
for p in reversed(_saved):
    if p not in sys.path:
        sys.path.append(p)

# CWD remains removed to prevent shadowing in workers
# Run gunicorn with remaining args
from gunicorn.app.wsgiapp import run
if __name__ == '__main__':
    run()
