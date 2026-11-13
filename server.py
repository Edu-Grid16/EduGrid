"""
EduGrid — Central Orchestrator (v3.0.0)
Flask REST API that queues compute tasks, dispatches them to school edge nodes,
and tracks revenue / ESG impact for the two dashboards.

New in v3.0.0
  * Server-side login for both portals (/api/login, /api/session, /api/logout).
    Passwords live in Render environment variables, never in the HTML.
  * Revenue now follows the quote the startup saw: each task is credited at
    quoted_cost_inr / frames, so the startup console and school NOC always agree.
  * Real fault tolerance: a task a node took but never finished goes back into
    the queue after LEASE_SECONDS, and a task is never paid twice.
  * /api/outputs (rendered images for the gallery), /download/node-agent
    (serves client.py to schools), /enroll (pairing QR landing page), /api/jobs.

Files Render needs in the repo:
    server.py
    requirements.txt
    templates/startup.html
    templates/school.html
    client.py            (optional: served to schools by /download/node-agent)
    static/outputs/      (optional: rendered images shown in the startup gallery)

Render environment variables (Dashboard > your service > Environment):
    SECRET_KEY            any long random string
    EDUGRID_STARTUP_USER  startup console username
    EDUGRID_STARTUP_PASS  startup console password
    EDUGRID_SCHOOL_USER   school NOC username
    EDUGRID_SCHOOL_PASS   school NOC password

Run locally:   py server.py
Run on Render: gunicorn server:app --workers 1 --threads 8 --timeout 120
               (keep --workers 1: the queue lives in memory and must be shared.)
"""

import os
import re
import time
import hmac
import secrets
import datetime
import logging
import functools
import threading
from collections import deque

from flask import (Flask, jsonify, request, render_template, redirect, session,
                   send_from_directory, url_for, abort)
from markupsafe import escape

os.system("")  # enable ANSI colours in Windows cmd

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
PORT                    = int(os.environ.get("PORT", 5000))   # Render injects PORT
DEFAULT_PRICE_PER_FRAME = 1.70    # ₹, used only if a request arrives without a quote
                                  # (Full HD, standard complexity, Standard schedule)
PLATFORM_FEE            = 0.20    # EduGrid's cut; school keeps the remaining 80%
CARBON_PER_RUPEE        = 0.004   # kg CO2 offset per ₹ earned (matches the school NOC)
MAX_FRAMES              = 1000    # guard rail on a single deploy request
LEASE_SECONDS           = 300     # unfinished task goes back to the queue after this
OUTPUT_EXTS             = (".png", ".jpg", ".jpeg", ".webp")

START_TIME = time.time()

# ----------------------------------------------------------------------
# Console styling
# ----------------------------------------------------------------------
class C:
    CYAN = '\033[96m'; GREEN = '\033[92m'; YELLOW = '\033[93m'
    RED = '\033[91m'; MAGENTA = '\033[95m'; RESET = '\033[0m'; BOLD = '\033[1m'

logging.getLogger('werkzeug').disabled = True  # silence default request spam

def _t():
    return datetime.datetime.now().strftime("%H:%M:%S")

def log_sys(msg):     print(f"{C.CYAN}[{_t()}] [SYS-CORE]{C.RESET} {msg}")
def log_cloud(msg):   print(f"{C.MAGENTA}[{_t()}] [EDGE-ROUTER]{C.RESET} {msg}")
def log_success(msg): print(f"{C.GREEN}[{_t()}] [FINANCE]{C.RESET} {msg}")
def log_warn(msg):    print(f"{C.YELLOW}[{_t()}] [WARN]{C.RESET} {msg}")

# ----------------------------------------------------------------------
# App + sessions
# ----------------------------------------------------------------------
app = Flask(__name__)

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    # Keeps the demo alive, but everyone is signed out whenever the server restarts.
    _secret = secrets.token_hex(32)
    log_warn("SECRET_KEY is not set. Using a temporary key; set it on Render.")
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),  # HTTPS on Render, plain HTTP locally
)

