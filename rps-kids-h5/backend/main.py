#!/usr/bin/env python3
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import cv2
except ImportError as error:
    print("backend requires the target image to provide python3-opencv", file=sys.stderr)
    raise SystemExit(1) from error


STREAM_PATHS = ("/tmp/neck_jpeg", "/tmp/foo_fhd", "/tmp/neck_hd")
state_lock = threading.Lock()
stop_event = threading.Event()
latest_frame = None
latest_update = 0.0
camera_problem = "等待摄像头初始化"


def log(message):
    print("[rps-backend] {0}".format(message), file=sys.stderr, flush=True)


def emit(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def set_problem(message):
    global camera_problem
    with state_lock:
        camera_problem = message


def find_stream():
    return next((path for path in STREAM_PATHS if os.path.exists(path)), None)


def capture():
    global latest_frame, latest_update, camera_problem
    while not stop_event.is_set():
        stream_path = find_stream()
        if stream_path is None:
            set_problem("未找到机器狗摄像头共享流")
            stop_event.wait(1.0)
            continue

        log("使用摄像头共享流 {0}".format(stream_path))
        pipeline = (
            "shmsrc socket-path={0} is-live=true do-timestamp=true ! "
            "image/jpeg,width=1920,height=1080,framerate=30/1 ! "
            "queue leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0 ! "
            "videorate drop-only=true ! image/jpeg,framerate=10/1 ! jpegdec ! "
            "videoconvert ! videoscale ! video/x-raw,format=BGR,width=640,height=360 ! "
            "appsink drop=true max-buffers=1 sync=false"
        ).format(stream_path)
        capture_device = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        try:
            if not capture_device.isOpened():
                set_problem("机器狗摄像头共享流无法打开")
                stop_event.wait(1.0)
                continue
            while not stop_event.is_set() and capture_device.isOpened():
                ok, image = capture_device.read()
                if not ok:
                    break
                ok, jpeg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 80])
                if ok:
                    with state_lock:
                        latest_frame = jpeg.tobytes()
                        latest_update = time.monotonic()
                        camera_problem = ""
        finally:
            capture_device.release()
        if not stop_event.is_set():
            set_problem("额头相机流已断开，正在重连")
            stop_event.wait(1.0)


class CameraServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def send_body(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/api/frame", "/api/status"):
            self.send_body(404, "application/json", b'{"error":"not found"}')
            return

        with state_lock:
            ready = latest_frame is not None and time.monotonic() - latest_update < 3
            data = latest_frame if ready else None
            message = camera_problem or "未收到新的视频帧"
            age_ms = round((time.monotonic() - latest_update) * 1000) if latest_update else None

        if path == "/api/status":
            body = json.dumps(
                {
                    "ready": ready,
                    "camera": "机器狗额头相机",
                    "source": "smartapp-backend",
                    "bridge_frame_age_ms": age_ms,
                    "error": "" if ready else message,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.send_body(200, "application/json; charset=utf-8", body)
        elif data is not None:
            self.send_body(200, "image/jpeg", data)
        else:
            self.send_body(503, "text/plain; charset=utf-8", message.encode("utf-8"))

    def log_message(self, format_string, *args):
        if self.path.split("?", 1)[0] == "/api/frame":
            return
        log(format_string % args)


def read_runtime_init():
    raw = sys.stdin.readline()
    if not raw:
        raise ValueError("runtime_init was not received")
    message = json.loads(raw)
    if type(message) is not dict or message.get("event") != "runtime_init":
        raise ValueError("first message must be runtime_init")
    return message


def terminate(_signum, _frame):
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    read_runtime_init()

    host = os.environ.get("SMARTAPP_DYNAMIC_HOST", "127.0.0.1")
    port = int(os.environ.get("SMARTAPP_DYNAMIC_PORT", "18081"))
    server = CameraServer((host, port), Handler)
    camera_thread = threading.Thread(target=capture, name="camera-capture", daemon=True)
    server_thread = threading.Thread(target=server.serve_forever, name="camera-http", daemon=True)
    camera_thread.start()
    server_thread.start()
    log("动态服务监听 http://{0}:{1}".format(host, port))
    emit({"event": "app_ready"})

    try:
        for raw in sys.stdin:
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                log("忽略无效 Runtime 消息")
                continue
            if message.get("event") == "app_stop":
                break
            if message.get("event") == "cloud_data":
                log("收到 cloud_data seq={0}".format(message.get("seq")))
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)
        camera_thread.join(timeout=2)
        log("backend 已停止")


if __name__ == "__main__":
    try:
        main()
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        log("启动失败：{0}".format(error))
        raise SystemExit(1)
