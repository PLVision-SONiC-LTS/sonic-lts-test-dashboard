"""
Test Dashboard — FastAPI backend.

Serves:
  GET  /api/config                    → allure base URL for frontend links
  GET  /api/projects                  → all projects with latest run stats + parsed metadata
  GET  /api/projects/{id}/runs        → full run history for one project (latest first)
  GET  /api/projects/{id}/boot-times  → boot time history for one project
  POST /api/auth/login                → LDAP authenticate, sets session cookie
  POST /api/auth/logout               → clears session cookie
  GET  /api/auth/me                   → current user info or 401
  /                                   → static SPA (static/index.html)

LDAP auth is enabled when LDAP_URI is set in the environment.
If LDAP_URI is not set, all API endpoints are accessible without auth.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Optional

import requests as _requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel

import config_store
from config_store import parse_project_id
from new_fails_db import try_load_stored_for_api, try_new_counts_for_runs
from report_generator import (
    build_failed_tests_index,
    build_passed_tests_index,
    extract_jira_tickets,
    fetch_json as _rg_fetch_json,
    fetch_prev_run_stats,
    fetch_status_message,
    fetch_test_case,
    get_new_failed_tests_from_previous_report,
    get_overall_summary,
    get_report_base_url,
    get_run_cadence,
    get_version_from_json,
    get_week_number,
    iter_leaf_tests,
    MAX_NEW_PASS_ITEMS,
    process_single_report,
)

load_dotenv()

DB_PATH     = os.getenv("DB_PATH", "allure_data.db")
ADMINS_FILE = os.getenv("ADMINS_FILE", "admins.json")
ALLURE_BASE_URL = os.getenv(
    "ALLURE_BASE_URL", "http://localhost:5050/allure-docker-service"
).rstrip("/")

# ---------------------------------------------------------------------------
# Jira config
# ---------------------------------------------------------------------------

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_USERNAME = os.getenv("JIRA_USERNAME", "")
JIRA_PASSWORD = os.getenv("JIRA_PASSWORD", "")

# ---------------------------------------------------------------------------
# LDAP / auth config
# ---------------------------------------------------------------------------

LDAP_URI         = os.getenv("LDAP_URI")            # e.g. ldap://ldap.example.com
LDAP_BASE_DN     = os.getenv("LDAP_BASE_DN")         # e.g. DC=example,DC=com
LDAP_USER_FILTER = os.getenv("LDAP_USER_FILTER", "(uid={username})")
LDAP_DOMAIN      = os.getenv("LDAP_DOMAIN")          # e.g. example.com  (AD UPN suffix)

SESSION_TTL = timedelta(hours=8)
COOKIE_NAME = "td_session"

# Sessions are stored in the DB (table `sessions`) so they are shared across
# uvicorn workers and survive restarts — an in-memory dict breaks with >1 worker.


_LDAP_TIMEOUT = 10  # seconds

def _ldap_authenticate(username: str, password: str) -> bool:
    """Return True if credentials are accepted by the LDAP server."""
    from ldap3 import NONE, Connection, Server  # imported lazily
    # get_info=NONE: a bind only needs to verify credentials — don't download the
    # server's DSA info/schema (huge on Active Directory; was making login ~30s+).
    server = Server(LDAP_URI, get_info=NONE, connect_timeout=_LDAP_TIMEOUT)
    # Active Directory: bind as username@domain (UPN)
    # Generic LDAP: bind directly with the username string
    bind_dn = f"{username}@{LDAP_DOMAIN}" if LDAP_DOMAIN else username
    try:
        conn = Connection(
            server,
            user=bind_dn,
            password=password,
            auto_bind=True,
            receive_timeout=_LDAP_TIMEOUT,
        )
        conn.unbind()
        return True
    except Exception:
        return False


def _get_session_user(session: Optional[str] = Cookie(None, alias=COOKIE_NAME)) -> Optional[str]:
    """Return the username for a valid session cookie, or None."""
    if not LDAP_URI:
        return "anonymous"   # auth disabled
    if not session:
        return None
    db = get_db()
    try:
        row = db.execute(
            "SELECT username, expires_at FROM sessions WHERE token = ?", (session,)
        ).fetchone()
        if not row:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
            db.execute("DELETE FROM sessions WHERE token = ?", (session,))
            db.commit()
            return None
        return row["username"]
    finally:
        db.close()


def require_auth(username: Optional[str] = Depends(_get_session_user)) -> str:
    if username is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username


def _load_admins() -> set[str]:
    """Read admins.json and return the set of admin usernames.

    The file is re-read on every call so edits take effect without a restart.
    When LDAP auth is disabled every user is treated as admin.
    """
    if not LDAP_URI:
        return {"anonymous"}
    try:
        with open(ADMINS_FILE) as f:
            data = json.load(f)
        return set(data.get("admins", []))
    except (OSError, json.JSONDecodeError):
        return set()


def is_admin(username: str = Depends(require_auth)) -> bool:
    return username in _load_admins()


def require_admin(username: str = Depends(require_auth)) -> str:
    if username not in _load_admins():
        raise HTTPException(status_code=403, detail="Admin access required")
    return username


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Test Dashboard API")

# ---------------------------------------------------------------------------
# Auth endpoints
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(body: LoginRequest, response: Response):
    if not LDAP_URI:
        # Auth disabled — return a no-op success
        return {"username": "anonymous"}

    if not body.username or not body.password:
        raise HTTPException(status_code=400, detail="Username and password required")

    if not _ldap_authenticate(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    now_utc = datetime.now(timezone.utc).isoformat()
    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO user_logins (username, last_login_at, login_count)
            VALUES (?, ?, 1)
            ON CONFLICT(username) DO UPDATE SET
                last_login_at = excluded.last_login_at,
                login_count = login_count + 1
            """,
            (body.username, now_utc),
        )
        db.execute(
            "INSERT INTO user_login_log (username, login_at) VALUES (?, ?)",
            (body.username, now_utc),
        )
        # Keep only the 5000 most recent log entries
        db.execute(
            """
            DELETE FROM user_login_log WHERE id IN (
                SELECT id FROM user_login_log ORDER BY login_at DESC LIMIT 5000 OFFSET 5000
            )
            """
        )
        db.commit()
    finally:
        db.close()

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + SESSION_TTL).isoformat()
    sdb = get_db()
    try:
        sdb.execute(
            "INSERT OR REPLACE INTO sessions (token, username, expires_at) VALUES (?, ?, ?)",
            (token, body.username, expires_at),
        )
        sdb.commit()
    finally:
        sdb.close()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=int(SESSION_TTL.total_seconds()),
    )
    return {"username": body.username}


