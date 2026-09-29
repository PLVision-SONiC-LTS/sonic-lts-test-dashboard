#!/usr/bin/env python3
"""
Allure Docker Service scraper.

Fetches project and report data from an Allure Docker Service instance
and stores it in a local SQLite database for dashboard development.

Usage:
    python scraper.py             # scrape once
    python scraper.py --watch     # scrape continuously on SCRAPE_INTERVAL
    python scraper.py --discover  # print available endpoints and sample data

Configuration via environment variables or a .env file (see .env.example).
"""

import argparse
import json
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv
import os

import config_store
from new_fails_db import backfill_project, upsert_run_new_fails

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = os.getenv("ALLURE_BASE_URL", "http://localhost:5050/allure-docker-service").rstrip("/")
ACCEPT_SELF_SIGNED = os.getenv("ALLURE_ACCEPT_SELF_SIGNED", "").lower() in ("1", "true", "yes")
USERNAME = os.getenv("ALLURE_USERNAME") or None
PASSWORD = os.getenv("ALLURE_PASSWORD") or None
DB_PATH = os.getenv("DB_PATH", "allure_data.db")
SCRAPE_INTERVAL = int(os.getenv("SCRAPE_INTERVAL", "300"))
SCRAPE_WORKERS = int(os.getenv("SCRAPE_WORKERS", "100"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id              TEXT PRIMARY KEY,
    created_at      TEXT,
    last_scraped_at TEXT
);

CREATE TABLE IF NOT EXISTS report_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL,
    scraped_at  TEXT NOT NULL,
    -- statistics from widgets/summary.json
    total       INTEGER,
    passed      INTEGER,
    failed      INTEGER,
    broken      INTEGER,
    skipped     INTEGER,
    unknown     INTEGER,
    -- timing
    duration_ms INTEGER,
    start_ms    INTEGER,
    stop_ms     INTEGER,
    -- full raw JSON blobs for forward-compatibility
    raw_summary TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS history_trend (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL,
    scraped_at   TEXT NOT NULL,
    build_order  INTEGER,
    report_name  TEXT,
    report_url   TEXT,
    total        INTEGER,
    passed       INTEGER,
    failed       INTEGER,
    broken       INTEGER,
    skipped      INTEGER,
    unknown      INTEGER,
    duration_ms  INTEGER,
    raw_entry    TEXT,
    UNIQUE (project_id, build_order),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS duration_trend (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL,
    scraped_at   TEXT NOT NULL,
    build_order  INTEGER,
    duration_ms  INTEGER,
    raw_entry    TEXT,
    UNIQUE (project_id, build_order),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- Per-run details: one row per (project, report_number).
-- Populated incrementally — only new report numbers are fetched.
-- Unlike history_trend (capped at ~20), this covers every available report.
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      TEXT    NOT NULL,
    report_number   INTEGER NOT NULL,
    scraped_at      TEXT    NOT NULL,
    report_name     TEXT,
    -- test-result counts
    total           INTEGER,
    passed          INTEGER,
    failed          INTEGER,
    broken          INTEGER,
    skipped         INTEGER,
    unknown         INTEGER,
    -- timing (all in ms)
    duration_ms     INTEGER,
    start_ms        INTEGER,
    stop_ms         INTEGER,
    min_duration_ms INTEGER,
    max_duration_ms INTEGER,
    -- build info from widgets/environment.json
    build_version   TEXT,
    -- raw blob for forward-compatibility
    raw_summary     TEXT,
    UNIQUE (project_id, report_number),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- Scraped numeric metrics live in the generic metric_values table (created and
-- migrated by config_store.ensure_schema; one row per run × metric × series key).
-- The legacy boot_times table is no longer created or used.

-- CLI execution-time aggregates per run from test_gather_execution_time.
-- Populated from the "Execution Time Log" CSV attachment.
CREATE TABLE IF NOT EXISTS execution_times_summary (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT    NOT NULL,
    report_number  INTEGER NOT NULL,
    scraped_at     TEXT    NOT NULL,
    test_uid       TEXT,
    test_status    TEXT,
    row_count      INTEGER,
    total_sec      REAL,
    avg_sec        REAL,
    p95_sec        REAL,
    max_sec        REAL,
    slowest_cmd    TEXT,
    UNIQUE (project_id, report_number),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- Per-row CLI timing data for drill-down.  One row per CSV line.
CREATE TABLE IF NOT EXISTS execution_times (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT    NOT NULL,
    report_number  INTEGER NOT NULL,
    ts             TEXT,
    command        TEXT,
    duration_sec   REAL,
    test_name      TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- Individual test-case results from data/suites.json, latest run per project.
-- Used to compute feature status in the dashboard.
-- test_name  = base name with parametrize suffix stripped  (e.g. test_vlan)
-- test_params = the stripped suffix, empty string if none  (e.g. [str-accton-ecs5550-54x-None])
CREATE TABLE IF NOT EXISTS test_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT    NOT NULL,
    report_number INTEGER NOT NULL,
    test_name     TEXT    NOT NULL,
    test_params   TEXT    NOT NULL DEFAULT '',
    suite_name    TEXT,
    status        TEXT    NOT NULL,
    uid           TEXT,
    start_ms      INTEGER,
    stop_ms       INTEGER,
    duration_ms   INTEGER,
    UNIQUE (project_id, report_number, test_name, test_params),
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

-- Runs whose suites.json was fetched but yielded no test cases to store.
-- Acts as a "looked, nothing there" marker so the test_results stage doesn't
-- re-download the same suites.json on every scrape. Delete rows to force a re-scrape.
CREATE TABLE IF NOT EXISTS test_results_skips (
    project_id    TEXT    NOT NULL,
    report_number INTEGER NOT NULL,
    reason        TEXT,
    scraped_at    TEXT    NOT NULL,
    PRIMARY KEY (project_id, report_number)
);

-- Snapshot: new fails vs max(report_number) in `runs` less than this run (not Allure order).
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

CREATE INDEX IF NOT EXISTS idx_snapshots_project  ON report_snapshots(project_id, scraped_at);
CREATE INDEX IF NOT EXISTS idx_history_project    ON history_trend(project_id, build_order);
CREATE INDEX IF NOT EXISTS idx_runs_project       ON runs(project_id, report_number);
CREATE INDEX IF NOT EXISTS idx_test_results       ON test_results(project_id, report_number);
CREATE INDEX IF NOT EXISTS idx_test_results_name  ON test_results(test_name);
CREATE INDEX IF NOT EXISTS idx_exec_summary       ON execution_times_summary(project_id, report_number);
CREATE INDEX IF NOT EXISTS idx_exec_times_run     ON execution_times(project_id, report_number);
"""


def _migrate_split_test_params(db: sqlite3.Connection) -> None:
    """For rows where test_name still contains a parametrize suffix, move it to test_params.

    e.g.  test_name='test_lldp[str-accton-ecs4650-t0]', test_params=''
          →  test_name='test_lldp', test_params='[str-accton-ecs4650-t0]'

    Duplicate (project_id, report_number, test_name, test_params) rows are deleted.
    """
    count = db.execute(
        "SELECT COUNT(*) FROM test_results WHERE test_name LIKE '%[%'"
    ).fetchone()[0]
    if not count:
        return

    log.info("Migrating test_results: splitting params from %d rows", count)

    rows = db.execute(
        "SELECT id, project_id, report_number, test_name FROM test_results WHERE test_name LIKE '%[%'"
    ).fetchall()

    seen: set[tuple] = set()
    to_update: list[tuple] = []
    to_delete: list[int]   = []

    for row in rows:
        m = _PARAM_SUFFIX_RE.search(row["test_name"])
        if m:
            base   = row["test_name"][:m.start()]
            params = m.group(0)
        else:
            base   = row["test_name"]
            params = ""
        key = (row["project_id"], row["report_number"], base, params)
        if key in seen:
            to_delete.append(row["id"])
        else:
            seen.add(key)
            to_update.append((base, params, row["id"]))

    for base, params, row_id in to_update:
        db.execute(
            "UPDATE test_results SET test_name = ?, test_params = ? WHERE id = ?",
            (base, params, row_id),
        )
    for row_id in to_delete:
        db.execute("DELETE FROM test_results WHERE id = ?", (row_id,))

    db.commit()
    log.info(
        "Migration done: %d rows split, %d duplicates removed",
        len(to_update), len(to_delete),
    )


def get_db(path: str) -> sqlite3.Connection:
    # isolation_level=None → autocommit: each write releases the write lock
    # immediately instead of being held open across network fetches in the
    # scrape loops, which would otherwise block other processes (the web app's
    # schema init) for the whole scrape. db.commit() calls become harmless no-ops.
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    # Migrate: add build_version column to existing runs tables
    try:
        db.execute("ALTER TABLE runs ADD COLUMN build_version TEXT")
        db.commit()
    except Exception:
        pass  # column already exists
    # Migrate: add uid column to test_results
    try:
        db.execute("ALTER TABLE test_results ADD COLUMN uid TEXT")
        db.commit()
    except Exception:
        pass  # column already exists
    # Migrate: add test_params column (was used to store parametrize suffix separately)
    try:
        db.execute("ALTER TABLE test_results ADD COLUMN test_params TEXT NOT NULL DEFAULT ''")
        db.commit()
    except Exception:
        pass  # column already exists
    # Migrate: merge test_params back into test_name (full name now stored in test_name)
    count = db.execute(
        "SELECT COUNT(*) FROM test_results WHERE test_params != ''"
    ).fetchone()[0]
    if count:
        log.info("Migrating test_results: merging test_params back into test_name (%d rows)", count)
        # Drop rows that would become duplicates after the merge
        db.execute("""
            DELETE FROM test_results
            WHERE test_params != ''
              AND rowid NOT IN (
                  SELECT MIN(rowid) FROM test_results
                  WHERE test_params != ''
                  GROUP BY project_id, report_number, test_name || test_params
              )
        """)
        db.execute("""
            UPDATE test_results
            SET test_name = test_name || test_params, test_params = ''
            WHERE test_params != ''
        """)
        db.commit()
        log.info("Migration done: test_params merged into test_name")
    # Migrate: add timing columns to test_results
    for col, typedef in [("start_ms", "INTEGER"), ("stop_ms", "INTEGER"), ("duration_ms", "INTEGER")]:
        try:
            db.execute(f"ALTER TABLE test_results ADD COLUMN {col} {typedef}")
            db.commit()
        except Exception:
            pass  # column already exists
    # Create + seed the UI-configurable parsing/labels config tables.
    config_store.ensure_schema(db)
    db.commit()
    return db


def open_db_light(path: str) -> sqlite3.Connection:
    """Open a connection without schema/migration work.

    Used by scrape worker threads: the schema is guaranteed by the main
    thread's get_db() call. check_same_thread=False so the main thread can
    close these connections after the pool shuts down; each one is only
    ever *used* by the single worker thread that created it.
    """
    db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    return db


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------

class AllureClient:
    def __init__(self, base_url: str, username: Optional[str] = None, password: Optional[str] = None):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers["Accept"] = "application/json"
        if ACCEPT_SELF_SIGNED:
            self.session.verify = False
            requests.packages.urllib3.disable_warnings(
                requests.packages.urllib3.exceptions.InsecureRequestWarning
            )
        if username and password:
            # Try cookie-based login first (Allure Docker Service security)
            self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        url = f"{self.base_url}/login"
        try:
            resp = self.session.post(url, json={"username": username, "password": password}, timeout=10)
            resp.raise_for_status()
            log.info("Authenticated successfully")
        except requests.RequestException as e:
            log.warning("Login failed, falling back to basic auth: %s", e)
            self.session.auth = (username, password)

    def get(self, path: str, timeout: int = 15, **params) -> Optional[Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = self.session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.json()
            # For static report files (widgets/*.json) the content-type may be
            # text/plain or application/octet-stream — try parsing anyway.
            return resp.json()
        except requests.exceptions.ConnectionError as e:
            log.error("Cannot connect to %s: %s", url, e)
        except requests.exceptions.HTTPError as e:
            log.warning("HTTP %s for %s", e.response.status_code, url)
        except (requests.RequestException, ValueError) as e:
            log.warning("Request failed for %s: %s", url, e)
        return None

    def get_projects(self) -> list[dict]:
        data = self.get("/projects")
        if not data:
            return []
        # Response: {"data": {"projects": {"project-id": {...}, ...}}, ...}
        projects_map = (data.get("data") or {}).get("projects") or {}
        result = []
        for pid, info in projects_map.items():
            result.append({"id": pid, **(info or {})})
        return result

    def get_project(self, project_id: str) -> Optional[dict]:
        data = self.get(f"/projects/{project_id}")
        if not data:
            return None
        return (data.get("data") or {}).get("project")

    def get_report_file(self, project_id: str, file_path: str) -> Optional[Any]:
        """Fetch a file from the latest report of a project.

        file_path examples:
            widgets/summary.json
            widgets/history-trend.json
            widgets/duration-trend.json
        """
        return self.get(f"/projects/{project_id}/reports/latest/{file_path}")

    def get_project_report_numbers(self, project_id: str) -> list[int]:
        """Return all numeric report IDs for a project, sorted ascending.

        GET /projects/{id} returns {"reports_id": ["latest", "66", "65", ...]}
        We strip "latest" and return integers so callers can iterate oldest-first.
        """
        project = self.get_project(project_id)
        if not project:
            return []
        numbers: list[int] = []
        for rid in (project.get("reports_id") or []):
            try:
                numbers.append(int(rid))
            except (ValueError, TypeError):
                pass  # skip "latest" and other non-numeric tokens
        return sorted(numbers)

    def get_run_widget(self, project_id: str, report_number: int, widget: str) -> Optional[Any]:
        """Fetch a widget JSON file from a specific numbered report."""
        return self.get(f"/projects/{project_id}/reports/{report_number}/widgets/{widget}")

    def get_run_suites(self, project_id: str, report_number: int) -> Optional[Any]:
        """Fetch data/suites.json for a specific report (all test cases with UIDs).

        Uses a longer timeout since suites.json can be very large (1000+ tests).
        """
        return self.get(
            f"/projects/{project_id}/reports/{report_number}/data/suites.json",
            timeout=120,
        )

    def get_run_test_case(self, project_id: str, report_number: int, uid: str) -> Optional[Any]:
        """Fetch a single test case JSON by UID."""
        return self.get(f"/projects/{project_id}/reports/{report_number}/data/test-cases/{uid}.json")

    def get_run_attachment(self, project_id: str, report_number: int, source: str) -> Optional[Any]:
        """Fetch a JSON attachment file by its source filename."""
        return self.get(f"/projects/{project_id}/reports/{report_number}/data/attachments/{source}")

    def get_run_attachment_text(
        self, project_id: str, report_number: int, source: str, timeout: int = 60
    ) -> Optional[str]:
        """Fetch a non-JSON attachment (e.g. CSV) as raw text."""
        url = f"{self.base_url}/projects/{project_id}/reports/{report_number}/data/attachments/{source}"
        try:
            resp = self.session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.ConnectionError as e:
            log.error("Cannot connect to %s: %s", url, e)
        except requests.exceptions.HTTPError as e:
            log.warning("HTTP %s for %s", e.response.status_code, url)
        except requests.RequestException as e:
            log.warning("Request failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

def upsert_project(db: sqlite3.Connection, project_id: str, now: str) -> None:
    db.execute(
        """
        INSERT INTO projects (id, created_at, last_scraped_at)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET last_scraped_at = excluded.last_scraped_at
        """,
        (project_id, now, now),
    )


def scrape_summary(db: sqlite3.Connection, client: AllureClient, project_id: str, now: str) -> bool:
    summary = client.get_report_file(project_id, "widgets/summary.json")
    if not summary:
        log.warning("[%s] No summary data", project_id)
        return False

    stat = summary.get("statistic") or {}
    timing = summary.get("time") or {}

    db.execute(
        """
        INSERT INTO report_snapshots
            (project_id, scraped_at, total, passed, failed, broken, skipped, unknown,
             duration_ms, start_ms, stop_ms, raw_summary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            now,
            stat.get("total"),
            stat.get("passed"),
            stat.get("failed"),
            stat.get("broken"),
            stat.get("skipped"),
            stat.get("unknown"),
            timing.get("duration"),
            timing.get("start"),
            timing.get("stop"),
            json.dumps(summary),
        ),
    )
    log.info(
        "[%s] snapshot: total=%s passed=%s failed=%s broken=%s skipped=%s",
        project_id,
        stat.get("total"),
        stat.get("passed"),
        stat.get("failed"),
        stat.get("broken"),
        stat.get("skipped"),
    )
    return True


def scrape_history_trend(db: sqlite3.Connection, client: AllureClient, project_id: str, now: str) -> int:
    trend = client.get_report_file(project_id, "widgets/history-trend.json")
    if not trend:
        log.debug("[%s] No history-trend data", project_id)
        return 0

    if not isinstance(trend, list):
        log.warning("[%s] Unexpected history-trend format: %s", project_id, type(trend))
        return 0

    inserted = 0
    for entry in trend:
        build_order = entry.get("buildOrder")
        # API returns stats under "data" key (not "testRun")
        stats = entry.get("data") or {}
        try:
            db.execute(
                """
                INSERT OR IGNORE INTO history_trend
                    (project_id, scraped_at, build_order, report_name, report_url,
                     total, passed, failed, broken, skipped, unknown, duration_ms, raw_entry)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    now,
                    build_order,
                    entry.get("reportName"),
                    entry.get("reportUrl"),
                    stats.get("total"),
                    stats.get("passed"),
                    stats.get("failed"),
                    stats.get("broken"),
                    stats.get("skipped"),
                    stats.get("unknown"),
                    stats.get("duration"),
                    json.dumps(entry),
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except sqlite3.Error as e:
            log.warning("[%s] DB error inserting history entry build_order=%s: %s", project_id, build_order, e)

    log.info("[%s] history-trend: %d new entries (total in feed: %d)", project_id, inserted, len(trend))
    return inserted


def scrape_duration_trend(db: sqlite3.Connection, client: AllureClient, project_id: str, now: str) -> int:
    trend = client.get_report_file(project_id, "widgets/duration-trend.json")
    if not trend:
        log.debug("[%s] No duration-trend data", project_id)
        return 0

    if not isinstance(trend, list):
        return 0

    inserted = 0
    for entry in trend:
        build_order = entry.get("buildOrder")
        try:
            db.execute(
                """
                INSERT OR IGNORE INTO duration_trend
                    (project_id, scraped_at, build_order, duration_ms, raw_entry)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    now,
                    build_order,
                    # API returns {"data": {"duration": N}} not {"data": N}
                    (entry.get("data") or {}).get("duration"),
                    json.dumps(entry),
                ),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        except sqlite3.Error as e:
            log.warning("[%s] DB error inserting duration entry: %s", project_id, e)

    log.info("[%s] duration-trend: %d new entries", project_id, inserted)
    return inserted


def scrape_runs(db: sqlite3.Connection, client: AllureClient, project_id: str, now: str) -> int:
    """Fetch widgets/summary.json for every report number not yet stored.

    This is the primary per-run data source.  It is incremental: already-stored
    report numbers are skipped so repeated scrapes are fast.
    """
    report_numbers = client.get_project_report_numbers(project_id)
    if not report_numbers:
        log.debug("[%s] No report numbers found", project_id)
        return 0

    existing: set[int] = {
        row[0]
        for row in db.execute(
            "SELECT report_number FROM runs WHERE project_id = ?", (project_id,)
        )
    }

    new_numbers = [n for n in report_numbers if n not in existing]
    if not new_numbers:
        log.debug("[%s] All %d run(s) already stored", project_id, len(report_numbers))
        return 0

    log.info("[%s] Fetching %d new run(s) (have %d / %d total)",
             project_id, len(new_numbers), len(existing), len(report_numbers))

    inserted = 0
    for num in new_numbers:
        summary = client.get_run_widget(project_id, num, "summary.json")
        if not summary:
            log.warning("[%s] No summary for report %d", project_id, num)
            continue
        stat = summary.get("statistic") or {}
        timing = summary.get("time") or {}
        try:
            db.execute(
                """
                INSERT OR IGNORE INTO runs
                    (project_id, report_number, scraped_at, report_name,
                     total, passed, failed, broken, skipped, unknown,
                     duration_ms, start_ms, stop_ms, min_duration_ms, max_duration_ms,
                     raw_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id, num, now,
                    summary.get("reportName"),
                    stat.get("total"),
                    stat.get("passed"),
                    stat.get("failed"),
                    stat.get("broken"),
                    stat.get("skipped"),
                    stat.get("unknown"),
                    timing.get("duration"),
                    timing.get("start"),
                    timing.get("stop"),
                    timing.get("minDuration"),
                    timing.get("maxDuration"),
                    json.dumps(summary),
                ),
            )
            inserted += 1
        except sqlite3.Error as e:
            log.warning("[%s] DB error inserting run %d: %s", project_id, num, e)

    log.info("[%s] runs: +%d new (total stored: %d)", project_id, inserted, len(existing) + inserted)
    return inserted


def _find_test_in_suites(node: Any, test_name: str, results: list) -> None:
    """Recursively walk suites tree and collect entries whose name matches test_name."""
    if isinstance(node, dict):
        if node.get("name") == test_name and node.get("uid") and node.get("status"):
            results.append({"uid": node["uid"], "status": node["status"], "name": test_name})
        for v in node.values():
            if isinstance(v, (dict, list)):
                _find_test_in_suites(v, test_name, results)
    elif isinstance(node, list):
        for item in node:
            _find_test_in_suites(item, test_name, results)


def _scrape_metric_for_run(
    db: sqlite3.Connection,
    client: AllureClient,
    project_id: str,
    num: int,
    metric: dict,
    suites: Any,
    now: str,
) -> None:
    """Extract one configured metric from an already-fetched suites.json.

    1. Find the first test matching the metric's `test_names` candidates
    2. Fetch the test case and locate the attachment named `attachment_name`
    3. Extract each `value_keys` series (first present source key wins) and
       store one row per series key (NULL value marks the run as scraped even
       when the matching test or its attachment was absent/unparseable —
       otherwise the run would be re-fetched on every scrape forever)
    """
    metric_name = metric["name"]
    test_names = metric["test_names"]
    attachment_name = metric.get("attachment_name") or ""
    value_keys: dict = metric["value_keys"]

    # Try each configured test-name candidate; first found wins.
    matches: list[dict] = []
    for tn in test_names:
        if not matches:
            _find_test_in_suites(suites, tn, matches)
    if not matches:
        log.debug("[%s] #%d no %s test (%s) found — marking run as scraped",
                  project_id, num, metric_name, ", ".join(test_names))
        db.executemany(
            """
            INSERT OR IGNORE INTO metric_values
                (project_id, report_number, metric_name, value_key, value,
                 test_uid, test_status, scraped_at)
            VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            [(project_id, num, metric_name, key, now) for key in value_keys],
        )
        return

    # Use the first (usually only) match
    test_uid    = matches[0]["uid"]
    test_status = matches[0]["status"]

    tc = client.get_run_test_case(project_id, num, test_uid)
    if not tc:
        log.warning("[%s] #%d could not fetch test case %s", project_id, num, test_uid)
        return

    # Collect attachments from all stages (test may fail mid-run; the
    # attachment could be in beforeStages, testStage, or afterStages)
    all_attachments: list[dict] = []
    for stage in (tc.get("beforeStages") or []):
        all_attachments.extend(stage.get("attachments") or [])
    all_attachments.extend((tc.get("testStage") or {}).get("attachments") or [])
    for stage in (tc.get("afterStages") or []):
        all_attachments.extend(stage.get("attachments") or [])
    att = next(
        (a for a in all_attachments if a.get("name") == attachment_name),
        None
    )

    att_data = None
    if att:
        att_data = client.get_run_attachment(project_id, num, att["source"])
        if not isinstance(att_data, dict):
            att_data = None

    def _first(keys):
        for k in (keys or []):
            v = (att_data or {}).get(k)
            if v is not None:
                return v
        return None

    values = {key: _first(candidates) for key, candidates in value_keys.items()}

    try:
        db.executemany(
            """
            INSERT OR IGNORE INTO metric_values
                (project_id, report_number, metric_name, value_key, value,
                 test_uid, test_status, scraped_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (project_id, num, metric_name, key, value, test_uid, test_status, now)
                for key, value in values.items()
            ],
        )
        log.info("[%s] #%d %s: %s test_status=%s test_name=%s",
                 project_id, num, metric_name, values, test_status,
                 matches[0].get("name", "?"))
    except sqlite3.Error as e:
        log.warning("[%s] #%d DB error inserting %s: %s", project_id, num, metric_name, e)


def _scrape_execution_times_for_run(
    db: sqlite3.Connection,
    client: AllureClient,
    project_id: str,
    num: int,
    suites: Any,
    now: str,
) -> None:
    """Extract CLI execution-time data from an already-fetched suites.json.

    1. Find test_gather_execution_time in the suites tree
    2. Fetch the test case and locate the "Execution Time Log" CSV attachment
    3. Download the CSV, insert every row into execution_times,
       and a single aggregate row into execution_times_summary.
    """
    import csv as _csv
    import io

    matches: list[dict] = []
    _find_test_in_suites(suites, "test_gather_execution_time", matches)
    if not matches:
        # Sentinel row (test_uid NULL): the test doesn't exist in this run,
        # so mark it done instead of re-fetching suites.json every scrape.
        log.debug("[%s] #%d no test_gather_execution_time — marking run as scraped",
                  project_id, num)
        db.execute(
            """
            INSERT OR IGNORE INTO execution_times_summary
                (project_id, report_number, scraped_at, test_uid, test_status,
                 row_count, total_sec, avg_sec, p95_sec, max_sec, slowest_cmd)
            VALUES (?, ?, ?, NULL, NULL, 0, 0, NULL, NULL, 0, NULL)
            """,
            (project_id, num, now),
        )
        return

    test_uid    = matches[0]["uid"]
    test_status = matches[0]["status"]

    tc = client.get_run_test_case(project_id, num, test_uid)
    if not tc:
        return

    all_attachments: list[dict] = []
    for stage in (tc.get("beforeStages") or []):
        all_attachments.extend(stage.get("attachments") or [])
    all_attachments.extend((tc.get("testStage") or {}).get("attachments") or [])
    for stage in (tc.get("afterStages") or []):
        all_attachments.extend(stage.get("attachments") or [])

    att = next(
        (a for a in all_attachments if a.get("name") == "Execution Time Log"),
        None,
    )

    row_count = 0
    total_sec = 0.0
    max_sec   = 0.0
    p95_sec   = None
    avg_sec   = None
    slowest_cmd: Optional[str] = None

    if att and att.get("source"):
        text = client.get_run_attachment_text(project_id, num, att["source"])
        if text:
            reader = _csv.DictReader(io.StringIO(text))
            durations: list[float] = []
            batch = []
            for row in reader:
                try:
                    dur = float(row.get("execution_time_sec") or 0)
                except ValueError:
                    continue
                batch.append((
                    project_id, num,
                    row.get("timestamp") or None,
                    row.get("command")   or None,
                    dur,
                    row.get("test_name") or None,
                ))
                durations.append(dur)
                if dur > max_sec:
                    max_sec     = dur
                    slowest_cmd = row.get("command")

            if batch:
                db.executemany(
                    """
                    INSERT INTO execution_times
                        (project_id, report_number, ts, command, duration_sec, test_name)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                row_count = len(batch)
                total_sec = sum(durations)
                avg_sec   = total_sec / row_count
                durations.sort()
                idx       = max(0, int(round(0.95 * (row_count - 1))))
                p95_sec   = durations[idx]

    try:
        db.execute(
            """
            INSERT OR IGNORE INTO execution_times_summary
                (project_id, report_number, scraped_at, test_uid, test_status,
                 row_count, total_sec, avg_sec, p95_sec, max_sec, slowest_cmd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, num, now, test_uid, test_status,
             row_count, total_sec, avg_sec, p95_sec, max_sec, slowest_cmd),
        )
        log.info(
            "[%s] #%d exec_time: rows=%d total=%.1fs max=%.2fs slowest=%r",
            project_id, num, row_count, total_sec, max_sec, slowest_cmd,
        )
    except sqlite3.Error as e:
        log.warning("[%s] #%d DB error inserting execution_times_summary: %s", project_id, num, e)


_PARAM_SUFFIX_RE = re.compile(r'(\[.*\])$')


def _split_test_name(name: str) -> tuple[str, str]:
    """Split a test name into (base_name, params).

    e.g. 'test_vlan[str-accton-ecs5550-54x-None]' → ('test_vlan', '[str-accton-ecs5550-54x-None]')
         'test_vlan'                                 → ('test_vlan', '')
    """
    m = _PARAM_SUFFIX_RE.search(name)
    if m:
        return name[:m.start()], m.group(1)
    return name, ""


def _collect_leaf_tests(node: Any, top_suite: Optional[str] = None) -> list[tuple]:
    """Walk Allure suites tree; return list of tuples for every leaf test.

    Each tuple: (test_name, test_params, top_suite_name, status, uid, start_ms, stop_ms, duration_ms)

    test_name  = base name with parametrize suffix stripped (e.g. 'test_vlan')
    test_params = the suffix, empty string if none (e.g. '[str-accton-ecs5550-54x-None]')

    Handles both dict root ({"name": "suites", "children": [...]}) and list root.
    """
    results: list[tuple] = []

    def walk(n: Any, ts: Optional[str]) -> None:
        if isinstance(n, list):
            for item in n:
                walk(item, ts)
            return
        if not isinstance(n, dict):
            return
        name     = n.get("name", "")
        uid      = n.get("uid")
        status   = n.get("status")
        children = n.get("children") or []
        # Only treat as a leaf when there are no children — even if uid+status
        # are set on a container node, we must descend to collect its children.
        if uid and status and not children:
            timing = n.get("time") or {}
            results.append((
                name, "", ts, status, uid,
                timing.get("start"), timing.get("stop"), timing.get("duration"),
            ))
            return
        for child in children:
            # Children of the root "suites" node become the top-level suite name
            if name in ("suites", "") and ts is None:
                walk(child, child.get("name") if isinstance(child, dict) else None)
            else:
                walk(child, ts)

    walk(node, top_suite)
    return results


def _scrape_test_results_for_run(
    db: sqlite3.Connection,
    client: AllureClient,
    project_id: str,
    num: int,
    suites: Any,
    expected_total: int,
    now: str,
) -> int:
    """Store individual test-case results from an already-fetched suites.json."""
    tests = _collect_leaf_tests(suites)
    if not tests:
        log.warning("[%s] #%d suites.json returned 0 leaf tests — marking as skipped", project_id, num)
        db.execute(
            "INSERT OR IGNORE INTO test_results_skips (project_id, report_number, reason, scraped_at) "
            "VALUES (?, ?, '0 leaf tests', ?)",
            (project_id, num, now),
        )
        return 0

    # Expect total from runs table; warn if collected count is much lower
    if expected_total > 0 and len(tests) < expected_total * 0.8:
        log.warning(
            "[%s] #%d collected only %d tests but report total=%d "
            "(parametrize dedup or partial response)",
            project_id, num, len(tests), expected_total,
        )

    inserted_names: list[str] = []
    skipped: list[tuple[str, str, str]] = []  # (display_name, status, reason)
    seen_keys: set[tuple[str, str]] = set()
    for test_name, test_params, suite_name, status, uid, start_ms, stop_ms, duration_ms in tests:
        key = (test_name, test_params)
        if key in seen_keys:
            skipped.append((f"{test_name}{test_params}", status, "duplicate in suites.json"))
            continue
        seen_keys.add(key)
        try:
            db.execute(
                """
                INSERT OR IGNORE INTO test_results
                    (project_id, report_number, test_name, test_params, suite_name, status, uid,
                     start_ms, stop_ms, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, num, test_name, test_params, suite_name, status, uid,
                 start_ms, stop_ms, duration_ms),
            )
            if db.execute("SELECT changes()").fetchone()[0]:
                inserted_names.append(f"{test_name}{test_params}")
            else:
                skipped.append((f"{test_name}{test_params}", status, "already in DB"))
        except sqlite3.Error as e:
            log.warning("[%s] #%d DB error inserting test_result %s: %s", project_id, num, test_name, e)
            skipped.append((f"{test_name}{test_params}", status, str(e)))

    suite_dups   = [n for n, _, r in skipped if r == "duplicate in suites.json"]
    other_skipped = [(n, r) for n, _, r in skipped if r != "duplicate in suites.json"]

    collected_names = [f"{n}{p}" for n, p, _, _, _, _, _, _ in tests]
    log.info(
        "[%s] #%d test_results: %d collected, %d inserted, %d suite-dups, %d skipped (expected total=%d)\n"
        "  collected: %s\n"
        "  inserted:  %s",
        project_id, num, len(tests), len(inserted_names), len(suite_dups), len(other_skipped), expected_total,
        ", ".join(collected_names) or "(none)",
        ", ".join(inserted_names) or "(none)",
    )
    if suite_dups:
        log.debug("[%s] #%d suite-dups (same test under multiple suites): %s",
                  project_id, num, ", ".join(suite_dups))
    if other_skipped:
        log.warning("[%s] #%d skipped (unexpected): %s",
                    project_id, num, ", ".join(f"{n} ({r})" for n, r in other_skipped))

    try:
        upsert_run_new_fails(db, client, project_id, num, now_iso=now)
    except Exception as e:
        log.warning("[%s] #%d run_new_fails upsert failed: %s", project_id, num, e)

    return len(inserted_names)


def scrape_run_details(db: sqlite3.Connection, client: AllureClient, project_id: str, now: str) -> None:
    """Fetch suites.json once per pending run and feed every consumer from it.

    Replaces the former per-stage loops (scrape_metrics, scrape_test_results,
    scrape_execution_times), each of which independently downloaded the same
    suites.json for a run. A run is pending when at least one stage still
    needs it; each stage keeps its own incremental "already done" bookkeeping
    (metric_values rows, test_results/test_results_skips rows, and
    execution_times_summary rows respectively), so runs where suites.json
    could not be fetched stay pending and are retried next scrape.
    """
    all_run_rows = db.execute(
        "SELECT report_number, total FROM runs WHERE project_id = ? ORDER BY report_number",
        (project_id,),
    ).fetchall()
    if not all_run_rows:
        return
    run_numbers = [r["report_number"] for r in all_run_rows]
    expected_totals = {r["report_number"]: (r["total"] or 0) for r in all_run_rows}

    # Pending per configured metric (invalid/incomplete definitions are skipped)
    metrics = [
        m for m in config_store.get_config(db).metrics
        if m.get("name") and m.get("test_names") and m.get("value_keys")
    ]
    metric_pending: dict[str, set[int]] = {}
    for metric in metrics:
        already_done = {
            row[0] for row in db.execute(
                "SELECT DISTINCT report_number FROM metric_values "
                "WHERE project_id = ? AND metric_name = ?",
                (project_id, metric["name"]),
            )
        }
        metric_pending[metric["name"]] = set(run_numbers) - already_done

    # Pending test_results: nothing stored and not marked as known-empty
    stored_counts: dict[int, int] = {
        row[0]: row[1]
        for row in db.execute(
            "SELECT report_number, COUNT(*) FROM test_results "
            "WHERE project_id = ? GROUP BY report_number",
            (project_id,),
        )
    }
    tr_skips: set[int] = {
        row[0] for row in db.execute(
            "SELECT report_number FROM test_results_skips WHERE project_id = ?",
            (project_id,),
        )
    }
    tr_pending = {n for n in run_numbers if stored_counts.get(n, 0) == 0 and n not in tr_skips}

    # Pending execution_times
    et_done: set[int] = {
        row[0] for row in db.execute(
            "SELECT report_number FROM execution_times_summary WHERE project_id = ?",
            (project_id,),
        )
    }
    et_pending = set(run_numbers) - et_done

    union: set[int] = tr_pending | et_pending
    for nums in metric_pending.values():
        union |= nums
    if not union:
        log.debug("[%s] run details up to date for all %d run(s)", project_id, len(run_numbers))
        return

    log.info(
        "[%s] Fetching suites.json for %d run(s) (metrics=%d, test_results=%d, exec_times=%d)",
        project_id, len(union),
        sum(len(nums) for nums in metric_pending.values()), len(tr_pending), len(et_pending),
    )

    for num in sorted(union):
        suites = client.get_run_suites(project_id, num)
        if not suites:
            log.warning("[%s] #%d no suites.json — run stays pending", project_id, num)
            continue
        for metric in metrics:
            if num in metric_pending[metric["name"]]:
                _scrape_metric_for_run(db, client, project_id, num, metric, suites, now)
        if num in tr_pending:
            _scrape_test_results_for_run(
                db, client, project_id, num, suites, expected_totals.get(num, 0), now
            )
        if num in et_pending:
            _scrape_execution_times_for_run(db, client, project_id, num, suites, now)


def scrape_build_versions(db: sqlite3.Connection, client: AllureClient, project_id: str) -> int:
    """Fetch widgets/environment.json for runs whose build_version is not yet stored.

    Extracts the 'Version' entry and stores it in runs.build_version.
    Runs where environment.json is unavailable are marked with '' to avoid
    re-fetching on every scrape.
    """
    pending = [
        row[0] for row in db.execute(
            "SELECT report_number FROM runs WHERE project_id = ? AND build_version IS NULL ORDER BY report_number",
            (project_id,),
        )
    ]
    if not pending:
        return 0

    updated = 0
    for num in pending:
        env = client.get_run_widget(project_id, num, "environment.json")
        version = None
        if env and isinstance(env, list):
            version = next(
                (
                    (entry.get("values") or [None])[0]
                    for entry in env
                    if entry.get("name") == "Version"
                ),
                None,
            )
        db.execute(
            "UPDATE runs SET build_version = ? WHERE project_id = ? AND report_number = ?",
            (version or "", project_id, num),
        )
        if version:
            updated += 1
            log.info("[%s] #%d build_version=%s", project_id, num, version)

    if updated:
        log.info("[%s] build_versions: +%d filled", project_id, updated)
    return updated


def clean_db(db: sqlite3.Connection) -> None:
    """Delete all scraped data from every table."""
    tables = [
        "execution_times", "execution_times_summary",
        "test_results", "test_results_skips", "run_new_fails", "metric_values", "runs",
        "duration_trend", "history_trend", "report_snapshots", "projects",
    ]
    # Legacy table on pre-migration DBs; cleared too when present.
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'boot_times'"
    ).fetchone():
        tables.append("boot_times")
    for table in tables:
        db.execute(f"DELETE FROM {table}")  # noqa: S608
    db.commit()
    log.info("Database cleaned: all rows deleted from %d tables", len(tables))


def scrape_project(db: sqlite3.Connection, client: AllureClient, pid: str, now: str) -> None:
    """Run every scrape stage for a single project."""
    upsert_project(db, pid, now)
    scrape_history_trend(db, client, pid, now)  # last ~20 from trend widget
    scrape_duration_trend(db, client, pid, now) # last ~20 from trend widget
    scrape_runs(db, client, pid, now)           # all individual runs, incremental
    scrape_build_versions(db, client, pid)     # Version from widgets/environment.json
    scrape_run_details(db, client, pid, now)   # suites.json once per pending run →
                                               # metrics, test_results, execution_times


def scrape_all(
    db: sqlite3.Connection,
    client: AllureClient,
    project_id: Optional[str] = None,
    db_path: Optional[str] = None,
    workers: int = SCRAPE_WORKERS,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    log.info("Starting scrape at %s", now)

    projects = client.get_projects()
    if not projects:
        log.warning("No projects returned. Check ALLURE_BASE_URL and credentials.")
        return

    if project_id:
        projects = [p for p in projects if p["id"] == project_id]
        if not projects:
            log.error("Project %r not found. Available: %s", project_id,
                      [p["id"] for p in client.get_projects()])
            return

    pids = [p["id"] for p in projects]
    log.info("Found %d project(s): %s", len(pids), pids)

    if workers <= 1 or len(pids) <= 1 or not db_path:
        for pid in pids:
            scrape_project(db, client, pid, now)
    else:
        # Each worker thread gets its own sqlite connection (the module forbids
        # cross-thread use) and its own AllureClient (requests.Session is not
        # thread-safe). WAL + busy_timeout + autocommit keep writers from clashing.
        tls = threading.local()
        thread_dbs: list[sqlite3.Connection] = []
        dbs_lock = threading.Lock()

        def run_one(pid: str) -> None:
            if getattr(tls, "db", None) is None:
                tls.db = open_db_light(db_path)
                tls.client = AllureClient(BASE_URL, USERNAME, PASSWORD)
                with dbs_lock:
                    thread_dbs.append(tls.db)
            try:
                scrape_project(tls.db, tls.client, pid, now)
            except Exception as e:
                log.error("[%s] project scrape failed: %s", pid, e, exc_info=True)

        with ThreadPoolExecutor(max_workers=min(workers, len(pids))) as pool:
            list(pool.map(run_one, pids))
        for conn in thread_dbs:
            conn.close()

    db.commit()
    log.info("Scrape complete.")


# ---------------------------------------------------------------------------
# Discover mode: print raw API responses
# ---------------------------------------------------------------------------

def discover(client: AllureClient) -> None:
    print("\n=== /projects ===")
    data = client.get("/projects")
    print(json.dumps(data, indent=2))

    projects = client.get_projects()
    if not projects:
        print("No projects found.")
        return

    pid = projects[0]["id"]
    print(f"\n=== /projects/{pid} ===")
    print(json.dumps(client.get_project(pid), indent=2))

    for widget in ("widgets/summary.json", "widgets/history-trend.json", "widgets/duration-trend.json"):
        print(f"\n=== reports/latest/{widget} ({pid}) ===")
        data = client.get_report_file(pid, widget)
        # Print only first 40 lines to keep output manageable
        text = json.dumps(data, indent=2)
        lines = text.splitlines()
        print("\n".join(lines[:40]))
        if len(lines) > 40:
            print(f"... ({len(lines) - 40} more lines)")


# ---------------------------------------------------------------------------
# Run-tests table
# ---------------------------------------------------------------------------

def print_run_tests(db: sqlite3.Connection, client: AllureClient, project_id: str, run_number: int) -> None:
    """Print a table of test names stored in the DB for a project+run,
    alongside the Allure total for that run."""

    # Allure total from runs table
    run_row = db.execute(
        "SELECT total, passed, failed, broken, skipped FROM runs WHERE project_id = ? AND report_number = ?",
        (project_id, run_number),
    ).fetchone()

    # Test results from DB
    rows = db.execute(
        "SELECT test_name, suite_name, status FROM test_results "
        "WHERE project_id = ? AND report_number = ? ORDER BY suite_name, test_name",
        (project_id, run_number),
    ).fetchall()

    allure_total = run_row["total"] if run_row else "?"
    db_count     = len(rows)

    # Column widths
    max_name  = max((len(r["test_name"])  for r in rows), default=9)
    max_suite = max((len(r["suite_name"] or "") for r in rows), default=5)
    w_name  = max(max_name,  9)
    w_suite = max(max_suite, 5)
    w_status = 7

    header = f"{'TEST NAME':<{w_name}}  {'SUITE':<{w_suite}}  {'STATUS':<{w_status}}"
    sep    = "-" * len(header)

    print(f"\nProject : {project_id}")
    print(f"Run     : #{run_number}")
    print(f"DB rows : {db_count}   Allure total : {allure_total}")
    if run_row:
        print(f"Allure  : passed={run_row['passed']} failed={run_row['failed']} "
              f"broken={run_row['broken']} skipped={run_row['skipped']}")
    print()
    print(header)
    print(sep)
    for r in rows:
        print(f"{r['test_name']:<{w_name}}  {(r['suite_name'] or ''):<{w_suite}}  {r['status']:<{w_status}}")
    print(sep)
    print(f"Total in DB: {db_count}   Total in Allure: {allure_total}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Allure Docker Service scraper")
    parser.add_argument("--watch", action="store_true", help="Scrape continuously on SCRAPE_INTERVAL")
    parser.add_argument("--discover", action="store_true", help="Print raw API responses and exit")
    parser.add_argument("--db", default=DB_PATH, help=f"SQLite database path (default: {DB_PATH})")
    parser.add_argument(
        "--project", metavar="PROJECT_ID", nargs="?", const="",
        help="Scrape only PROJECT_ID; omit the value to list all available project IDs",
    )
    parser.add_argument("--clean", action="store_true", help="Delete all data from the database and exit")
    parser.add_argument("--backfill-metrics", "--backfill-boot-times",
                        dest="backfill_metrics", action="store_true",
                        help="Re-scrape all configured metrics for all runs "
                             "(clears existing metric_values rows first). "
                             "Use with --project to limit scope.")
    parser.add_argument("--backfill-execution-times", action="store_true",
                        help="Re-scrape CLI execution-time log for all runs "
                             "(clears existing execution_times + summary rows first). "
                             "Use with --project to limit scope.")
    parser.add_argument(
        "--backfill-new-fails",
        action="store_true",
        help="Recompute run_new_fails for every run that has test_results (uses Allure for status messages). "
             "Use with --project to limit scope.",
    )
    parser.add_argument("--run", metavar="RUN_NUMBER", type=int,
                        help="Print test table for PROJECT_ID + RUN_NUMBER and exit")
    args = parser.parse_args()

    client = AllureClient(BASE_URL, USERNAME, PASSWORD)

    if args.discover:
        discover(client)
        return

    # --project with no value → list projects and exit (no DB needed)
    if args.project == "":
        projects = client.get_projects()
        if not projects:
            print("No projects found.")
        else:
            print(f"Available projects ({len(projects)}):")
            for p in sorted(projects, key=lambda x: x["id"]):
                print(f"  {p['id']}")
        return

    db = get_db(args.db)
    log.info("Database: %s", Path(args.db).resolve())
    log.info("Allure base URL: %s", BASE_URL)

    if args.clean:
        clean_db(db)
        db.close()
        return

    if args.backfill_metrics:
        now = datetime.now(timezone.utc).isoformat()
        if args.project:
            projects = [args.project]
        else:
            projects = [
                row[0] for row in db.execute("SELECT id FROM projects ORDER BY id")
            ]
        for pid in projects:
            deleted = db.execute(
                "DELETE FROM metric_values WHERE project_id = ?", (pid,)
            ).rowcount
            db.commit()
            log.info("[%s] cleared %d metric_values row(s), re-scraping...", pid, deleted)
            # Only the cleared metric rows are pending now, so this re-scrapes just metrics
            scrape_run_details(db, client, pid, now)
        db.commit()
        db.close()
        return

    if args.backfill_execution_times:
        now = datetime.now(timezone.utc).isoformat()
        if args.project:
            projects = [args.project]
        else:
            projects = [
                row[0] for row in db.execute("SELECT id FROM projects ORDER BY id")
            ]
        for pid in projects:
            d1 = db.execute("DELETE FROM execution_times WHERE project_id = ?", (pid,)).rowcount
            d2 = db.execute("DELETE FROM execution_times_summary WHERE project_id = ?", (pid,)).rowcount
            db.commit()
            log.info("[%s] cleared %d detail + %d summary row(s), re-scraping...", pid, d1, d2)
            # Only the cleared exec-time rows are pending now, so this re-scrapes just those
            scrape_run_details(db, client, pid, now)
        db.commit()
        db.close()
        return

    if args.backfill_new_fails:
        now = datetime.now(timezone.utc).isoformat()
        if args.project:
            projects = [args.project]
        else:
            projects = [
                row[0] for row in db.execute("SELECT id FROM projects ORDER BY id")
            ]
        for pid in projects:
            n = backfill_project(db, client, pid, now_iso=now)
            db.commit()
            log.info("[%s] run_new_fails backfill: %d run(s) upserted", pid, n)
        db.close()
        return

    if args.run is not None:
        if not args.project:
            print("Error: --run requires --project PROJECT_ID")
            db.close()
            return
        print_run_tests(db, client, args.project, args.run)
        db.close()
        return

    if args.watch:
        log.info("Watch mode: scraping every %ds. Ctrl-C to stop.", SCRAPE_INTERVAL)
        while True:
            try:
                scrape_all(db, client, project_id=args.project, db_path=args.db)
            except Exception as e:
                log.error("Scrape failed: %s", e, exc_info=True)
            log.info("Next scrape in %ds...", SCRAPE_INTERVAL)
            time.sleep(SCRAPE_INTERVAL)
    else:
        scrape_all(db, client, project_id=args.project, db_path=args.db)
        db.close()


if __name__ == "__main__":
    main()
