#!/usr/bin/env python3
"""Start 听间 (the local podcast library) from the repository root.

    python run.py                 # serve on 127.0.0.1:8765 and open a browser
    python run.py --port 9000     # use another port
    python run.py --no-browser    # do not open a browser

Standard library only: nothing to install for the core app.
"""
import argparse
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'app'))

import podcast_server  # noqa: E402  (path is prepared above)


def wait_until_ready(port, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2) as response:
                if b'podcast-local' in response.read(200):
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    parser = argparse.ArgumentParser(description='本地播客书架 · 听间')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PODCAST_PORT', 8765)))
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()

    httpd = podcast_server.serve(args.port)
    url = f'http://127.0.0.1:{args.port}'
    if not args.no_browser:
        threading.Thread(
            target=lambda: webbrowser.open(url) if wait_until_ready(args.port) else None,
            daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止。音频和笔记都留在这台电脑上。', flush=True)
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
