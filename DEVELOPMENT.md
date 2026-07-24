# PicoBot Contest Server — Developer Guide

Everything a developer needs to understand, run locally, and extend the server.

---

## Project Structure

```
.
├── app.py               # Entire application — routes, templates, DB, WSGI wiring
├── config.py            # Configuration pulled from environment variables
├── requirements.txt     # Python dependencies (Flask, gunicorn)
├── Dockerfile           # Production image (python:3.12-slim + gunicorn)
├── docker-compose.yaml  # Docker Compose for Traefik deployment
├── README.md            # End-user / ops documentation
└── DEVELOPMENT.md       # This file
```

All HTML templates are defined as Python strings inside `app.py` using
`render_template_string`. There are no separate template files.

---

## Architecture Overview

```
Browser / Robot
      │
      ▼
   Traefik (TLS termination)
      │  forwards full path /picobot-contest/...
      ▼
   gunicorn (2 workers, port 5000)
      │
      ▼
   ProxyFix  ← trusts X-Forwarded-Proto so redirects use https://
      │
      ▼
   DispatcherMiddleware  ← mounts Flask at /picobot-contest
      │                    splits PATH_INFO so url_for() generates correct URLs
      ▼
   Flask app
      │
      ▼
   SQLite  (/app/data/picobot.db, persisted via Docker volume)
```

**Key design decision — no Traefik `stripPrefix`.**  
Werkzeug's `DispatcherMiddleware` handles the sub-path split internally.
Traefik forwards the full path unchanged; the app mounts itself at
`MOUNT_PATH` and Flask's `url_for()` automatically includes the prefix in
every generated URL.

---

## Running Locally (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: override defaults
export DATABASE=/tmp/picobot.db
export MOUNT_PATH=/picobot-contest

# Run with Flask dev server (single worker, auto-reload)
FLASK_APP=app:app flask run --port 5000

# Or run the full WSGI stack (DispatcherMiddleware + ProxyFix) via gunicorn
gunicorn --bind 0.0.0.0:5000 --workers 1 --reload app:application
```

Open `http://localhost:5000/picobot-contest/` in your browser.

> **Note:** When running locally the `MOUNT_PATH` prefix is required in the
> URL because `DispatcherMiddleware` is always active.

---

## Configuration (`config.py`)

| Variable | Default | Description |
|---|---|---|
| `DATABASE` | `/app/data/picobot.db` | SQLite file path |
| `REPORT_AUTH` | *(empty)* | Global bearer token for robot POSTs |
| `MOUNT_PATH` | `/picobot-contest` | WSGI sub-path |

All values are read from environment variables at startup.

---

## Database

### Initialization and Migrations

`init_db()` creates all tables with `CREATE TABLE IF NOT EXISTS`.  
`migrate_db()` uses `PRAGMA table_info` to detect missing columns and adds
them with `ALTER TABLE … ADD COLUMN`. Both run at module import time:

```python
with app.app_context():
    init_db()
    migrate_db()
```

### Adding a new column

1. Add the column to the `CREATE TABLE` statement in `init_db()`.
2. Add a migration block in `migrate_db()`:

```python
existing = {row[1] for row in db.execute("PRAGMA table_info(robots)")}
if 'my_new_column' not in existing:
    db.execute("ALTER TABLE robots ADD COLUMN my_new_column TEXT")
```

This keeps existing databases in sync without wiping data.

### Schema

```sql
robots (mac PK, first_seen, last_seen, request_count, ip,
        servo_base, servo_arm, servo_claw, uptime_ms,
        command,              -- server_command bitmask returned to robot
        last_competition_id)  -- last competition this robot authenticated against

requests (id PK, mac FK, received_at, servo_base, servo_arm, servo_claw, uptime_ms)

competitions (id PK, name, created_at, started_at, ended_at,
              bearer_token, time_limit_seconds)

competition_runs (id PK, competition_id FK, mac FK,
                  enabled_at, disabled_at,
                  UNIQUE(competition_id, mac))
```

