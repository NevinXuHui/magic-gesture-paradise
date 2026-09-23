#!/usr/bin/env python3
"""Serve the packaged game on the robot and use its existing Electron URL renderer."""
import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith('/api/'):
            return super().do_GET()
        connection = http.client.HTTPConnection('127.0.0.1', self.server.backend_port, timeout=10)
        try:
            connection.request('GET', self.path)
            response = connection.getresponse()
            body = response.read()
            self.send_response(response.status)
            self.send_header('Content-Type', response.getheader('Content-Type', 'application/octet-stream'))
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)
        except (OSError, http.client.HTTPException) as error:
            body = str(error).encode()
            self.send_response(503)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser()
    project = Path(__file__).resolve().parents[1]
    parser.add_argument('--app-root', type=Path,
                        default=project / 'build/run/rock_paper_scissors')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=18080)
    parser.add_argument('--backend-port', type=int, default=18081)
    args = parser.parse_args()
    app_root = args.app_root.resolve()
    backend = app_root / 'backend/main.py'
    web = app_root / 'web'
    if not backend.is_file() or not (web / 'index.html').is_file():
        parser.error('请先把 SmartApp tar.gz 解压到 build/run/')

    env = os.environ.copy()
    env['SMARTAPP_DYNAMIC_HOST'] = '127.0.0.1'
    env['SMARTAPP_DYNAMIC_PORT'] = str(args.backend_port)
    process = subprocess.Popen([sys.executable, '-u', str(backend)], cwd=str(app_root),
                               env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        init = {'event': 'runtime_init', 'sessionId': 'dog-standalone',
                'appId': 'rock_paper_scissors', 'version': '0.1.8'}
        process.stdin.write((json.dumps(init) + '\n').encode())
        process.stdin.flush()
        ready = process.stdout.readline()
        if not ready or json.loads(ready).get('event') != 'app_ready':
            raise RuntimeError('Python 推理服务未就绪: {}'.format(ready.decode(errors='replace')))
        server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(web)))
        server.backend_port = args.backend_port
        signal.signal(signal.SIGTERM,
                      lambda _s, _f: threading.Thread(target=server.shutdown, daemon=True).start())
        print('游戏地址: http://<机器狗IP>:{}/'.format(args.port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        if process.poll() is None:
            try:
                process.stdin.write(b'{"event":"app_stop"}\n')
                process.stdin.flush()
                process.wait(timeout=6)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
