"""
EduGrid — Central Orchestrator (v4.0.0)
Flask REST API that queues compute tasks, dispatches them to school edge nodes,
and RECORDS everything in a database: jobs, every frame, every lab PC, the money
split, and an event log (logins, deploys, student interruptions, re-queues).

New in v4.0.0 — the records backend
  * Data survives restarts. Render's free server sleeps and restarts often; v3 lost
    everything each time. v4 writes to a real database.
  * DATABASE_URL set (Neon or Render Postgres)  -> permanent Postgres records.
    DATABASE_URL not set                        -> local SQLite file (fine on your PC;
                                                   on Render it is wiped on restart).
    Postgres unreachable at boot                -> falls back to SQLite, so the demo
                                                   never goes down.
  * "Reset demo data" no longer deletes anything: it starts a new demo run. The
    dashboards count the current run; all history stays in the records.
  * Records API + CSV export for the new Records / Job History tabs.

Render environment variables:
    SECRET_KEY, EDUGRID_STARTUP_USER, EDUGRID_STARTUP_PASS,
    EDUGRID_SCHOOL_USER, EDUGRID_SCHOOL_PASS
    DATABASE_URL   (Postgres connection string from Neon or Render Postgres)

Run locally:   py server.py
Run on Render: gunicorn server:app --workers 1 --threads 8 --timeout 120
               (keep --workers 1: one process owns the dispatch lock.)
"""

import os
import io
import re
import csv
import time
import hmac
import sqlite3
import secrets
import datetime
import logging
import functools
import threading

from flask import (Flask, jsonify, request, render_template, redirect, session,
                   send_from_directory, url_for, abort, Response)
from markupsafe import escape

os.system("")  # enable ANSI colours in Windows cmd

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
PORT                    = int(os.environ.get("PORT", 5000))
DEFAULT_PRICE_PER_FRAME = 1.70    # ₹, only if a request arrives without a quote
SCHOOL_SHARE            = 0.80    # school keeps 80% of gross
CARE_FUND_SHARE         = 0.05    # hardware care fund, paid out of EduGrid's 20%
CARBON_PER_RUPEE        = 0.004   # kg CO2 offset per ₹ earned (matches the school NOC)
MAX_FRAMES              = 1000
LEASE_SECONDS           = 300     # unfinished task goes back to the queue after this
OUTPUT_EXTS             = (".png", ".jpg", ".jpeg", ".webp")
SQLITE_PATH             = os.environ.get("EDUGRID_SQLITE_PATH", "edugrid.db")

START_TIME = time.time()

# ----------------------------------------------------------------------
# Console styling
# ----------------------------------------------------------------------
class C:
    CYAN = '\033[96m'; GREEN = '\033[92m'; YELLOW = '\033[93m'
    RED = '\033[91m'; MAGENTA = '\033[95m'; RESET = '\033[0m'; BOLD = '\033[1m'

logging.getLogger('werkzeug').disabled = True

def _t():
    return datetime.datetime.now().strftime("%H:%M:%S")

def log_sys(msg):     print(f"{C.CYAN}[{_t()}] [SYS-CORE]{C.RESET} {msg}", flush=True)
def log_cloud(msg):   print(f"{C.MAGENTA}[{_t()}] [EDGE-ROUTER]{C.RESET} {msg}", flush=True)
def log_success(msg): print(f"{C.GREEN}[{_t()}] [FINANCE]{C.RESET} {msg}", flush=True)
def log_warn(msg):    print(f"{C.YELLOW}[{_t()}] [WARN]{C.RESET} {msg}", flush=True)
def log_db(msg):      print(f"{C.CYAN}[{_t()}] [RECORDS]{C.RESET} {msg}", flush=True)

def iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else None

