"""Local-only watchdog, hosted independently by Windows Task Scheduler."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parent
opener = build_opener(ProxyHandler({}))

def healthy(port):
    try:
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=3) as r:
            h = json.load(r)
        return h.get('ok') is True and h.get('service') == 'chanxu-local-data'
    except Exception:
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    port = parser.parse_args().port
    if not 1024 <= port <= 65535:
        parser.error('invalid port')
    handler = RotatingFileHandler(ROOT / f'supervisor-{port}.log', maxBytes=1024*1024, backupCount=3, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(levelname)s %(message)s')
    logging.info('supervisor started port=%s', port)
    child = None
    failures = 0
    while True:
        if child is not None and child.poll() is not None:
            logging.warning('server exited pid=%s code=%s; will restart', child.pid, child.returncode)
            child = None
        if healthy(port):
            failures = 0
        elif child is None:
            # Binding conflicts are logged; never kill an unrelated listener.
            with (ROOT / 'server.log').open('ab', buffering=0) as out, (ROOT / 'server-error.log').open('ab', buffering=0) as err:
                child = subprocess.Popen([sys.executable, '-u', str(ROOT / 'server.py'), '--port', str(port)], cwd=ROOT, stdout=out, stderr=err, creationflags=subprocess.CREATE_NO_WINDOW)
            logging.info('server spawned pid=%s', child.pid)
            failures = 0
        else:
            failures += 1
            if failures >= 6:
                logging.error('owned server unresponsive pid=%s; restarting', child.pid)
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                child = None
                failures = 0
        time.sleep(5)

if __name__ == '__main__':
    main()
