#!/usr/bin/env python3
"""Start, stop, and inspect the local background observatory."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('up', 'down', 'status', 'url', 'logs'))
    args = parser.parse_args()
    if not shutil.which('docker'):
        parser.exit(1, 'Docker Desktop is required. Install it and start it, then try again.\n')
    environment = dict(os.environ, LOCAL_UID=str(os.getuid()), LOCAL_GID=str(os.getgid()))
    commands = {'up': ['up', '-d', '--build', '--wait'], 'down': ['down'], 'status': ['ps'],
                'url': ['exec', '-T', 'observatory', 'cat', '/tmp/observatory-link'],
                'logs': ['logs', '--tail', '50']}
    folder = Path(__file__).resolve().parent
    try:
        subprocess.run(['docker', 'compose', *commands[args.action]], cwd=folder, env=environment, check=True)
        if args.action == 'up':
            subprocess.run(['docker', 'compose', *commands['url']], cwd=folder, env=environment, check=True)
            print('Running in the background. Stop with: python3 manage.py down')
    except (OSError, subprocess.CalledProcessError):
        print('Docker operation failed. Check that Docker Desktop is running and the local port is free.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