# ----------------------------------------------------------------------
# Database layer: one small wrapper for Postgres and SQLite
# All SQL below is written with '?' placeholders and works on both.
# Every write happens under _lock (single worker), so no row-locking tricks needed.
# ----------------------------------------------------------------------
_lock = threading.RLock()

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runs (
        id         INTEGER PRIMARY KEY,
        started_at DOUBLE PRECISION NOT NULL,
        note       TEXT)""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id             TEXT PRIMARY KEY,
        run_id         INTEGER NOT NULL,
        created_at     DOUBLE PRECISION NOT NULL,
        completed_at   DOUBLE PRECISION,
        status         TEXT NOT NULL,
        workload       TEXT,
        quality        TEXT,
        complexity     TEXT,
        tier           TEXT,
        frames         INTEGER NOT NULL,
        rate           DOUBLE PRECISION,
        quoted_cost    DOUBLE PRECISION NOT NULL,
        price_per_task DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS tasks (
        id           TEXT PRIMARY KEY,
        job_id       TEXT NOT NULL,
        run_id       INTEGER NOT NULL,
        seq          INTEGER NOT NULL,
        price        DOUBLE PRECISION NOT NULL,
        status       TEXT NOT NULL,
        node         TEXT,
        attempts     INTEGER NOT NULL DEFAULT 0,
        assigned_at  DOUBLE PRECISION,
        completed_at DOUBLE PRECISION)""",
    "CREATE INDEX IF NOT EXISTS tasks_status ON tasks (status, seq)",
    "CREATE INDEX IF NOT EXISTS tasks_run ON tasks (run_id, status)",
    """CREATE TABLE IF NOT EXISTS nodes (
        node_id      TEXT PRIMARY KEY,
        first_seen   DOUBLE PRECISION NOT NULL,
        last_seen    DOUBLE PRECISION NOT NULL,
        tasks_done   INTEGER NOT NULL DEFAULT 0,
        earned_gross DOUBLE PRECISION NOT NULL DEFAULT 0,
        interrupts   INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS events (
        id      TEXT PRIMARY KEY,
        at      DOUBLE PRECISION NOT NULL,
        run_id  INTEGER,
        type    TEXT NOT NULL,
        node    TEXT,
        job_id  TEXT,
        task_id TEXT,
        detail  TEXT)""",
    "CREATE INDEX IF NOT EXISTS events_at ON events (at)",
]


class DB:
    def __init__(self):
        self.kind = None
        self.conn = None
        self.url = os.environ.get("DATABASE_URL", "").strip()
        self.connect()

    # -- connection ------------------------------------------------------
    def connect(self):
        if self.url:
            try:
                import psycopg
                from psycopg.rows import dict_row
                self.conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row,
                                            connect_timeout=15)
                self.kind = "postgres"
                return
            except Exception as e:
                log_warn(f"Could not reach DATABASE_URL ({e.__class__.__name__}: {str(e)[:120]}).")
                log_warn("Falling back to a local SQLite file so the demo keeps running.")
        self.conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.kind = "sqlite"

    def _sql(self, q):
        return q.replace("?", "%s") if self.kind == "postgres" else q

    def _run(self, q, params=()):
        """Execute with one reconnect attempt (Neon suspends idle connections)."""
        for attempt in (1, 2):
            try:
                cur = self.conn.cursor()
                cur.execute(self._sql(q), params)
                return cur
            except Exception as e:
                if self.kind == "postgres" and attempt == 1 and self._is_connection_error(e):
                    log_warn("Database connection dropped; reconnecting…")
                    self.connect()
                    continue
                raise

    @staticmethod
    def _is_connection_error(e):
        try:
            import psycopg
            return isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError))
        except Exception:
            return False

    # -- helpers ---------------------------------------------------------
    def execute(self, q, params=()):
        with _lock:
            self._run(q, params)

    def all(self, q, params=()):
        with _lock:
            rows = self._run(q, params).fetchall()
        return [dict(r) for r in rows]

    def one(self, q, params=()):
        rows = self.all(q, params)
        return rows[0] if rows else None

    def value(self, q, params=(), default=0):
        row = self.one(q, params)
        if not row:
            return default
        v = list(row.values())[0]
        return default if v is None else v

    def init(self):
        with _lock:
            for stmt in SCHEMA:
                self._run(stmt)
            if not self.one("SELECT value FROM meta WHERE key = ?", ("current_run",)):
                self._run("INSERT INTO runs (id, started_at, note) VALUES (?, ?, ?)", (1, time.time(), "first run"))
                for k, v in (("current_run", "1"), ("job_counter", "0")):
                    self._run("INSERT INTO meta (key, value) VALUES (?, ?)", (k, v))

    # meta counters
    def meta(self, key, default="0"):
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, str(value)))


db = DB()
db.init()


def current_run():
    return int(db.meta("current_run", "1"))


def record_event(type_, node=None, job_id=None, task_id=None, detail=None, run_id=None):
    db.execute("INSERT INTO events (id, at, run_id, type, node, job_id, task_id, detail) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (secrets.token_hex(8), time.time(), run_id or current_run(), type_,
                node, job_id, task_id, (detail or "")[:300]))


def touch_node(node, done=False, earned=0.0, interrupt=False):
    now = time.time()
    db.execute(
        "INSERT INTO nodes (node_id, first_seen, last_seen, tasks_done, earned_gross, interrupts) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (node_id) DO UPDATE SET last_seen = excluded.last_seen, "
        "tasks_done = nodes.tasks_done + excluded.tasks_done, "
        "earned_gross = nodes.earned_gross + excluded.earned_gross, "
        "interrupts = nodes.interrupts + excluded.interrupts",
        (node, now, now, 1 if done else 0, earned, 1 if interrupt else 0))


def split(gross):
    """How every rupee is divided."""
    gross = float(gross or 0)
    school = gross * SCHOOL_SHARE
    care = gross * CARE_FUND_SHARE
    return {"gross": round(gross, 2), "school": round(school, 2), "care_fund": round(care, 2),
            "edugrid": round(gross - school - care, 2), "carbon_kg": round(gross * CARBON_PER_RUPEE, 2)}

# ----------------------------------------------------------------------
# App + sessions
# ----------------------------------------------------------------------
app = Flask(__name__)

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    log_warn("SECRET_KEY is not set. Using a temporary key; set it on Render.")
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
)

ROLES = {
    "startup": ("EDUGRID_STARTUP_USER", "EDUGRID_STARTUP_PASS"),
    "school":  ("EDUGRID_SCHOOL_USER",  "EDUGRID_SCHOOL_PASS"),
}

def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

def login_required(*roles):
    """Allow a route only for signed-in users of the given portal(s)."""
    def wrap(view):
        @functools.wraps(view)
        def inner(*args, **kwargs):
            if request.method == 'OPTIONS':
                return jsonify({"status": "ok"}), 200
            if session.get("role") not in roles:
                return jsonify({"status": "error", "message": "not signed in"}), 401
            return view(*args, **kwargs)
        return inner
    return wrap

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
# Login
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

    user_ok = _same(str(data.get("username", "")), expected_user)
    pass_ok = _same(str(data.get("password", "")), expected_pass)
    if not (user_ok and pass_ok):
        record_event("login_failed", detail=f"{role} portal")   # never store what was typed
        return jsonify({"error": "invalid credentials"}), 401

    session.clear()
    session["role"] = role
    session.permanent = True
    record_event("login", detail=f"{role} portal")
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
# Live counters for the dashboards (current demo run only)
# ----------------------------------------------------------------------
def run_counters(run_id=None):
    run_id = run_id or current_run()
    row = db.one("SELECT COUNT(*) AS deployed, "
                 "SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS completed, "
                 "SUM(CASE WHEN status = 'done' THEN price ELSE 0 END) AS revenue, "
                 "SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) AS pending, "
                 "SUM(CASE WHEN status = 'in_flight' THEN 1 ELSE 0 END) AS in_flight "
                 "FROM tasks WHERE run_id = ?", (run_id,)) or {}
    return {k: (row.get(k) or 0) for k in ("deployed", "completed", "revenue", "pending", "in_flight")}

@app.route('/api/data')
def api_data():
    return jsonify({"revenue": round(float(run_counters()["revenue"]), 2)})

@app.route('/api/startup_status')
def api_startup_status():
    c = run_counters()
    return jsonify({"deployed": int(c["deployed"]), "completed": int(c["completed"])})

@app.route('/api/stats')
def api_stats():
    c = run_counters()
    s = split(c["revenue"])
    return jsonify({"run": current_run(), "revenue": s["gross"], "deployed": int(c["deployed"]),
                    "completed": int(c["completed"]), "pending": int(c["pending"]),
                    "in_flight": int(c["in_flight"]), "platform_fee": round(s["gross"] - s["school"], 2),
                    "net_to_school": s["school"], "carbon_kg": s["carbon_kg"], "storage": db.kind})

@app.route('/api/health')
def api_health():
    return jsonify({"status": "ok", "uptime_s": int(time.time() - START_TIME), "storage": db.kind})

@app.route('/api/outputs')
def api_outputs():
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
    data = request.get_json(silent=True) or {}
    try:
        num_frames = int(data.get('frames', 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "frames must be a number"}), 400
    if num_frames <= 0:
        return jsonify({"status": "error", "message": "frames must be > 0"}), 400
    num_frames = min(num_frames, MAX_FRAMES)

    try:
        quoted = float(data.get('quoted_cost_inr') or 0)
    except (TypeError, ValueError):
        quoted = 0.0
    if quoted <= 0:
        quoted = round(num_frames * DEFAULT_PRICE_PER_FRAME, 2)
    price = quoted / num_frames

    def _txt(k):
        v = data.get(k)
        return str(v)[:40] if v not in (None, "") else None

    try:
        rate = float(data.get('rate_per_gpu_hour')) if data.get('rate_per_gpu_hour') is not None else None
    except (TypeError, ValueError):
        rate = None

    with _lock:
        n = int(db.meta("job_counter", "0")) + 1
        db.set_meta("job_counter", n)
        job_id = f"JOB_{n:04d}"
        run_id = current_run()
        now = time.time()
        db.execute("INSERT INTO jobs (id, run_id, created_at, status, workload, quality, complexity, tier, "
                   "frames, rate, quoted_cost, price_per_task) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (job_id, run_id, now, "running", _txt('workload') or "render", _txt('quality'),
                    _txt('complexity'), _txt('tier') or "standard", num_frames, rate, round(quoted, 2), price))
        for i in range(1, num_frames + 1):
            db.execute("INSERT INTO tasks (id, job_id, run_id, seq, price, status) VALUES (?, ?, ?, ?, ?, ?)",
                       (f"{job_id}-F{i:03d}", job_id, run_id, n * 10000 + i, price, "queued"))
        record_event("deploy", job_id=job_id,
                     detail=f"{num_frames} tasks, {_txt('workload') or 'render'}, {_txt('quality') or 'n/a'}, "
                            f"{_txt('tier') or 'standard'}, quote ₹{quoted:,.2f}")
        pending = run_counters(run_id)["pending"]

    print("")
    log_cloud(f"{job_id}: {num_frames} tasks ({_txt('workload') or 'render'}, {_txt('quality') or 'n/a'}, "
              f"{_txt('tier') or 'standard'}), quote ₹{quoted:,.2f}. Recorded.")
    log_cloud(f"Queue → {pending} tasks pending dispatch.")
    return jsonify({"status": "success", "job_id": job_id, "queued": num_frames, "pending": int(pending)})


def _requeue_expired():
    cutoff = time.time() - LEASE_SECONDS
    stale = db.all("SELECT id, job_id, node FROM tasks WHERE status = 'in_flight' AND assigned_at < ?", (cutoff,))
    for t in stale:
        db.execute("UPDATE tasks SET status = 'queued', node = NULL, assigned_at = NULL "
                   "WHERE id = ? AND status = 'in_flight'", (t["id"],))
        record_event("requeue", node=t["node"], job_id=t["job_id"], task_id=t["id"],
                     detail=f"no result within {LEASE_SECONDS}s")
        log_warn(f"{t['id']} timed out on {t['node']}; re-queued for another node.")


@app.route('/get_task', methods=['GET'])
def get_task():
    node = (request.args.get('node') or 'unknown-node')[:60]
    with _lock:
        touch_node(node)
        _requeue_expired()
        task = db.one("SELECT id, job_id FROM tasks WHERE status = 'queued' ORDER BY seq LIMIT 1")
        if not task:
            return jsonify({"task_id": None})
        db.execute("UPDATE tasks SET status = 'in_flight', node = ?, assigned_at = ?, attempts = attempts + 1 "
                   "WHERE id = ?", (node, time.time(), task["id"]))
    return jsonify({"task_id": task["id"], "job": task["job_id"]})


@app.route('/submit_result', methods=['POST', 'OPTIONS'])
def submit_result():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    task_id = str(data.get('task_id', 'UNKNOWN'))[:60]
    node = str(data.get('node', 'edge node'))[:60]

    with _lock:
        task = db.one("SELECT id, job_id, price, status FROM tasks WHERE id = ?", (task_id,))
        if not task or task["status"] != "in_flight":
            log_warn(f"Ignored result for {task_id} from {node}: not in flight.")
            return jsonify({"status": "ignored"})
        now = time.time()
        db.execute("UPDATE tasks SET status = 'done', completed_at = ?, node = ? WHERE id = ?", (now, node, task_id))
        touch_node(node, done=True, earned=task["price"])
        record_event("task_done", node=node, job_id=task["job_id"], task_id=task_id,
                     detail=f"₹{task['price']:,.2f}")
        left = db.value("SELECT COUNT(*) AS n FROM tasks WHERE job_id = ? AND status <> 'done'", (task["job_id"],))
        if int(left) == 0:
            db.execute("UPDATE jobs SET status = 'done', completed_at = ? WHERE id = ?", (now, task["job_id"]))
            record_event("job_done", job_id=task["job_id"])
        rev = run_counters()["revenue"]

    log_success(f"Verified {C.BOLD}{task_id}{C.RESET}{C.GREEN} rendered by {node}.")
    log_success(f"Credited {C.BOLD}+₹{task['price']:,.2f}{C.RESET}{C.GREEN} "
                f"(school share ₹{task['price'] * SCHOOL_SHARE:,.2f}; run total ₹{float(rev):,.2f}).")
    return jsonify({"status": "success"})


@app.route('/node/event', methods=['POST', 'OPTIONS'])
def node_event():
    """Edge nodes report student interruptions here (pause / resume)."""
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200
    data = request.get_json(silent=True) or {}
    kind = data.get("type")
    if kind not in ("interrupt", "resume"):
        return jsonify({"status": "error", "message": "type must be interrupt or resume"}), 400
    node = str(data.get("node", "edge node"))[:60]
    task_id = str(data.get("task_id") or "")[:60] or None
    if kind == "interrupt":
        touch_node(node, interrupt=True)
    record_event(kind, node=node, task_id=task_id,
                 detail="student used the PC; work paused" if kind == "interrupt" else "PC idle again; work resumed")
    return jsonify({"status": "recorded"})


@app.route('/reset', methods=['POST', 'OPTIONS'])
@login_required('school')
def reset():
    """Start a new demo run. Nothing is deleted: history stays in the records."""
    with _lock:
        old = current_run()
        # Close out the old run's unfinished work so it never pays out later
        db.execute("UPDATE tasks SET status = 'cancelled' WHERE run_id = ? AND status IN ('queued', 'in_flight')", (old,))
        db.execute("UPDATE jobs SET status = 'cancelled' WHERE run_id = ? AND status = 'running'", (old,))
        new = old + 1
        db.execute("INSERT INTO runs (id, started_at, note) VALUES (?, ?, ?)", (new, time.time(), "reset from school NOC"))
        db.set_meta("current_run", new)
        record_event("reset", detail=f"run {old} closed, run {new} started", run_id=new)
    log_warn(f"New demo run {new} started. Run {old} kept in the records.")
    return jsonify({"status": "reset", "run": new})

# ----------------------------------------------------------------------
# Records API (signed-in users) + CSV export
# ----------------------------------------------------------------------
@app.route('/api/records/summary')
@login_required('startup', 'school')
def records_summary():
    def block(where, params):
        row = db.one("SELECT COUNT(*) AS frames, SUM(price) AS gross FROM tasks WHERE status = 'done'" + where, params) or {}
        jobs = db.value("SELECT COUNT(*) AS n FROM jobs" + where.replace(" AND ", " WHERE ", 1), params)
        out = split(row.get("gross") or 0)
        out.update({"frames": int(row.get("frames") or 0), "jobs": int(jobs)})
        return out
    run = current_run()
    return jsonify({
        "storage": db.kind,
        "permanent": db.kind == "postgres",
        "current_run": run,
        "runs": int(db.value("SELECT COUNT(*) AS n FROM runs")),
        "this_run": block(" AND run_id = ?", (run,)),
        "all_time": block("", ()),
        "nodes": int(db.value("SELECT COUNT(*) AS n FROM nodes")),
        "interrupts": int(db.value("SELECT COUNT(*) AS n FROM events WHERE type = 'interrupt'")),
        "requeues": int(db.value("SELECT COUNT(*) AS n FROM events WHERE type = 'requeue'")),
        "since": iso(db.value("SELECT MIN(started_at) AS t FROM runs", default=None)),
    })


def _jobs_rows(limit):
    rows = db.all("SELECT j.*, "
                  "(SELECT COUNT(*) FROM tasks t WHERE t.job_id = j.id AND t.status = 'done') AS frames_done, "
                  "(SELECT SUM(price) FROM tasks t WHERE t.job_id = j.id AND t.status = 'done') AS earned "
                  "FROM jobs j ORDER BY j.created_at DESC LIMIT ?", (limit,))
    for r in rows:
        r["created"] = iso(r.pop("created_at"))
        r["completed"] = iso(r.pop("completed_at"))
        r["earned"] = round(float(r["earned"] or 0), 2)
        r.update({k: v for k, v in split(r["earned"]).items() if k in ("school", "care_fund", "edugrid")})
    return rows

@app.route('/api/records/jobs')
@login_required('startup', 'school')
def records_jobs():
    limit = max(1, min(int(request.args.get("limit", 50)), 500))
    return jsonify({"jobs": _jobs_rows(limit)})

@app.route('/api/jobs')   # kept for older pages
def api_jobs():
    return jsonify({"jobs": _jobs_rows(20)})

@app.route('/api/records/nodes')
@login_required('startup', 'school')
def records_nodes():
    rows = db.all("SELECT * FROM nodes ORDER BY last_seen DESC")
    now = time.time()
    for r in rows:
        r["online"] = now - r["last_seen"] < 60
        r["first_seen"], r["last_seen"] = iso(r["first_seen"]), iso(r["last_seen"])
        r["earned_gross"] = round(float(r["earned_gross"] or 0), 2)
        r["school_share"] = round(r["earned_gross"] * SCHOOL_SHARE, 2)
    return jsonify({"nodes": rows})

@app.route('/api/records/events')
@login_required('startup', 'school')
def records_events():
    limit = max(1, min(int(request.args.get("limit", 100)), 1000))
    rows = db.all("SELECT * FROM events ORDER BY at DESC LIMIT ?", (limit,))
    for r in rows:
        r["at"] = iso(r["at"])
    return jsonify({"events": rows})

EXPORTS = {
    "jobs":   "SELECT * FROM jobs ORDER BY created_at",
    "tasks":  "SELECT * FROM tasks ORDER BY seq",
    "nodes":  "SELECT * FROM nodes ORDER BY first_seen",
    "events": "SELECT * FROM events ORDER BY at",
    "runs":   "SELECT * FROM runs ORDER BY id",
}
TIME_COLS = {"created_at", "completed_at", "assigned_at", "first_seen", "last_seen", "at", "started_at"}

@app.route('/api/records/export/<name>.csv')
@login_required('startup', 'school')
def records_export(name):
    if name not in EXPORTS:
        abort(404)
    rows = db.all(EXPORTS[name])
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: (iso(v) if k in TIME_COLS and v else v) for k, v in r.items()})
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=edugrid_{name}_{stamp}.csv"})

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

=== ENTERPRISE ORCHESTRATOR v4.0.0 (PROD) ==={C.RESET}
""", flush=True)
log_sys("Initializing EduGrid Central Command…")
if db.kind == "postgres":
    log_db("Connected to Postgres. Records are permanent.")
else:
    where = "on this PC" if not os.environ.get("RENDER") else "on Render's disk, which is wiped on restart"
    log_db(f"Using SQLite file {SQLITE_PATH} ({where}). Set DATABASE_URL for permanent records.")
log_db(f"Current demo run: {current_run()}  ·  jobs recorded so far: {db.meta('job_counter', '0')}")
for _role, (_u, _p) in ROLES.items():
    if not (os.environ.get(_u) and os.environ.get(_p)):
        log_warn(f"{_u} / {_p} not set: the {_role} portal cannot sign in yet.")
log_sys("Waiting for school nodes and startup deployments…\n")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, threaded=True)
