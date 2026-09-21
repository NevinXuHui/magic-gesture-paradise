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
    # 优先尝试机器狗共享流，不使用 USB 摄像头
    # 检查多个可能的共享流
    shared_stream_path = None
    for path in ['/tmp/neck_jpeg', '/tmp/foo_fhd', '/tmp/neck_hd']:
        if os.path.exists(path):
            shared_stream_path = path
            break

    if not shared_stream_path:
        problem = '未找到机器狗摄像头共享流'
        print(f"错误：{problem}")
        print("尝试的路径：/tmp/neck_jpeg, /tmp/foo_fhd, /tmp/neck_hd")
        time.sleep(60)  # 避免循环重试
        return

    use_shared_stream = True
    print(f"检测到机器狗共享流，使用 {shared_stream_path}")

    while True:
        path = shared_stream_path
        if not os.path.exists(path):
            problem = '机器狗额头相机共享流已断开'
            time.sleep(2)
            continue
        pipeline = (f'shmsrc socket-path={path} is-live=true do-timestamp=true ! '
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
                camera_type = '机器狗额头相机'
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