@app.post("/api/auth/logout")
def logout(response: Response, session: Optional[str] = Cookie(None, alias=COOKIE_NAME)):
    if session:
        db = get_db()
        try:
            db.execute("DELETE FROM sessions WHERE token = ?", (session,))
            db.commit()
        finally:
            db.close()
    response.delete_cookie(COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/me")
def me(username: str = Depends(require_auth)):
    return {"username": username, "is_admin": username in _load_admins()}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def _init_db_once() -> None:
    """Create application tables that may not exist yet."""
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_settings (
            username      TEXT PRIMARY KEY,
            settings_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            username   TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_logins (
            username      TEXT PRIMARY KEY,
            last_login_at TEXT NOT NULL,
            login_count   INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS user_login_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            username  TEXT NOT NULL,
            login_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_user_login_log_login_at ON user_login_log(login_at DESC);

        CREATE TABLE IF NOT EXISTS features (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT    NOT NULL,
            feature_group TEXT    NOT NULL DEFAULT '',
            sort_order    INTEGER NOT NULL DEFAULT 0,
            feature_scope TEXT    NOT NULL DEFAULT 'standard',
            UNIQUE (name, feature_scope)
        );

        CREATE TABLE IF NOT EXISTS feature_mappings (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_id INTEGER NOT NULL REFERENCES features(id) ON DELETE CASCADE,
            test_name  TEXT    NOT NULL,
            UNIQUE (feature_id, test_name)
        );

        CREATE INDEX IF NOT EXISTS idx_feature_mappings ON feature_mappings(feature_id);
        CREATE INDEX IF NOT EXISTS idx_feature_mappings_name ON feature_mappings(test_name);
        """
    )
    # Migrate: add feature_scope column to existing databases
    try:
        db.execute("ALTER TABLE features ADD COLUMN feature_scope TEXT NOT NULL DEFAULT 'standard'")
        db.commit()
    except Exception:
        pass  # column already exists

    # Migrate: add build_version column to runs table
    try:
        db.execute("ALTER TABLE runs ADD COLUMN build_version TEXT")
        db.commit()
    except Exception:
        pass  # column already exists

    # Migrate: add test_params column to test_results (scraper populates it)
    try:
        db.execute("ALTER TABLE test_results ADD COLUMN test_params TEXT NOT NULL DEFAULT ''")
        db.commit()
    except Exception:
        pass  # column already exists

    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS run_new_fails (
            project_id                  TEXT    NOT NULL,
            report_number               INTEGER NOT NULL,
            compared_to_report_number  INTEGER,
            new_fail_count              INTEGER NOT NULL DEFAULT 0,
            new_pass_count              INTEGER NOT NULL DEFAULT 0,
            new_fails_note              TEXT    NOT NULL DEFAULT '',
            payload_json                TEXT    NOT NULL DEFAULT '[]',
            computed_at                 TEXT    NOT NULL,
            PRIMARY KEY (project_id, report_number)
        );
        """
    )

    # Migrate: change features UNIQUE(name) → UNIQUE(name, feature_scope) so
    # OLS and standard features can share names without a 409 conflict.
    schema_row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='features'"
    ).fetchone()
    if schema_row and "UNIQUE (name, feature_scope)" not in schema_row["sql"]:
        db.executescript("""
            CREATE TABLE features_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT    NOT NULL,
                feature_group TEXT    NOT NULL DEFAULT '',
                sort_order    INTEGER NOT NULL DEFAULT 0,
                feature_scope TEXT    NOT NULL DEFAULT 'standard',
                UNIQUE (name, feature_scope)
            );
            INSERT OR IGNORE INTO features_new
                SELECT id, name, feature_group, sort_order, feature_scope FROM features;
            DROP TABLE features;
            ALTER TABLE features_new RENAME TO features;
            CREATE INDEX IF NOT EXISTS idx_feature_mappings ON feature_mappings(feature_id);
            CREATE INDEX IF NOT EXISTS idx_feature_mappings_name ON feature_mappings(test_name);
        """)

    # Feature catalogs are not seeded in code — populate them via Manage
    # Features (import / add / seed-from-suites) or a config preset.

    # Create + seed the UI-configurable parsing/labels config tables.
    config_store.ensure_schema(db)

    db.commit()
    db.close()


def _init_db() -> None:
    """Run schema init, retrying briefly if the DB is momentarily locked by the
    scraper (the entrypoint starts the scraper before the web app)."""
    for attempt in range(30):
        try:
            _init_db_once()
            return
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < 29:
                time.sleep(2)
                continue
            raise


_init_db()


# ---------------------------------------------------------------------------
# Project ID parsing
#
# parse_project_id is imported from config_store (DB-backed, UI-configurable).
# It returns the same shape as before — top-level <dim>/<dim>_label keys for
# every configured dimension (variant/cadence/platform by default) plus
# `dimensions` and `labels` maps — so existing call sites are unchanged.
# ---------------------------------------------------------------------------


def row_to_stats(row) -> dict:
    total  = row["total"]  or 0
    passed = row["passed"] or 0
    keys   = row.keys() if hasattr(row, "keys") else []
    return {
        "report_number": row["report_number"],
        "total":    total,
        "passed":   passed,
        "failed":   row["failed"]  or 0,
        "broken":   row["broken"]  or 0,
        "skipped":  row["skipped"] or 0,
        "unknown":  row["unknown"] or 0,
        "pass_pct": round(passed * 100.0 / total, 1) if total else None,
        "duration_ms":   row["duration_ms"],
        "start_ms":      row["start_ms"],
        "stop_ms":       row["stop_ms"],
        "build_version": row["build_version"] if "build_version" in keys else None,
    }


# ---------------------------------------------------------------------------
# API routes  (all protected — require_auth dependency)
# ---------------------------------------------------------------------------

@app.get("/api/config")
def get_config(_: str = Depends(require_auth)):
    """Client config: Allure base URL plus the parsed dimension/label config.

    Backward compatible — `allure_base_url` is unchanged; the dimension/label
    fields are additive and drive the (later) dimension-aware UI.
    """
    cfg = config_store.get_config(get_db())
    return {
        "allure_base_url": ALLURE_BASE_URL,
        "jira_base_url": JIRA_BASE_URL,
        "config_version": cfg.version,
        "dimensions": cfg.raw_dimensions,
        "labels": cfg.labels,
        "views": cfg.views,
        "metrics": [config_store.metric_display(m) for m in cfg.metrics],
    }


# --- Admin: parsing/labels configuration (edited from the UI) ---------------

class ConfigDimensionBody(BaseModel):
    key: str
    label: str = ""
    sort_order: int = 0
    role: str = ""   # 'title' marks the card-title dimension

class ConfigPatternBody(BaseModel):
    name: str
    regex: str
    group_map: dict[str, str] = {}
    constants: dict[str, str] = {}
    transforms: dict[str, str] = {}
    sort_order: int = 0

class ConfigViewBody(BaseModel):
    name: str
    scope: str = "standard"
    filter: dict = {}
    sort_order: int = 0

class ConfigDocumentBody(BaseModel):
    dimensions: list[ConfigDimensionBody]
    patterns: list[ConfigPatternBody] = []
    labels: dict[str, dict[str, str]] = {}
    project_map: dict[str, dict[str, str]] = {}
    views: Optional[list[ConfigViewBody]] = None   # None => keep existing views
    automap: Optional[dict[str, str]] = None       # None => keep existing auto-map
    metrics: Optional[list[dict]] = None           # None => keep existing metrics
    report: Optional[dict] = None                  # None => keep existing report settings

class PatternTestBody(BaseModel):
    patterns: list[ConfigPatternBody]
    project_ids: Optional[list[str]] = None  # default: distinct IDs from runs


@app.get("/api/admin/config")
def admin_get_config(_: str = Depends(require_admin)):
    """Full editable config (also used as the export payload)."""
    return config_store.dump_config(get_db())


@app.put("/api/admin/config")
def admin_put_config(body: ConfigDocumentBody, _: str = Depends(require_admin)):
    """Validate and replace the whole parsing config (save + import). Bumps version."""
    try:
        version = config_store.replace_config(
            get_db(),
            dimensions=[d.model_dump() for d in body.dimensions],
            patterns=[p.model_dump() for p in body.patterns],
            labels=body.labels,
            project_map=body.project_map,
            views=([v.model_dump() for v in body.views] if body.views is not None else None),
            automap=body.automap,
            metrics=body.metrics,
            report=body.report,
        )
    except config_store.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"version": version}


@app.post("/api/admin/config/test-pattern")
def admin_test_pattern(body: PatternTestBody, _: str = Depends(require_admin)):
    """Preview how the candidate patterns match real project IDs (live tester)."""
    db = get_db()
    pids = body.project_ids
    if pids is None:
        pids = [r[0] for r in db.execute("SELECT DISTINCT project_id FROM runs ORDER BY 1")]
    return {"results": config_store.test_patterns([p.model_dump() for p in body.patterns], pids)}


@app.get("/api/projects")
def list_projects(_: str = Depends(require_auth)):
    """Return all projects with latest-run stats, parsed metadata, and pass_pct trend."""
    db = get_db()
    rows = db.execute(
        """
        SELECT r.project_id,
               r.report_number,
               r.total, r.passed, r.failed, r.broken, r.skipped, r.unknown,
               r.duration_ms, r.start_ms, r.stop_ms, r.build_version
        FROM runs r
        JOIN (
            SELECT project_id, max(report_number) AS max_num
            FROM runs GROUP BY project_id
        ) latest
          ON r.project_id = latest.project_id
         AND r.report_number = latest.max_num
        ORDER BY r.project_id
        """
    ).fetchall()

    # Fetch last 15 pass_pct values per project (oldest first) for sparkline
    trend_rows = db.execute(
        """
        SELECT project_id, passed, total, skipped
        FROM (
            SELECT project_id, passed, total, skipped, report_number,
                   ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY report_number DESC) AS rn
            FROM runs
        ) t
        WHERE rn <= 15
        ORDER BY project_id, report_number ASC
        """
    ).fetchall()
    trend_map: dict[str, list] = {}
    trend_total_map: dict[str, list] = {}
    trend_passed_map: dict[str, list] = {}
    trend_skipped_map: dict[str, list] = {}
    for tr in trend_rows:
        pid = tr["project_id"]
        tot = tr["total"] or 0
        psd = tr["passed"] or 0
        skp = tr["skipped"] or 0
        pct = round(psd * 100.0 / tot, 1) if tot else None
        trend_map.setdefault(pid, []).append(pct)
        trend_total_map.setdefault(pid, []).append(tot or None)
        trend_passed_map.setdefault(pid, []).append(psd)
        trend_skipped_map.setdefault(pid, []).append(skp)

    # Latest value + last-15 trend of the first configured metric's primary
    # series per project, for the tile sparkline.
    cfg = config_store.get_config(db)
    primary_metric = config_store.metric_display(cfg.metrics[0]) if cfg.metrics else None
    metric_latest_map: dict[str, dict] = {}
    metric_trend_map: dict[str, list] = {}
    if primary_metric and primary_metric["primary_key"]:
        mkey = (primary_metric["name"], primary_metric["primary_key"])
        metric_latest_rows = db.execute(
            """
            SELECT m.project_id, m.value, m.test_status
            FROM metric_values m
            JOIN (
                SELECT project_id, MAX(report_number) AS max_num
                FROM metric_values
                WHERE metric_name = ? AND value_key = ? AND value IS NOT NULL
                GROUP BY project_id
            ) lb ON m.project_id = lb.project_id AND m.report_number = lb.max_num
            WHERE m.metric_name = ? AND m.value_key = ?
            """,
            mkey + mkey,
        ).fetchall()
        metric_latest_map = {
            row["project_id"]: {"value": row["value"], "test_status": row["test_status"]}
            for row in metric_latest_rows
        }

        metric_trend_rows = db.execute(
            """
            SELECT project_id, value
            FROM (
                SELECT project_id, value, report_number,
                       ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY report_number DESC) AS rn
                FROM metric_values
                WHERE metric_name = ? AND value_key = ? AND value IS NOT NULL
            ) t
            WHERE rn <= 15
            ORDER BY project_id, report_number ASC
            """,
            mkey,
        ).fetchall()
        for row in metric_trend_rows:
            metric_trend_map.setdefault(row["project_id"], []).append(row["value"])

    result = []
    for row in rows:
        pid  = row["project_id"]
        meta = parse_project_id(pid)
        result.append(
            {
                "id":             pid,
                "variant":        meta["variant"],
                "variant_label":  meta["variant_label"],
                "cadence":        meta["cadence"],
                "cadence_label":  meta["cadence_label"],
                "platform":       meta["platform"],
                "platform_label": meta["platform_label"],
                "dimensions":     meta["dimensions"],
                "labels":         meta["labels"],
                "latest_run":     row_to_stats(row),
                "trend":          trend_map.get(pid, []),
                "trend_total":    trend_total_map.get(pid, []),
                "trend_passed":   trend_passed_map.get(pid, []),
                "trend_skipped":  trend_skipped_map.get(pid, []),
                "metric_latest":  metric_latest_map.get(pid),
                "metric_trend":   metric_trend_map.get(pid, []),
            }
        )
    return result


@app.get("/api/projects/{project_id}/runs")
def get_runs(project_id: str, limit: int = 60, _: str = Depends(require_auth)):
    """Return run history for one project, latest first (newest report_number first)."""
    db = get_db()
    rows = db.execute(
        """
        SELECT report_number, total, passed, failed, broken, skipped, unknown,
               duration_ms, start_ms, stop_ms, build_version
        FROM runs
        WHERE project_id = ?
        ORDER BY report_number DESC
        LIMIT ?
        """,
        (project_id, limit),
    ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="Project not found or no runs")

    return [row_to_stats(r) for r in rows]


