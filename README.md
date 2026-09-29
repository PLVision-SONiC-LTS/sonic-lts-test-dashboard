# Test Dashboard

A generic web dashboard for any project that uses an [Allure Docker Service](https://github.com/fescobar/allure-docker-service) instance: it scrapes test results, stores them in SQLite, and visualises pass rates, trends, metrics, and feature coverage across whatever dimensions you configure.

**Everything domain-specific is configured in the UI, not in code** — how project IDs are parsed into dimensions, the display labels, feature-matrix views, scraped metrics, and the suite→feature auto-map all live in the **Configuration** tab (admin-only). The bundled defaults are an **example** for PLVision SONiC Lite/X; a fresh deployment can edit them (or **Load SONiC preset** / import a config JSON) to fit its own projects. See [Configuration tab](#configuration-tab).

---

## Features

| Tab | What it shows |
|-----|--------------|
| **Dashboard** | Project cards grouped by your configured dimensions (default: cadence / platform / variant) with pass-rate bars, latest run stats, and a detail panel with history charts and a metric trend |
| **Feature Matrix** | A configurable **view**: features × project columns coverage matrix, with auto-mapping from suite names |
| **OLS** | A second configurable view (in the example: OLS-cadence projects) — just another entry in the Views config |

- LDAP / Active Directory authentication (session cookies, 8 h TTL)
- Per-user persistent display settings (hide/show values per dimension)
- Clickable run rows open the corresponding Allure report
- Configurable metric tracking (the example scrapes boot time from a reboot test's attachment)
- Docker-ready: single image runs both the scraper and the web server

---

## Quick start

### First deployment (recommended)

Use `./easy_setup.sh` for the first deploy (same as `./easy_setup.sh deploy`). It builds the image, generates a self-signed TLS cert, mounts `admins.json`, and starts the container with host networking (HTTPS on port 8080).

Before running it, prepare the local files:

1. Edit .env — set ALLURE_BASE_URL, LDAP_URI, JIRA_*, etc.
```bash
cp .env.example .env
```
2. Edit admins.json — list LDAP usernames that get the Configuration tab
3. *(Optional)* Edit the default seed constants in config_store.py
   (_SEED_DIMENSIONS, _SEED_PATTERNS, _SEED_LABELS, _SEED_VIEWS, _SEED_AUTOMAP, _SEED_METRICS, REPORT_DEFAULTS) if the bundled SONiC example does not match your projects. Seeds apply once on a fresh database; you can also change them later in the UI.
4. Execute deployment script:
```bash
./easy_setup.sh
```

Dashboard is available at **https://localhost:8080**.

`easy_setup.sh` also starts the MCP HTTP/SSE servers on **http://localhost:8001/sse** (test dashboard) and **http://localhost:8002/sse** (Jira search — set `JIRA_*` in `.env`).

To tear everything down (container, image, build cache, and the SQLite volume / DB):
> **WARNING**: `clean` command drops the DB. Backup your data first.
```bash
./easy_setup.sh clean
```

### With Docker Compose

```bash
cp .env.example .env
# Edit .env, admins.json, and optionally config_store.py (see above)

docker compose up -d --build
```

Dashboard is available at **http://localhost:8080** (Compose does not enable TLS by default).

### Without Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env, admins.json, and optionally config_store.py (see above)

# Populate the database (run once, or use --watch for continuous scraping)
python scraper.py

# Start the web server
uvicorn app:app --host 0.0.0.0 --port 8080 --reload
```

---

## Configuration

All settings are read from environment variables (or a `.env` file).

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLURE_BASE_URL` | `http://localhost:5050/allure-docker-service` | Base URL of the Allure Docker Service |
| `ALLURE_USERNAME` | _(empty)_ | HTTP basic-auth username (leave blank if security is disabled) |
| `ALLURE_PASSWORD` | _(empty)_ | HTTP basic-auth password |
| `DB_PATH` | `allure_data.db` | Path to the SQLite database file |
| `SCRAPE_INTERVAL` | `300` | Seconds between scrapes in `--watch` mode |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LDAP_URI` | _(empty)_ | LDAP server URI, e.g. `ldap://ldap.example.com`. Leave blank to disable auth entirely |
| `LDAP_BASE_DN` | _(empty)_ | LDAP base DN, e.g. `DC=example,DC=com` |
| `LDAP_USER_FILTER` | `(sAMAccountName={username})` | LDAP user search filter |
| `LDAP_DOMAIN` | _(empty)_ | AD UPN suffix, e.g. `example.com` (users log in as `username@example.com`) |

---

## Scraper

```bash
python scraper.py              # scrape once and exit
python scraper.py --watch      # scrape continuously (every SCRAPE_INTERVAL seconds)
python scraper.py --discover   # print raw API responses for debugging
```

The scraper is incremental — it skips runs already in the database.

### Data collected per project

- Summary stats (passed / failed / broken / skipped / total / duration)
- History trend (per-run result counts)
- Duration trend (per-run duration in ms)
- Full run list with timestamps
- Configured numeric metrics (e.g. boot time) scraped from test attachments — see below
- Per-test results with suite name (used by the Feature Matrix)

---

## Configurable metrics

Numeric metrics such as boot time are **config-driven** (the `config_metrics`
table, editable in **Configuration → Advanced → Metrics**). Every configured
metric is scraped into the generic `metric_values` table (one row per
run × metric × series key) — add a new metric in the UI and the scraper, the
dashboard charts and the MCP `get_metric_history` tool pick it up with no code
changes. A metric maps an Allure test attachment to stored series:

| Field | Meaning |
|-------|---------|
| `name` | metric id (also the `metric_name` stored in `metric_values`) |
| `test_names` | candidate test names; the first found in a run wins (handles renames) |
| `attachment_name` | the attachment on that test whose JSON holds the values |
| `value_keys` | `{ series_key: [candidate attachment keys] }` — each series key becomes one chart series; the first present source key wins. The **first** series key is the metric's primary series (project-tile sparkline and runs-table column) |
| `label` / `unit` | display name and unit for charts (label falls back to `name`) |

Example (the seeded default, reproducing the original SONiC boot-time scraping):

```json
{
  "name": "boot_time",
  "label": "Boot Time",
  "unit": "s",
  "test_names": ["test_boot_time_sonic_bpa_after_reboot", "test_reboot_klish"],
  "attachment_name": "boot-times",
  "value_keys": { "proc_uptime": ["port_oper", "proc_uptime"], "sonic_bpa": ["sonic_bpa"] }
}
```

> Existing databases are migrated automatically on first start: the legacy
> `boot_times` columns are copied into `metric_values` (the old table is left
> in place but no longer used).

---

## Configuration tab

Admins get a **Configuration** tab that drives all domain-specific behaviour from
the UI (stored in `config_*` tables, shared by the web app, scraper and MCP server):

| Section | What it controls |
|---------|------------------|
| **Projects** | Assign dimension values to specific project IDs directly (takes precedence over patterns — no regex needed for one-offs) |
| **Dimensions** | The axes your projects are grouped by (default: cadence, platform, variant); one is marked `title` for the card title |
| **Patterns** | Regex that parses a project ID into dimension values (first match wins), with a live tester |
| **Labels** | Display names per dimension value (e.g. `SL` → "Sonic Lite") |
| **Views** | Feature-matrix tabs: a feature scope + a column filter over dimensions (the example seeds "Feature Matrix" and "OLS") |
| **Auto-map** | suite name → feature heuristics for the ✨ Auto-map button |
| **Metrics** | Numeric values scraped from a test's attachment (see [Configurable metrics](#configurable-metrics)) |
| **Report** | HTML-report settings: title/branding strings, unit label, image-server URL, version parsing (`version_prefix`, family/tag/cadence rules, env keys), special-flavor keyword/labels, and known-issue keywords/excludes |

**Load SONiC preset** restores the bundled PLVision SONiC example, and **Export / Import**
round-trip the whole config as JSON. Defaults are seeded once per database, so your
edits and deletions are preserved across restarts.

### Project-ID parsing — the bundled example

The bundled preset parses project IDs of the form:

```
regression-{sl|sx}-{cadence}-str-{platform}-t0
```

…into the `variant` / `cadence` / `platform` dimensions. This is just the **default
example** — edit the Patterns/Dimensions in the Configuration tab to match your own
project naming. IDs that match no pattern fall back to showing the raw ID.

---

## Feature Matrix & views

Every configured **view** gets its own matrix tab — a view = a name (the tab label),
a feature scope, and a column filter over dimensions, all edited in the Configuration
tab. The example seeds a "Feature Matrix" view (non-OLS cadences) and an "OLS" view
(OLS cadences); add a third view and a third tab appears.

- **Auto-map** (`✨ Auto-map` button) — maps test cases to features using the configured suite-name heuristics
- **Manage Features** — add/delete features per scope, add/remove individual test-case mappings, import/export as JSON
- **🌱 Seed from suites** (Manage Features) — scans the current scope's projects and creates one feature per distinct suite name, with all its tests mapped
- Cells are colour-coded: green ≥ 90 %, yellow ≥ 70 %, orange ≥ 50 %, red < 50 %

Feature catalogs are not seeded in code — populate them via Manage Features
(add / import / seed-from-suites).

---

## Docker

```bash
# Build
docker build -t test-dashboard .

# Run (with a named volume for SQLite persistence)
docker run -d \
  -p 8080:8080 \
  -p 8001:8001 \
  -p 8002:8002 \
  --env-file .env \
  -e DB_PATH=/data/allure_data.db \
  -v dashboard-data:/data \
  test-dashboard
```

The container entrypoint:
1. Runs an initial scrape on startup
2. Starts `scraper.py --watch` in the background
3. Starts `mcp_server.py --http` (test dashboard MCP, port **8001**)
4. Starts `jira_mcp_server.py --http` (Jira MCP, port **8002**)
5. Starts `uvicorn` as PID 1

---

## Project structure

```
test-dashboard/
├── app.py           # FastAPI backend — API routes, auth, DB helpers
├── scraper.py       # Allure Docker Service → SQLite scraper
├── config_store.py  # DB-backed config + default seed defaults
├── admins.json      # LDAP usernames with admin (Configuration) access
├── requirements.txt
├── static/
│   └── index.html   # Single-page app (Tailwind CSS + Chart.js)
├── Dockerfile
├── entrypoint.sh
├── easy_setup.sh    # [deploy|clean] — first-deploy helper and full teardown
├── docker-compose.yml
├── mcp_server.py
├── jira_mcp_server.py
├── .env.example
└── README.md
```