### Inspecting the live database

```bash
docker exec picobot-contest python3 -c "
import sqlite3
db = sqlite3.connect('/app/data/picobot.db')
for row in db.execute('SELECT * FROM robots'):
    print(dict(row))
"
```

---

## Key Code Patterns

### Server command bitmask

```
bit 0 (0x01) — competition_ready
bit 1 (0x02) — competition_running
```

| Value | Meaning |
|-------|---------|
| `0` | Idle / no competition |
| `1` | Competition ready (not yet running) |
| `3` | Competition running / robot enabled |

### Auth flow

`@require_auth` decorator on `receive_status`:
1. If an active competition has a `bearer_token` set → that token is required.
2. Else if global `REPORT_AUTH` is set → that token is required.
3. Else → all requests accepted.

Robots that fail auth return `401 Unauthorized` and are **not stored** in the
database.

### Robot ID

Short human-readable ID shown in the UI:

```python
'R-' + mac.replace(':', '').upper()[-4:]
# e.g. mac "2c:cf:67:f2:d9:1a" → "R-D91A"
```

### Offline detection

Template filter `is_stale(seconds=4)` — returns `True` if `last_seen` is
older than `seconds`. Used to show the **Offline** badge on the dashboard.

Competition tab uses a 15-second threshold in Python to hide idle robots that
have gone offline (robots with an active/finished run are always shown).

### Templates

All HTML lives in module-level string constants:

| Constant | Page |
|----------|------|
| `DASHBOARD_HTML` | `/picobot-contest/` |
| `COMPETITION_HTML` | `/picobot-contest/competition` |

Rendered with `render_template_string(COMPETITION_HTML, ...)`.

Custom Jinja2 filters registered with `@app.template_filter`:
- `bitand(mask)` — bitwise AND for checking command flags
- `is_stale(seconds)` — staleness check on ISO-8601 UTC timestamps

### Auto-refresh

- **Dashboard** — always auto-refreshes every 5 s (`setInterval(() => location.reload(), 5000)`).
- **Competition** — auto-refreshes every 5 s **only while the competition is
  running** (`{% if active and active.started_at %}`).

---

## Adding a New Route

```python
@app.route('/my-feature', methods=['GET', 'POST'])
def my_feature():
    db = get_db()
    # ... query, mutate, redirect
    return render_template_string(MY_TEMPLATE, ...)
```

Add a link in the navbar via `_nav()` helper and register it in
`NAV_DASHBOARD` / `NAV_COMPETITION`.

---

## Deployment

### First deploy

```bash
cd /path/to/project
docker compose up -d
# Add route to /path/to/traefik/dynamic.yml (see README.md)
docker restart traefik
```

### Rebuild after code changes

```bash
docker compose up -d --build
# No traefik restart needed unless dynamic.yml changed
```

### Logs

```bash
docker logs picobot-contest -f
```

Gunicorn does not log HTTP requests by default. To enable access logging add
`--access-logfile -` to the `CMD` in `Dockerfile`:

```dockerfile
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2",
     "--timeout", "60", "--access-logfile", "-", "app:application"]
```

### Database backup

```bash
docker run --rm -v picobot-contest_db-data:/data \
  -v $(pwd):/out alpine cp /data/picobot.db /out/picobot-backup.db
```

---

## Common Pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| `url_for()` generates wrong prefix | Running without `DispatcherMiddleware` | Always run `app:application`, not `app:app` |
| Redirects use `http://` instead of `https://` | Missing `ProxyFix` | Already wrapped — don't remove it |
| Traefik not picking up config changes | `--providers.file.watch=false` | `docker restart traefik` after editing `dynamic.yml` |
| Second robot not appearing | Competition bearer token mismatch | Configure the same token on both robots, or clear the token in the UI |
| JS error on competition page | Jinja2 values with quotes injected into HTML attributes | Use `data-*` attributes to pass values to JS, read via `dataset` |
