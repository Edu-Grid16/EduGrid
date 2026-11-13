# EduGrid

EduGrid turns idle school computer labs into an affordable compute network. Startups deploy rendering, video, AI and data jobs from a cloud console; a small agent on each lab PC processes the work only when no student is using it. Schools receive 80% of the revenue.

Live demo: https://edugrid.onrender.com/startup (startup console) and https://edugrid.onrender.com/school (school dashboard).

## How it works

| Part | File | Runs on |
| --- | --- | --- |
| Orchestrator (task queue, pricing ledger, login) | `server.py` | Render (Flask + gunicorn) |
| Records database (jobs, frames, lab PCs, money split, event log) | Postgres via `DATABASE_URL` | Neon or Render Postgres |
| Startup console | `templates/startup.html` | Served by `server.py` |
| School network operations dashboard | `templates/school.html` | Served by `server.py` |
| Edge node agent | `client.py` | Each school lab PC |
| Sample rendered output for the gallery | `static/outputs/` | Served by `server.py` |
| Blender scene used to render those frames | `tools/edugrid_render_scene.py` | Blender 4.x |

Key behaviour:

- A startup gets a live quote by workload, output quality (HD to 4K), and schedule; each task is credited at exactly the quoted price.
- The agent pauses the instant a student touches the mouse or keyboard and resumes after 5 seconds idle.
- A task that a node takes but never finishes returns to the queue after 5 minutes, and is never paid twice.
- Logins are checked on the server; no credentials are stored in the pages.
- Every job, frame, lab PC, rupee and student interruption is recorded in Postgres and survives restarts. Both dashboards show the records and export them as CSV.

## Deploy on Render

- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn server:app --workers 1 --threads 8 --timeout 120`
- Environment variables: `SECRET_KEY`, `EDUGRID_STARTUP_USER`, `EDUGRID_STARTUP_PASS`, `EDUGRID_SCHOOL_USER`, `EDUGRID_SCHOOL_PASS`, `DATABASE_URL`
- Without `DATABASE_URL` the server uses a local SQLite file, which Render wipes on every restart.

## Run a lab PC node

```
pip install requests pynput
python client.py --node Lab-PC-04
```

Built by EduGrid for the SPARDHA Ideathon.
