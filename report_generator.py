#!/usr/bin/env python3
"""
Generate an HTML test report from one or more Allure JSON reports.

Usage:
    python3 test_report_generate.py -f <REPORT1_URL> <REPORT2_URL> ... [-v <VERSION>] [-o <OUTPUT_FILE>]

Options:
    -f, --files    URLs or paths to Allure report index.html files (required, can specify multiple).
    -v, --version  Version tag. If not provided, it will be extracted from the first report.
    -o, --output   Path to the output HTML file (default: test_report.html).

Branding, image-server URL, version parsing and known-issue keywords come from
the report settings (config_store.REPORT_DEFAULTS, UI-editable via
Configuration → Advanced → Report when run inside the dashboard).

Example:
    python3 report_generator.py -f http://localhost:5050/allure-docker-service/projects/my-project/reports/305/index.html -v my-build-1.2.3 -o report.html
    python3 report_generator.py -f http://localhost:5050/allure-docker-service/projects/my-project/reports/305/index.html http://localhost:5050/allure-docker-service/projects/my-project/reports/latest/index.html
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from functools import partial
import re
import threading
from jinja2 import Environment, FileSystemLoader
import requests
import os
from config_store import REPORT_DEFAULTS
from parse_allure_results import AllureSuitesParser
from get_known_issues_from_allure_report import get_known_issues_from_allure_report

HEADERS = {"accept": "*/*"}
REQUEST_TIMEOUT_SEC = 15
TEST_CASE_TIMEOUT_SEC = 60
MAX_FETCH_WORKERS = 8
FAILED_STATUSES = {"failed", "broken", "unknown"}
PROJECT_REPORT_URL_PATTERN = re.compile(r"^(https?://.*/projects/)([^/]+)/reports/([^/]+)/index\.html$")
MAX_NEW_PASS_ITEMS = 50
# Ticket ids look like PROJ-123. UTF-8/UTF-16 match that shape but are encodings.
_JIRA_TICKET_RE = re.compile(r"\b(?!UTF-\d+\b)([A-Z]{2,5}-\d+)\b")

_allure_auth_lock = threading.Lock()
_allure_auth_cache: dict | None = None


def _allure_verify() -> bool:
    """False when ALLURE_ACCEPT_SELF_SIGNED is set, matching the scraper."""
    flag = os.getenv("ALLURE_ACCEPT_SELF_SIGNED", "").lower()
    return flag not in ("1", "true", "yes")


def _allure_auth() -> dict:
    """Credentials for Allure Docker Service.

    The scraper logs in before reading report files. The dashboard used to
    call those same URLs with no session, so test-case JSON (where the xfail
    reason lives) came back empty and the Jira tab found no tickets.
    """
    global _allure_auth_cache
    with _allure_auth_lock:
        if _allure_auth_cache is not None:
            return _allure_auth_cache
        user = (os.getenv("ALLURE_USERNAME") or "").strip()
        password = os.getenv("ALLURE_PASSWORD") or ""
        verify = _allure_verify()
        auth = (user, password) if user and password else None
        cookies = None
        base = (os.getenv("ALLURE_BASE_URL") or "").rstrip("/")
        if auth and base:
            try:
                resp = requests.post(
                    f"{base}/login",
                    json={"username": user, "password": password},
                    timeout=10,
                    verify=verify,
                )
                if resp.ok:
                    cookies = requests.utils.dict_from_cookiejar(resp.cookies) or None
            except requests.RequestException:
                cookies = None
        _allure_auth_cache = {"auth": auth, "cookies": cookies, "verify": verify}
        return _allure_auth_cache


def fetch_json(url, timeout=REQUEST_TIMEOUT_SEC):
    """Fetch JSON data from a given URL."""
    opts = _allure_auth()
    r = requests.get(
        url,
        headers=HEADERS,
        timeout=timeout,
        verify=opts["verify"],
        auth=opts["auth"],
        cookies=opts["cookies"],
    )
    r.raise_for_status()
    return r.json()

def get_report_base_url(report_url):
    """Return report base URL ending with a slash."""
    if "index.html" in report_url:
        return report_url.rsplit("index.html", 1)[0]
    return f"{report_url.rstrip('/')}/"

def _rcfg(report_cfg=None):
    """Report settings merged over the defaults (see config_store.REPORT_DEFAULTS)."""
    return {**REPORT_DEFAULTS, **(report_cfg or {})}


def is_flagged_report(report_index_url, image_version, image_url, report_cfg=None):
    """Detect whether the report targets the configured special flavor (e.g. OLS)."""
    keyword = (_rcfg(report_cfg).get("flag_keyword") or "").lower()
    if not keyword:
        return False
    combined = " ".join([report_index_url or "", image_version or "", image_url or ""]).lower()
    return keyword in combined

def iter_leaf_tests(node):
    """Yield leaf tests from suites.json hierarchy."""
    if not isinstance(node, dict):
        return

    children = node.get("children") or []
    if children:
        for child in children:
            yield from iter_leaf_tests(child)
        return

    if "status" in node:
        yield node

def build_failed_tests_index(suites_json):
    """Build an index of failed tests by test name."""
    failed_tests = {}
    for test in iter_leaf_tests(suites_json):
        status = (test.get("status") or "").lower()
        test_name = test.get("name")
        if status in FAILED_STATUSES and test_name:
            failed_tests[test_name] = {
                "name": test_name,
                "uid": test.get("uid"),
                "status": status,
            }
    return failed_tests

def build_passed_tests_index(suites_json):
    """Build an index of passed tests by test name."""
    passed_tests = {}
    for test in iter_leaf_tests(suites_json):
        status = (test.get("status") or "").lower()
        test_name = test.get("name")
        if status == "passed" and test_name:
            passed_tests[test_name] = {
                "name": test_name,
                "uid": test.get("uid"),
                "status": status,
            }
    return passed_tests

def get_previous_report_url(report_index_url):
    """Resolve previous report URL in the same Allure project."""
    match = PROJECT_REPORT_URL_PATTERN.match(report_index_url)
    if not match:
        return None

    project_prefix, project_name, report_token = match.groups()
    project_meta_url = f"{project_prefix}{project_name}/"

    try:
        project_json = fetch_json(project_meta_url)
    except Exception:
        return None

    report_ids = project_json.get("data", {}).get("project", {}).get("reports_id", [])
    numeric_ids = sorted({int(rid) for rid in report_ids if str(rid).isdigit()})
    if len(numeric_ids) < 2:
        return None

    if str(report_token).isdigit():
        current_id = int(report_token)
    else:
        current_id = numeric_ids[-1]

    previous_candidates = [rid for rid in numeric_ids if rid < current_id]
    if not previous_candidates:
        return None

    previous_id = previous_candidates[-1]
    return f"{project_prefix}{project_name}/reports/{previous_id}/index.html"

def fetch_prev_run_stats(previous_report_url):
    """Fetch totals/duration from a prior report's summary.json. Best-effort; returns None on failure."""
    try:
        prev_base = get_report_base_url(previous_report_url)
        summary = fetch_json(f"{prev_base}widgets/summary.json")
    except Exception:
        return None

    stat = summary.get("statistic", {}) or {}
    passed  = stat.get("passed", 0)
    failed  = stat.get("failed", 0)
    broken  = stat.get("broken", 0)
    unknown = stat.get("unknown", 0)
    skipped = stat.get("skipped", 0)
    total   = passed + failed + broken + unknown + skipped
    duration_ms = (summary.get("time", {}) or {}).get("duration", 0)
    return {
        "total":   total,
        "passed":  passed,
        "failed":  failed,
        "broken":  broken,
        "unknown": unknown,
        "skipped": skipped,
        "duration_ms": duration_ms,
        "duration":    format_duration(duration_ms),
    }

