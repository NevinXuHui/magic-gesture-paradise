"""Local static site + serialized native MediaPipe inference; no images are saved."""
import argparse
import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2

from backend.inference import Inference

ROOT = Path(__file__).resolve().parent
MAX_FRAME_BYTES = 2 * 1024 * 1024


class Handler(SimpleHTTPRequestHandler):
    def json_response(self, code, data):
        body = json.dumps(data, separators=(',', ':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.split('?')[0] == '/api/status':
            return self.json_response(200, dict(ready=True, engine='MediaPipe Python CPU', model='gesture_recognizer.task'))
        return super().do_GET()

    def do_POST(self):
        if self.path != '/api/infer':
            return self.json_response(404, dict(error='Unknown endpoint'))
        # Only accept same-origin browser clients. Bind to loopback by default.
        origin = self.headers.get('Origin')
        if origin and origin not in (scheme + self.headers.get('Host', '') for scheme in ('http://', 'https://')):
            return self.json_response(403, dict(error='Origin not allowed'))
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_FRAME_BYTES:
                return self.json_response(413, dict(error='Invalid frame size'))
            result = self.server.inference.recognize(self.rfile.read(length))
            self.json_response(429 if result is None else 200, result or dict(error='识别服务忙，请关闭其他游戏页面'))
        except ValueError as error:
            self.json_response(400, dict(error=str(error)))
        except Exception as error:
            self.log_error('Inference failed: %s', error)
            self.json_response(500, dict(error='Python 推理失败，请查看服务终端'))

    def log_message(self, fmt, *args):
        if self.path.startswith('/api/') and len(args) > 1 and str(args[1]) == '200':
            return
        super().log_message(fmt, *args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=5174)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--cert')
    parser.add_argument('--key')
    args = parser.parse_args()
    if not (ROOT / 'dist/index.html').exists():
        parser.error('请先运行 npm run build')
    cv2.setNumThreads(1)
    inference = Inference()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(ROOT / 'dist')))
    if args.cert:
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.cert, args.key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.inference = inference
    print(f'Python 模型已就绪，打开 http://127.0.0.1:{args.port}/?debug=1', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        inference.close()


if __name__ == '__main__':
    main()
