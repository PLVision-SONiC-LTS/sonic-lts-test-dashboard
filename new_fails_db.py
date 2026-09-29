"""
Persisted new-fail snapshots vs the previous run stored in the DB (see run_new_fails table).

"Previous" run = largest report_number in `runs` for the same project_id that is < current
(Allure project ordering is not consulted).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from report_generator import MAX_NEW_PASS_ITEMS

if TYPE_CHECKING:
    from scraper import AllureClient

log = logging.getLogger(__name__)

# Keep in sync with get_new_failed_tests_from_previous_report when no predecessor.
NOTE_NO_PREV = "Previous report not found. Unable to compare new fails/passes."
# Keep in sync when previous run exists in `runs` but we have no test_results for it.
NOTE_PREV_UNAVAIL = "Previous report data is unavailable. Unable to compare new fails/passes."


def test_display_name(test_name: str, test_params: str) -> str:
    return (test_name or "") + (test_params or "")


def expected_prev_report_number(db: sqlite3.Connection, project_id: str, report_number: int) -> Optional[int]:
    row = db.execute(
        """
        SELECT MAX(report_number) AS m
        FROM runs
        WHERE project_id = ? AND report_number < ?
        """,
        (project_id, report_number),
    ).fetchone()
    m = row["m"] if row else None
    return int(m) if m is not None else None


def _test_results_count(db: sqlite3.Connection, project_id: str, report_number: int) -> int:
    r = db.execute(
        "SELECT COUNT(*) AS c FROM test_results WHERE project_id = ? AND report_number = ?",
        (project_id, report_number),
    ).fetchone()
    return int(r["c"] or 0)


def _failed_name_set(db: sqlite3.Connection, project_id: str, report_number: int) -> set[str]:
    rows = db.execute(
        """
        SELECT test_name, test_params
        FROM test_results
        WHERE project_id = ? AND report_number = ? AND LOWER(status) IN ('failed', 'broken', 'unknown')
        """,
        (project_id, report_number),
    ).fetchall()
    return {test_display_name(r["test_name"], r["test_params"] or "") for r in rows}


def _passed_name_set(db: sqlite3.Connection, project_id: str, report_number: int) -> set[str]:
    rows = db.execute(
        """
        SELECT test_name, test_params
        FROM test_results
        WHERE project_id = ? AND report_number = ? AND LOWER(status) = 'passed'
        """,
        (project_id, report_number),
    ).fetchall()
    return {test_display_name(r["test_name"], r["test_params"] or "") for r in rows}


def _new_fail_items_from_db(
    db: sqlite3.Connection, project_id: str, num: int, new_names: set[str]
) -> list[dict[str, Any]]:
    if not new_names:
        return []
    rows = db.execute(
        """
        SELECT test_name, test_params, LOWER(status) AS st, uid
        FROM test_results
        WHERE project_id = ? AND report_number = ?
          AND LOWER(status) IN ('failed', 'broken', 'unknown')
        """,
        (project_id, num),
    ).fetchall()
    by_name: dict[str, Any] = {}
    for r in rows:
        dn = test_display_name(r["test_name"], r["test_params"] or "")
        if dn in new_names:
            by_name[dn] = r
    out: list[dict[str, Any]] = []
    for n in sorted(new_names):
        r = by_name.get(n)
        if not r:
            continue
        out.append({
            "name": n,
            "uid": r["uid"],
            "status": r["st"] or "",
            "statusMessage": "",  # filled by caller with Allure fetches
        })
    return out


def _fetch_status_message(
    client: "AllureClient", project_id: str, report_number: int, uid: Optional[str]
) -> str:
    if not uid:
        return ""
    tc = client.get_run_test_case(project_id, report_number, uid)
    if not isinstance(tc, dict):
        return ""
    return (tc.get("statusMessage") or "") or ""


def upsert_run_new_fails(
    db: sqlite3.Connection, client: "AllureClient", project_id: str, report_number: int, *, now_iso: str | None = None
) -> None:
    """Recompute and store run_new_fails for one (project, report_number) after test_results exist for that run."""
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()

    prev = expected_prev_report_number(db, project_id, report_number)
    n_curr = _test_results_count(db, project_id, report_number)

    compared_to: Optional[int] = prev
    new_fails_note = ""
    payload: list[dict[str, Any]] = []
    new_pass_count = 0
    total_new_fail = 0

    if prev is None:
        new_fails_note = NOTE_NO_PREV
        compared_to = None
    elif n_curr == 0:
        new_fails_note = NOTE_PREV_UNAVAIL
    else:
        n_prev = _test_results_count(db, project_id, prev)
        if n_prev == 0:
            new_fails_note = NOTE_PREV_UNAVAIL
        else:
            failed_prev = _failed_name_set(db, project_id, prev)
            failed_curr = _failed_name_set(db, project_id, report_number)
            new_names = {n for n in failed_curr if n not in failed_prev}
            total_new_fail = len(new_names)
            items = _new_fail_items_from_db(db, project_id, report_number, new_names)
            for item in items:
                item["statusMessage"] = _fetch_status_message(
                    client, project_id, report_number, item.get("uid")
                )
            payload = items

            passed_curr = _passed_name_set(db, project_id, report_number)
            matches = sum(1 for name in sorted(passed_curr) if name in failed_prev)
            new_pass_count = min(MAX_NEW_PASS_ITEMS, matches)

    db.execute(
        """
        INSERT INTO run_new_fails (
            project_id, report_number, compared_to_report_number,
            new_fail_count, new_pass_count, new_fails_note, payload_json, computed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, report_number) DO UPDATE SET
            compared_to_report_number = excluded.compared_to_report_number,
            new_fail_count = excluded.new_fail_count,
            new_pass_count = excluded.new_pass_count,
            new_fails_note = excluded.new_fails_note,
            payload_json = excluded.payload_json,
            computed_at = excluded.computed_at
        """,
        (
            project_id,
            report_number,
            compared_to,
            total_new_fail,
            new_pass_count,
            new_fails_note,
            json.dumps(payload, ensure_ascii=False),
            now_iso,
        ),
    )
    log.debug(
        "[%s] #%d run_new_fails: compared_to=%s new_fail_count=%d new_pass_count=%d",
        project_id, report_number, compared_to, total_new_fail, new_pass_count,
    )


def row_is_valid_for_read(
    db: sqlite3.Connection, project_id: str, report_number: int, row: sqlite3.Row
) -> bool:
    exp = expected_prev_report_number(db, project_id, report_number)
    stored = row["compared_to_report_number"]
    if exp is None:
        return stored is None
    if stored is None:
        return False
    return int(stored) == int(exp)


def try_new_counts_for_runs(
    db: sqlite3.Connection, project_id: str, report_numbers: list[int]
) -> Optional[list[tuple[int, int, int]]]:
    """If every run has a valid `run_new_fails` row, return [(report_number, nf, np), ...] in the same order."""
    if not report_numbers:
        return []
    ph = ",".join("?" * len(report_numbers))
    by_rn: dict[int, sqlite3.Row] = {
        r["report_number"]: r
        for r in db.execute(
            f"SELECT * FROM run_new_fails WHERE project_id = ? AND report_number IN ({ph})",
            (project_id, *report_numbers),
        )
    }
    out: list[tuple[int, int, int]] = []
    for rn in report_numbers:
        row = by_rn.get(rn)
        if row is None:
            return None
        if not row_is_valid_for_read(db, project_id, rn, row):
            return None
        out.append((rn, int(row["new_fail_count"] or 0), int(row["new_pass_count"] or 0)))
    return out


def try_load_stored_for_api(db: sqlite3.Connection, project_id: str, report_number: int) -> Optional[dict[str, Any]]:
    """If a valid run_new_fails row exists, return the dict shape expected by _get_new_fails_cached. Else None."""
    row = db.execute(
        """
        SELECT compared_to_report_number, new_fail_count, new_pass_count, new_fails_note, payload_json
        FROM run_new_fails
        WHERE project_id = ? AND report_number = ?
        """,
        (project_id, report_number),
    ).fetchone()
    if not row:
        return None
    if not row_is_valid_for_read(db, project_id, report_number, row):
        return None
    try:
        new_failed = json.loads(row["payload_json"] or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(new_failed, list):
        return None
    nf = int(row["new_fail_count"] or 0)
    np_ = int(row["new_pass_count"] or 0)
    return {
        "new_failed_tests": new_failed,
        "new_fails_note": row["new_fails_note"] or "",
        "new_pass_count": np_,
        "total_new_fail_count": nf,
    }


def backfill_project(
    db: sqlite3.Connection, client: "AllureClient", project_id: str, now_iso: str | None = None
) -> int:
    """Recompute run_new_fails for every run that has test_results. Returns number of upserts."""
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    runs = [
        r[0]
        for r in db.execute(
            """
            SELECT DISTINCT report_number
            FROM test_results
            WHERE project_id = ?
            ORDER BY report_number
            """,
            (project_id,),
        )
    ]
    n = 0
    for rn in runs:
        if _test_results_count(db, project_id, rn) == 0:
            continue
        upsert_run_new_fails(db, client, project_id, rn, now_iso=now_iso)
        n += 1
    return n
