"""
PicoBot Fleet Server
====================
Flask application that receives HTTPS POST status reports from PicoBot robots,
stores them in a SQLite database, and shows a live dashboard.

Each robot is identified by its MAC address.  The server returns a single
integer (server_command bitmask) that the robot interprets as:
  bit 0 (0x01) — competition_ready
  bit 1 (0x02) — competition_running
"""

import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, redirect, render_template_string, request, url_for
from werkzeug.exceptions import NotFound
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix

import config

app = Flask(__name__)
app.config.from_object(config)

# ---------------------------------------------------------------------------
# Sub-path deployment via DispatcherMiddleware.
# Traefik routes /picobot-contest/* to this container WITHOUT stripping the
# prefix.  DispatcherMiddleware splits PATH_INFO into SCRIPT_NAME + PATH_INFO
# so Flask's url_for() generates correct prefixed URLs automatically.
# ---------------------------------------------------------------------------

_mount_path = os.environ.get('MOUNT_PATH', '/picobot-contest').rstrip('/')
application = DispatcherMiddleware(NotFound(), {_mount_path: app})
# Trust X-Forwarded-Proto from Traefik so redirects use https://
application = ProxyFix(application, x_for=1, x_proto=1, x_host=1)


@app.template_filter('bitand')
def bitand_filter(value, mask):
    return int(value) & int(mask)