def _iter_status_texts(node, depth=0):
    """Yield status text that Allure shows on the test overview.

    Pytest xfail puts ``XFAIL <jira url>`` in statusDetails. Allure copies
    that onto statusMessage and/or statusTrace, sometimes only on testStage
    or a step. The overview renders whichever of those is set.
    """
    if depth > 8 or not isinstance(node, dict):
        return
    for key in ("statusMessage", "statusTrace", "description", "descriptionHtml"):
        val = node.get(key)
        if isinstance(val, str) and val:
            yield val
    for link in node.get("links") or []:
        if isinstance(link, dict):
            for key in ("url", "name"):
                val = link.get(key)
                if isinstance(val, str) and val:
                    yield val
    for step in node.get("steps") or []:
        yield from _iter_status_texts(step, depth + 1)
    stage = node.get("testStage")
    if isinstance(stage, dict):
        yield from _iter_status_texts(stage, depth + 1)
    for key in ("beforeStages", "afterStages"):
        for stage in node.get(key) or []:
            yield from _iter_status_texts(stage, depth + 1)


def extract_jira_tickets(case) -> list[str]:
    """Return unique Jira ticket ids mentioned in an Allure test-case JSON."""
    if not isinstance(case, dict):
        return []
    found: list[str] = []
    seen: set[str] = set()
    for text in _iter_status_texts(case):
        for match in _JIRA_TICKET_RE.finditer(text):
            ticket = match.group(1)
            if ticket not in seen:
                seen.add(ticket)
                found.append(ticket)
    return found


