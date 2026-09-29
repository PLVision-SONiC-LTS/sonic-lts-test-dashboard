"""
Test Dashboard — MCP (Model Context Protocol) server.

Exposes dashboard data as MCP tools so AI assistants can query test results,
run history, scraped metrics, and the feature-status matrix directly.

Default tool parameters favor compact JSON (short keys, tabular matrix, no
heavy log attachments) to reduce token use—set compact=false where available
for the previous verbose shapes. Call compact_format_help() for a static key
legend (no DB). Repeated long project_id strings are deduplicated in
get_feature_status (refs) and search_test_results (projects + pi) by default.

Timestamps: *ms, t0, t1, ls, etc. are Unix epoch milliseconds (absolute UTC instant).
Optional include_iso_utc=true adds ISO-8601 UTC strings (…Z) alongside them on supported tools.

Run with:
    .venv/bin/python mcp_server.py                        # stdio (default)
    .venv/bin/python mcp_server.py --http                 # HTTP/SSE on 0.0.0.0:8001
    .venv/bin/python mcp_server.py --http --port 9000
    .venv/bin/python mcp_server.py --http --host 127.0.0.1 --port 8001

Connect from Claude Code (HTTP/SSE):
    claude mcp add --transport sse test-dashboard http://localhost:8001/sse
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional, Union

import requests as _requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "allure_data.db")
ALLURE_BASE_URL = os.getenv(
    "ALLURE_BASE_URL", "http://localhost:5050/allure-docker-service"
).rstrip("/")

_args = argparse.ArgumentParser()
_args.add_argument("--http", action="store_true", help="Run as HTTP/SSE server")
_args.add_argument("--host", default="0.0.0.0")
_args.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8001")))
_parsed = _args.parse_args()

mcp = FastMCP(
    "Test Dashboard",
    host=_parsed.host,
    port=_parsed.port,
)

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


# ---------------------------------------------------------------------------
# Project-ID parsing — shared, DB-backed config (see config_store.py)
# ---------------------------------------------------------------------------

import config_store  # noqa: E402
from config_store import parse_project_id as _parse_project_id  # noqa: E402


def _iso_utc_ms(ms: Optional[int]) -> Optional[str]:
    """Format epoch milliseconds as ISO-8601 UTC (…Z)."""
    if ms is None:
        return None
    return (
        datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        + "Z"
    )


def _pass_pct(passed: int, total: int, skipped: int, exclude_skipped: bool) -> float | None:
    """Pass rate as a 1-decimal percent, or None when the denominator is empty.

    When exclude_skipped is true, skipped tests are dropped from the denominator
    (passed / (total - skipped)); otherwise the Allure total is used as-is.
    """
    denom = total - skipped if exclude_skipped else total
    return round(passed * 100.0 / denom, 1) if denom > 0 else None


def _row_to_stats(row, *, include_iso_utc: bool = False, exclude_skipped: bool = False) -> dict:
    total  = row["total"]  or 0
    passed = row["passed"] or 0
    skipped = row["skipped"] or 0
    keys   = row.keys() if hasattr(row, "keys") else []
    out = {
        "report_number": row["report_number"],
        "total":    total,
        "passed":   passed,
        "failed":   row["failed"]  or 0,
        "broken":   row["broken"]  or 0,
        "skipped":  skipped,
        "unknown":  row["unknown"] or 0,
        "pass_pct": _pass_pct(passed, total, skipped, exclude_skipped),
        "duration_ms":   row["duration_ms"],
        "start_ms":      row["start_ms"],
        "stop_ms":       row["stop_ms"],
        "build_version": row["build_version"] if "build_version" in keys else None,
    }
    if include_iso_utc:
        out["start_iso"] = _iso_utc_ms(row["start_ms"])
        out["stop_iso"] = _iso_utc_ms(row["stop_ms"])
    return out


# Compact encoding for LLM-friendly, low-token payloads
_MATRIX_CODE = {
    "passed": "p", "failed": "f", "broken": "b", "skipped": "k", "unknown": "u",
    "no_tests": ".", None: "n",
}
_MATRIX_CODE_REV = "p=passed f=failed b=broken k=skipped u=unknown .=no_tests n=unmapped"


def _compact_format_help_dict() -> dict[str, Any]:
    """Single reference for short keys (kept in sync with compact outputs)."""
    return {
        "schema_version": 3,
        "when_to_use": (
            "Most tools default to compact=true. If a key is ambiguous, call this tool "
            "(no database access). For human-readable field names, pass compact=false on the same tool."
        ),
        "suggested_flow": [
            "dashboard_overview — all projects, latest pass%",
            "get_project_runs — history (latest run first); use get_failed_tests + get_test_log for details",
            "list_views — configured matrix views; their scope values feed get_feature_status/get_feature_tests",
            "get_feature_status — feature×platform matrix when compact=true uses columns + rows + legend",
        ],
        "dashboard_overview": {
            "n": "number of projects",
            "legend": "inline string on the response",
            "projects[]": "id, v, c, pl, rn, pct, t, x",
        },
        "project_row": {
            "id": "project_id string",
            "v": "variant key (as parsed from the project id)",
            "c": "cadence key (daily, weekly, …)",
            "pl": "platform slug",
        },
        "run_stats_compact": {
            "rn": "report_number",
            "t": "total tests",
            "p": "passed count (not pass%)",
            "f": "failed + broken count combined",
            "pct": "pass rate percent (passed/total; passed/(total-skipped) when exclude_skipped=true)",
            "bv": "build_version when present",
            "s0": "optional: ISO UTC start when include_iso_utc=true",
            "s1": "optional: ISO UTC stop when include_iso_utc=true",
        },
        "get_feature_status_compact": {
            "columns": "with dedupe_projects=true (default): p0,p1,…; refs maps pk→full project_id",
            "refs": "only when dedupe_projects=true; same column order as cells in each row",
            "legend": _MATRIX_CODE_REV,
            "rows": "each row is [feature_label, cell0, cell1, …]; feature_label is group|name or name",
        },
        "get_feature_tests": "verbose keys; lists test names mapped to each feature (what get_feature_status aggregates)",
        "repeated_strings": (
            "Long project_id values are deduplicated: feature matrix uses refs+short column ids; "
            "search_test_results uses projects[] + pi index per row when dedupe_project_ids=true."
        ),
        "timestamps": (
            "All *ms, t0, t1, ls (and similar) values are Unix epoch milliseconds — absolute instants in UTC. "
            "When include_iso_utc=true on list_projects, get_project_runs, dashboard_overview, get_metric_history, "
            "get_scraper_summary: compact run stats add s0/s1 (ISO UTC start/stop); verbose adds start_iso/stop_iso; "
            "overview adds start_iso per project; metric history adds start_iso; scraper adds ls_iso."
        ),
        "get_failed_tests_compact": {"n": "full test name", "s": "suite", "t": "status", "u": "uid for get_test_log", "ms": "duration ms", "t0": "start_ms epoch", "t1": "stop_ms epoch"},
        "get_run_timeline_compact": {"n": "test name", "s": "suite", "z": "status", "u": "uid", "t0": "start_ms epoch", "t1": "stop_ms epoch", "ms": "duration ms", "order": "sorted by t0 asc, nulls last"},
        "get_passed_tests_compact": {"n": "test name", "s": "suite", "u": "uid for get_test_log", "ms": "duration ms", "t0": "start_ms epoch", "t1": "stop_ms epoch"},
        "get_skipped_tests_compact": {"n": "test name", "s": "suite", "u": "uid", "ms": "duration ms", "t0": "start_ms epoch", "t1": "stop_ms epoch"},
        "search_test_results_compact": {
            "list_form": "when dedupe_project_ids=false: [{pid, rn, n, s, t, ms, t0, t1}, …]",
            "deduped_form": "when dedupe_project_ids=true (default): {projects: [id…], rows: [{pi, rn, n, s, t, ms, t0, t1}]} — pi indexes projects",
            "ms": "duration ms", "t0": "start_ms epoch", "t1": "stop_ms epoch",
        },
        "get_test_log_compact": {
            "n": "test name",
            "z": "status",
            "m": "status message",
            "tr": "trace (truncated)",
            "prm": "parameters as comma-separated name=value",
            "ms": "duration ms",
            "ts": "test steps tree; n=name z=status c=child steps",
        },
        "get_metric_history_compact": {
            "rn": "report_number",
            "v": "values map {series_key: value} for the metric's configured series",
            "z": "test_status",
            "ms": "start_ms",
        },
        "get_scraper_summary_compact": {
            "tr": "total runs",
            "tp": "total projects",
            "p[]": "pid, rc=run_count, lr=latest_run, ls=latest_stop_ms",
        },
        "get_test_neighbors_compact": {"returns": "{before:[...], target:{...}, after:[...]}", "keys": "n, s, z, u, t0, t1, ms", "note": "before list is chronological (oldest first); ordered by start_ms"},
        "get_test_history_compact": {"rn": "report_number", "n": "test name+params", "z": "status", "u": "uid", "ms": "duration ms", "t0": "start_ms epoch", "t1": "stop_ms epoch", "order": "newest run first; test_name accepts LIKE patterns"},
        "get_test_logs_bulk": "each item includes uid plus get_test_log fields; max 30 uids per call",
        "errors": "get_test_log may return error + url on fetch failure (verbose keys)",
    }


def _row_to_stats_compact(row, *, include_iso_utc: bool = False, exclude_skipped: bool = False) -> dict[str, Any]:
    total  = row["total"] or 0
    passed = row["passed"] or 0
    skipped = row["skipped"] or 0
    keys   = row.keys() if hasattr(row, "keys") else []
    out: dict[str, Any] = {
        "rn": row["report_number"],
        "t": total,
        "p": passed,
        "f": (row["failed"] or 0) + (row["broken"] or 0),
        "pct": _pass_pct(passed, total, skipped, exclude_skipped),
    }
    if "build_version" in keys and row["build_version"]:
        out["bv"] = row["build_version"]
    if include_iso_utc:
        out["s0"] = _iso_utc_ms(row["start_ms"])
        out["s1"] = _iso_utc_ms(row["stop_ms"])
    return out


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

@mcp.tool()
def compact_format_help() -> dict[str, Any]:
    """Explain short JSON keys used when compact=true (the default on most tools).

    Does not query the database. Use when interpreting list_projects, get_feature_status,
    get_test_log, etc. Avoids a separate ultra-compact encoding layer—one schema reference
    keeps tokens low while staying understandable.
    """
    return _compact_format_help_dict()


@mcp.tool()
def dashboard_overview(include_iso_utc: bool = False, exclude_skipped: bool = False) -> dict[str, Any]:
    """One-shot snapshot of all projects (latest run per project). Prefer this over list_projects when you only need pass rates and IDs — minimal JSON, fewer tokens.

    Keys are short: id, v (variant), c (cadence), pl (platform), rn (report #), pct, t (total tests), x (failed+broken count).
    Set include_iso_utc=true to add start_iso (UTC) from the latest run start_ms.
    Set exclude_skipped=true to drop skipped tests from the pass-rate denominator (pct = passed / (total - skipped)).
    """
    db = _get_db()
    try:
        rows = db.execute(
            """
            SELECT r.project_id,
                   r.report_number,
                   r.total, r.passed, r.failed, r.broken, r.skipped,
                   r.start_ms
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
        projects = []
        for row in rows:
            pid = row["project_id"]
            meta = _parse_project_id(pid)
            total = row["total"] or 0
            passed = row["passed"] or 0
            fb = (row["failed"] or 0) + (row["broken"] or 0)
            entry = {
                "id": pid,
                "v": meta["variant"],
                "c": meta["cadence"],
                "pl": meta["platform"],
                "rn": row["report_number"],
                "pct": _pass_pct(passed, total, row["skipped"] or 0, exclude_skipped),
                "t": total,
                "x": fb,
            }
            if include_iso_utc:
                entry["start_iso"] = _iso_utc_ms(row["start_ms"])
            projects.append(entry)
        out = {
            "n": len(projects),
            "legend": "v=variant c=cadence pl=platform rn=report# pct=pass% t=tests x=fail+broken"
            + ("; start_iso=latest run start (UTC)" if include_iso_utc else ""),
            "projects": projects,
        }
        return out
    finally:
        db.close()


@mcp.tool()
def list_projects(compact: bool = True, include_iso_utc: bool = False, exclude_skipped: bool = False) -> list[dict[str, Any]]:
    """List all test projects with their latest run statistics and metadata.

    Set compact=false for full labels and nested latest_run stats (more verbose).

    When compact=true (default), each row: id, v, c, pl, run — run uses short keys (rn, t, p, f, pct, bv).
    include_iso_utc=true adds s0/s1 (compact) or start_iso/stop_iso (verbose) on run stats.
    exclude_skipped=true drops skipped tests from the pass-rate denominator (pct/pass_pct = passed / (total - skipped)).
    """
    db = _get_db()
    try:
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
        result = []
        for row in rows:
            pid  = row["project_id"]
            meta = _parse_project_id(pid)
            if compact:
                result.append({
                    "id": pid,
                    "v": meta["variant"],
                    "c": meta["cadence"],
                    "pl": meta["platform"],
                    "run": _row_to_stats_compact(row, include_iso_utc=include_iso_utc, exclude_skipped=exclude_skipped),
                })
            else:
                result.append({
                    "id":             pid,
                    "variant":        meta["variant"],
                    "cadence":        meta["cadence"],
                    "cadence_label":  meta["cadence_label"],
                    "platform":       meta["platform"],
                    "platform_label": meta["platform_label"],
                    "latest_run":     _row_to_stats(row, include_iso_utc=include_iso_utc, exclude_skipped=exclude_skipped),
                })
        return result
    finally:
        db.close()


@mcp.tool()
def get_project_runs(
    project_id: str,
    limit: int = 60,
    compact: bool = True,
    include_iso_utc: bool = False,
    exclude_skipped: bool = False,
) -> list[dict[str, Any]]:
    """Get run history for a specific project, latest first (highest report_number first).

    Args:
        project_id: The project ID as listed by dashboard_overview / list_projects
        limit: Maximum number of runs to return (default 60; the most recent N runs)
        compact: If true (default), short keys per run; if false, full stats dicts.
        include_iso_utc: If true, add s0/s1 (compact) or start_iso/stop_iso (verbose) per run.
        exclude_skipped: If true, drop skipped tests from the pass-rate denominator (pct/pass_pct = passed / (total - skipped)).

    Returns a list of run stats sorted with the newest run at index 0.
    """
    db = _get_db()
    try:
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
            return []
        if compact:
            return [_row_to_stats_compact(r, include_iso_utc=include_iso_utc, exclude_skipped=exclude_skipped) for r in rows]
        return [_row_to_stats(r, include_iso_utc=include_iso_utc, exclude_skipped=exclude_skipped) for r in rows]
    finally:
        db.close()


@mcp.tool()
def get_metric_history(
    project_id: str,
    metric_name: str = "",
    compact: bool = True,
    include_iso_utc: bool = False,
) -> dict[str, Any]:
    """Get per-run history of a configured scraped metric for a project.

    Metrics are defined in the dashboard Configuration tab (e.g. a boot-time
    metric scraped from a test attachment). Each metric has one or more series
    keys; values are returned as a {series_key: value} map per run. Runs where
    every series is NULL are omitted.

    Args:
        project_id: The project ID
        metric_name: Which metric to return; empty = the first configured metric.
        compact: If true (default), runs use short keys rn, v, z, ms
        include_iso_utc: If true, add start_iso from run start_ms (same instant as ms).

    Returns {metric, label, unit, keys, runs: [...]}; metric is "" when nothing
    is configured.
    """
    db = _get_db()
    try:
        cfg = config_store.get_config(db)
        metric = config_store.metric_by_name(cfg, metric_name) if cfg.metrics else None
        if not metric:
            return {"metric": "", "label": "", "unit": "", "keys": [], "runs": []}
        disp = config_store.metric_display(metric)

        rows = db.execute(
            """
            SELECT m.report_number, m.value_key, m.value, m.test_status,
                   r.start_ms
            FROM metric_values m
            LEFT JOIN runs r
              ON m.project_id = r.project_id AND m.report_number = r.report_number
            WHERE m.project_id = ? AND m.metric_name = ?
            ORDER BY m.report_number ASC
            """,
            (project_id, disp["name"]),
        ).fetchall()

        by_run: dict[int, dict] = {}
        for row in rows:
            entry = by_run.setdefault(row["report_number"], {
                "report_number": row["report_number"],
                "values": {},
                "test_status": row["test_status"],
                "start_ms": row["start_ms"],
            })
            entry["values"][row["value_key"]] = row["value"]

        runs = []
        for entry in by_run.values():
            if not any(v is not None for v in entry["values"].values()):
                continue
            if compact:
                d = {"rn": entry["report_number"], "v": entry["values"],
                     "z": entry["test_status"], "ms": entry["start_ms"]}
            else:
                d = entry
            if include_iso_utc:
                d["start_iso"] = _iso_utc_ms(entry["start_ms"])
            runs.append(d)

        return {"metric": disp["name"], "label": disp["label"], "unit": disp["unit"],
                "keys": disp["keys"], "runs": runs}
    finally:
        db.close()


@mcp.tool()
def get_failed_tests(
    project_id: str,
    report_number: int,
    compact: bool = True,
) -> list[dict[str, Any]]:
    """Get failed and broken tests for a specific run from the local database.

    Args:
        project_id: The project ID
        report_number: The report/run number
        compact: If true (default), short keys n, s, u, t; if false, test_name, suite_name, uid, status.

    Returns list of test rows (use uid with get_test_log).
    """
    db = _get_db()
    try:
        rows = db.execute(
            """
            SELECT test_name, test_params, suite_name, status, uid, start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE project_id = ? AND report_number = ? AND status IN ('failed', 'broken')
            ORDER BY suite_name, test_name
            """,
            (project_id, report_number),
        ).fetchall()
        if compact:
            return [
                {
                    "n": row["test_name"] + (row["test_params"] or ""),
                    "s": row["suite_name"],
                    "t": row["status"],
                    "u": row["uid"],
                    "ms": row["duration_ms"],
                    "t0": row["start_ms"],
                    "t1": row["stop_ms"],
                }
                for row in rows
            ]
        return [
            {
                "test_name":   row["test_name"] + (row["test_params"] or ""),
                "suite_name":  row["suite_name"],
                "status":      row["status"],
                "uid":         row["uid"],
                "start_ms":    row["start_ms"],
                "stop_ms":     row["stop_ms"],
                "duration_ms": row["duration_ms"],
            }
            for row in rows
        ]
    finally:
        db.close()


@mcp.tool()
def get_run_timeline(
    project_id: str,
    report_number: int,
    compact: bool = True,
) -> list[dict[str, Any]]:
    """Get all tests in a run ordered by start time (chronological execution order).

    Useful for understanding test sequencing, spotting gaps, and correlating
    failures with position in the run.

    Args:
        project_id: The project ID
        report_number: The report/run number
        compact: If true (default), short keys n, s, z, u, t0, t1, ms; if false, verbose keys.

    Returns list of tests sorted by start_ms ascending (nulls last).
    """
    db = _get_db()
    try:
        rows = db.execute(
            """
            SELECT test_name, test_params, suite_name, status, uid, start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE project_id = ? AND report_number = ?
            ORDER BY start_ms ASC NULLS LAST, suite_name, test_name
            """,
            (project_id, report_number),
        ).fetchall()
        if compact:
            return [
                {
                    "n":  row["test_name"] + (row["test_params"] or ""),
                    "s":  row["suite_name"],
                    "z":  row["status"],
                    "u":  row["uid"],
                    "t0": row["start_ms"],
                    "t1": row["stop_ms"],
                    "ms": row["duration_ms"],
                }
                for row in rows
            ]
        return [
            {
                "test_name":   row["test_name"] + (row["test_params"] or ""),
                "suite_name":  row["suite_name"],
                "status":      row["status"],
                "uid":         row["uid"],
                "start_ms":    row["start_ms"],
                "stop_ms":     row["stop_ms"],
                "duration_ms": row["duration_ms"],
            }
            for row in rows
        ]
    finally:
        db.close()


@mcp.tool()
def get_passed_tests(
    project_id: str,
    report_number: int,
    compact: bool = True,
) -> list[dict[str, Any]]:
    """Get passed tests for a specific run from the local database.

    Useful for retrieving UIDs to pass to get_test_log / get_test_logs_bulk.

    Args:
        project_id: The project ID
        report_number: The report/run number
        compact: If true (default), short keys n, s, u, ms, t0, t1; if false, verbose keys.

    Returns list of test rows.
    """
    db = _get_db()
    try:
        rows = db.execute(
            """
            SELECT test_name, test_params, suite_name, uid, start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE project_id = ? AND report_number = ? AND status = 'passed'
            ORDER BY suite_name, test_name
            """,
            (project_id, report_number),
        ).fetchall()
        if compact:
            return [
                {
                    "n": row["test_name"] + (row["test_params"] or ""),
                    "s": row["suite_name"],
                    "u": row["uid"],
                    "ms": row["duration_ms"],
                    "t0": row["start_ms"],
                    "t1": row["stop_ms"],
                }
                for row in rows
            ]
        return [
            {
                "test_name":   row["test_name"] + (row["test_params"] or ""),
                "suite_name":  row["suite_name"],
                "uid":         row["uid"],
                "start_ms":    row["start_ms"],
                "stop_ms":     row["stop_ms"],
                "duration_ms": row["duration_ms"],
            }
            for row in rows
        ]
    finally:
        db.close()


@mcp.tool()
def get_skipped_tests(
    project_id: str,
    report_number: int,
    compact: bool = True,
) -> list[dict[str, Any]]:
    """Get skipped tests for a specific run from the local database.

    Args:
        project_id: The project ID
        report_number: The report/run number
        compact: If true (default), short keys n, s, u.

    Returns list of test rows.
    """
    db = _get_db()
    try:
        rows = db.execute(
            """
            SELECT test_name, test_params, suite_name, uid, start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE project_id = ? AND report_number = ? AND status = 'skipped'
            ORDER BY suite_name, test_name
            """,
            (project_id, report_number),
        ).fetchall()
        if compact:
            return [
                {
                    "n": row["test_name"] + (row["test_params"] or ""),
                    "s": row["suite_name"],
                    "u": row["uid"],
                    "ms": row["duration_ms"],
                    "t0": row["start_ms"],
                    "t1": row["stop_ms"],
                }
                for row in rows
            ]
        return [
            {
                "test_name":   row["test_name"] + (row["test_params"] or ""),
                "suite_name":  row["suite_name"],
                "uid":         row["uid"],
                "start_ms":    row["start_ms"],
                "stop_ms":     row["stop_ms"],
                "duration_ms": row["duration_ms"],
            }
            for row in rows
        ]
    finally:
        db.close()


@mcp.tool()
def list_views() -> dict[str, Any]:
    """List the dashboard's configured matrix views and their feature scopes.

    Call this to discover valid values for the `scope` argument of
    get_feature_status and get_feature_tests. The first view is the default
    scope used when those tools are called without one.

    Returns {views: [{name, scope, features, filter}]} in dashboard order;
    features is the number of features configured in that scope, and filter
    is the view's project-column filter over parsed project-id dimensions.
    """
    db = _get_db()
    try:
        cfg = config_store.get_config(db)
        counts = dict(
            db.execute(
                "SELECT feature_scope, COUNT(*) FROM features GROUP BY feature_scope"
            ).fetchall()
        )
        return {
            "views": [
                {
                    "name": v["name"],
                    "scope": v["scope"],
                    "features": counts.get(v["scope"], 0),
                    "filter": v["filter"],
                }
                for v in cfg.views
            ],
        }
    finally:
        db.close()


@mcp.tool()
def get_feature_status(
    scope: str = "",
    compact: bool = True,
    dedupe_projects: bool = True,
) -> dict[str, Any]:
    """Get the feature × platform status matrix.

    For each feature, shows whether tests passed, failed, or are unmapped
    on the latest run of each platform.

    Args:
        scope: a view's feature scope — call list_views for valid values;
            empty = the first configured view. Features and project columns
            are both filtered by the view owning the scope.
        compact: If true (default), tabular rows + single-char cells (see legend); much smaller than nested matrix. If false, full {features, projects, matrix} dicts.
        dedupe_projects: If true (default), columns are p0,p1,… and refs maps to full project_id (saves tokens vs repeating long ids). If false, columns lists full project ids.

    Returns either compact {columns, legend, rows} or verbose {features, projects, matrix}.
    """
    db = _get_db()
    try:
        cfg = config_store.get_config(db)
        if not scope:
            scope = cfg.views[0]["scope"] if cfg.views else "standard"

        features = db.execute(
            "SELECT id, name, feature_group FROM features "
            "WHERE feature_scope = ? ORDER BY sort_order, feature_group, name",
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
        feature_tests: dict[int, list[str]] = {}
        for m in mappings:
            feature_tests.setdefault(m["feature_id"], []).append(m["test_name"])

        latest_rows = db.execute(
            "SELECT project_id, MAX(report_number) AS max_num FROM runs GROUP BY project_id"
        ).fetchall()
        latest: dict[str, int] = {r["project_id"]: r["max_num"] for r in latest_rows}

        # Filter projects via the column filter of the view owning this scope
        # (config-driven; same behaviour as the dashboard's /api/feature-status).
        view = next((v for v in cfg.views if v["scope"] == scope), None)
        view_filter = view["filter"] if view else {}
        scope_pids = [
            pid for pid in latest
            if config_store.matches_filter(_parse_project_id(pid, cfg)["dimensions"], view_filter)
        ]

        # Fetch test results for the latest run of each in-scope project
        project_results: dict[str, dict[str, list[str]]] = {}
        for pid in scope_pids:
            run_num = latest[pid]
            rows = db.execute(
                """
                SELECT test_name, test_params, status
                FROM test_results
                WHERE project_id = ? AND report_number = ?
                """,
                (pid, run_num),
            ).fetchall()
            results: dict[str, list[str]] = {}
            for r in rows:
                name = r["test_name"] + (r["test_params"] or "")
                results.setdefault(name, []).append(r["status"])
            project_results[pid] = results

        # Build matrix
        _STATUS_PRIORITY = {"failed": 0, "broken": 1, "skipped": 2, "unknown": 3, "passed": 4}

        def _best_status(statuses: list[str]) -> str:
            return min(statuses, key=lambda s: _STATUS_PRIORITY.get(s, 99))

        matrix: dict[str, dict[str, str | None]] = {}
        for f in features:
            fname = f["name"]
            tests = feature_tests.get(f["id"], [])
            row: dict[str, str | None] = {}
            for pid in scope_pids:
                results = project_results.get(pid, {})
                matched: list[str] = []
                for test_name in tests:
                    if test_name in results:
                        matched.extend(results[test_name])
                    else:
                        # prefix match for parametrised tests
                        for k, v in results.items():
                            if k.startswith(test_name + "["):
                                matched.extend(v)
                if matched:
                    row[pid] = _best_status(matched)
                elif tests:
                    row[pid] = "no_tests"
                else:
                    row[pid] = None  # unmapped
            matrix[fname] = row

        if compact:
            tab_rows: list[list[str]] = []
            for f in features:
                fname = f["name"]
                grp = f["feature_group"] or ""
                label = f"{grp}|{fname}" if grp else fname
                cells = [_MATRIX_CODE.get(matrix[fname].get(pid), "?") for pid in scope_pids]
                tab_rows.append([label, *cells])
            if dedupe_projects and scope_pids:
                col_labels = [f"p{i}" for i in range(len(scope_pids))]
                refs = {f"p{i}": pid for i, pid in enumerate(scope_pids)}
                return {
                    "columns": col_labels,
                    "refs": refs,
                    "legend": _MATRIX_CODE_REV,
                    "rows": tab_rows,
                }
            return {
                "columns": scope_pids,
                "legend": _MATRIX_CODE_REV,
                "rows": tab_rows,
            }

        return {
            "features": [{"name": f["name"], "group": f["feature_group"]} for f in features],
            "projects": scope_pids,
            "matrix":   matrix,
        }
    finally:
        db.close()


@mcp.tool()
def get_feature_tests(
    scope: str = "",
    feature: str = "",
) -> dict[str, Any]:
    """List the test cases mapped to each feature (the mappings behind get_feature_status).

    Use this to find out *which* tests drive a feature's status cell; the matrix
    from get_feature_status only shows the aggregated status.

    Args:
        scope: a view's feature scope — call list_views for valid values; empty =
            the first configured view (same behaviour as get_feature_status).
        feature: optional SQL LIKE pattern on the feature name, e.g. '%bgp%';
            empty = all features in the scope.

    Returns {scope, features: [{name, group, tests: [test_name, …]}]};
    tests is empty for features with no mappings yet.
    """
    db = _get_db()
    try:
        cfg = config_store.get_config(db)
        if not scope:
            scope = cfg.views[0]["scope"] if cfg.views else "standard"

        sql = (
            "SELECT id, name, feature_group FROM features WHERE feature_scope = ?"
        )
        params: list[Any] = [scope]
        if feature:
            sql += " AND name LIKE ?"
            params.append(feature)
        sql += " ORDER BY sort_order, feature_group, name"
        features = db.execute(sql, params).fetchall()

        fids = [f["id"] for f in features]
        mappings = (
            db.execute(
                f"SELECT feature_id, test_name FROM feature_mappings "
                f"WHERE feature_id IN ({','.join('?' * len(fids))}) "
                f"ORDER BY test_name",
                fids,
            ).fetchall()
            if fids else []
        )
        feature_tests: dict[int, list[str]] = {}
        for m in mappings:
            feature_tests.setdefault(m["feature_id"], []).append(m["test_name"])

        return {
            "scope": scope,
            "features": [
                {
                    "name": f["name"],
                    "group": f["feature_group"],
                    "tests": feature_tests.get(f["id"], []),
                }
                for f in features
            ],
        }
    finally:
        db.close()


@mcp.tool()
def search_test_results(
    test_name_pattern: str,
    project_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    compact: bool = True,
    dedupe_project_ids: bool = True,
) -> Union[list[dict[str, Any]], dict[str, Any]]:
    """Search for test results across runs by test name pattern.

    Args:
        test_name_pattern: SQL LIKE pattern, e.g. '%bgp%' or 'test_vlan_%'
        project_id: Optionally filter to one project
        status: Optionally filter by status (passed/failed/broken/skipped/unknown)
        limit: Max results (default 50; lower saves tokens)
        compact: If true (default), short keys pid, rn, n, s, t
        dedupe_project_ids: If true (default) with compact, return {projects, rows} with pi index instead of repeating pid on every row.

    Returns list of rows, or {projects, rows} when compact and dedupe_project_ids.
    """
    db = _get_db()
    try:
        conditions = ["test_name LIKE ?"]
        params: list[Any] = [test_name_pattern]
        if project_id:
            conditions.append("project_id = ?")
            params.append(project_id)
        if status:
            conditions.append("status = ?")
            params.append(status)
        params.append(limit)

        rows = db.execute(
            f"""
            SELECT project_id, report_number, test_name, test_params, suite_name, status,
                   start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE {' AND '.join(conditions)}
            ORDER BY project_id, report_number DESC, test_name
            LIMIT ?
            """,
            params,
        ).fetchall()
        if compact:
            if dedupe_project_ids:
                projects: list[str] = []
                pid_index: dict[str, int] = {}
                out_rows: list[dict[str, Any]] = []
                for row in rows:
                    pid = row["project_id"]
                    if pid not in pid_index:
                        pid_index[pid] = len(projects)
                        projects.append(pid)
                    out_rows.append(
                        {
                            "pi": pid_index[pid],
                            "rn": row["report_number"],
                            "n": row["test_name"] + (row["test_params"] or ""),
                            "s": row["suite_name"],
                            "t": row["status"],
                            "ms": row["duration_ms"],
                            "t0": row["start_ms"],
                            "t1": row["stop_ms"],
                        }
                    )
                return {"projects": projects, "rows": out_rows}
            return [
                {
                    "pid": row["project_id"],
                    "rn": row["report_number"],
                    "n": row["test_name"] + (row["test_params"] or ""),
                    "s": row["suite_name"],
                    "t": row["status"],
                    "ms": row["duration_ms"],
                    "t0": row["start_ms"],
                    "t1": row["stop_ms"],
                }
                for row in rows
            ]
        return [
            {
                "project_id":    row["project_id"],
                "report_number": row["report_number"],
                "test_name":     row["test_name"] + (row["test_params"] or ""),
                "suite_name":    row["suite_name"],
                "status":        row["status"],
                "start_ms":      row["start_ms"],
                "stop_ms":       row["stop_ms"],
                "duration_ms":   row["duration_ms"],
            }
            for row in rows
        ]
    finally:
        db.close()


def _extract_steps_compact(stage: dict) -> list[dict[str, Any]]:
    """Step tree without log attachment fetches (token- and bandwidth-efficient)."""
    steps = []
    for step in stage.get("steps", []):
        entry: dict[str, Any] = {
            "n": step.get("name"),
            "z": step.get("status"),
        }
        if step.get("steps"):
            entry["c"] = _extract_steps_compact(step)
        steps.append(entry)
    return steps


@mcp.tool()
def get_test_log(project_id: str, report_number: int, uid: str, compact: bool = True) -> dict[str, Any]:
    """Fetch the full log for a single test case from the Allure server.

    Use the uid from get_failed_tests() or get_skipped_tests() results.

    Args:
        project_id: The project ID
        report_number: The report/run number
        uid: The test case UID (from get_failed_tests or get_skipped_tests)
        compact: If true (default), short keys, truncated trace, step tree without attachment downloads.
                 If false, full message/trace, log attachments on steps, verbose keys.

    Returns dict with status, messages, steps; shape depends on compact.
    """
    report_base = f"{ALLURE_BASE_URL}/projects/{project_id}/reports/{report_number}/"
    url = f"{report_base}data/test-cases/{uid}.json"
    try:
        r = _requests.get(url, timeout=15, verify=False)
        r.raise_for_status()
        tc = r.json()
    except Exception as e:
        return {"error": str(e), "url": url}

    def _extract_steps(stage: dict) -> list[dict]:
        steps = []
        for step in stage.get("steps", []):
            entry: dict[str, Any] = {
                "name":   step.get("name"),
                "status": step.get("status"),
                "log":    None,
            }
            # Grab any "log" attachment text
            for att in step.get("attachments", []):
                if att.get("type") == "text/plain" and att.get("source"):
                    try:
                        log_url = f"{report_base}data/attachments/{att['source']}"
                        resp = _requests.get(log_url, timeout=10, verify=False)
                        if resp.ok:
                            entry["log"] = resp.text[:4000]  # cap at 4 KB
                    except Exception:
                        pass
            if step.get("steps"):
                entry["sub_steps"] = _extract_steps(step)
            steps.append(entry)
        return steps

    test_stage = tc.get("testStage") or {}
    before_stages = tc.get("beforeStages") or []
    after_stages  = tc.get("afterStages") or []

    msg = test_stage.get("statusMessage") or tc.get("statusMessage") or ""
    trace = test_stage.get("statusTrace") or tc.get("statusTrace") or ""

    if compact:
        trace_max = 1200
        tr = trace if len(trace) <= trace_max else trace[:trace_max] + "…"
        params = tc.get("parameters") or []
        param_s = ",".join(
            f"{p.get('name')}={p.get('value')}" for p in params
        )
        return {
            "n": tc.get("name"),
            "z": tc.get("status"),
            "m": msg,
            "tr": tr,
            "prm": param_s or None,
            "ms": tc.get("time", {}).get("duration"),
            "ts": _extract_steps_compact(test_stage),
        }

    return {
        "name":          tc.get("name"),
        "full_name":     tc.get("fullName"),
        "status":        tc.get("status"),
        "statusMessage": msg,
        "statusTrace":   trace,
        "parameters":    [{"name": p.get("name"), "value": p.get("value")} for p in tc.get("parameters", [])],
        "links":         [{"name": l.get("name"), "url": l.get("url"), "type": l.get("type")} for l in tc.get("links", [])],
        "start":         tc.get("time", {}).get("start"),
        "stop":          tc.get("time", {}).get("stop"),
        "duration":      tc.get("time", {}).get("duration"),
        "setup_steps":   [_extract_steps(s) for s in before_stages],
        "test_steps":    _extract_steps(test_stage),
        "teardown_steps": [_extract_steps(s) for s in after_stages],
    }


@mcp.tool()
def get_test_neighbors(
    project_id: str,
    report_number: int,
    uid: str,
    before: int = 5,
    after: int = 2,
    compact: bool = True,
) -> dict[str, Any]:
    """Get the tests that ran immediately before and after a given test in the same run.

    Uses start_ms ordering. Useful for understanding what was executing
    around a failure.

    Args:
        project_id: The project ID
        report_number: The report/run number
        uid: UID of the reference test
        before: Number of tests to return before the reference (default 5)
        after: Number of tests to return after the reference (default 2)
        compact: If true (default), short keys n, s, z, u, t0, t1, ms; if false, verbose keys.

    Returns {before: [...], target: {...}, after: [...]}.
    """
    db = _get_db()
    try:
        ref = db.execute(
            """
            SELECT test_name, test_params, suite_name, status, uid, start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE project_id = ? AND report_number = ? AND uid = ?
            """,
            (project_id, report_number, uid),
        ).fetchone()
        if ref is None:
            return {"error": f"uid {uid!r} not found in {project_id} run {report_number}"}

        ref_start = ref["start_ms"]

        def _row(row) -> dict:
            if compact:
                return {
                    "n":  row["test_name"] + (row["test_params"] or ""),
                    "s":  row["suite_name"],
                    "z":  row["status"],
                    "u":  row["uid"],
                    "t0": row["start_ms"],
                    "t1": row["stop_ms"],
                    "ms": row["duration_ms"],
                }
            return {
                "test_name":   row["test_name"] + (row["test_params"] or ""),
                "suite_name":  row["suite_name"],
                "status":      row["status"],
                "uid":         row["uid"],
                "start_ms":    row["start_ms"],
                "stop_ms":     row["stop_ms"],
                "duration_ms": row["duration_ms"],
            }

        before_rows = db.execute(
            """
            SELECT test_name, test_params, suite_name, status, uid, start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE project_id = ? AND report_number = ? AND uid != ?
              AND start_ms < ?
            ORDER BY start_ms DESC
            LIMIT ?
            """,
            (project_id, report_number, uid, ref_start, before),
        ).fetchall()

        after_rows = db.execute(
            """
            SELECT test_name, test_params, suite_name, status, uid, start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE project_id = ? AND report_number = ? AND uid != ?
              AND start_ms > ?
            ORDER BY start_ms ASC
            LIMIT ?
            """,
            (project_id, report_number, uid, ref_start, after),
        ).fetchall()

        return {
            "before": [_row(r) for r in reversed(before_rows)],
            "target": _row(ref),
            "after":  [_row(r) for r in after_rows],
        }
    finally:
        db.close()


@mcp.tool()
def get_test_history(
    project_id: str,
    test_name: str,
    limit: int = 30,
    compact: bool = True,
) -> list[dict[str, Any]]:
    """Get the history of a specific test across runs for a project, latest first.

    Useful for checking whether a test was previously passing, when it started
    failing, or how its duration has changed over time.

    Args:
        project_id: The project ID
        test_name: Exact test name (without params suffix) or SQL LIKE pattern, e.g. 'test_bgp_basic' or 'test_bgp%'
        limit: Max runs to return (default 30)
        compact: If true (default), short keys rn, n, z, u, ms, t0, t1; if false, verbose keys.

    Returns list sorted newest run first.
    """
    db = _get_db()
    try:
        rows = db.execute(
            """
            SELECT report_number, test_name, test_params, suite_name, status, uid,
                   start_ms, stop_ms, duration_ms
            FROM test_results
            WHERE project_id = ? AND test_name LIKE ?
            ORDER BY report_number DESC
            LIMIT ?
            """,
            (project_id, test_name, limit),
        ).fetchall()
        if compact:
            return [
                {
                    "rn": row["report_number"],
                    "n":  row["test_name"] + (row["test_params"] or ""),
                    "z":  row["status"],
                    "u":  row["uid"],
                    "ms": row["duration_ms"],
                    "t0": row["start_ms"],
                    "t1": row["stop_ms"],
                }
                for row in rows
            ]
        return [
            {
                "report_number": row["report_number"],
                "test_name":     row["test_name"] + (row["test_params"] or ""),
                "suite_name":    row["suite_name"],
                "status":        row["status"],
                "uid":           row["uid"],
                "start_ms":      row["start_ms"],
                "stop_ms":       row["stop_ms"],
                "duration_ms":   row["duration_ms"],
            }
            for row in rows
        ]
    finally:
        db.close()


@mcp.tool()
def get_test_logs_bulk(
    project_id: str,
    report_number: int,
    uids: list[str],
    compact: bool = True,
) -> list[dict[str, Any]]:
    """Fetch logs for multiple failed tests at once.

    Same as calling get_test_log() for each uid, but in one call.
    Useful for analysing all failures in a run without multiple round-trips.

    Args:
        project_id: The project ID
        report_number: The report/run number
        uids: List of test UIDs (from get_failed_tests); capped at 30 per call to limit tokens
        compact: Passed to get_test_log (default true = no per-step attachment downloads)
    """
    from concurrent.futures import ThreadPoolExecutor
    uid_list = uids[:30]
    if not uid_list:
        return []

    def _fetch(uid: str) -> dict:
        result = get_test_log(project_id, report_number, uid, compact=compact)
        result["uid"] = uid
        return result

    with ThreadPoolExecutor(max_workers=min(10, len(uid_list))) as ex:
        return list(ex.map(_fetch, uid_list))


@mcp.tool()
def get_scraper_summary(compact: bool = True, include_iso_utc: bool = False) -> dict[str, Any]:
    """Get a summary of what data has been scraped into the local database.

    Args:
        compact: If true (default), short keys tr, tp, projects use pid, rc, lr, ls
        include_iso_utc: If true, add ls_iso (ISO UTC) next to latest_stop_ms (ls).

    Returns total run count, project count, and per-project latest run numbers.
    """
    db = _get_db()
    try:
        total_runs = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        total_projects = db.execute("SELECT COUNT(DISTINCT project_id) FROM runs").fetchone()[0]
        project_rows = db.execute(
            """
            SELECT project_id,
                   COUNT(*) AS run_count,
                   MAX(report_number) AS latest_run,
                   MAX(stop_ms) AS latest_stop_ms
            FROM runs
            GROUP BY project_id
            ORDER BY project_id
            """
        ).fetchall()
        if compact:
            plist = []
            for row in project_rows:
                e = {
                    "pid": row["project_id"],
                    "rc": row["run_count"],
                    "lr": row["latest_run"],
                    "ls": row["latest_stop_ms"],
                }
                if include_iso_utc:
                    e["ls_iso"] = _iso_utc_ms(row["latest_stop_ms"])
                plist.append(e)
            return {"tr": total_runs, "tp": total_projects, "p": plist}
        plistv = []
        for row in project_rows:
            e = {
                "project_id":    row["project_id"],
                "run_count":     row["run_count"],
                "latest_run":    row["latest_run"],
                "latest_stop_ms": row["latest_stop_ms"],
            }
            if include_iso_utc:
                e["latest_stop_iso"] = _iso_utc_ms(row["latest_stop_ms"])
            plistv.append(e)
        return {
            "total_runs": total_runs,
            "total_projects": total_projects,
            "projects": plistv,
        }
    finally:
        db.close()


if __name__ == "__main__":
    if _parsed.http:
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
