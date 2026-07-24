# PicoBot Contest Server

A **Flask + gunicorn** web application that acts as the competition-management
backend for a fleet of PicoBot robots.  
Deployed at **`https://limetka.fei.tuke.sk/picobot-contest`** via Docker and
Traefik, but can be run anywhere with a single `docker compose up -d`.

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start (Docker)](#quick-start-docker)
3. [Traefik Sub-path Deployment](#traefik-sub-path-deployment)
4. [Configuration](#configuration)
5. [Web Interface — Dashboard Tab](#web-interface--dashboard-tab)
6. [Web Interface — Competition Tab](#web-interface--competition-tab)
7. [Step-by-step Competition Workflow](#step-by-step-competition-workflow)
8. [Robot Status API](#robot-status-api)
9. [Server Command Bit Flags](#server-command-bit-flags)
10. [Database Schema](#database-schema)

---

## Overview

Every PicoBot robot periodically sends an HTTPS POST request with its current
state (MAC address, IP, servo positions, uptime). This server:

- Stores every report in a **SQLite** database.
- Identifies each robot by its **Wi-Fi MAC address** and assigns a short
  human-readable **ID** (e.g. `R-EEFF`) based on the last 4 MAC hex digits.
- Returns a single integer (`server_command`) to the robot on each request.
  The robot interprets individual bits as competition-state flags.
- Provides a **Fleet Dashboard** listing all known robots with live state
  indicators (auto-refreshes every 5 s).
- Provides a **Competition Management** page for creating timed competitions,
  managing per-robot laps with Enable / Disable controls, and archiving
  results (auto-refreshes every 5 s).

---

## Quick Start (Docker)

### Prerequisites

- Docker with the Compose plugin (`docker compose version`)
- Port 5000 available (or override with `SERVER_PORT`)

### Run

```bash
cd Server
docker compose up -d
```

The server starts on **`http://localhost:5000`**.

Open your browser at `http://localhost:5000` to view the dashboard.

### Stop / rebuild

```bash
docker compose down          # stop (database volume kept)
docker compose up -d --build # rebuild image and restart
```

The `db-data` named volume persists the SQLite database across rebuilds.

---

## Traefik Sub-path Deployment

The provided `docker-compose.yaml` is configured for deployment behind a
running **Traefik v3** instance that handles TLS termination.

### Prerequisites

- A running Traefik container joined to a `traefik-net` Docker network.
- A Traefik `dynamic.yml` file-based provider.

### 1 — Add the container to Traefik's network

The `docker-compose.yaml` already attaches the container to `traefik-net`:

```yaml
networks:
  traefik-net:
    external: true
```

### 2 — Register the route in `dynamic.yml`

Add the following to your Traefik `dynamic.yml`:

```yaml
http:
  routers:
    picobot-contest:
      rule: "Host(`your.domain.example`) && PathPrefix(`/picobot-contest`)"
      entryPoints:
        - websecure
      service: picobot-contest
      tls:
        certResolver: myresolver

  services:
    picobot-contest:
      loadBalancer:
        servers:
          - url: "http://picobot-contest:5000"
```

> **No `stripPrefix` middleware needed.** The app uses Werkzeug's
> `DispatcherMiddleware` internally to mount itself at `/picobot-contest`,
> so Traefik forwards the full path and the app handles the sub-path split.

### 3 — Start

```bash
cd Server
docker compose up -d
docker restart traefik      # reload dynamic.yml (watch=false)
```

The server is now available at
`https://your.domain.example/picobot-contest/`.

### Changing the mount path

Set the `MOUNT_PATH` environment variable:

```yaml
environment:
  - MOUNT_PATH=/my-custom-path
```

Update the Traefik route rule to match.

---

## Configuration

All settings live in **`config.py`** and can be overridden with environment
variables passed to the container.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE` | `/app/data/picobot.db` | Path to the SQLite database file |
| `REPORT_AUTH` | *(empty)* | Global bearer token. Robots must send `Authorization: Bearer <token>`. A competition-level token overrides this. |
| `MOUNT_PATH` | `/picobot-contest` | WSGI sub-path the app is mounted at |

### Matching robot configuration

The robot's `PicoBot/picobot_config.py` must point at the correct URL and
use the same bearer token:

```python
# Robot: PicoBot/picobot_config.py
REPORT_URL  = 'https://limetka.fei.tuke.sk/picobot-contest/picobot/status'
REPORT_AUTH = 'your-bearer-token'
```

---

## Web Interface — Dashboard Tab

URL: `/picobot-contest/`  
Auto-refreshes every **5 seconds**.

### Robots table

| Column | Description |
|--------|-------------|
| **ID** | Short robot identifier derived from the last 4 hex digits of the MAC address (e.g. `R-EEFF`). Click to open the robot detail page. |
| **First Seen (UTC)** | Timestamp of the robot's very first report. |
| **MAC / IP** | Full MAC address and last-seen IP address. |
| **Requests** | Total number of POST requests received from this robot. |
| **Base / Arm / Claw** | Last reported servo positions in degrees. |
| **State** | Live state badge (see below). Updates on every page refresh. |
| **Force Cmd** | Manual command override (see below). |

### State badges

| Badge | Colour | Meaning |
|-------|--------|---------|
| **Offline** | Dark | No ping received in the last 4 seconds. |
| **Running** | Blue | Robot is in `competition_running` state (command bit 1 set). |
| **Ready** | Green | Robot is in `competition_ready` state (command bit 0 set) but not running. |
| **Ready** | Green | No command bits set; robot is idle / waiting. |

### Force Cmd

The **Force Cmd** input lets you manually set the `server_command` integer
sent back to a specific robot on its next ping.

| Value | Meaning |
|-------|---------|
| `0` | Use global competition state (default) |
| `1` | Force `competition_ready = True` |
| `3` | Force `competition_ready = True` + `competition_running = True` |

> **Note:** The **Enable / Disable** buttons on the Competition tab
> automatically write to this field. In normal competition use you do not need
> to touch Force Cmd directly.

Type a value and press **✓** to apply. The robot will receive the new command
on its next ping.

---

## Web Interface — Competition Tab

URL: `/picobot-contest/competition`  
Auto-refreshes every **5 seconds**.

### Create Competition form

| Field | Description |
|-------|-------------|
| **Competition name** | Free-text label shown on the dashboard and in the past-competitions table. |
| **Time limit per robot** | How long each robot has for its lap. Options: 30 s, 1 min, 1 min 30 s, 2 min *(default)*, 3 min, 5 min, 10 min. |
| **Bearer token** | Optional per-competition token. Overrides the global `REPORT_AUTH`. Only robots that send this exact token will appear in the competition robots table. Use **Generate** to create a cryptographically random 32-character hex token. Use **📋** to copy it to the clipboard. |

### Active competition panel

Displayed when a competition exists and has not yet ended.

#### Ready state

The competition is set up. Robots that report with the correct token receive
`competition_ready = True`.

- **▶ Start Competition** — transitions to Running state.
- **✕ Cancel** — discards the competition (no results saved).
- **Token** field — update the bearer token at any time; press **Save**.

#### Running state

The competition is live. Robots receive `competition_ready = True` and
`competition_running = True`. A live elapsed timer is shown.

- **⏹ Stop Competition** — ends the competition and archives it.

### Robots table (active competition)

Appears below the active competition panel. Shows **only robots that have
authenticated with this competition's bearer token** (or any robot when no
token is set).

| Column | Description |
|--------|-------------|
| **ID** | Short robot ID (same as Dashboard, e.g. `R-EEFF`). |
| **MAC / IP** | Full MAC and IP address. |
| **Last Seen (UTC)** | When the server last received a ping from this robot. |
| **State** | Same badges as the Dashboard — reflects the command this robot is currently receiving. |
| **Start Time** | UTC timestamp when the judge pressed **Enable** for this robot. |
| **End Time** | UTC timestamp when the judge pressed **Disable** (or when the time limit expired). |
| **Result / Remaining** | Live countdown while running; final lap time when finished. Green if within the time limit, red if overtime. |
| **Action** | **▶ Enable** / **⏹ Disable** / **↺** (re-run) button. |

#### Row ordering

1. Currently running robots (Enable pressed, timer active) — top.
2. Finished robots (Disable pressed or time expired) — middle.
3. Idle robots (not yet enabled) — bottom.

### Past competitions table

Lists all ended competitions with name, timestamps, and duration.

---

## Step-by-step Competition Workflow

```
Judge                              Server                        Robots
──────────────────────────────────────────────────────────────────────
1. Open /competition
2. Enter name, time limit,          Creates competition row
   bearer token → Create            competition_ready = False

3. Distribute the bearer token
   to all robot operators
   (robots are configured to
   POST with this token)                                  Robots start
                                                          pinging →
                                    Robots appear in
                                    the Robots table

4. Press ▶ Start Competition        started_at = now      Robots receive
                                    competition_ready=1   ready=True
                                    competition_running=1 running=True

5. Robot comes to start line
6. Press ▶ Enable for that robot    enabled_at = now      Robot receives
                                    robot.command = 0x03  running=True ✓
                                    Countdown starts

7. Robot completes run
8. Press ⏹ Disable for that robot   disabled_at = now     Robot receives
                                    robot.command = 0x00  running=False
                                    Final lap time shown

   ── OR time limit expires ──
   Robot pings server               Server detects        Robot receives
                                    elapsed ≥ time_limit  command = 0x01
                                    disabled_at = now     ready, not running

9. Repeat steps 6-8 for each robot

10. Press ⏹ Stop Competition        ended_at = now        All robots revert
                                    Archived to past      to global default
                                    competitions table
```

---

## Robot Status API

### `POST /picobot-contest/picobot/status`

Receives a status report and returns the `server_command` integer.

**Headers**

| Header | Required | Value |
|--------|----------|-------|
| `Content-Type` | Yes | `application/json` |
| `Authorization` | When token is set | `Bearer <token>` |

**Request body**

```json
{
    "mac":        "aa:bb:cc:dd:ee:ff",
    "ip":         "192.168.1.42",
    "servo_base": 90,
    "servo_arm":  90,
    "servo_claw": 90,
    "uptime_ms":  12345
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mac` | string | **Yes** | Wi-Fi MAC address — robot primary key |
| `ip` | string | No | Robot's current IP address |
| `servo_base` | integer | No | Base servo angle in degrees |
| `servo_arm` | integer | No | Arm servo angle in degrees |
| `servo_claw` | integer | No | Claw servo angle in degrees |
| `uptime_ms` | integer | No | Milliseconds since boot |

**Response**

| Status | Body | Meaning |
|--------|------|---------|
| `200 OK` | Plain integer, e.g. `3` | `server_command` for this robot |
| `400 Bad Request` | Error text | Missing or malformed JSON / missing `mac` |
| `401 Unauthorized` | `Unauthorized` | Wrong or missing bearer token |

**Command determination logic (server-side)**

1. If there is an **active competition** and this robot has a running lap:
   - If elapsed time ≥ time limit → **auto-disable** (set `disabled_at`, return `0x01`)
   - Otherwise → return `0x03` (running)
2. If the robot has a **finished lap** (disabled) → return whatever command was set at disable time.
3. If there is an active competition but no lap yet → return the global competition command (`0x01` or `0x03`).
4. If no active competition → return robot's Force Cmd override, or `DEFAULT_COMMAND` (`0`).

---

## Server Command Bit Flags

The `server_command` integer uses a bitmask:

| Bit | Mask | Robot flag | Meaning |
|-----|------|-----------|---------|
| 0 (LSB) | `0x01` | `competition_ready` | Competition is prepared |
| 1 | `0x02` | `competition_running` | Competition is actively running |

**Common values**

| Value | `competition_ready` | `competition_running` | Typical source |
|-------|--------------------|-----------------------|----------------|
| `0` | False | False | No active competition / Force Cmd = 0 |
| `1` | True | False | Competition ready (not yet started) / auto-disable |
| `3` | True | True | Competition running / robot enabled by judge |

---

## Database Schema

```sql
CREATE TABLE robots (
    mac                 TEXT PRIMARY KEY,   -- "aa:bb:cc:dd:ee:ff"
    first_seen          TEXT NOT NULL,      -- ISO-8601 UTC
    last_seen           TEXT NOT NULL,      -- ISO-8601 UTC
    request_count       INTEGER NOT NULL DEFAULT 0,
    ip                  TEXT,
    servo_base          INTEGER,
    servo_arm           INTEGER,
    servo_claw          INTEGER,
    uptime_ms           INTEGER,
    command             INTEGER NOT NULL DEFAULT 0,  -- server_command bitmask
    last_competition_id INTEGER             -- last competition this robot authenticated against
);

CREATE TABLE requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mac         TEXT NOT NULL,
    received_at TEXT NOT NULL,
    servo_base  INTEGER,
    servo_arm   INTEGER,
    servo_claw  INTEGER,
    uptime_ms   INTEGER,
    FOREIGN KEY (mac) REFERENCES robots(mac)
);

CREATE TABLE competitions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    created_at          TEXT NOT NULL,      -- ISO-8601 UTC
    started_at          TEXT,               -- NULL = Ready state
    ended_at            TEXT,               -- NULL = still active
    bearer_token        TEXT,               -- per-competition token (overrides global)
    time_limit_seconds  INTEGER NOT NULL DEFAULT 120
);

CREATE TABLE competition_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    competition_id  INTEGER NOT NULL,
    mac             TEXT NOT NULL,
    enabled_at      TEXT,                   -- when judge pressed Enable
    disabled_at     TEXT,                   -- when judge pressed Disable or time expired
    FOREIGN KEY (competition_id) REFERENCES competitions(id),
    UNIQUE(competition_id, mac)             -- one run per robot per competition
);
```

The database is created automatically on first startup. Incremental migrations
are applied automatically for existing databases when the server version is
updated.