# In-memory cache for new-fails results (project_id, report_number) -> { data, expires_at }
# TTL so Allure updates are eventually visible without restart
_NEW_FAILS_CACHE: dict[tuple[str, int], dict] = {}
_NEW_FAILS_CACHE_LOCK = threading.Lock()
_NEW_FAILS_CACHE_TTL_SEC = int(os.getenv("NEW_FAILS_CACHE_TTL_SEC", "600"))  # 10 min default

# In-memory cache for skipped-tests results
_SKIPPED_CACHE: dict[tuple[str, int], dict] = {}
_SKIPPED_CACHE_LOCK = threading.Lock()
_SKIPPED_CACHE_TTL_SEC = int(os.getenv("SKIPPED_CACHE_TTL_SEC", "600"))  # 10 min default

# Statuses whose Allure status messages are scanned for Jira ticket ids.
# Skipped alone misses xfail rows and failures that cite a ticket.
_JIRA_TEST_STATUSES = ("skipped", "xfail", "failed", "broken")

# In-memory cache for the Jira-tab test source (same TTL as skipped tests)
_JIRA_TESTS_CACHE: dict[tuple[str, int], dict] = {}
_JIRA_TESTS_CACHE_LOCK = threading.Lock()


def _get_new_fails_cached(project_id: str, report_number: int) -> dict:
    """Return cached or freshly fetched new_failed_tests, new_fails_note, new_pass_count. Raises on fetch error."""
    key = (project_id, report_number)
    now = time.monotonic()
    with _NEW_FAILS_CACHE_LOCK:
        entry = _NEW_FAILS_CACHE.get(key)
        if entry and entry["expires_at"] > now:
            return entry["data"]

    db_try = get_db()
    try:
        stored = try_load_stored_for_api(db_try, project_id, report_number)
    finally:
        db_try.close()
    if stored is not None:
        with _NEW_FAILS_CACHE_LOCK:
            _NEW_FAILS_CACHE[key] = {"data": stored, "expires_at": now + _NEW_FAILS_CACHE_TTL_SEC}
        return stored

    report_index_url = f"{ALLURE_BASE_URL}/projects/{project_id}/reports/{report_number}/index.html"
    report_base = get_report_base_url(report_index_url)
    try:
        suites_json = _rg_fetch_json(f"{report_base}data/suites.json")
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Could not fetch report data: {e!s}",
        )
    new_fails, new_passes, _prev_url, new_fails_note, total_new_fail_count = get_new_failed_tests_from_previous_report(
        report_index_url,
        suites_json,
    )
    data = {
        "new_failed_tests": new_fails,
        "new_fails_note": new_fails_note,
        "new_pass_count": len(new_passes),
        "total_new_fail_count": total_new_fail_count,
    }
    with _NEW_FAILS_CACHE_LOCK:
        _NEW_FAILS_CACHE[key] = {"data": data, "expires_at": now + _NEW_FAILS_CACHE_TTL_SEC}
    return data


@app.get("/api/projects/{project_id}/runs/{report_number}/new-fails")
def get_run_new_fails(
    project_id: str,
    report_number: int,
    _: str = Depends(require_auth),
):
    """Return new failed tests (vs previous run) and fail reason for a given run. Cached server-side."""
    data = _get_new_fails_cached(project_id, report_number)
    return {
        "new_failed_tests": data["new_failed_tests"],
        "new_fails_note": data["new_fails_note"],
        "total_new_fail_count": data.get("total_new_fail_count", len(data["new_failed_tests"])),
    }


_ALL_FAILS_CACHE: dict[tuple[str, int], dict] = {}
_ALL_FAILS_CACHE_LOCK = threading.Lock()


def _get_all_fails_cached(project_id: str, report_number: int) -> dict:
    """Return cached or freshly fetched all failed/broken tests with status messages."""
    key = (project_id, report_number)
    now = time.monotonic()
    with _ALL_FAILS_CACHE_LOCK:
        entry = _ALL_FAILS_CACHE.get(key)
        if entry and entry["expires_at"] > now:
            return entry["data"]

    report_index_url = f"{ALLURE_BASE_URL}/projects/{project_id}/reports/{report_number}/index.html"
    report_base = get_report_base_url(report_index_url)
    try:
        suites_json = _rg_fetch_json(f"{report_base}data/suites.json")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch report data: {e!s}")

    failed_index = build_failed_tests_index(suites_json)
    all_fails = []
    for test_name in sorted(failed_index):
        t = failed_index[test_name]
        all_fails.append({
            "name": t["name"],
            "uid": t.get("uid"),
            "status": t["status"],
            "statusMessage": fetch_status_message(report_base, t.get("uid")),
        })

    data = {"all_failed_tests": all_fails, "total": len(all_fails)}
    with _ALL_FAILS_CACHE_LOCK:
        _ALL_FAILS_CACHE[key] = {"data": data, "expires_at": now + _NEW_FAILS_CACHE_TTL_SEC}
    return data


@app.get("/api/projects/{project_id}/runs/{report_number}/all-fails")
def get_run_all_fails(
    project_id: str,
    report_number: int,
    _: str = Depends(require_auth),
):
    """Return all failed/broken tests with status messages for a given run. Cached server-side."""
    data = _get_all_fails_cached(project_id, report_number)
    return data


def _load_tests_with_status_messages(
    project_id: str,
    report_number: int,
    statuses: tuple[str, ...],
) -> list:
    """Load stored tests for the given statuses and attach Allure status messages."""
    if not statuses:
        return []

    db = get_db()
    placeholders = ",".join("?" * len(statuses))
    rows = db.execute(
        f"""
        SELECT test_name, test_params, suite_name, uid, status
        FROM test_results
        WHERE project_id = ? AND report_number = ?
          AND lower(status) IN ({placeholders})
        ORDER BY suite_name, test_name
        """,
        (project_id, report_number, *[s.lower() for s in statuses]),
    ).fetchall()
    db.close()

    report_base = get_report_base_url(
        f"{ALLURE_BASE_URL}/projects/{project_id}/reports/{report_number}/index.html"
    )

    def fetch_one(row):
        msg = fetch_status_message(report_base, row["uid"]) if row["uid"] else ""
        return {
            "name": row["test_name"] + (row["test_params"] or ""),
            "suite_name": row["suite_name"],
            "uid": row["uid"],
            "status": (row["status"] or "").lower(),
            "statusMessage": msg,
        }

    if not rows:
        return []
    max_workers = min(10, len(rows))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(fetch_one, rows))


def _get_skipped_cached(project_id: str, report_number: int) -> list:
    """Return cached or freshly fetched skipped tests with status messages."""
    key = (project_id, report_number)
    now = time.monotonic()
    with _SKIPPED_CACHE_LOCK:
        entry = _SKIPPED_CACHE.get(key)
        if entry and entry["expires_at"] > now:
            return entry["data"]

    data = _load_tests_with_status_messages(project_id, report_number, ("skipped",))

    with _SKIPPED_CACHE_LOCK:
        _SKIPPED_CACHE[key] = {"data": data, "expires_at": now + _SKIPPED_CACHE_TTL_SEC}
    return data


@app.get("/api/projects/{project_id}/runs/{report_number}/skipped")
def get_run_skipped(
    project_id: str,
    report_number: int,
    _: str = Depends(require_auth),
):
    """Return skipped tests with their skip reason for a given run. Cached server-side."""
    data = _get_skipped_cached(project_id, report_number)
    return {"skipped_tests": data}


