import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


Path("leader.pid").write_text(str(os.getpid()))
mode = json.loads(sys.stdin.readline())["data"].get("mode", "term")
# Auto-reap the child even when the leader must later be killed.
signal.signal(signal.SIGCHLD, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import signal,time; "
    "from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_DFL); "
    "Path('child.ready').write_text('yes'); time.sleep(60)"])
Path("child.pid").write_text(str(child.pid))
while not Path("child.ready").exists():
    time.sleep(0.005)
print('{"event":"app_ready"}', flush=True)
if mode == "leader_exit":
    sys.exit(0)
for line in sys.stdin:
    if json.loads(line)["event"] == "app_stop":
        time.sleep(60)