def fetch_test_case(report_base, test_uid):
    """Fetch one Allure test-case JSON. Returns {} when it cannot be read."""
    if not test_uid:
        return {}
    try:
        case_json = fetch_json(
            f"{report_base}data/test-cases/{test_uid}.json",
            timeout=TEST_CASE_TIMEOUT_SEC,
        )
    except Exception:
        return {}
    return case_json if isinstance(case_json, dict) else {}


def fetch_status_message(report_base, test_uid):
    """Fetch the status message Allure shows for a test case.

    Prefers statusMessage, including the copy stored on testStage. When the
    xfail reason was recorded only in the trace, returns the start of that
    trace so callers still see the Jira url.
    """
    case_json = fetch_test_case(report_base, test_uid)
    if not case_json:
        return ""
    stage = case_json.get("testStage") or {}
    message = case_json.get("statusMessage") or stage.get("statusMessage") or ""
    if message:
        return message
    trace = case_json.get("statusTrace") or stage.get("statusTrace") or ""
    return trace[:2000] if trace else ""

def get_new_failed_tests_from_previous_report(report_index_url, current_suites_json):
    """Collect tests that changed status compared to previous report."""
    current_failed_tests = build_failed_tests_index(current_suites_json)
    current_passed_tests = build_passed_tests_index(current_suites_json)
    if not current_failed_tests and not current_passed_tests:
        previous_report_url = get_previous_report_url(report_index_url)
        return [], [], previous_report_url, "", 0

    previous_report_url = get_previous_report_url(report_index_url)
    if not previous_report_url:
        return [], [], None, "Previous report not found. Unable to compare new fails/passes.", 0

    previous_base = get_report_base_url(previous_report_url)
    try:
        previous_suites_json = fetch_json(f"{previous_base}data/suites.json")
    except Exception:
        return [], [], previous_report_url, "Previous report data is unavailable. Unable to compare new fails/passes.", 0

    previous_failed_tests = build_failed_tests_index(previous_suites_json)
    previous_failed_names = set(previous_failed_tests)
    current_base = get_report_base_url(report_index_url)

    # Collect all new-fail names first (cheap — no HTTP calls yet)
    all_new_fail_names = sorted(
        name for name in current_failed_tests if name not in previous_failed_names
    )
    total_new_fail_count = len(all_new_fail_names)

    new_fails = []
    for test_name in all_new_fail_names:
        current_test = current_failed_tests[test_name]
        new_fails.append({
            "name": current_test["name"],
            "uid": current_test.get("uid"),
            "status": current_test["status"],
            "statusMessage": fetch_status_message(current_base, current_test["uid"]),
        })

    new_passes = []
    for test_name in sorted(current_passed_tests):
        previous_failed = previous_failed_tests.get(test_name)
        if not previous_failed:
            continue

        new_passes.append({
            "name": test_name,
            "previous_status": previous_failed.get("status", ""),
        })

        if len(new_passes) >= MAX_NEW_PASS_ITEMS:
            break

    return new_fails, new_passes, previous_report_url, "", total_new_fail_count

