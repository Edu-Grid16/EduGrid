"""
EduGrid — Edge Node Agent (v4.0.0)
Runs on a school lab PC. Asks the EduGrid orchestrator for work, processes it in the
background, and steps aside the moment a student touches the mouse or keyboard.

Setup (once per PC):
    pip install requests pynput

Run:
    python client.py
    python client.py --node Lab-PC-07                      # give each PC its own name
    python client.py --server http://localhost:5000        # test against a local server.py

Stop: Ctrl+C
"""

import os
import sys
import time
import random
import socket
import argparse
import datetime
import threading

try:
    import requests
except ImportError:
    print("Missing library. Run:  pip install requests pynput")
    sys.exit(1)

os.system("")  # enable ANSI colours in Windows cmd

# ----------------------------------------------------------------------
# Config (command-line flags or environment variables override these)
# ----------------------------------------------------------------------
DEFAULT_SERVER = "https://edugrid.onrender.com"
DEFAULT_NODE   = f"Lab-PC-{socket.gethostname()[-4:].upper()}"

POLL_SECONDS        = 2      # how often to ask for work when the queue is empty
FRAME_SECONDS       = 8      # simulated processing time per frame (keep short for demos)
IDLE_RESUME_SECONDS = 5      # resume this long after the student stops using the PC
MAX_BACKOFF_SECONDS = 15     # longest wait between retries when the server is unreachable
TIMEOUT             = (10, 70)   # (connect, read); read is long because Render wakes slowly

# ----------------------------------------------------------------------
# Console styling
# ----------------------------------------------------------------------
class C:
    CYAN = '\033[96m'; GREEN = '\033[92m'; YELLOW = '\033[93m'
    RED = '\033[91m'; MAGENTA = '\033[95m'; DIM = '\033[2m'; RESET = '\033[0m'; BOLD = '\033[1m'

def _t():
    return datetime.datetime.now().strftime("%H:%M:%S")

def log(tag, colour, msg):
    print(f"{colour}[{_t()}] [{tag}]{C.RESET} {msg}", flush=True)

def log_sys(msg):   log("NODE", C.CYAN, msg)
def log_net(msg):   log("UPLINK", C.MAGENTA, msg)
def log_ok(msg):    log("DONE", C.GREEN, msg)
def log_warn(msg):  log("WARN", C.YELLOW, msg)
def log_user(msg):  log("STUDENT", C.YELLOW, msg)

# ----------------------------------------------------------------------
# Stealth interrupt: any mouse or keyboard activity pauses work
# ----------------------------------------------------------------------
_last_activity = 0.0
_activity_lock = threading.Lock()

def _touch(*_args, **_kwargs):
    global _last_activity
    with _activity_lock:
        _last_activity = time.time()

def start_activity_watch():
    """Returns True if interrupt detection is active."""
    try:
        from pynput import mouse, keyboard
        mouse.Listener(on_move=_touch, on_click=_touch, on_scroll=_touch, daemon=True).start()
        keyboard.Listener(on_press=_touch, daemon=True).start()
        return True
    except Exception as e:  # pynput missing, or no desktop session
        log_warn(f"Student-interrupt detection is off ({e.__class__.__name__}). "
                 "Install it with: pip install pynput")
        return False

def student_active():
    with _activity_lock:
        return time.time() - _last_activity < IDLE_RESUME_SECONDS

def wait_for_student_to_leave(task_id, link=None):
    log_user(f"Someone is using this PC. Pausing {task_id} and handing the PC back.")
    if link:
        link.report("interrupt", task_id)
    while student_active():
        time.sleep(0.25)
    log_user(f"PC idle for {IDLE_RESUME_SECONDS}s. Resuming {task_id}.")
    if link:
        link.report("resume", task_id)

