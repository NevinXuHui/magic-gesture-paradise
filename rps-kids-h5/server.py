#!/usr/bin/env python3
import json
import os
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import cv2

BASE = Path(__file__).resolve().parent
lock = threading.Lock()
frame = None
updated = 0.0
problem = '等待摄像头初始化'

def capture():
    global frame, updated, problem
    # 优先尝试机器狗共享流，否则使用 USB 摄像头
    use_shared_stream = os.path.exists('/tmp/foo_jpeg')

    if use_shared_stream:
        print("检测到机器狗共享流，使用 /tmp/foo_jpeg")
        while True:
            path = '/tmp/foo_jpeg'
            if not os.path.exists(path):
                problem = '机器狗额头相机共享流已断开'
                time.sleep(2)
                continue
            pipeline = ('shmsrc socket-path=/tmp/foo_jpeg is-live=true do-timestamp=true ! '
                        'image/jpeg,width=1920,height=1080,framerate=30/1 ! '
                        'queue leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0 ! '
                        'videorate drop-only=true ! image/jpeg,framerate=15/1 ! jpegdec ! '
                        'videoconvert ! videoscale ! video/x-raw,format=BGR,width=960,height=540 ! '
                        'appsink drop=true max-buffers=1 sync=false')
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            try:
                while cap.isOpened():
                    ok, image = cap.read()
                    if not ok:
                        break
                    ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok:
                        with lock:
                            frame = jpeg.tobytes()
                            updated = time.monotonic()
                            problem = ''
            finally:
                cap.release()
            problem = '额头相机流已断开，正在重连'
            time.sleep(2)
    else:
        print("使用 USB 摄像头 /dev/video0")
        while True:
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                problem = 'USB 摄像头打开失败，2秒后重试'
                time.sleep(2)
                continue

            # 设置摄像头参数
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)

            try:
                while cap.isOpened():
                    ok, image = cap.read()
                    if not ok:
                        break
                    ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if ok:
                        with lock:
                            frame = jpeg.tobytes()
                            updated = time.monotonic()
                            problem = ''
            finally:
                cap.release()
            problem = 'USB 摄像头已断开，正在重连'
            time.sleep(2)

class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/api/frame', '/api/status'):
            with lock:
                ready = frame is not None and time.monotonic() - updated < 3
                data = frame if ready else None
                message = problem or '未收到新的视频帧'
                age_ms = round((time.monotonic() - updated) * 1000) if updated else None
            if path == '/api/status':
                camera_type = 'USB摄像头' if not os.path.exists('/tmp/foo_jpeg') else '机器狗额头相机'
                body = json.dumps({'ready': ready, 'camera': camera_type, 'source': 'server', 'bridge_frame_age_ms': age_ms, 'error': '' if ready else message}).encode()
                code, mime = 200, 'application/json'
            elif data:
                body, code, mime = data, 200, 'image/jpeg'
            else:
                body, code, mime = message.encode(), 503, 'text/plain; charset=utf-8'
            self.send_response(code)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        super().do_GET()

if __name__ == '__main__':
    threading.Thread(target=capture, daemon=True).start()
    port = int(os.environ.get('PORT', '5174'))
    print(f"服务启动在 0.0.0.0:{port}")
    print(f"访问地址：http://192.168.123.99:{port}")
    ThreadingHTTPServer(('0.0.0.0', port), partial(Handler, directory=str(BASE / 'dist'))).serve_forever()