ROLES = {
    "startup": ("EDUGRID_STARTUP_USER", "EDUGRID_STARTUP_PASS"),
    "school":  ("EDUGRID_SCHOOL_USER",  "EDUGRID_SCHOOL_PASS"),
}

def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

def login_required(role):
    """Use on routes that only a signed-in portal user may call."""
    def wrap(view):
        @functools.wraps(view)
        def inner(*args, **kwargs):
            if request.method == 'OPTIONS':
                return jsonify({"status": "ok"}), 200
            if session.get("role") != role:
                return jsonify({"status": "error", "message": "not signed in"}), 401
            return view(*args, **kwargs)
        return inner
    return wrap

# ----------------------------------------------------------------------
# Shared state (guarded by a lock — nodes POST concurrently)
# ----------------------------------------------------------------------
_lock = threading.Lock()

tasks           = deque()   # pending tasks: {"task_id", "price", "job"}
in_flight       = {}        # task_id -> {"task", "node", "since"}
jobs            = []        # recent deploys, newest last (for /api/jobs)
task_counter    = 0
job_counter     = 0
total_deployed  = 0
total_completed = 0
total_revenue   = 0.0


def _snapshot():
    """Return a consistent view of the current stats."""
    with _lock:
        revenue = round(total_revenue, 2)
        fee = round(revenue * PLATFORM_FEE, 2)
        return {
            "revenue":   revenue,
            "deployed":  total_deployed,
            "completed": total_completed,
            "pending":   len(tasks),
            "in_flight": len(in_flight),
            "platform_fee":  fee,
            "net_to_school": round(revenue - fee, 2),
            "carbon_kg": round(revenue * CARBON_PER_RUPEE, 2),
        }


def _requeue_expired_locked():
    """Put tasks back in the queue if their node stopped reporting. Caller holds _lock."""
    now = time.time()
    expired = [tid for tid, f in in_flight.items() if now - f["since"] > LEASE_SECONDS]
    for tid in expired:
        f = in_flight.pop(tid)
        tasks.appendleft(f["task"])
        log_warn(f"{tid} timed out on {f['node']}; re-queued for another node.")


# ----------------------------------------------------------------------
# CORS (allow the dashboards + a judge's phone to connect)
# ----------------------------------------------------------------------
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type,Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,OPTIONS'
    return response


# ----------------------------------------------------------------------
# Pages
# ----------------------------------------------------------------------
@app.route('/')
def root():
    return redirect('/startup', code=302)

@app.route('/school')
def school_dashboard():
    return render_template('school.html')

@app.route('/startup')
def startup_dashboard():
    return render_template('startup.html')


# ----------------------------------------------------------------------
# Login (used by both dashboards)
# ----------------------------------------------------------------------
@app.route('/api/login', methods=['POST', 'OPTIONS'])
def api_login():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if role not in ROLES:
        return jsonify({"error": "unknown portal"}), 400

    user_var, pass_var = ROLES[role]
    expected_user, expected_pass = os.environ.get(user_var), os.environ.get(pass_var)
    if not expected_user or not expected_pass:
        log_warn(f"Login attempt on '{role}' but {user_var}/{pass_var} are not set.")
        return jsonify({"error": "login not configured on the server"}), 503

    # Evaluate both so timing doesn't reveal which one was wrong
    user_ok = _same(str(data.get("username", "")), expected_user)
    pass_ok = _same(str(data.get("password", "")), expected_pass)
    if not (user_ok and pass_ok):
        return jsonify({"error": "invalid credentials"}), 401

    session.clear()
    session["role"] = role
    session.permanent = True
    log_sys(f"{role.title()} portal signed in.")
    return jsonify({"authenticated": True, "role": role})

@app.route('/api/session')
def api_session():
    role = request.args.get("role")
    return jsonify({"authenticated": bool(role) and session.get("role") == role})

@app.route('/api/logout', methods=['POST', 'OPTIONS'])
def api_logout():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
    session.clear()
    return jsonify({"authenticated": False})


