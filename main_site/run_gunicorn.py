#!/usr/bin/env python3
"""Platform service gunicorn launcher - avoids platform module shadowing."""
import sys, os

# Remove CWD (platform/ dir) from sys.path so stdlib platform can be imported
cwd = os.getcwd()
sys.path = [p for p in sys.path if p not in ('', cwd)]

# Now safe to import gunicorn - stdlib platform is accessible
from gunicorn.app.wsgiapp import run

if __name__ == '__main__':
    run()