def _db_jira_candidates(project_id: str, report_number: int) -> list:
    """Stored tests whose Allure status may cite a Jira ticket. No HTTP."""
    db = get_db()
    try:
        placeholders = ",".join("?" * len(_JIRA_TEST_STATUSES))
        rows = db.execute(
            f"""
            SELECT test_name, test_params, suite_name, uid, status
            FROM test_results
            WHERE project_id = ? AND report_number = ?
              AND lower(status) IN ({placeholders})
            ORDER BY suite_name, test_name
            """,
            (project_id, report_number, *[s.lower() for s in _JIRA_TEST_STATUSES]),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    return [
        {
            "name": row["test_name"] + (row["test_params"] or ""),
            "suite_name": row["suite_name"],
            "uid": row["uid"],
            "status": (row["status"] or "").lower(),
        }
        for row in rows
    ]


def _suites_jira_candidates(suites_json) -> list:
    """Leaf tests from suites.json. Used when the DB has no uid to fetch with."""
    found = []
    for node in iter_leaf_tests(suites_json):
        status = (node.get("status") or "").lower()
        tags = [str(t).lower() for t in (node.get("tags") or [])]
        if status not in _JIRA_TEST_STATUSES and not any(t in ("xfail", "xpass") for t in tags):
            continue
        uid = node.get("uid")
        if not uid:
            continue
        found.append({
            "name": node.get("name") or "",
            "suite_name": "",
            "uid": uid,
            "status": status or "xfail",
        })
    return found


def _get_jira_tests_cached(project_id: str, report_number: int) -> list:
    """Return tests that cite a Jira ticket in the Allure status text.

    Pytest xfail is stored as Allure status "skipped". The ticket url is in
    statusMessage or, when that field is empty, in statusTrace / testStage —
    the same text the Allure overview shows. Passed tests are not scanned.
    """
    key = (project_id, report_number)
    now = time.monotonic()
    with _JIRA_TESTS_CACHE_LOCK:
        entry = _JIRA_TESTS_CACHE.get(key)
        if entry and entry["expires_at"] > now:
            return entry["data"]

    candidates = _db_jira_candidates(project_id, report_number)
    if not candidates or any(not item.get("uid") for item in candidates):
        report_index_url = f"{ALLURE_BASE_URL}/projects/{project_id}/reports/{report_number}/index.html"
        try:
            suites_json = _rg_fetch_json(f"{get_report_base_url(report_index_url)}data/suites.json")
            from_suites = _suites_jira_candidates(suites_json)
            if from_suites:
                candidates = from_suites
        except Exception:
            candidates = [item for item in candidates if item.get("uid")]

    report_base = get_report_base_url(
        f"{ALLURE_BASE_URL}/projects/{project_id}/reports/{report_number}/index.html"
    )

    def fetch_one(item):
        case = fetch_test_case(report_base, item.get("uid"))
        tickets = extract_jira_tickets(case)
        if not tickets:
            return None
        return {
            "name": item["name"],
            "suite_name": item.get("suite_name") or "",
            "uid": item.get("uid"),
            "status": item.get("status") or "",
            "tickets": tickets,
        }

    if candidates:
        max_workers = min(10, len(candidates))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            data = [row for row in executor.map(fetch_one, candidates) if row]
    else:
        data = []

    with _JIRA_TESTS_CACHE_LOCK:
        _JIRA_TESTS_CACHE[key] = {"data": data, "expires_at": now + _SKIPPED_CACHE_TTL_SEC}
    return data


@app.get("/api/projects/{project_id}/runs/{report_number}/jira-tests")
def get_run_jira_tests(
    project_id: str,
    report_number: int,
    _: str = Depends(require_auth),
):
    """Return skipped, xfail, failed, and broken tests with status messages.

    The Jira tab extracts ticket ids from these messages. Cached server-side.
    """
    data = _get_jira_tests_cached(project_id, report_number)
    return {"tests": data}


_JIRA_CACHE: dict[str, dict] = {}
_JIRA_CACHE_LOCK = threading.Lock()
_JIRA_CACHE_TTL_SEC = int(os.getenv("JIRA_CACHE_TTL_SEC", "3600"))  # 1 hour


_JIRA_TICKET_RE = re.compile(r'\b(?!UTF-\d+\b)([A-Z]{2,5}-\d+)\b')
_JIRA_URL_RE    = re.compile(r'https?://[^\s<>&"]+/browse/([A-Z]+-\d+)[^\s<>&"]*', re.IGNORECASE)


def _fetch_jira_statuses(ticket_list: list[str]) -> dict[str, dict]:
    """Fetch Jira status+summary for a list of ticket IDs. Uses cache; silently returns {} on error."""
    if not JIRA_BASE_URL or not JIRA_USERNAME or not JIRA_PASSWORD or not ticket_list:
        return {}

    now = time.monotonic()
    result: dict[str, dict] = {}
    to_fetch: list[str] = []

    with _JIRA_CACHE_LOCK:
        for t in ticket_list:
            entry = _JIRA_CACHE.get(t)
            if entry and entry["expires_at"] > now:
                result[t] = entry["data"]
            else:
                to_fetch.append(t)

    if to_fetch:
        jql = f"issueKey in ({','.join(to_fetch)})"
        try:
            resp = _requests.get(
                f"{JIRA_BASE_URL}/rest/api/2/search",
                params={"jql": jql, "fields": "status,summary", "maxResults": len(to_fetch)},
                auth=(JIRA_USERNAME, JIRA_PASSWORD),
                timeout=10,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return result  # return whatever we got from cache

        with _JIRA_CACHE_LOCK:
            for issue in data.get("issues", []):
                key = issue["key"]
                fields = issue.get("fields", {})
                status = fields.get("status", {})
                info = {
                    "status": status.get("name", ""),
                    "color": status.get("statusCategory", {}).get("colorName", ""),
                    "summary": fields.get("summary", ""),
                }
                _JIRA_CACHE[key] = {"data": info, "expires_at": now + _JIRA_CACHE_TTL_SEC}
                result[key] = info

    return result


def _jinja_linkify_jira(text: str, jira_statuses: dict | None = None) -> str:
    """Jinja2 filter: linkify Jira ticket IDs/URLs and optionally embed status badges."""
    import html as _html
    if not text:
        return ""
    base = JIRA_BASE_URL + "/browse/" if JIRA_BASE_URL else ""
    escaped = _html.escape(text)

    def _badge(ticket: str) -> str:
        if not jira_statuses:
            return ""
        info = jira_statuses.get(ticket)
        if not info or not info.get("status"):
            return ""
        color_map = {"green": "#22c55e", "yellow": "#eab308", "blue-grey": "#94a3b8"}
        color = color_map.get(info["color"], "#94a3b8")
        return (f' <span style="display:inline-block;padding:0 4px;border-radius:3px;'
                f'font-size:10px;font-weight:600;background:{color}22;color:{color};'
                f'border:1px solid {color}44" title="{_html.escape(info.get("summary",""))}">'
                f'{_html.escape(info["status"])}</span>')

    def replace_url(m: re.Match) -> str:
        ticket = m.group(1).upper()
        return f'<a href="{m.group(0)}" target="_blank" style="color:#60a5fa">{ticket} ↗</a>{_badge(ticket)}'

    escaped = _JIRA_URL_RE.sub(replace_url, escaped)

    def replace_parts(m: re.Match) -> str:
        link, ticket = m.group(1), m.group(2)
        if link:
            return link
        if not base:
            return ticket + _badge(ticket)
        return f'<a href="{base}{ticket}" target="_blank" style="color:#60a5fa">{ticket} ↗</a>{_badge(ticket)}'

    return re.sub(r'(<a[\s\S]*?</a>(?:<span[^>]*>.*?</span>)?)|(\b(?!UTF-\d+\b)[A-Z]{2,5}-\d+\b)', replace_parts, escaped)


def _jinja_inject_jira_badges(html: str, jira_statuses: dict | None = None) -> str:
    """Jinja2 filter: inject status badges after existing Jira links in already-HTML content."""
    import html as _html
    if not html or not jira_statuses:
        return html

    def _badge(ticket: str) -> str:
        info = jira_statuses.get(ticket.upper())
        if not info or not info.get("status"):
            return ""
        color_map = {"green": "#22c55e", "yellow": "#eab308", "blue-grey": "#94a3b8"}
        color = color_map.get(info["color"], "#94a3b8")
        return (f' <span style="display:inline-block;padding:0 4px;border-radius:3px;'
                f'font-size:10px;font-weight:600;background:{color}22;color:{color};'
                f'border:1px solid {color}44" title="{_html.escape(info.get("summary",""))}">'
                f'{_html.escape(info["status"])}</span>')

    def replace_link(m: re.Match) -> str:
        href_match = re.search(r'browse/([A-Z]+-\d+)', m.group(0), re.IGNORECASE)
        if href_match:
            return m.group(0) + _badge(href_match.group(1))
        return m.group(0)

    return re.sub(r'<a\b[^>]+>.*?</a>', replace_link, html)


def _make_jinja_env() -> "Environment":
    """Return a Jinja2 Environment with custom filters registered."""
    env = Environment(loader=FileSystemLoader(_REPORT_TEMPLATE_DIR))
    env.filters["linkify_jira"] = _jinja_linkify_jira
    env.filters["inject_jira_badges"] = _jinja_inject_jira_badges
    return env


@app.get("/api/jira/issues")
def get_jira_issues(tickets: str = Query(...), _: str = Depends(require_auth)):
    """Return Jira status+summary for comma-separated ticket IDs. Results cached 1 hour."""
    if not JIRA_BASE_URL or not JIRA_USERNAME or not JIRA_PASSWORD:
        raise HTTPException(status_code=503, detail="Jira not configured")
    ticket_list = [t.strip().upper() for t in tickets.split(",") if t.strip()]
    return _fetch_jira_statuses(ticket_list)


def _fetch_new_counts_for_run(project_id: str, report_number: int) -> tuple[int, int, int]:
    """Return (report_number, new_fail_count, new_pass_count) for one run. Uses cache. On error returns (report_number, 0, 0)."""
    try:
        data = _get_new_fails_cached(project_id, report_number)
        nf = data.get("total_new_fail_count", len(data["new_failed_tests"]))
        np = data["new_pass_count"]
        return (report_number, nf, np)
    except HTTPException:
        return (report_number, 0, 0)
    except Exception:
        return (report_number, 0, 0)


@app.get("/api/projects/{project_id}/runs/new-counts")
def get_runs_new_counts(
    project_id: str,
    limit: int = 60,
    _: str = Depends(require_auth),
):
    """Return new_fail_count and new_pass_count per run (vs previous run) for chart. Same run order as get_runs (latest first)."""
    db = get_db()
    rows = db.execute(
        """
        SELECT report_number
        FROM runs
        WHERE project_id = ?
        ORDER BY report_number DESC
        LIMIT ?
        """,
        (project_id, limit),
    ).fetchall()
    if not rows:
        db.close()
        return []
    report_numbers = [r["report_number"] for r in rows]
    batch = try_new_counts_for_runs(db, project_id, report_numbers)
    db.close()
    if batch is not None:
        return [
            {"report_number": rn, "new_fail_count": nf, "new_pass_count": np}
            for rn, nf, np in batch
        ]
    max_workers = min(5, len(report_numbers))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_fetch_new_counts_for_run, project_id, rn)
            for rn in report_numbers
        ]
        results = [f.result() for f in futures]
    return [
        {"report_number": rn, "new_fail_count": nf, "new_pass_count": np}
        for rn, nf, np in results
    ]


