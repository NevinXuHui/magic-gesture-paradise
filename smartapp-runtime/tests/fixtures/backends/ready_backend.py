import json
import os
from pathlib import Path
import signal
import sys
import time


Path("leader.pid").write_text(str(os.getpid()))
if os.environ.get("FIXTURE_NO_READ") == "yes":
    time.sleep(60)
message = json.loads(sys.stdin.readline())
Path("init.json").write_text(json.dumps(message))
data = message["data"]
mode = data.get("mode", "ready")
if mode == "early_exit":
    sys.exit(7)
if mode == "timeout":
    time.sleep(60)
if mode == "kill":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
elif mode == "term":
    def terminate(signum, frame):
        Path("terminated").write_text("term")
        sys.exit(0)
    signal.signal(signal.SIGTERM, terminate)
print('{"event":"app_ready"}', flush=True)
if mode == "exit":
    time.sleep(0.1)
    sys.exit(9)
if mode == "report":
    print(json.dumps({"event": "app_data", "dataType": "report", "data": {
        "environment": dict(os.environ), "argv": sys.argv,
        "cwd": os.getcwd(), "init": message,
    }}), flush=True)
if mode == "stderr":
    sys.stderr.buffer.write(b"log\xff\n" + b"x" * 10000 + b"\nafter\n")
    sys.stderr.flush()
if mode == "closed_stdin":
    os.close(0)
    Path("stdin.closed").write_text("yes")
    time.sleep(60)
if mode == "flood":
    print('{"event":"app_data","dataType":"x","data":{}}', flush=True)
    sys.stdout.buffer.write(b"x" * 1000000 + b"\n")
    sys.stdout.flush()
for line in sys.stdin:
    command = json.loads(line)
    if command["event"] == "app_stop":
        Path("stop.json").write_text(json.dumps(command))
        if mode in ("term", "kill"):
            time.sleep(60)
        break
    print(json.dumps({"event": "app_data", "dataType": command.get("dataType", "echo"),
                      "data": command["data"]}), flush=True)