def process_single_report(report, links, report_cfg=None):
    """Fetch, parse, and prepare a single report payload."""
    cfg = _rcfg(report_cfg)
    report_base = get_report_base_url(report)
    report_index_url = f"{report_base}index.html"
    suites_json = fetch_json(f"{report_base}data/suites.json")
    env_json = fetch_json(f"{report_base}widgets/environment.json")
    summary_json = fetch_json(f"{report_base}widgets/summary.json")

    parser = AllureSuitesParser(data=suites_json, env_json=env_json, summary_json=summary_json,
                                unit_env_key=cfg.get("unit_env_key") or "HwSKU")
    parser.parse_report()

    duration_ms = summary_json.get("time", {}).get("duration", 0)
    duration_str = format_duration(duration_ms)

    report_general_stats: dict = dict(parser.general_stats)
    report_general_stats["duration"] = duration_str
    report_general_stats["duration_ms"] = duration_ms

    known_issues = get_known_issues_from_allure_report(
        report_index_url,
        keywords=cfg.get("known_issue_keywords"),
        excludes=cfg.get("known_issue_excludes"),
    )
    report_general_stats["known_issues"] = [
        {"name": issue["name"], "statusMessage": issue.get("statusMessage", "")}
        for issue in known_issues
    ]
    new_failed_tests, new_passed_tests, previous_report_url, new_fails_note, total_new_fail_count = get_new_failed_tests_from_previous_report(
        report_index_url,
        suites_json
    )
    report_general_stats["new_failed_tests"] = new_failed_tests
    report_general_stats["new_passed_tests"] = new_passed_tests
    report_general_stats["total_new_fail_count"] = total_new_fail_count
    report_general_stats["previous_report_url"] = previous_report_url or ""
    report_general_stats["new_fails_note"] = new_fails_note

    prev_stats = fetch_prev_run_stats(previous_report_url) if previous_report_url else None
    if prev_stats:
        report_general_stats["prev"] = prev_stats

    image_version = get_version_from_json(env_json, cfg)
    image_url = get_image_url_from_json(env_json, cfg)
    flagged = is_flagged_report(report_index_url, image_version, image_url, cfg)
    image_family = get_image_family_from_version(image_version, cfg)

    report_links = {
        "workweek": links["workweek"],
        "version": links["version"],
        "allure_report": report_index_url,
        "image": image_url,
        "image_version": image_version,
        "image_family": image_family,
        "is_ols": flagged,   # legacy key name; means "matches flag_keyword"
        "image_type": (cfg.get("flag_label") or "Flagged") if flagged else "Standard",
    }

    report_stats_copy = {
        **report_general_stats,
        "links": report_links,
        "test_summary": parser.suite_results,
    }

    return {
        "general_stats": report_general_stats,
        "general_stats_copy": report_stats_copy,
        "duration_ms": duration_ms,
        "suite_results": parser.suite_results,
    }

def get_version_from_json(env_json, report_cfg=None):
    """Extract the build version from environment.json (configured env key)."""
    key = _rcfg(report_cfg).get("version_env_key") or "Version"
    return next((item["values"][0] for item in env_json if item["name"] == key), "Unknown-Version")

def get_image_url_from_json(env_json, report_cfg=None):
    """Construct the build-image URL from environment.json and the configured server."""
    cfg = _rcfg(report_cfg)
    version = get_version_from_json(env_json, cfg)
    server = (cfg.get("image_server_url") or "").rstrip("/")
    if not version or not server:
        return "N/A"

    prefix = cfg.get("version_prefix") or ""
    image_name = version[len(prefix):] if prefix and version.startswith(prefix) else version
    return f"{server}/{image_name}/"

def get_image_family_from_version(version, report_cfg=None):
    """Classify the image family from configured version-substring rules."""
    cfg = _rcfg(report_cfg)
    version_lower = (version or "").lower()
    for rule in cfg.get("family_map") or []:
        if (rule.get("contains") or "").lower() in version_lower:
            return rule.get("family") or ""
    return cfg.get("family_default") or ""

def get_run_cadence(version, report_cfg=None):
    """Cadence label from configured version substrings ('Custom' when none match)."""
    version_lower = (version or "").lower()
    for keyword, label in (_rcfg(report_cfg).get("cadence_map") or {}).items():
        if keyword.lower() in version_lower:
            return label
    return "Custom"

def get_week_number():
    """Get current ISO week number."""
    return datetime.now().isocalendar()[1]