@app.get("/api/projects/{project_id}/cli-trend")
def get_cli_trend(project_id: str, _: str = Depends(require_auth)):
    """Return CLI execution-time aggregates per run, oldest first."""
    db = get_db()
    rows = db.execute(
        """
        SELECT s.report_number, s.row_count, s.total_sec, s.avg_sec, s.p95_sec, s.max_sec,
               s.slowest_cmd, s.test_status, r.start_ms
        FROM execution_times_summary s
        LEFT JOIN runs r
          ON s.project_id = r.project_id AND s.report_number = r.report_number
        -- test_uid IS NULL rows are scrape sentinels ("run has no exec-time test"), not data
        WHERE s.project_id = ? AND s.test_uid IS NOT NULL
        ORDER BY s.report_number ASC
        """,
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/projects/{project_id}/cli-top")
def get_cli_top(
    project_id: str,
    n: int = Query(5, ge=1, le=20),
    _: str = Depends(require_auth),
):
    """For each run, return the N longest-running CLI commands.

    Response: [{report_number, test_status, start_ms, top: [{rank, command, duration_sec, test_name}, ...]}, ...]
    oldest run first.
    """
    db = get_db()
    # For each run, pick the slowest invocation of each unique command,
    # then keep the top-N such rows (so the same command can't fill all slots).
    rows = db.execute(
        """
        WITH max_per_cmd AS (
            SELECT report_number, command, duration_sec, test_name
            FROM (
                SELECT report_number, command, duration_sec, test_name,
                       ROW_NUMBER() OVER (
                           PARTITION BY report_number, command
                           ORDER BY duration_sec DESC, id ASC
                       ) AS cmd_rank
                FROM execution_times
                WHERE project_id = ?
            )
            WHERE cmd_rank = 1
        )
        SELECT report_number, command, duration_sec, test_name, rnk
        FROM (
            SELECT report_number, command, duration_sec, test_name,
                   ROW_NUMBER() OVER (
                       PARTITION BY report_number
                       ORDER BY duration_sec DESC, command ASC
                   ) AS rnk
            FROM max_per_cmd
        )
        WHERE rnk <= ?
        ORDER BY report_number ASC, rnk ASC
        """,
        (project_id, n),
    ).fetchall()

    # Resolve test_uid by matching the test_name's base (after "::") against
    # test_results.test_name for the same run.
    uid_lookup: dict[tuple[int, str], str] = {}
    for tr in db.execute(
        "SELECT report_number, test_name, uid FROM test_results WHERE project_id = ? AND uid IS NOT NULL",
        (project_id,),
    ):
        uid_lookup[(tr["report_number"], tr["test_name"])] = tr["uid"]

    meta = {
        r["report_number"]: r
        for r in db.execute(
            """
            SELECT s.report_number, s.test_status, r.start_ms
            FROM execution_times_summary s
            LEFT JOIN runs r
              ON s.project_id = r.project_id AND s.report_number = r.report_number
            WHERE s.project_id = ?
            """,
            (project_id,),
        ).fetchall()
    }

    by_run: dict[int, list[dict]] = {}
    for r in rows:
        rn        = r["report_number"]
        test_full = r["test_name"]
        test_base = test_full.split("::")[-1] if test_full else None
        test_uid  = uid_lookup.get((rn, test_base)) if test_base else None
        by_run.setdefault(rn, []).append({
            "rank":         r["rnk"],
            "command":      r["command"],
            "duration_sec": r["duration_sec"],
            "test_name":    test_full,
            "test_uid":     test_uid,
        })

    out = []
    for rn, items in sorted(by_run.items()):
        m = meta.get(rn)
        out.append({
            "report_number": rn,
            "test_status":   m["test_status"] if m else None,
            "start_ms":      m["start_ms"]    if m else None,
            "top":           items,
        })
    return out


@app.get("/api/projects/{project_id}/runs/{report_number}/cli-log")
def get_cli_log(
    project_id: str,
    report_number: int,
    limit: int = Query(2000, ge=1, le=20000),
    _: str = Depends(require_auth),
):
    """Return per-command execution-time rows for a single run, slowest first."""
    db = get_db()
    rows = db.execute(
        """
        SELECT ts, command, duration_sec, test_name
        FROM execution_times
        WHERE project_id = ? AND report_number = ?
        ORDER BY duration_sec DESC
        LIMIT ?
        """,
        (project_id, report_number, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/projects/{project_id}/metrics")
def get_project_metrics(project_id: str, _: str = Depends(require_auth)):
    """Per-run values for every configured metric, oldest run first.

    One entry per metric: display info plus runs with a values map keyed by the
    metric's series keys. Runs where every series is NULL are omitted.
    """
    db = get_db()
    cfg = config_store.get_config(db)
    out = []
    for m in cfg.metrics:
        disp = config_store.metric_display(m)
        if not disp["keys"]:
            continue
        rows = db.execute(
            """
            SELECT m.report_number, m.value_key, m.value, m.test_status, r.start_ms
            FROM metric_values m
            LEFT JOIN runs r
              ON m.project_id = r.project_id AND m.report_number = r.report_number
            WHERE m.project_id = ? AND m.metric_name = ?
            ORDER BY m.report_number ASC
            """,
            (project_id, disp["name"]),
        ).fetchall()
        runs: dict[int, dict] = {}
        for row in rows:
            entry = runs.setdefault(row["report_number"], {
                "report_number": row["report_number"],
                "values": {},
                "test_status": row["test_status"],
                "start_ms": row["start_ms"],
            })
            entry["values"][row["value_key"]] = row["value"]
        disp["runs"] = [
            e for e in runs.values()
            if any(v is not None for v in e["values"].values())
        ]
        out.append(disp)
    return out


# ---------------------------------------------------------------------------
# User settings
# ---------------------------------------------------------------------------

_SETTINGS_DEFAULT = {"hidden": {}, "show_date_filter": False, "exclude_skipped": False}


class UserSettings(BaseModel):
    # Generic per-dimension hidden values: {dimension_key: [hidden_value, ...]}.
    hidden: dict[str, list[str]] = {}
    show_date_filter: bool = False
    # When true, skipped tests are dropped from the pass-rate denominator
    # (client-side recomputation; the API keeps returning raw counts).
    exclude_skipped: bool = False
    model_config = {"extra": "ignore"}


@app.get("/api/settings")
def get_settings(username: str = Depends(require_auth)):
    db = get_db()
    row = db.execute(
        "SELECT settings_json FROM user_settings WHERE username = ?", (username,)
    ).fetchone()
    if row:
        return json.loads(row["settings_json"])
    return _SETTINGS_DEFAULT.copy()


@app.put("/api/settings")
def put_settings(body: UserSettings, username: str = Depends(require_auth)):
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO user_settings (username, settings_json) VALUES (?, ?)",
        (username, json.dumps(body.model_dump())),
    )
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Features & mappings (CRUD + feature-status matrix)
# ---------------------------------------------------------------------------

# suite_name → feature name heuristic map used by the guess-mappings endpoint
# suite_name → feature heuristics now live in config (config_store, config_automap),
# editable from the Configuration tab. See config_store._SEED_AUTOMAP for defaults.


class FeatureImportItem(BaseModel):
    name: str
    group: str = ""
    mappings: list[str] = []

class FeatureImportBody(BaseModel):
    version: int = 1
    scope: str = ""       # empty → default view's scope
    features: list[FeatureImportItem] = []


class FeatureCreate(BaseModel):
    name: str
    feature_group: str = ""
    feature_scope: str = ""   # empty → default view's scope


def _resolve_scope(db, scope: str) -> str:
    """Resolve a feature scope; empty means the first configured view's scope.

    Scopes are arbitrary names owned by config views — nothing outside the
    views config defines which scopes exist.
    """
    scope = (scope or "").strip()
    if scope:
        return scope
    cfg = config_store.get_config(db)
    return cfg.views[0]["scope"] if cfg.views else "standard"

class FeatureUpdate(BaseModel):
    name: Optional[str] = None
    feature_group: Optional[str] = None

class MappingCreate(BaseModel):
    test_name: str


@app.post("/api/features/seed-from-suites")
def seed_features_from_suites(scope: str = Query(""), _: str = Depends(require_auth)):
    """Create one feature per distinct suite_name found in the scope's projects.

    Projects are selected via the column filter of the view owning the scope
    (config-driven). Each feature gets that scope, its group is the view name,
    and every test in the suite is mapped to it. Existing features and mappings
    are preserved (INSERT OR IGNORE).
    """
    db = get_db()
    scope = _resolve_scope(db, scope)

    all_pids = [r["project_id"] for r in db.execute(
        "SELECT DISTINCT project_id FROM test_results"
    ).fetchall()]
    cfg = config_store.get_config(db)
    view = next((v for v in cfg.views if v["scope"] == scope), None)
    view_filter = view["filter"] if view else {}
    group = view["name"] if view else scope
    scope_pids = [pid for pid in all_pids
                  if config_store.matches_filter(parse_project_id(pid)["dimensions"], view_filter)]

    if not scope_pids:
        return {"features_added": 0, "mappings_added": 0}

    placeholders = ",".join("?" * len(scope_pids))
    suite_rows = db.execute(
        f"SELECT DISTINCT suite_name FROM test_results "
        f"WHERE project_id IN ({placeholders}) AND suite_name IS NOT NULL",
        scope_pids,
    ).fetchall()
    suite_names = sorted(r["suite_name"] for r in suite_rows if r["suite_name"])

    features_added = 0
    mappings_added = 0

    for suite in suite_names:
        db.execute(
            "INSERT OR IGNORE INTO features (name, feature_group, feature_scope) VALUES (?, ?, ?)",
            (suite, group, scope),
        )
        if db.execute("SELECT changes()").fetchone()[0]:
            features_added += 1

        fid_row = db.execute(
            "SELECT id FROM features WHERE name = ? AND feature_scope = ?", (suite, scope)
        ).fetchone()
        if not fid_row:
            continue
        fid = fid_row["id"]

        test_rows = db.execute(
            f"SELECT DISTINCT test_name FROM test_results "
            f"WHERE suite_name = ? AND project_id IN ({placeholders})",
            [suite] + scope_pids,
        ).fetchall()
        for tr in test_rows:
            db.execute(
                "INSERT OR IGNORE INTO feature_mappings (feature_id, test_name) VALUES (?, ?)",
                (fid, tr["test_name"]),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                mappings_added += 1

    db.commit()
    return {"features_added": features_added, "mappings_added": mappings_added}


@app.post("/api/features/guess-mappings")
def guess_mappings(_: str = Depends(require_auth)):
    """Auto-populate feature_mappings from suite_name heuristics.

    For each (suite_name → feature_name) pair in the configured auto-map
    (config.automap), finds all distinct test_names in test_results with that
    suite_name and inserts them as
    mappings for the corresponding feature.  Existing mappings are preserved
    (INSERT OR IGNORE).  Returns a summary of what was added.
    """
    db = get_db()

    # Build feature_name → feature_id lookup
    rows = db.execute("SELECT id, name FROM features").fetchall()
    feature_id_map: dict[str, int] = {r["name"]: r["id"] for r in rows}

    added_total = 0
    summary: list[dict] = []

    suite_feature_map = config_store.get_config(db).automap
    for suite_name, feature_name in suite_feature_map.items():
        fid = feature_id_map.get(feature_name)
        if fid is None:
            continue  # feature not in DB (maybe deleted)

        test_names = [
            r["test_name"]
            for r in db.execute(
                "SELECT DISTINCT test_name FROM test_results WHERE suite_name = ?",
                (suite_name,),
            ).fetchall()
        ]
        added = 0
        for tn in test_names:
            try:
                db.execute(
                    "INSERT OR IGNORE INTO feature_mappings (feature_id, test_name) VALUES (?, ?)",
                    (fid, tn),
                )
                if db.execute("SELECT changes()").fetchone()[0]:
                    added += 1
            except Exception:
                pass

        if added:
            added_total += added
            summary.append({"feature": feature_name, "suite": suite_name, "added": added})

    db.commit()
    return {"total_added": added_total, "summary": summary}


@app.get("/api/features")
def list_features(scope: str = Query(""), _: str = Depends(require_auth)):
    db = get_db()
    scope = _resolve_scope(db, scope)
    features = db.execute(
        "SELECT id, name, feature_group, sort_order FROM features WHERE feature_scope = ? "
        "ORDER BY sort_order, feature_group, name",
        (scope,),
    ).fetchall()
    fids = [f["id"] for f in features]
    mappings = (
        db.execute(
            f"SELECT id, feature_id, test_name FROM feature_mappings "
            f"WHERE feature_id IN ({','.join('?' * len(fids))})",
            fids,
        ).fetchall()
        if fids else []
    )

    mapping_map: dict[int, list] = {}
    for m in mappings:
        mapping_map.setdefault(m["feature_id"], []).append({"id": m["id"], "test_name": m["test_name"]})

    return [
        {
            "id":            f["id"],
            "name":          f["name"],
            "feature_group": f["feature_group"],
            "sort_order":    f["sort_order"],
            "mappings":      mapping_map.get(f["id"], []),
        }
        for f in features
    ]


@app.get("/api/features/export")
def export_features(scope: str = Query(""), _: str = Depends(require_auth)):
    """Return all features + mappings for the given scope as a portable JSON blob."""
    db = get_db()
    scope = _resolve_scope(db, scope)
    features = db.execute(
        "SELECT id, name, feature_group, sort_order FROM features WHERE feature_scope = ? "
        "ORDER BY sort_order, feature_group, name",
        (scope,),
    ).fetchall()
    fids = [f["id"] for f in features]
    mappings = (
        db.execute(
            f"SELECT feature_id, test_name FROM feature_mappings "
            f"WHERE feature_id IN ({','.join('?' * len(fids))})",
            fids,
        ).fetchall()
        if fids else []
    )
    mapping_map: dict[int, list[str]] = {}
    for m in mappings:
        mapping_map.setdefault(m["feature_id"], []).append(m["test_name"])

    return {
        "version": 1,
        "scope": scope,
        "features": [
            {
                "name":     f["name"],
                "group":    f["feature_group"] or "",
                "mappings": sorted(mapping_map.get(f["id"], [])),
            }
            for f in features
        ],
    }


@app.post("/api/features/import")
def import_features(body: FeatureImportBody, _: str = Depends(require_auth)):
    """Upsert features and mappings from a JSON blob.

    For each feature in the payload:
    - If a feature with the same name+scope already exists, reuse it.
    - Otherwise create a new feature.
    - For each mapping in the payload, insert it if it does not already exist.
    Returns counts of features and mappings added.
    """
    db = get_db()
    scope = _resolve_scope(db, body.scope)
    features_added = 0
    mappings_added = 0

    for item in body.features:
        name  = item.name.strip()
        group = item.group.strip()
        if not name:
            continue
        row = db.execute(
            "SELECT id FROM features WHERE name = ? AND feature_scope = ?", (name, scope)
        ).fetchone()
        if row:
            fid = row["id"]
        else:
            cur = db.execute(
                "INSERT INTO features (name, feature_group, feature_scope) VALUES (?, ?, ?)",
                (name, group, scope),
            )
            fid = cur.lastrowid
            features_added += 1

        for test_name in item.mappings:
            test_name = test_name.strip()
            if not test_name:
                continue
            try:
                db.execute(
                    "INSERT OR IGNORE INTO feature_mappings (feature_id, test_name) VALUES (?, ?)",
                    (fid, test_name),
                )
                if db.execute("SELECT changes()").fetchone()[0]:
                    mappings_added += 1
            except Exception:
                pass

    db.commit()
    return {"features_added": features_added, "mappings_added": mappings_added}


@app.post("/api/features", status_code=201)
def create_feature(body: FeatureCreate, _: str = Depends(require_auth)):
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO features (name, feature_group, feature_scope) VALUES (?, ?, ?)",
            (body.name.strip(), body.feature_group.strip(), _resolve_scope(db, body.feature_scope)),
        )
        db.commit()
        return {"id": cur.lastrowid, "name": body.name, "feature_group": body.feature_group, "mappings": []}
    except Exception:
        raise HTTPException(status_code=409, detail="Feature name already exists")


@app.put("/api/features/{feature_id}")
def update_feature(feature_id: int, body: FeatureUpdate, _: str = Depends(require_auth)):
    db = get_db()
    row = db.execute("SELECT * FROM features WHERE id = ?", (feature_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Feature not found")
    name  = body.name.strip()          if body.name          is not None else row["name"]
    group = body.feature_group.strip() if body.feature_group is not None else row["feature_group"]
    db.execute("UPDATE features SET name = ?, feature_group = ? WHERE id = ?", (name, group, feature_id))
    db.commit()
    return {"id": feature_id, "name": name, "feature_group": group}


@app.delete("/api/features/{feature_id}", status_code=204)
def delete_feature(feature_id: int, _: str = Depends(require_auth)):
    db = get_db()
    db.execute("DELETE FROM feature_mappings WHERE feature_id = ?", (feature_id,))
    db.execute("DELETE FROM features WHERE id = ?", (feature_id,))
    db.commit()


@app.post("/api/features/{feature_id}/mappings", status_code=201)
def add_mapping(feature_id: int, body: MappingCreate, _: str = Depends(require_auth)):
    db = get_db()
    if not db.execute("SELECT 1 FROM features WHERE id = ?", (feature_id,)).fetchone():
        raise HTTPException(status_code=404, detail="Feature not found")
    try:
        cur = db.execute(
            "INSERT INTO feature_mappings (feature_id, test_name) VALUES (?, ?)",
            (feature_id, body.test_name.strip()),
        )
        db.commit()
        return {"id": cur.lastrowid, "feature_id": feature_id, "test_name": body.test_name}
    except Exception:
        raise HTTPException(status_code=409, detail="Mapping already exists")


@app.delete("/api/features/{feature_id}/mappings/{mapping_id}", status_code=204)
def remove_mapping(feature_id: int, mapping_id: int, _: str = Depends(require_auth)):
    db = get_db()
    db.execute("DELETE FROM feature_mappings WHERE id = ? AND feature_id = ?", (mapping_id, feature_id))
    db.commit()


@app.get("/api/features/{feature_id}/tests")
def get_feature_tests(
    feature_id: int,
    project_id: str,
    run: int,
    _: str = Depends(require_auth),
):
    db = get_db()
    rows = db.execute(
        """
        SELECT tr.test_name, tr.status, tr.uid
        FROM feature_mappings fm
        JOIN test_results tr ON (tr.test_name = fm.test_name
                              OR tr.test_name LIKE fm.test_name || '[%')
        WHERE fm.feature_id = ? AND tr.project_id = ? AND tr.report_number = ?
        ORDER BY tr.test_name
        """,
        (feature_id, project_id, run),
    ).fetchall()
    return [{"test_name": r["test_name"], "status": r["status"], "uid": r["uid"]} for r in rows]


def _resolve_feature_tests(
    test_names: set[str],
    results: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Match mapped test names against actual results, including parametrized variants.

    A mapped name like ``test_forward_delay`` matches both an exact key and any
    key of the form ``test_forward_delay[...]``.
    """
    found: dict[str, list[str]] = {}
    for t in test_names:
        if t in results:
            found[t] = results[t]
        else:
            prefix = t + "["
            for key, statuses in results.items():
                if key.startswith(prefix):
                    found[key] = statuses
    return found


@app.get("/api/feature-status")
def get_feature_status(
    scope: str = Query(""),
    run_overrides: str = Query("{}"),
    _: str = Depends(require_auth),
):
    """Return the feature × platform status matrix based on test_results.

    scope          → a view's feature scope; features and project columns are
                     both filtered by the view owning it (empty = default view)
    run_overrides  → JSON dict of {project_id: report_number} to pin specific runs
    """
    db = get_db()
    scope = _resolve_scope(db, scope)

    # Parse run overrides
    try:
        overrides: dict[str, int] = json.loads(run_overrides)
    except Exception:
        overrides = {}

    # Features filtered by scope
    features = db.execute(
        "SELECT id, name, feature_group, sort_order FROM features "
        "WHERE feature_scope = ? ORDER BY sort_order, feature_group, name",
        (scope,),
    ).fetchall()

    # Only fetch mappings for scope-appropriate features
    fids = [f["id"] for f in features]
    mappings = (
        db.execute(
            f"SELECT feature_id, test_name FROM feature_mappings "
            f"WHERE feature_id IN ({','.join('?' * len(fids))})",
            fids,
        ).fetchall()
        if fids else []
    )
    feature_tests: dict[int, list[str]] = {}
    for m in mappings:
        feature_tests.setdefault(m["feature_id"], []).append(m["test_name"])

    # Latest run number per project
    latest_rows = db.execute(
        "SELECT project_id, MAX(report_number) AS max_num FROM runs GROUP BY project_id"
    ).fetchall()
    latest: dict[str, int] = {r["project_id"]: r["max_num"] for r in latest_rows}

    # Filter projects via the matching view's column filter (config-driven;
    # replaces the previously hardcoded OLS cadence special-casing).
    cfg = config_store.get_config(db)
    view = next((v for v in cfg.views if v["scope"] == scope), None)
    view_filter = view["filter"] if view else {}
    scope_pids: list[str] = [
        pid for pid in latest
        if config_store.matches_filter(parse_project_id(pid)["dimensions"], view_filter)
    ]

    # Available runs per project (newest first, capped at 60, scope-appropriate only)
    all_run_rows = db.execute(
        "SELECT project_id, report_number, start_ms, total, build_version "
        "FROM runs ORDER BY project_id, report_number DESC"
    ).fetchall()
    project_runs: dict[str, list[dict]] = {}
    for r in all_run_rows:
        pid = r["project_id"]
        if pid not in scope_pids:
            continue
        if len(project_runs.get(pid, [])) < 60:
            project_runs.setdefault(pid, []).append({
                "run":           r["report_number"],
                "start_ms":      r["start_ms"],
                "total":         r["total"],
                "build_version": r["build_version"] or None,
            })

    # Latest run that has test_results per project (may lag behind latest run)
    latest_with_results = {
        r["project_id"]: r["max_num"]
        for r in db.execute(
            "SELECT project_id, MAX(report_number) AS max_num FROM test_results GROUP BY project_id"
        ).fetchall()
    }

    # Selected run per project: use override if provided, else latest run with results,
    # falling back to absolute latest (scope-appropriate only)
    selected: dict[str, int] = {
        pid: overrides.get(pid, latest_with_results.get(pid, latest[pid]))
        for pid in scope_pids
    }

    # Test results for selected run per project
    # dict[project_id, dict[test_name, list[status]]] — all param variants per test name
    project_results: dict[str, dict[str, list[str]]] = {}
    for pid, num in selected.items():
        rows = db.execute(
            "SELECT test_name, status FROM test_results WHERE project_id = ? AND report_number = ?",
            (pid, num),
        ).fetchall()
        if rows:
            tr: dict[str, list[str]] = {}
            for r in rows:
                tr.setdefault(r["test_name"], []).append(r["status"])
            project_results[pid] = tr

    # Build platform metadata for projects that have test results (already scope-filtered)
    platforms = []
    for pid in project_results:
        meta = parse_project_id(pid)
        platforms.append({
            "project_id":     pid,
            "platform":       meta["platform"],
            "platform_label": meta["platform_label"],
            "variant":        meta["variant"],
            "variant_label":  meta["variant_label"],
            "cadence":        meta["cadence"],
            "cadence_label":  meta["cadence_label"],
            "dimensions":     meta["dimensions"],
            "labels":         meta["labels"],
            "selected_run":   selected.get(pid),
            "available_runs": project_runs.get(pid, []),
        })
    platforms.sort(key=lambda x: (x["platform"], x["variant"]))

    # Per-cell trend: pass_pct per (feature_id, project_id, report_number),
    # filtered to scope-appropriate features and projects, oldest first.
    # Expand parametrized test names in Python to avoid a slow LIKE join in SQL.
    cell_trends: dict[str, dict[str, list]] = {}
    if fids and scope_pids:
        pid_ph = ",".join("?" * len(scope_pids))

        # All distinct test names that exist in scope projects
        actual_names: set[str] = {
            r["test_name"]
            for r in db.execute(
                f"SELECT DISTINCT test_name FROM test_results WHERE project_id IN ({pid_ph})",
                scope_pids,
            ).fetchall()
        }

        # Expand each mapped name to matching actual names (exact + parametrized variants)
        expanded_pairs: list[tuple[int, str]] = []
        for fid_int, mapped_names in feature_tests.items():
            if fid_int not in fids:
                continue
            for m in mapped_names:
                if m in actual_names:
                    expanded_pairs.append((fid_int, m))
                else:
                    prefix = m + "["
                    for n in actual_names:
                        if n.startswith(prefix):
                            expanded_pairs.append((fid_int, n))

        if expanded_pairs:
            db.execute("CREATE TEMP TABLE IF NOT EXISTS _feat_trend_names (feature_id INTEGER, test_name TEXT)")
            db.execute("DELETE FROM _feat_trend_names")
            db.executemany("INSERT INTO _feat_trend_names VALUES (?,?)", expanded_pairs)
            cell_trend_rows = db.execute(
                f"""
                SELECT fn.feature_id,
                       tr.project_id,
                       tr.report_number,
                       ROUND(100.0 * SUM(CASE WHEN tr.status = 'passed' THEN 1 ELSE 0 END) / COUNT(*), 1) AS pass_pct
                FROM _feat_trend_names fn
                JOIN test_results tr ON tr.test_name = fn.test_name
                WHERE tr.project_id IN ({pid_ph})
                GROUP BY fn.feature_id, tr.project_id, tr.report_number
                ORDER BY fn.feature_id, tr.project_id, tr.report_number ASC
                """,
                scope_pids,
            ).fetchall()
            for ctr in cell_trend_rows:
                fid = str(ctr["feature_id"])
                pid = ctr["project_id"]
                cell_trends.setdefault(fid, {}).setdefault(pid, []).append(
                    {"run": ctr["report_number"], "pct": ctr["pass_pct"]}
                )

    # Compute matrix: {feature_id -> {project_id -> stats + trend}}
    # results[pid] is dict[test_name, list[status]] — multiple param variants per name
    matrix: dict[str, dict] = {}
    for f in features:
        fid = f["id"]
        test_names = set(feature_tests.get(fid, []))
        row_data: dict[str, dict] = {}
        for pid, results in project_results.items():
            found = _resolve_feature_tests(test_names, results)
            if not found:
                continue
            # Count ALL parametrized variants, not just one per test name
            total   = sum(len(v) for v in found.values())
            passed  = sum(1 for v in found.values() for s in v if s == "passed")
            failed  = sum(1 for v in found.values() for s in v if s in ("failed", "broken"))
            skipped = sum(1 for v in found.values() for s in v if s == "skipped")
            row_data[pid] = {
                "total":         total,
                "passed":        passed,
                "failed":        failed,
                "skipped":       skipped,
                "pass_pct":      round(passed * 100.0 / total, 1) if total else None,
                "report_number": selected.get(pid),
                "trend":         cell_trends.get(str(fid), {}).get(pid, []),
            }
        matrix[str(fid)] = row_data

    return {
        "platforms": platforms,
        "features": [
            {
                "id":            f["id"],
                "name":          f["name"],
                "feature_group": f["feature_group"],
                "mapped_count":  len(feature_tests.get(f["id"], [])),
            }
            for f in features
        ],
        "matrix": matrix,
    }


@app.get("/api/projects/{project_id}/feature-status")
def get_project_feature_status(
    project_id: str,
    run: Optional[int] = Query(None),
    scope: str = Query(""),
    _: str = Depends(require_auth),
):
    """Return per-feature pass/fail stats for a single project at a specific run.

    Used to update one matrix column without re-fetching all data.
    If run is omitted, uses the latest run for the project.
    """
    db = get_db()
    scope = _resolve_scope(db, scope)

    if run is None:
        row = db.execute(
            "SELECT MAX(report_number) AS max_num FROM test_results WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not row or row["max_num"] is None:
            raise HTTPException(status_code=404, detail="Project not found or no runs")
        run = row["max_num"]

    rows = db.execute(
        "SELECT test_name, status FROM test_results WHERE project_id = ? AND report_number = ?",
        (project_id, run),
    ).fetchall()
    # dict[test_name, list[status]] — all param variants per test name
    results: dict[str, list[str]] = {}
    for r in rows:
        results.setdefault(r["test_name"], []).append(r["status"])

    features = db.execute(
        "SELECT id FROM features WHERE feature_scope = ?", (scope,)
    ).fetchall()
    mappings = db.execute("SELECT feature_id, test_name FROM feature_mappings").fetchall()
    feature_tests: dict[int, list[str]] = {}
    for m in mappings:
        feature_tests.setdefault(m["feature_id"], []).append(m["test_name"])

    out: dict[str, dict | None] = {}
    for f in features:
        fid = f["id"]
        test_names = set(feature_tests.get(fid, []))
        found = _resolve_feature_tests(test_names, results)
        if not found:
            out[str(fid)] = None
            continue
        total   = sum(len(v) for v in found.values())
        passed  = sum(1 for v in found.values() for s in v if s == "passed")
        failed  = sum(1 for v in found.values() for s in v if s in ("failed", "broken"))
        skipped = sum(1 for v in found.values() for s in v if s == "skipped")
        out[str(fid)] = {
            "total":         total,
            "passed":        passed,
            "failed":        failed,
            "skipped":       skipped,
            "pass_pct":      round(passed * 100.0 / total, 1) if total else None,
            "report_number": run,
        }

    return {"report_number": run, "results": out}


# ---------------------------------------------------------------------------
# Scraper summary — per-project, per-run test_results coverage
# ---------------------------------------------------------------------------

@app.get("/api/scraper/summary")
def get_scraper_summary(_: str = Depends(require_auth)):
    """Return per-project list of runs with expected vs stored test_results counts."""
    db = get_db()

    # All runs with their expected test count
    run_rows = db.execute(
        "SELECT project_id, report_number, total, start_ms "
        "FROM runs ORDER BY project_id, report_number"
    ).fetchall()

    # Actual stored test_results count per (project, run)
    stored_rows = db.execute(
        "SELECT project_id, report_number, COUNT(*) AS cnt "
        "FROM test_results GROUP BY project_id, report_number"
    ).fetchall()
    stored_map: dict[tuple, int] = {
        (r["project_id"], r["report_number"]): r["cnt"] for r in stored_rows
    }

    # Group by project
    projects_map: dict[str, list] = {}
    for r in run_rows:
        pid = r["project_id"]
        num = r["report_number"]
        stored = stored_map.get((pid, num), 0)
        projects_map.setdefault(pid, []).append({
            "run":      num,
            "start_ms": r["start_ms"],
            "total":    r["total"] or 0,
            "stored":   stored,
        })

    return [
        {
            "id":          pid,
            "runs":        runs,
            "total_runs":  len(runs),
            "total_stored": sum(r["stored"] for r in runs),
        }
        for pid, runs in sorted(projects_map.items())
    ]


@app.get("/api/test-names")
def get_test_names(q: str = "", _: str = Depends(require_auth)):
    """Return distinct test names for autocomplete (optionally filtered by substring q).

    Parametrized variants like test_foo[param1] and test_foo[param2] are collapsed to test_foo.
    """
    db = get_db()
    if q:
        rows = db.execute(
            "SELECT DISTINCT test_name FROM test_results WHERE test_name LIKE ? ORDER BY test_name LIMIT 200",
            (f"%{q}%",),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT DISTINCT test_name FROM test_results ORDER BY test_name LIMIT 2000"
        ).fetchall()
    # Strip parametrize suffix [...]  and deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for r in rows:
        name = r["test_name"].split("[")[0]
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result[:60 if q else 200]


# ---------------------------------------------------------------------------
# Admin — read-only DB browser
# ---------------------------------------------------------------------------

# Whitelisted tables (prevents SQL injection via table name)
_ADMIN_TABLES = {
    "projects", "report_snapshots", "history_trend", "duration_trend",
    "runs", "metric_values", "test_results", "run_new_fails", "features", "feature_mappings",
    "execution_times", "execution_times_summary",
    "user_settings", "user_logins", "user_login_log",
}


@app.get("/api/admin/tables")
def admin_list_tables(_: str = Depends(require_admin)):
    """Return all tables with their row counts."""
    db = get_db()
    out = []
    for name in sorted(_ADMIN_TABLES):
        try:
            count = db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]  # noqa: S608
        except Exception:
            count = 0
        out.append({"name": name, "rows": count})
    return out


@app.get("/api/admin/table/{table_name}")
def admin_get_table(
    table_name: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    q: str = Query(""),
    _: str = Depends(require_admin),
):
    """Return paginated rows for a whitelisted table.

    Optional ?q= does a case-insensitive substring search across all text columns.
    """
    if table_name not in _ADMIN_TABLES:
        raise HTTPException(status_code=404, detail="Table not found")
    db = get_db()

    # Fetch column info
    col_rows = db.execute(f"PRAGMA table_info({table_name})").fetchall()  # noqa: S608
    columns = [r["name"] for r in col_rows]
    col_types = {r["name"]: r["type"].upper() for r in col_rows}

    if q:
        # Build WHERE clause filtering text columns by substring
        text_cols = [c for c in columns if "INT" not in col_types[c] and "REAL" not in col_types[c]]
        if text_cols:
            where = " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in text_cols)
            params_filter = [f"%{q}%"] * len(text_cols)
            total = db.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE {where}", params_filter  # noqa: S608
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM {table_name} WHERE {where} LIMIT ? OFFSET ?",  # noqa: S608
                params_filter + [limit, offset],
            ).fetchall()
        else:
            total = db.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]  # noqa: S608
            rows = db.execute(
                f"SELECT * FROM {table_name} LIMIT ? OFFSET ?", (limit, offset)  # noqa: S608
            ).fetchall()
    else:
        total = db.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]  # noqa: S608
        rows = db.execute(
            f"SELECT * FROM {table_name} LIMIT ? OFFSET ?", (limit, offset)  # noqa: S608
        ).fetchall()

    return {
        "table":   table_name,
        "columns": columns,
        "total":   total,
        "offset":  offset,
        "limit":   limit,
        "rows":    [list(r) for r in rows],
    }


class ClearRequest(BaseModel):
    tables: Optional[list[str]] = None   # None = clear all whitelisted tables


@app.post("/api/admin/clear")
def admin_clear(req: ClearRequest = ClearRequest(), _: str = Depends(require_admin)):
    """Delete all rows from the requested tables (or all whitelisted tables if none specified)."""
    targets = req.tables if req.tables else list(_ADMIN_TABLES)
    invalid = [t for t in targets if t not in _ADMIN_TABLES]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Unknown tables: {invalid}")
    db = get_db()
    deleted: dict[str, int] = {}
    for name in targets:
        cur = db.execute(f"DELETE FROM {name}")  # noqa: S608
        deleted[name] = cur.rowcount
    db.commit()
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

_REPORT_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "templates")


class ReportEntry(BaseModel):
    project_id: str
    report_number: int


class ReportRequest(BaseModel):
    reports: list[ReportEntry]
    version: str = ""


@app.post("/api/report/generate")
def generate_report(body: ReportRequest, _: str = Depends(require_auth)):
    """Generate an HTML test report from one or more Allure reports.

    Each entry in `reports` identifies a project + run number.
    The Allure report URLs are constructed from ALLURE_BASE_URL.
    Returns the rendered HTML as a downloadable file.
    """
    if not body.reports:
        raise HTTPException(status_code=400, detail="At least one report entry is required")

    report_urls = [
        f"{ALLURE_BASE_URL}/projects/{e.project_id}/reports/{e.report_number}/index.html"
        for e in body.reports
    ]

    rcfg = config_store.get_config(get_db()).report
    week_number = get_week_number()
    version = body.version

    if not version:
        first_base = get_report_base_url(report_urls[0])
        try:
            env_json_first = _rg_fetch_json(f"{first_base}widgets/environment.json")
            version = get_version_from_json(env_json_first, rcfg)
        except Exception:
            version = "Unknown"

    links = {
        "workweek":    week_number,
        "version":     version,
        "generated_on": datetime.now().strftime("%Y-%m-%d"),
        "run_cadence": get_run_cadence(version, rcfg),
    }

    unique_urls = list(dict.fromkeys(report_urls))
    workers = min(8, max(1, len(unique_urls)))
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            unique_results = list(executor.map(
                partial(process_single_report, links=links, report_cfg=rcfg), unique_urls))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    by_url = dict(zip(unique_urls, unique_results))
    ordered = [by_url[url] for url in report_urls]

    general_stats      = [e["general_stats"]      for e in ordered]
    general_stats_copy = [e["general_stats_copy"] for e in ordered]
    durations          = [e["duration_ms"]         for e in ordered]
    suite_results      = [e["suite_results"]       for e in ordered]

    links["contains_ols"] = any(
        e["general_stats_copy"].get("links", {}).get("is_ols") for e in ordered
    )
    image_families = {
        e["general_stats_copy"].get("links", {}).get("image_family", rcfg.get("family_default") or "")
        for e in ordered
    }
    links["image_family"] = image_families.pop() if len(image_families) == 1 else "Mixed"

    overall = get_overall_summary(general_stats, durations_ms=durations)
    overall.pop("duration", None)

    general_stats.insert(0, overall)

    all_msgs = " ".join(
        f.get("statusMessage", "") or ""
        for entry in ordered
        for f in (entry["general_stats"].get("new_failed_tests") or [])
        + (entry["general_stats"].get("known_issues") or [])
    )
    jira_tickets = list(dict.fromkeys(_JIRA_TICKET_RE.findall(all_msgs)))
    jira_statuses = _fetch_jira_statuses(jira_tickets)

    env = _make_jinja_env()
    template = env.get_template("test_report.html")
    html = template.render(
        general_stats_list=general_stats,
        test_summary=suite_results,
        general_stats_list_copy=general_stats_copy,
        links=links,
        jira_statuses=jira_statuses,
        report_cfg=rcfg,
    )

    return Response(content=html, media_type="text/html")


class CompareRequest(BaseModel):
    project_id: str
    run_a: int   # base / older run
    run_b: int   # current / newer run


@app.post("/api/report/compare")
def compare_runs(body: CompareRequest, _: str = Depends(require_auth)):
    """Generate a report for run_b with new-fails/passes compared explicitly against run_a."""
    run_a_url  = f"{ALLURE_BASE_URL}/projects/{body.project_id}/reports/{body.run_a}/index.html"
    run_b_url  = f"{ALLURE_BASE_URL}/projects/{body.project_id}/reports/{body.run_b}/index.html"
    run_a_base = get_report_base_url(run_a_url)
    run_b_base = get_report_base_url(run_b_url)

    rcfg = config_store.get_config(get_db()).report
    links = {
        "workweek":     get_week_number(),
        "version":      "",
        "generated_on": datetime.now().strftime("%Y-%m-%d"),
    }

    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fut_entry    = ex.submit(process_single_report, run_b_url, links, rcfg)
            fut_suites_a = ex.submit(_rg_fetch_json, f"{run_a_base}data/suites.json")
            entry    = fut_entry.result()
            suites_a = fut_suites_a.result()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Re-fetch run_b suites for explicit comparison computation
    try:
        suites_b = _rg_fetch_json(f"{run_b_base}data/suites.json")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    failed_a       = build_failed_tests_index(suites_a)
    failed_b       = build_failed_tests_index(suites_b)
    passed_b       = build_passed_tests_index(suites_b)
    failed_a_names = set(failed_a)

    new_fails = []
    for name in sorted(failed_b):
        if name in failed_a_names:
            continue
        test = failed_b[name]
        msg  = fetch_status_message(run_b_base, test["uid"])
        new_fails.append({"name": name, "status": test["status"], "statusMessage": msg})

    new_passes = []
    for name in sorted(passed_b):
        prev = failed_a.get(name)
        if not prev:
            continue
        new_passes.append({"name": name, "previous_status": prev["status"]})
        if len(new_passes) >= MAX_NEW_PASS_ITEMS:
            break

    prev_stats_a = fetch_prev_run_stats(run_a_url)

    for d in (entry["general_stats"], entry["general_stats_copy"]):
        d["new_failed_tests"]    = new_fails
        d["new_passed_tests"]    = new_passes
        d["previous_report_url"] = run_a_url
        d["new_fails_note"]      = ""
        if prev_stats_a:
            d["prev"] = prev_stats_a
        else:
            d.pop("prev", None)

    version = entry["general_stats_copy"].get("links", {}).get("image_version", "")
    links["version"] = version
    links["run_cadence"] = get_run_cadence(version, rcfg)
    entry["general_stats_copy"]["links"]["version"] = version
    links["contains_ols"] = entry["general_stats_copy"].get("links", {}).get("is_ols", False)
    links["image_family"] = entry["general_stats_copy"].get("links", {}).get(
        "image_family", rcfg.get("family_default") or "")

    all_msgs = " ".join(
        f.get("statusMessage", "") or ""
        for f in new_fails + (entry["general_stats"].get("known_issues") or [])
    )
    jira_tickets = list(dict.fromkeys(_JIRA_TICKET_RE.findall(all_msgs)))
    jira_statuses = _fetch_jira_statuses(jira_tickets)

    env      = _make_jinja_env()
    template = env.get_template("test_report.html")
    html     = template.render(
        general_stats_list=      [entry["general_stats"]],
        test_summary=             [entry["suite_results"]],
        general_stats_list_copy= [entry["general_stats_copy"]],
        links=links,
        jira_statuses=jira_statuses,
        report_cfg=rcfg,
    )
    return Response(content=html, media_type="text/html")


# ---------------------------------------------------------------------------
# Static SPA — must come LAST so API routes take priority
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="static", html=True), name="static")