# ----------------------------------------------------------------------
# Talking to the orchestrator
# ----------------------------------------------------------------------
class Uplink:
    def __init__(self, server, node):
        self.server = server.rstrip("/")
        self.node = node
        self.http = requests.Session()
        self.backoff = 2

    def _call(self, method, path, **kw):
        """Retries until the server answers (Render free instances sleep when idle)."""
        while True:
            try:
                r = self.http.request(method, self.server + path, timeout=TIMEOUT, **kw)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}")
                self.backoff = 2
                return r.json()
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError, ValueError) as e:
                log_net(f"{C.DIM}No answer from orchestrator ({e.__class__.__name__}). "
                        f"It may be waking up; retrying in {self.backoff}s…{C.RESET}")
                time.sleep(self.backoff)
                self.backoff = min(self.backoff * 2, MAX_BACKOFF_SECONDS)

    def get_task(self):
        return self._call("GET", "/get_task", params={"node": self.node})

    def submit(self, task_id):
        return self._call("POST", "/submit_result", json={"task_id": task_id, "node": self.node})

    def report(self, kind, task_id=None):
        """Record a student interruption on the server. Never blocks or retries:
        the student comes first, the record is a bonus."""
        def send():
            try:
                self.http.post(self.server + "/node/event", timeout=5,
                               json={"type": kind, "node": self.node, "task_id": task_id})
            except Exception:
                pass
        threading.Thread(target=send, daemon=True).start()

# ----------------------------------------------------------------------
# Work
# ----------------------------------------------------------------------
def process(task_id, job, link=None):
    """Simulated render with live progress. Pauses whenever a student is active."""
    steps = 20
    log_sys(f"Picked up {C.BOLD}{task_id}{C.RESET} ({job or 'job'}). Rendering in the background…")
    for i in range(1, steps + 1):
        if student_active():
            wait_for_student_to_leave(task_id, link)
        time.sleep(FRAME_SECONDS / steps * random.uniform(0.8, 1.2))
        bar = "█" * i + "░" * (steps - i)
        print(f"\r    {C.CYAN}{bar}{C.RESET} {i * 100 // steps:3d}%", end="", flush=True)
    print()

def main():
    ap = argparse.ArgumentParser(description="EduGrid edge node agent")
    ap.add_argument("--server", default=os.environ.get("EDUGRID_SERVER", DEFAULT_SERVER))
    ap.add_argument("--node", default=os.environ.get("EDUGRID_NODE", DEFAULT_NODE))
    args = ap.parse_args()

    print(f"""{C.CYAN}{C.BOLD}
    ______    __      ______     _     __
   / ____/___/ /_  __/ ____/____(_)___/ /
  / __/ / __  / / / / / __/ ___/ / __  /
 / /___/ /_/ / /_/ / /_/ / /  / / /_/ /
/_____/\\__,_/\\__,_/\\____/_/  /_/\\__,_/

=== EDGE NODE AGENT v4.0.0 ==={C.RESET}
""")
    log_sys(f"Node name: {C.BOLD}{args.node}{C.RESET}")
    log_sys(f"Orchestrator: {args.server}")
    watching = start_activity_watch()
    if watching:
        log_sys("Student-first mode on: any mouse or keyboard use pauses work instantly.")

    link = Uplink(args.server, args.node)
    log_net("Polling for workloads… (Ctrl+C to stop)")
    idle_notice = 0.0

    while True:
        if student_active():
            time.sleep(0.5)
            continue

        task = link.get_task()
        task_id = task.get("task_id")
        if not task_id:
            if time.time() - idle_notice > 30:
                log_net(f"{C.DIM}Queue empty. Waiting for a startup to deploy work…{C.RESET}")
                idle_notice = time.time()
            time.sleep(POLL_SECONDS)
            continue

        process(task_id, task.get("job"), link)
        result = link.submit(task_id)
        if result.get("status") == "success":
            log_ok(f"{task_id} delivered to the orchestrator. Earnings credited to the school.")
        else:
            log_warn(f"{task_id} was already finished by another node, so this copy was not counted.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        log_sys("Node stopped. This PC is fully back to normal use.")
