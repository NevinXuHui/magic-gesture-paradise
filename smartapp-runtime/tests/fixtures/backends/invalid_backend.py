import json
import os
from pathlib import Path
import sys
import time


Path("leader.pid").write_text(str(os.getpid()))
init = json.loads(sys.stdin.readline())["data"]
if not init.get("before_ready"):
    print('{"event":"app_ready"}', flush=True)
for item in init["lines"]:
    if item == "oversized":
        sys.stdout.buffer.write(b"x" * 200000 + b"\n")
    elif item == "utf8":
        sys.stdout.buffer.write(b"\xff\n")
    else:
        sys.stdout.buffer.write(item.encode("utf-8") + b"\n")
    sys.stdout.flush()
if init.get("before_ready"):
    print('{"event":"app_ready"}', flush=True)
for line in sys.stdin:
    if json.loads(line)["event"] == "app_stop":
        break