def format_duration(ms):
    """Format duration from milliseconds to HH:MM:SS."""
    total_seconds = int((ms or 0) / 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

def get_overall_summary(result_list, durations_ms=None):
    """Aggregate overall summary from multiple test results.

    NOTE: It was agreed within the team that tests marked as
    broken or unknown are to be considered as failed.
    That’s why the broken and unknown statuses are commented out.
    """
    total_passed = total_failed = total_skipped = total_tests = 0
    total_duration_ms = 0

    for i, entry in enumerate(result_list):
        total_passed += entry.get("passed", 0)
        total_failed += entry.get("failed", 0) + entry.get("broken", 0) + entry.get("unknown", 0)
        total_skipped += entry.get("skipped", 0)
        if durations_ms and i < len(durations_ms):
            total_duration_ms += durations_ms[i]

    total_tests = total_passed + total_failed + total_skipped

    return {
        "DUT": "Regression report",
        "total": total_tests,
        "passed": total_passed,
        "failed": total_failed,
        "skipped": total_skipped,
        # "broken": total_broken,
        # "unknown": total_unknown,
        "passed_percentage": round((total_passed / total_tests) * 100, 2) if total_tests else 0.0,
        "failed_percentage": round((total_failed / total_tests) * 100, 2) if total_tests else 0.0,
        "skipped_percentage": round((total_skipped / total_tests) * 100, 2) if total_tests else 0.0,
        # "broken_percentage": round((total_broken / total_tests) * 100, 2) if total_tests else 0.0,
        # "unknown_percentage": round((total_unknown / total_tests) * 100, 2) if total_tests else 0.0,
        "duration": format_duration(total_duration_ms) if durations_ms else "-"
    }

def get_version_tag(version: str, report_cfg=None) -> str:
    """Convert full version string to a concise tag via configured rules."""
    version_lower = (version or "").lower()
    for rule in _rcfg(report_cfg).get("tag_map") or []:
        substrings = rule.get("contains") or []
        if substrings and all(s.lower() in version_lower for s in substrings):
            return rule.get("tag") or version
    return version  # fallback to original version if no rule matches

def main(report_files, version, output_file="test_report.html", report_cfg=None):
    cfg = _rcfg(report_cfg)
    general_stats = []
    general_stats_copy = []
    durations = []
    links = {
        "workweek": get_week_number(),
        "version": version,
        "generated_on": datetime.now().strftime("%Y-%m-%d"),
        "run_cadence": get_run_cadence(version, cfg),
    }

    # De-duplicate URLs first to avoid repeated network fetches for identical reports.
    unique_reports = list(dict.fromkeys(report_files))
    workers = min(MAX_FETCH_WORKERS, max(1, len(unique_reports)))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        worker = partial(process_single_report, links=links, report_cfg=cfg)
        unique_results = list(executor.map(worker, unique_reports))

    by_report = dict(zip(unique_reports, unique_results))
    ordered_results = [by_report[report] for report in report_files]

    for entry in ordered_results:
        general_stats.append(entry["general_stats"])
        general_stats_copy.append(entry["general_stats_copy"])
        durations.append(entry["duration_ms"])

    links["contains_ols"] = any(
        entry["general_stats_copy"].get("links", {}).get("is_ols")
        for entry in ordered_results
    )
    image_families = {
        entry["general_stats_copy"].get("links", {}).get("image_family", cfg.get("family_default") or "")
        for entry in ordered_results
    }
    links["image_family"] = image_families.pop() if len(image_families) == 1 else "Mixed"

    suite_results = [entry["suite_results"] for entry in ordered_results]

    # Create overall summary
    overall_summary = get_overall_summary(general_stats, durations_ms=durations)

    # Remove duration from the first row
    overall_summary.pop("duration", None)

    # Insert as the first row
    general_stats.insert(0, overall_summary)

    env = Environment(
        loader=FileSystemLoader(os.path.join(os.path.dirname(os.path.realpath(__file__)), "templates"))
    )
    template = env.get_template("test_report.html")
    output = template.render(
        general_stats_list=general_stats,
        test_summary=suite_results,
        general_stats_list_copy=general_stats_copy,
        links=links,
        report_cfg=cfg,
    )

    with open(output_file, "w", encoding="utf-8") as file:
        file.write(output)

    version_tag = get_version_tag(version, cfg)
    print(f"Test report generated: [{version_tag} Jenkins]:[{version_tag} {links['workweek']}] Version {version}  {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate HTML test report from Allure JSON files.")
    parser.add_argument("-f", "--files", nargs="+", required=True, help="URLs to Allure reports (index.html)")
    parser.add_argument("-v", "--version", default="", help="Version tag. If not provided, it will be extracted from the first report.")
    parser.add_argument("-o", "--output", default="test_report.html", help="Path to the output HTML file.")
    args = parser.parse_args()

    if not args.version:
        report_base = get_report_base_url(args.files[0])
        env_json_first = fetch_json(f"{report_base}widgets/environment.json")
        args.version = get_version_from_json(env_json_first)
    main(args.files, args.version, args.output)
