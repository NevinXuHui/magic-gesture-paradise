import json
import signal
import sys
import time


def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def terminate(_signum, _frame):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, terminate)
initial = json.loads(sys.stdin.readline())
if initial.get("event") != "runtime_init":
    raise SystemExit(2)
data = initial.get("data", {})
mode = data.get("mode")
if mode == "timeout":
    while True:
        time.sleep(1)
delay = data.get("readyDelay", 0)
if delay:
    time.sleep(float(delay))
emit({"event": "app_ready"})
emit({"event": "app_data", "dataType": "init", "data": data})
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("event") == "app_stop":
        break
    if message.get("event") == "cloud_data":
        if message.get("data", {}).get("action") == "crash":
            raise SystemExit(23)
        emit({
            "event": "app_data",
            "dataType": message.get("dataType", "echo"),
            "data": {"seq": message["seq"], "payload": message["data"]},
        })