@app.template_filter('is_stale')
def is_stale_filter(dt_str, seconds=4):
    if not dt_str:
        return True
    try:
        dt = datetime.fromisoformat(dt_str)
        return (datetime.now(timezone.utc) - dt).total_seconds() > seconds
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(
            app.config['DATABASE'],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(app.config['DATABASE'])
    db.execute("""
        CREATE TABLE IF NOT EXISTS robots (
            mac                 TEXT PRIMARY KEY,
            first_seen          TEXT NOT NULL,
            last_seen           TEXT NOT NULL,
            request_count       INTEGER NOT NULL DEFAULT 0,
            ip                  TEXT,
            servo_base          INTEGER,
            servo_arm           INTEGER,
            servo_claw          INTEGER,
            uptime_ms           INTEGER,
            command             INTEGER NOT NULL DEFAULT 0,
            last_competition_id INTEGER
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mac         TEXT NOT NULL,
            received_at TEXT NOT NULL,
            servo_base  INTEGER,
            servo_arm   INTEGER,
            servo_claw  INTEGER,
            uptime_ms   INTEGER,
            FOREIGN KEY (mac) REFERENCES robots(mac)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS competitions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            name                TEXT NOT NULL,
            created_at          TEXT NOT NULL,
            started_at          TEXT,
            ended_at            TEXT,
            bearer_token        TEXT,
            time_limit_seconds  INTEGER NOT NULL DEFAULT 120
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS competition_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id  INTEGER NOT NULL,
            mac             TEXT NOT NULL,
            enabled_at      TEXT,
            disabled_at     TEXT,
            FOREIGN KEY (competition_id) REFERENCES competitions(id),
            UNIQUE(competition_id, mac)
        )
    """)
    db.commit()
    db.close()


def migrate_db():
    """Apply incremental schema migrations to an existing database."""
    db = sqlite3.connect(app.config['DATABASE'])
    # robots table migrations
    existing = {row[1] for row in db.execute("PRAGMA table_info(robots)")}
    if 'ip' not in existing:
        db.execute("ALTER TABLE robots ADD COLUMN ip TEXT")
    if 'last_competition_id' not in existing:
        db.execute("ALTER TABLE robots ADD COLUMN last_competition_id INTEGER")
    # competitions table migrations
    comp_cols = {row[1] for row in db.execute("PRAGMA table_info(competitions)")}
    if 'bearer_token' not in comp_cols:
        db.execute("ALTER TABLE competitions ADD COLUMN bearer_token TEXT")
    if 'time_limit_seconds' not in comp_cols:
        db.execute("ALTER TABLE competitions ADD COLUMN time_limit_seconds INTEGER NOT NULL DEFAULT 120")
    # competition_runs table
    db.execute("""
        CREATE TABLE IF NOT EXISTS competition_runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id  INTEGER NOT NULL,
            mac             TEXT NOT NULL,
            enabled_at      TEXT,
            disabled_at     TEXT,
            FOREIGN KEY (competition_id) REFERENCES competitions(id),
            UNIQUE(competition_id, mac)
        )
    """)
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Competition helpers
# ---------------------------------------------------------------------------

def get_active_competition(db):
    """Return the active competition row (ready or running), or None."""
    return db.execute(
        "SELECT * FROM competitions WHERE ended_at IS NULL ORDER BY created_at DESC LIMIT 1"
    ).fetchone()


def get_competition_command(db):
    """
    Derive server_command from the current competition state:
      no active competition → DEFAULT_COMMAND
      competition ready (not started) → 0x01
      competition running (started, not ended) → 0x03
    """
    comp = get_active_competition(db)
    if comp is None:
        return app.config['DEFAULT_COMMAND']
    if comp['started_at'] is None:
        return 0x01  # competition_ready
    return 0x03      # competition_ready + competition_running


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def require_auth(f):
    """Decorator: verify Bearer token — competition token overrides global."""
    @wraps(f)
    def decorated(*args, **kwargs):
        expected = app.config.get('REPORT_AUTH')
        # An active competition's bearer_token takes priority when set
        try:
            db = get_db()
            comp = get_active_competition(db)
            if comp and comp['bearer_token']:
                expected = comp['bearer_token']
        except Exception:
            pass
        if expected is not None:
            auth_header = request.headers.get('Authorization', '')
            if auth_header != f'Bearer {expected}':
                return 'Unauthorized', 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route(config.STATUS_ENDPOINT, methods=['POST'])
@require_auth
def receive_status():
    """
    Accept a JSON status report from a robot and return its server_command.

    Expected JSON body:
        {
            "mac":        "aa:bb:cc:dd:ee:ff",
            "ip":         "192.168.4.1",
            "servo_base": 90,
            "servo_arm":  90,
            "servo_claw": 90,
            "uptime_ms":  12345
        }

    Response: plain integer text, e.g. "3"
    """
    data = request.get_json(silent=True)
    if not data or 'mac' not in data:
        return 'Bad Request: missing mac field', 400

    mac        = str(data['mac']).lower().strip()
    now        = datetime.now(timezone.utc).isoformat()
    ip         = str(data['ip']).strip() if data.get('ip') else None
    servo_base = data.get('servo_base')
    servo_arm  = data.get('servo_arm')
    servo_claw = data.get('servo_claw')
    uptime_ms  = data.get('uptime_ms')

    db = get_db()

    db.execute(
        """INSERT INTO robots
               (mac, first_seen, last_seen, request_count,
                ip, servo_base, servo_arm, servo_claw, uptime_ms)
               VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
           ON CONFLICT(mac) DO UPDATE SET
               last_seen     = excluded.last_seen,
               request_count = request_count + 1,
               ip            = excluded.ip,
               servo_base    = excluded.servo_base,
               servo_arm     = excluded.servo_arm,
               servo_claw    = excluded.servo_claw,
               uptime_ms     = excluded.uptime_ms""",
        (mac, now, now, ip, servo_base, servo_arm, servo_claw, uptime_ms))

    # Determine command to send back
    robot_row = db.execute('SELECT command FROM robots WHERE mac = ?', (mac,)).fetchone()
    comp = get_active_competition(db)
    if comp:
        # Tag this robot as belonging to the current competition
        db.execute('UPDATE robots SET last_competition_id = ? WHERE mac = ?', (comp['id'], mac))
    if comp:
        run = db.execute(
            "SELECT * FROM competition_runs WHERE competition_id = ? AND mac = ? AND enabled_at IS NOT NULL",
            (comp['id'], mac),
        ).fetchone()
        if run:
            if run['disabled_at']:
                # Use whatever command was set when the run was disabled
                command = robot_row['command'] if (robot_row and robot_row['command'] is not None) else 0x00
            else:
                limit = comp['time_limit_seconds'] or 120
                enabled_dt = datetime.fromisoformat(run['enabled_at'])
                elapsed = (datetime.now(timezone.utc) - enabled_dt).total_seconds()
                if elapsed >= limit:
                    # Auto-disable: time limit exceeded → drop to ready, not idle
                    db.execute(
                        "UPDATE competition_runs SET disabled_at = ? WHERE competition_id = ? AND mac = ?",
                        (now, comp['id'], mac),
                    )
                    db.execute('UPDATE robots SET command = ? WHERE mac = ?', (0x01, mac))
                    command = 0x01
                else:
                    command = robot_row['command'] if (robot_row and robot_row['command']) else get_competition_command(db)
        else:
            command = robot_row['command'] if (robot_row and robot_row['command']) else get_competition_command(db)
    else:
        command = robot_row['command'] if (robot_row and robot_row['command']) else get_competition_command(db)

    # Record individual request in history
    db.execute(
        """INSERT INTO requests (mac, received_at, servo_base, servo_arm, servo_claw, uptime_ms)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (mac, now, servo_base, servo_arm, servo_claw, uptime_ms),
    )
    db.commit()
    return str(command), 200


@app.route('/robot/<mac>')
def robot_detail(mac):
    """Show all recorded requests from a single robot."""
    db = get_db()
    robot = db.execute('SELECT * FROM robots WHERE mac = ?', (mac,)).fetchone()
    if robot is None:
        return f'Robot {mac} not found', 404
    rows = db.execute(
        'SELECT * FROM requests WHERE mac = ? ORDER BY received_at DESC',
        (mac,),
    ).fetchall()
    return render_template_string(DETAIL_HTML, robot=robot, rows=rows)


@app.route('/robot/<mac>/command', methods=['POST'])
def set_command(mac):
    """Set the server_command integer for a specific robot (submitted from the dashboard)."""
    command = request.form.get('command', type=int, default=0)
    db = get_db()
    db.execute('UPDATE robots SET command = ? WHERE mac = ?', (command, mac))
    db.commit()
    return redirect(url_for('dashboard'))


@app.route('/dashboard/clear_commands')
def clear_commands():
    """Clear per-robot command overrides for all robots."""
    db = get_db()
    db.execute('UPDATE robots SET command = 0')
    db.commit()
    return redirect(url_for('dashboard'))


def _fmt_secs(total):
    m = int(total) // 60
    s = int(total) % 60
    return f'{m}:{s:02d}'


@app.route('/competition')
def competition_view():
    db = get_db()
    active = get_active_competition(db)
    past = db.execute(
        'SELECT * FROM competitions WHERE ended_at IS NOT NULL ORDER BY ended_at DESC'
    ).fetchall()

    robot_runs = []
    if active:
        rows = db.execute("""
            SELECT r.mac, r.ip, r.last_seen, r.command,
                   cr.enabled_at, cr.disabled_at
            FROM robots r
            LEFT JOIN competition_runs cr
                ON cr.mac = r.mac AND cr.competition_id = ?
            WHERE r.last_competition_id = ?
               OR cr.id IS NOT NULL
            ORDER BY
                CASE
                    WHEN cr.enabled_at IS NOT NULL AND cr.disabled_at IS NULL THEN 0
                    WHEN cr.disabled_at IS NOT NULL THEN 1
                    ELSE 2
                END,
                cr.enabled_at DESC,
                r.last_seen DESC
        """, (active['id'], active['id'])).fetchall()

        limit = active['time_limit_seconds'] or 120
        for r in rows:
            base = {
                'mac': r['mac'], 'ip': r['ip'],
                'last_seen': r['last_seen'], 'command': r['command'] or 0,
                'enabled_at': r['enabled_at'], 'disabled_at': r['disabled_at'],
            }
            if r['enabled_at'] and r['disabled_at']:
                ea = datetime.fromisoformat(r['enabled_at'])
                da = datetime.fromisoformat(r['disabled_at'])
                lap = (da - ea).total_seconds()
                robot_runs.append({**base, 'status': 'finished',
                                   'lap_str': _fmt_secs(lap), 'lap_over': lap > limit})
            elif r['enabled_at']:
                robot_runs.append({**base, 'status': 'running',
                                   'lap_str': None, 'lap_over': False})
            else:
                robot_runs.append({**base, 'status': 'idle',
                                   'lap_str': None, 'lap_over': False})

    def duration(row):
        if not row['started_at'] or not row['ended_at']:
            return '—'
        try:
            s = datetime.fromisoformat(row['started_at'])
            e = datetime.fromisoformat(row['ended_at'])
            secs = int((e - s).total_seconds())
            h, rem = divmod(secs, 3600)
            m, s = divmod(rem, 60)
            return f'{h:02d}:{m:02d}:{s:02d}'
        except Exception:
            return '—'

    return render_template_string(COMPETITION_HTML, active=active, past=past,
                                  duration=duration, robot_runs=robot_runs)


@app.route('/competition/create', methods=['POST'])
def competition_create():
    name = request.form.get('name', '').strip()
    if not name:
        return redirect(url_for('competition_view'))
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    token = request.form.get('bearer_token', '').strip() or None
    time_limit = request.form.get('time_limit_seconds', type=int, default=120)
    if time_limit not in (30, 60, 90, 120, 180, 300, 600):
        time_limit = 120
    db.execute(
        'INSERT INTO competitions (name, created_at, bearer_token, time_limit_seconds) VALUES (?, ?, ?, ?)',
        (name, now, token, time_limit),
    )
    db.commit()
    return redirect(url_for('competition_view'))


@app.route('/competition/<int:comp_id>/start', methods=['POST'])
def competition_start(comp_id):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        'UPDATE competitions SET started_at = ? WHERE id = ? AND started_at IS NULL AND ended_at IS NULL',
        (now, comp_id),
    )
    db.commit()
    return redirect(url_for('competition_view'))


@app.route('/competition/<int:comp_id>/stop', methods=['POST'])
def competition_stop(comp_id):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        'UPDATE competitions SET ended_at = ? WHERE id = ? AND started_at IS NOT NULL AND ended_at IS NULL',
        (now, comp_id),
    )
    db.commit()
    return redirect(url_for('competition_view'))


@app.route('/competition/<int:comp_id>/token', methods=['POST'])
def competition_token(comp_id):
    db = get_db()
    token = request.form.get('bearer_token', '').strip() or None
    db.execute(
        'UPDATE competitions SET bearer_token = ? WHERE id = ?',
        (token, comp_id),
    )
    db.commit()
    return redirect(url_for('competition_view'))


@app.route('/competition/<int:comp_id>/robot/<mac>/enable', methods=['POST'])
def competition_robot_enable(comp_id, mac):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        INSERT INTO competition_runs (competition_id, mac, enabled_at, disabled_at)
        VALUES (?, ?, ?, NULL)
        ON CONFLICT(competition_id, mac) DO UPDATE SET
            enabled_at  = excluded.enabled_at,
            disabled_at = NULL
    """, (comp_id, mac, now))
    db.execute('UPDATE robots SET command = ? WHERE mac = ?', (0x03, mac))
    db.commit()
    return redirect(url_for('competition_view'))


@app.route('/competition/<int:comp_id>/robot/<mac>/disable', methods=['POST'])
def competition_robot_disable(comp_id, mac):
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute("""
        UPDATE competition_runs SET disabled_at = ?
        WHERE competition_id = ? AND mac = ? AND disabled_at IS NULL
    """, (now, comp_id, mac))
    db.execute('UPDATE robots SET command = 0 WHERE mac = ?', (mac,))
    db.commit()
    return redirect(url_for('competition_view'))


@app.route('/')
def dashboard():
    db = get_db()
    robots = db.execute(
        'SELECT * FROM robots ORDER BY last_seen DESC'
    ).fetchall()
    active = get_active_competition(db)
    return render_template_string(DASHBOARD_HTML, robots=robots, active=active)


# ---------------------------------------------------------------------------
# Shared HTML fragments (Bootstrap 5)
# ---------------------------------------------------------------------------

_BS_HEAD = """\
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body { background: #f0f2f5; }
  .table thead { background: #343a40; color: #fff; }
  .timer { font-size: 3rem; font-variant-numeric: tabular-nums; font-weight: 700; letter-spacing: .05em; }
  .stat-label { font-size: .75rem; text-transform: uppercase; letter-spacing: .06em; color: #6c757d; }
  .stat-value { font-size: 1.5rem; font-weight: 700; }
</style>"""

def _nav(active):
    """Render a Bootstrap navbar. active: 'dashboard' | 'competition'"""
    def _li(label, href, key):
        cls = 'nav-link active fw-semibold' if active == key else 'nav-link'
        return f'<li class="nav-item"><a class="{cls}" href="{href}">{label}</a></li>'
    return (
        '<nav class="navbar navbar-expand navbar-dark bg-dark px-3 mb-4">'
        '<a class="navbar-brand me-4" href="{{ url_for(\'dashboard\') }}">🤖 PicoBot</a>'
        '<ul class="navbar-nav">'
        + _li('Dashboard', "{{ url_for('dashboard') }}", 'dashboard')
        + _li('Competition', "{{ url_for('competition_view') }}", 'competition')
        + '</ul></nav>'
    )

NAV_DASHBOARD   = _nav('dashboard')
NAV_COMPETITION = _nav('competition')
NAV_DETAIL      = _nav(None)

# ---------------------------------------------------------------------------
# Dashboard template
# ---------------------------------------------------------------------------

DASHBOARD_HTML = (
"""<!DOCTYPE html>
<html lang="en">
<head>""" + _BS_HEAD + """
<title>PicoBot Dashboard</title>
</head>
<body>
""" + NAV_DASHBOARD + """
<div class="container-fluid px-4">
  <h4 class="mb-3">Fleet Dashboard</h4>

  {% if active %}
    {% if active.started_at %}
      <div class="alert alert-primary d-flex align-items-center gap-2 py-2" role="alert">
        <span class="badge bg-primary">🏁 Running</span>
        Competition: <strong>{{ active.name }}</strong>
        — robots receive <code>competition_running = True</code>
      </div>
    {% else %}
      <div class="alert alert-success d-flex align-items-center gap-2 py-2" role="alert">
        <span class="badge bg-success">✅ Ready</span>
        Competition: <strong>{{ active.name }}</strong>
        — robots receive <code>competition_ready = True</code>
      </div>
    {% endif %}
  {% endif %}

  {% if robots %}
  <div class="card shadow-sm">
    <div class="card-body p-0">
      <table class="table table-hover table-bordered mb-0 align-middle">
        <thead>
          <tr>
            <th>ID</th>
            <th>First Seen (UTC)</th>
            <th>MAC / IP</th>
            <th>Requests</th>
            <th>Base</th><th>Arm</th><th>Claw</th>
            <th>State</th>
            <th>Force Cmd <span data-bs-toggle="tooltip" data-bs-placement="left"
                title="Manually override the command sent to this robot on its next ping. 0 = use global competition state, 1 = ready, 3 = ready + running. Overridden automatically by Enable / Disable in the Competition tab."
                style="cursor:help;opacity:.6">ⓘ</span></th>
          </tr>
        </thead>
        <tbody>
        {% for r in robots %}
          {% set cmd = r.command %}
          {% set rid = 'R-' + (r.mac | replace(':', '') | upper)[-4:] %}
          <tr>
            <td><a href="{{ url_for('robot_detail', mac=r.mac) }}" class="text-decoration-none"><span class="badge bg-secondary fs-6">{{ rid }}</span></a></td>
            <td class="text-nowrap">{{ r.first_seen[:19].replace('T',' ') }}</td>
            <td><code>{{ r.mac }}</code>{% if r.ip %}<br><small class="text-muted">{{ r.ip }}</small>{% endif %}</td>
            <td class="text-center">{{ r.request_count }}</td>
            <td class="text-center">{{ r.servo_base }}°</td>
            <td class="text-center">{{ r.servo_arm }}°</td>
            <td class="text-center">{{ r.servo_claw }}°</td>
            <td>
              {% if r.last_seen | is_stale(4) %}
                <span class="badge bg-dark">Offline</span>
              {% else %}
                {% if cmd | bitand(2) %}<span class="badge bg-primary">Running</span>{% endif %}
                {% if cmd | bitand(1) %}<span class="badge bg-success">Ready</span>{% endif %}
                {% if not (cmd | bitand(3)) %}<span class="badge bg-success">Ready</span>{% endif %}
              {% endif %}
            </td>
            <td>
              <form method="post" action="{{ url_for('set_command', mac=r.mac) }}" class="d-flex gap-1 align-items-center">
                <input type="number" name="command" class="form-control form-control-sm" style="width:64px"
                       value="{{ cmd }}" min="0" max="255" placeholder="0">
                <button type="submit" class="btn btn-sm btn-outline-secondary">✓</button>
              </form>
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% else %}
    <div class="text-center text-muted py-5">No robots have reported in yet.</div>
  {% endif %}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => new bootstrap.Tooltip(el));
  setInterval(() => location.reload(), 5000);
</script>
</body>
</html>""")

# ---------------------------------------------------------------------------
# Robot detail template
# ---------------------------------------------------------------------------

DETAIL_HTML = (
"""<!DOCTYPE html>
<html lang="en">
<head>""" + _BS_HEAD + """
<title>PicoBot {{ robot.mac }}</title>
</head>
<body>
""" + NAV_DETAIL + """
<div class="container-fluid px-4">
  <a href="{{ url_for('dashboard') }}" class="btn btn-sm btn-outline-secondary mb-3">← Back to Dashboard</a>

  <h4 class="mb-0"><code>{{ robot.mac }}</code>{% if robot.ip %} <small class="text-muted fs-6">{{ robot.ip }}</small>{% endif %}</h4>
  <p class="text-muted small mb-3">
    First seen: {{ robot.first_seen[:19].replace('T',' ') }} UTC
     |  Total requests: <strong>{{ robot.request_count }}</strong>
  </p>

  <div class="row g-3 mb-4">
    {% for label, val in [
        ('Servo Base',  robot.servo_base|string + '°'),
        ('Servo Arm',   robot.servo_arm|string  + '°'),
        ('Servo Claw',  robot.servo_claw|string + '°'),
        ('Uptime ms',   robot.uptime_ms|string),
    ] %}
    <div class="col-6 col-sm-3 col-lg-2">
      <div class="card shadow-sm text-center py-3">
        <div class="stat-label">{{ label }}</div>
        <div class="stat-value">{{ val }}</div>
      </div>
    </div>
    {% endfor %}
  </div>

  <h5 class="mb-3">Request History</h5>
  {% if rows %}
  <div class="card shadow-sm">
    <div class="card-body p-0">
      <table class="table table-hover table-bordered mb-0 align-middle">
        <thead>
          <tr>
            <th>#</th>
            <th>Received (UTC)</th>
            <th>Servo Base</th><th>Servo Arm</th><th>Servo Claw</th>
            <th>Uptime (ms)</th>
          </tr>
        </thead>
        <tbody>
        {% for row in rows %}
          <tr>
            <td>{{ row.id }}</td>
            <td class="text-nowrap">{{ row.received_at[:19].replace('T',' ') }}</td>
            <td class="text-center">{{ row.servo_base }}°</td>
            <td class="text-center">{{ row.servo_arm }}°</td>
            <td class="text-center">{{ row.servo_claw }}°</td>
            <td class="text-center">{{ row.uptime_ms }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% else %}
    <div class="text-center text-muted py-5">No requests recorded yet.</div>
  {% endif %}
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>""")

# ---------------------------------------------------------------------------
# Competition template
# ---------------------------------------------------------------------------

COMPETITION_HTML = (
"""<!DOCTYPE html>
<html lang="en">
<head>""" + _BS_HEAD + """
<title>PicoBot Competition</title>
</head>
<body>
""" + NAV_COMPETITION + """

{# ── Robot lap-tracking table macro ── #}
{% macro _robot_table(comp, runs) %}
{% if runs %}
<div class="card shadow-sm mb-4 mt-3">
  <div class="card-header d-flex align-items-center justify-content-between py-2">
    <strong>Robots</strong>
    <span class="text-muted small">Time limit: <code>{{ comp.time_limit_seconds }}s</code>
      ({{ comp.time_limit_seconds // 60 }}{% if comp.time_limit_seconds % 60 %}:{{ '%02d'|format(comp.time_limit_seconds % 60) }}{% endif %} min)</span>
  </div>
  <div class="card-body p-0">
    <table class="table table-hover table-bordered mb-0 align-middle">
      <thead>
        <tr>
          <th>ID</th>
          <th>MAC / IP</th>
          <th>Last Seen (UTC)</th>
          <th>State</th>
          <th>Start Time</th>
          <th>End Time</th>
          <th>Result / Remaining</th>
          <th class="text-center">Action</th>
        </tr>
      </thead>
      <tbody>
      {% for r in runs %}
        {% set rid = 'R-' + (r.mac | replace(':', '') | upper)[-4:] %}
        <tr>
          <td><span class="badge bg-secondary fs-6">{{ rid }}</span></td>
          <td><code>{{ r.mac }}</code>{% if r.ip %}<br><small class="text-muted">{{ r.ip }}</small>{% endif %}</td>
          <td class="text-nowrap text-muted small">{{ r.last_seen[:19].replace('T',' ') }}</td>
          <td>
            {% if r.last_seen | is_stale(4) %}
              <span class="badge bg-dark">Offline</span>
            {% else %}
              {% if r.command | bitand(2) %}<span class="badge bg-primary">Running</span>{% endif %}
              {% if r.command | bitand(1) %}<span class="badge bg-success">Ready</span>{% endif %}
              {% if not (r.command | bitand(3)) %}<span class="badge bg-success">Ready</span>{% endif %}
            {% endif %}
          </td>
          <td class="text-nowrap">
            {% if r.enabled_at %}{{ r.enabled_at[:19].replace('T',' ') }}{% else %}—{% endif %}
          </td>
          <td class="text-nowrap">
            {% if r.disabled_at %}{{ r.disabled_at[:19].replace('T',' ') }}{% else %}—{% endif %}
          </td>
          <td class="text-center fw-bold">
            {% if r.status == 'finished' %}
              <span class="{{ 'text-success' if not r.lap_over else 'text-danger' }}">{{ r.lap_str }}</span>
              {% if r.lap_over %}<span class="badge bg-danger ms-1">OVERTIME</span>{% endif %}
            {% elif r.status == 'running' %}
              <span class="countdown" data-enabled-at="{{ r.enabled_at }}" data-limit="{{ comp.time_limit_seconds }}">—</span>
            {% else %}
              <span class="text-muted">—</span>
            {% endif %}
          </td>
          <td class="text-center">
            {% if r.status == 'idle' %}
              <form method="post" action="{{ url_for('competition_robot_enable', comp_id=comp.id, mac=r.mac) }}" class="d-inline">
                <button class="btn btn-sm btn-success" type="submit">▶ Enable</button>
              </form>
            {% elif r.status == 'running' %}
              <form method="post" action="{{ url_for('competition_robot_disable', comp_id=comp.id, mac=r.mac) }}" class="d-inline">
                <button class="btn btn-sm btn-danger" type="submit">⏹ Disable</button>
              </form>
            {% else %}
              <form method="post" action="{{ url_for('competition_robot_enable', comp_id=comp.id, mac=r.mac) }}" class="d-inline">
                <button class="btn btn-sm btn-outline-secondary btn-sm" type="submit" title="Re-run">↺</button>
              </form>
            {% endif %}
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
{% else %}
  <p class="text-muted mt-3">No robots have reported in yet.</p>
{% endif %}
{% endmacro %}

<div class="container-fluid px-4">
  <h4 class="mb-4">Competition</h4>

  {# ── Active competition panel ── #}
  {% if active %}

    {% if active.started_at is none %}
    {# READY #}
    <div class="card shadow-sm mb-4" style="max-width:560px">
      <div class="card-body">
        <h5 class="card-title d-flex align-items-center gap-2">
          {{ active.name }}
          <span class="badge bg-success">✅ Ready</span>
        </h5>
        <p class="text-muted mb-3">
          Competition is set up. Robots are receiving
          <code>competition_ready = True</code>.
        </p>
        <form method="post" action="{{ url_for('competition_token', comp_id=active.id) }}" class="mb-3"
              data-comp-name="{{ active.name | e }}" onsubmit="downloadToken(event, 'token-active', this.dataset.compName)">
          <div class="input-group input-group-sm">
            <span class="input-group-text">Token</span>
            <input type="text" class="form-control font-monospace" name="bearer_token" id="token-active"
                   value="{{ active.bearer_token or '' }}"
                   placeholder="Global config token">
            <button type="button" class="btn btn-outline-secondary" onclick="generateToken('token-active')">Generate</button>
            <button type="button" class="btn btn-outline-secondary" onclick="copyToken('token-active', this)" title="Copy to clipboard">📋</button>
            <button class="btn btn-outline-primary" type="submit">Save</button>
          </div>
          {% if active.bearer_token %}<small class="text-muted mt-1">Overrides global REPORT_AUTH</small>{% endif %}
        </form>
        <div class="d-flex gap-2">
          <form method="post" action="{{ url_for('competition_start', comp_id=active.id) }}">
            <button class="btn btn-success" type="submit">▶ Start Competition</button>
          </form>
          <form method="post" action="{{ url_for('competition_stop', comp_id=active.id) }}"
                onsubmit="return confirm('Cancel this competition?')">
            <button class="btn btn-outline-danger" type="submit">✕ Cancel</button>
          </form>
        </div>
      </div>
    </div>
    {{ _robot_table(active, robot_runs) }}

    {% else %}
    {# RUNNING #}
    <div class="card shadow-sm mb-4 border-primary" style="max-width:560px">
      <div class="card-body">
        <h5 class="card-title d-flex align-items-center gap-2">
          {{ active.name }}
          <span class="badge bg-primary">🏁 Running</span>
        </h5>
        <div class="timer text-primary my-3" id="timer">00:00:00</div>
        <p class="text-muted mb-3">
          Robots are receiving <code>competition_ready = True</code>
          and <code>competition_running = True</code>.
        </p>
        <form method="post" action="{{ url_for('competition_token', comp_id=active.id) }}" class="mb-3"
              data-comp-name="{{ active.name | e }}" onsubmit="downloadToken(event, 'token-active', this.dataset.compName)">
          <div class="input-group input-group-sm">
            <span class="input-group-text">Token</span>
            <input type="text" class="form-control font-monospace" name="bearer_token" id="token-active"
                   value="{{ active.bearer_token or '' }}"
                   placeholder="Global config token">
            <button type="button" class="btn btn-outline-secondary" onclick="generateToken('token-active')">Generate</button>
            <button type="button" class="btn btn-outline-secondary" onclick="copyToken('token-active', this)" title="Copy to clipboard">📋</button>
            <button class="btn btn-outline-primary" type="submit">Save</button>
          </div>
          {% if active.bearer_token %}<small class="text-muted mt-1">Overrides global REPORT_AUTH</small>{% endif %}
        </form>
        <form method="post" action="{{ url_for('competition_stop', comp_id=active.id) }}"
              onsubmit="return confirm('Stop the competition?')">
          <button class="btn btn-danger" type="submit">⏹ Stop Competition</button>
        </form>
      </div>
    </div>
    {{ _robot_table(active, robot_runs) }}
    <script>
      const startedAt = new Date("{{ active.started_at }}");
      function updateTimer() {
        const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
        const h = Math.floor(elapsed / 3600);
        const m = Math.floor((elapsed % 3600) / 60);
        const s = elapsed % 60;
        document.getElementById('timer').textContent =
          String(h).padStart(2,'0') + ':' +
          String(m).padStart(2,'0') + ':' +
          String(s).padStart(2,'0');
      }
      setInterval(updateTimer, 1000);
      updateTimer();
    </script>
    {% endif %}

  {% else %}
  {# NO ACTIVE COMPETITION #}
  <div class="card shadow-sm mb-4" style="max-width:560px">
    <div class="card-body">
      <h5 class="card-title">Create Competition</h5>
      <p class="text-muted mb-3">
        Enter a name and press <em>Create</em>. The competition will be set to
        <strong>Ready</strong> and robots will receive
        <code>competition_ready = True</code>.
      </p>
      <form method="post" action="{{ url_for('competition_create') }}">
        <div class="mb-2">
          <input type="text" class="form-control form-control-sm" name="name"
                 placeholder="Competition name…" required autofocus>
        </div>
        <div class="mb-2">
          <label class="form-label form-label-sm mb-1 text-muted">Time limit per robot</label>
          <select class="form-select form-select-sm" name="time_limit_seconds">
            <option value="30">30 seconds</option>
            <option value="60">1 minute</option>
            <option value="90">1 minute 30 s</option>
            <option value="120" selected>2 minutes</option>
            <option value="180">3 minutes</option>
            <option value="300">5 minutes</option>
            <option value="600">10 minutes</option>
          </select>
        </div>
        <div class="input-group input-group-sm mb-3">
          <input type="text" class="form-control font-monospace" name="bearer_token" id="token-new"
                 placeholder="Bearer token (optional)">
          <button type="button" class="btn btn-outline-secondary" onclick="generateToken('token-new')">Generate</button>
          <button type="button" class="btn btn-outline-secondary" onclick="copyToken('token-new', this)" title="Copy to clipboard">📋</button>
        </div>
        <button class="btn btn-primary" type="submit">Create</button>
      </form>
    </div>
  </div>
  {% endif %}

  {# ── Past competitions ── #}
  {% if past %}
  <h5 class="mb-3">Past Competitions</h5>
  <div class="card shadow-sm">
    <div class="card-body p-0">
      <table class="table table-hover table-bordered mb-0 align-middle">
        <thead>
          <tr>
            <th>#</th><th>Name</th>
            <th>Created (UTC)</th><th>Started (UTC)</th>
            <th>Ended (UTC)</th><th>Duration</th>
          </tr>
        </thead>
        <tbody>
        {% for c in past %}
          <tr>
            <td>{{ c.id }}</td>
            <td>{{ c.name }}</td>
            <td class="text-nowrap">{{ c.created_at[:19].replace('T',' ') }}</td>
            <td class="text-nowrap">{{ c.started_at[:19].replace('T',' ') if c.started_at else '—' }}</td>
            <td class="text-nowrap">{{ c.ended_at[:19].replace('T',' ') }}</td>
            <td><code>{{ duration(c) }}</code></td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}

</div>
<script>
  function downloadToken(event, inputId, compName) {
    const val = document.getElementById(inputId).value;
    if (!val) return;
    event.preventDefault();
    const blob = new Blob([val], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (compName || 'competition') + '-token.txt';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
    setTimeout(() => event.target.submit(), 200);
  }
  function generateToken(inputId) {
    const arr = new Uint8Array(16);
    crypto.getRandomValues(arr);
    document.getElementById(inputId).value =
      Array.from(arr).map(b => b.toString(16).padStart(2, '0')).join('');
  }
  function copyToken(inputId, btn) {
    const val = document.getElementById(inputId).value;
    if (!val) return;
    navigator.clipboard.writeText(val).then(() => {
      const orig = btn.textContent;
      btn.textContent = '✓';
      setTimeout(() => { btn.textContent = orig; }, 1200);
    });
  }

  // Live countdown for running robots
  function fmtSecs(secs) {
    const abs = Math.abs(secs);
    const m = Math.floor(abs / 60);
    const s = Math.floor(abs % 60);
    return m + ':' + String(s).padStart(2, '0');
  }
  function tickCountdowns() {
    const now = Date.now();
    document.querySelectorAll('.countdown[data-enabled-at]').forEach(el => {
      const enabledAt = new Date(el.dataset.enabledAt).getTime();
      const limit = parseInt(el.dataset.limit, 10);
      const remaining = limit - (now - enabledAt) / 1000;
      if (remaining > 0) {
        el.textContent = fmtSecs(remaining) + ' left';
        el.className = 'countdown fw-bold ' + (remaining < 30 ? 'text-danger' : 'text-warning');
      } else {
        el.textContent = "TIME'S UP (+" + fmtSecs(remaining) + ')';
        el.className = 'countdown fw-bold text-danger';
      }
    });
  }
  if (document.querySelector('.countdown')) {
    setInterval(tickCountdowns, 500);
    tickCountdowns();
  }
  setInterval(() => location.reload(), 5000);
</script>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>""")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# Initialize DB on startup (runs under both gunicorn and dev server)
with app.app_context():
    init_db()
    migrate_db()

if __name__ == '__main__':
    app.run(
        host=app.config['HOST'],
        port=app.config['PORT'],
        debug=False,
    )