# ----------------------------------------------------------------------
# Read APIs (polled by the dashboards)
# ----------------------------------------------------------------------
@app.route('/api/data')
def api_data():
    return jsonify({"revenue": _snapshot()["revenue"]})

@app.route('/api/startup_status')
def api_startup_status():
    s = _snapshot()
    return jsonify({"deployed": s["deployed"], "completed": s["completed"]})

@app.route('/api/stats')
def api_stats():
    return jsonify(_snapshot())

@app.route('/api/jobs')
def api_jobs():
    with _lock:
        return jsonify({"jobs": list(reversed(jobs[-20:]))})

@app.route('/api/health')
def api_health():
    return jsonify({"status": "ok", "uptime_s": int(time.time() - START_TIME)})

@app.route('/api/outputs')
def api_outputs():
    """Rendered images in static/outputs/ for the startup console gallery."""
    folder = os.path.join(app.static_folder or "static", "outputs")
    if not os.path.isdir(folder):
        return jsonify({"outputs": []})
    files = sorted(f for f in os.listdir(folder) if f.lower().endswith(OUTPUT_EXTS))
    return jsonify({"outputs": [url_for("static", filename=f"outputs/{f}") for f in files]})


# ----------------------------------------------------------------------
# Job lifecycle
# ----------------------------------------------------------------------
@app.route('/deploy_workload', methods=['POST', 'OPTIONS'])
@login_required('startup')
def deploy_workload():
    global task_counter, job_counter, total_deployed
    data = request.get_json(silent=True) or {}
    try:
        num_frames = int(data.get('frames', 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "frames must be a number"}), 400
    if num_frames <= 0:
        return jsonify({"status": "error", "message": "frames must be > 0"}), 400
    num_frames = min(num_frames, MAX_FRAMES)

    # Price each task from the quote shown in the console
    try:
        quoted = float(data.get('quoted_cost_inr') or 0)
    except (TypeError, ValueError):
        quoted = 0.0
    if quoted <= 0:
        quoted = round(num_frames * DEFAULT_PRICE_PER_FRAME, 2)
    price_per_task = quoted / num_frames

    with _lock:
        job_counter += 1
        job_id = f"JOB_{str(job_counter).zfill(3)}"
        jobs.append({
            "job_id": job_id, "frames": num_frames, "quoted_cost_inr": round(quoted, 2),
            "workload": data.get('workload'), "quality": data.get('quality'),
            "complexity": data.get('complexity'), "tier": data.get('tier'),
            "rate_per_gpu_hour": data.get('rate_per_gpu_hour'),
            "created": datetime.datetime.now().isoformat(timespec="seconds"),
        })
        del jobs[:-50]   # keep the last 50
        total_deployed += num_frames
        for _ in range(num_frames):
            task_counter += 1
            tasks.append({"task_id": f"FRAME_{str(task_counter).zfill(3)}",
                          "price": price_per_task, "job": job_id})
        pending = len(tasks)

    print("")
    log_cloud(f"{job_id}: {num_frames} tasks ({data.get('workload') or 'render'}, "
              f"{data.get('quality') or 'n/a'}, {data.get('tier') or 'standard'}), quote ₹{quoted:,.2f}.")
    log_cloud(f"Queue expanded → {pending} tasks pending dispatch.")
    return jsonify({"status": "success", "job_id": job_id, "queued": num_frames, "pending": pending})


@app.route('/get_task', methods=['GET'])
def get_task():
    node = request.args.get('node', 'unknown-node')
    with _lock:
        _requeue_expired_locked()
        if not tasks:
            return jsonify({"task_id": None})
        task = tasks.popleft()
        in_flight[task["task_id"]] = {"task": task, "node": node, "since": time.time()}
    # client.py only needs task_id; extra keys are ignored by older clients
    return jsonify({"task_id": task["task_id"], "job": task["job"]})


