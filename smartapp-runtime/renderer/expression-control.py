"""Keep ROS discovery warm without changing expression state until requested."""
import fcntl
import json
import math
import os
import signal
import socket
import sys
import threading
import time


SOCKET_PATH = '/tmp/smartapp-expression-control.sock'


class UnavailableError(OSError):
    pass


def request(path=SOCKET_PATH, action='disable', timeout=8):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(timeout + 0.5)
        try:
            connection.connect(path)
        except OSError as error:
            raise UnavailableError(str(error)) from error
        command = {'action': action, 'deadline': time.monotonic() + timeout}
        connection.sendall((json.dumps(command) + '\n').encode())
        with connection.makefile('rb') as source:
            result = json.loads(source.readline(4096))
        if not result.get('success'):
            raise RuntimeError(result.get('error', 'expression config failed'))


def serve(configure, path, stop):
    # The lock prevents a second daemon from unlinking an active socket.
    with open(path + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.path.exists(path):
            os.unlink(path)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(4)
            server.settimeout(0.2)
            try:
                while not stop.is_set():
                    try:
                        connection, _ = server.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(1)
                        started = time.monotonic()
                        try:
                            with connection.makefile('rb') as source:
                                command = json.loads(source.readline(512))
                            action = command.get('action')
                            deadline = command.get('deadline')
                            if action not in ('disable', 'restore'):
                                raise ValueError('unsupported expression command')
                            if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
                                raise ValueError('invalid expression deadline')
                            if deadline <= time.monotonic():
                                raise RuntimeError('expression request expired')
                            configure(action, deadline)
                            result = {'success': True}
                        except Exception as error:
                            result = {'success': False, 'error': str(error)}
                        print('[expression-control] command: {} ms, success={}'.format(
                            round((time.monotonic() - started) * 1000), result['success']),
                            flush=True)
                        try:
                            connection.sendall((json.dumps(result) + '\n').encode())
                        except OSError:
                            pass
            finally:
                os.unlink(path)


class RosController:
    def __init__(self):
        import rclpy
        from homi_speech_interface.srv import ExpressionConfig
        self.ros = rclpy
        self.service = ExpressionConfig
        rclpy.init(args=[])
        self.node = rclpy.create_node('smartapp_expression_control')
        self.client = self.node.create_client(ExpressionConfig, '/expression/config')
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.spin, daemon=True)
        self.thread.start()

    def spin(self):
        while not self.stop.is_set() and self.ros.ok():
            self.ros.spin_once(self.node, timeout_sec=0.1)

    def configure(self, action, deadline):
        if not self.client.wait_for_service(timeout_sec=min(5, max(0, deadline - time.monotonic()))):
            raise RuntimeError('expression service unavailable')
        if deadline <= time.monotonic():
            raise RuntimeError('expression request expired')
        request = self.service.Request()
        request.action = 'set'
        request.expression_enabled = 'false' if action == 'disable' else 'true'
        request.status_publish_enabled = 'true'
        if action == 'restore':
            request.default_video = '/usr/bin/cmcc_robot/install/expression/share/expression/resource/video/default/default.mp4'
            request.default_image = ''
        future = self.client.call_async(request)
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        if not done.wait(min(5, max(0, deadline - time.monotonic()))):
            future.cancel()
            raise RuntimeError('expression config timed out')
        response = future.result()
        if response is None or not response.success:
            raise RuntimeError('expression config failed')

    def close(self):
        self.stop.set()
        self.thread.join()
        self.node.destroy_node()
        self.ros.shutdown()


def main():
    mode = sys.argv[1]
    if mode == 'request':
        action = sys.argv[2] if len(sys.argv) > 2 else 'disable'
        try:
            # Restore also waits for any earlier in-flight disable to complete.
            request(action=action, timeout=16 if action == 'restore' else 8)
        except UnavailableError:
            return 2
        return 0
    controller = RosController()
    try:
        if mode == 'serve':
            stop = threading.Event()
            for signum in (signal.SIGINT, signal.SIGTERM):
                signal.signal(signum, lambda *_: stop.set())
            serve(controller.configure, SOCKET_PATH, stop)
        elif mode == 'disable':
            controller.configure('disable', time.monotonic() + 7)
        else:
            raise ValueError('unsupported mode')
    finally:
        controller.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