@app.route('/submit_result', methods=['POST', 'OPTIONS'])
def submit_result():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200

    global total_revenue, total_completed
    data = request.get_json(silent=True) or {}
    task_id = data.get('task_id', 'UNKNOWN')
    node = data.get('node', 'edge node')

    with _lock:
        flight = in_flight.pop(task_id, None)
        if flight is None:
            # Not handed out, or already paid (e.g. finished late after a re-queue)
            log_warn(f"Ignored result for {task_id} from {node}: not in flight.")
            return jsonify({"status": "ignored"})
        # If it had been re-queued, drop the duplicate so no one renders it twice
        for i, tk in enumerate(tasks):
            if tk["task_id"] == task_id:
                del tasks[i]
                break
        price = flight["task"]["price"]
        total_revenue += price
        total_completed += 1
        rev = total_revenue

    log_success(f"Verified {C.BOLD}{task_id}{C.RESET}{C.GREEN} rendered by {node}.")
    log_success(f"Credited {C.BOLD}+₹{price:,.2f}{C.RESET}{C.GREEN} "
                f"(school share ₹{price * (1 - PLATFORM_FEE):,.2f}; gross yield ₹{rev:,.2f}).")
    return jsonify({"status": "success"})


@app.route('/reset', methods=['POST', 'OPTIONS'])
@login_required('school')
def reset():
    """Clear all demo state — handy between pitch runs. School admin only."""
    global task_counter, job_counter, total_deployed, total_completed, total_revenue
    with _lock:
        tasks.clear(); in_flight.clear(); jobs.clear()
        task_counter = job_counter = total_deployed = total_completed = 0
        total_revenue = 0.0
    log_warn("Demo state reset. Queue and ledger cleared.")
    return jsonify({"status": "reset"})


# ----------------------------------------------------------------------
# School onboarding
# ----------------------------------------------------------------------
@app.route('/download/node-agent')
def download_node_agent():
    root = app.root_path
    if not os.path.isfile(os.path.join(root, "client.py")):
        abort(404, description="client.py is not in the repository next to server.py.")
    return send_from_directory(root, "client.py", as_attachment=True,
                               download_name="edugrid-node-agent.py")

@app.route('/enroll')
def enroll():
    school = request.args.get("school", "")
    code = request.args.get("code", "")
    if not re.fullmatch(r"[a-z0-9-]{1,60}", school) or not re.fullmatch(r"[A-Z0-9-]{4,12}", code):
        abort(400, description="This pairing link is not valid.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>EduGrid pairing</title></head>
<body style="font-family:system-ui,sans-serif;background:#05080a;color:#e6efec;display:grid;place-items:center;min-height:100vh;margin:0">
<main style="max-width:420px;padding:28px;text-align:center">
<h1 style="font-size:22px;margin:0 0 8px">Pairing code {escape(code)}</h1>
<p style="color:#8a9a96">Type this code into the EduGrid node agent on the lab PC to link it to
<b>{escape(school)}</b>. The PC will appear in the school dashboard within a minute.</p>
</main></body></html>"""


# ----------------------------------------------------------------------
# Boot
# ----------------------------------------------------------------------
print(f"""{C.CYAN}{C.BOLD}
    ______    __      ______     _     __
   / ____/___/ /_  __/ ____/____(_)___/ /
  / __/ / __  / / / / / __/ ___/ / __  /
 / /___/ /_/ / /_/ / /_/ / /  / / /_/ /
/_____/\\__,_/\\__,_/\\____/_/  /_/\\__,_/

=== ENTERPRISE ORCHESTRATOR v3.0.0 (PROD) ==={C.RESET}
""")
log_sys("Initializing EduGrid Central Command…")
log_sys(f"REST API online · quote-based pricing · fee {int(PLATFORM_FEE*100)}% · lease {LEASE_SECONDS}s")
for _role, (_u, _p) in ROLES.items():
    if not (os.environ.get(_u) and os.environ.get(_p)):
        log_warn(f"{_u} / {_p} not set: the {_role} portal cannot sign in yet.")
log_sys("Waiting for school nodes and startup deployments…\n")

if __name__ == '__main__':
    # 0.0.0.0 lets a phone on the same Wi-Fi reach it; threaded for concurrent nodes.
    app.run(host='0.0.0.0', port=PORT, threaded=True)
