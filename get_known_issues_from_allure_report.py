#!/usr/bin/env python3
"""
Extract known issues from an Allure report.

Usage:
    python3 get_known_issues_from_allure_report.py --url <ALLURE_REPORT_URL> [--keywords <KEYWORD1> <KEYWORD2> ...]

Options:
    --url       URL of the Allure report (required).
    --keywords  List of keywords to filter known issues (default: the module constants).

Example:
    python3 get_known_issues_from_allure_report.py --url http://localhost:5050/allure-docker-service/projects/my-project/reports/305/index.html --keywords PROJ- jira.example
    python3 get_known_issues_from_allure_report.py --url http://localhost:5050/allure-docker-service/projects/my-project/reports/latest/
"""

import argparse
import os
import requests
import sys
import re


# -----------------------------
# DEFAULT CONFIG
# -----------------------------
KNOWN_ISSUE_KEYWORDS_DEFAULT = ["SLE-", "jira.plvision"]
EXCLUDE_KNOWN_ISSUE_KEYWORDS = ["not implemented", "under investigation"]  # exclude msgs are not related to bug
HEADERS = {"accept": "*/*"}

# -----------------------------
# Vars for making clickable link (href)
# -----------------------------
_JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
BASE_TICKET_URL = f"{_JIRA_BASE_URL}/browse/" if _JIRA_BASE_URL else ""
TICKET_PATTERN = re.compile(r"\b(?!UTF-\d+\b)([A-Z]{2,5}-\d+)\b")
URL_TICKET_PATTERN = re.compile(r"https?://[^\s]+/([A-Z]+-\d+)", re.IGNORECASE)

# -----------------------------
# FUNCTIONS
# -----------------------------
def fetch_json(url):
    """Fetch JSON from Allure Docker Service"""
    r = requests.get(url, headers=HEADERS, timeout=10, verify=False)
    r.raise_for_status()
    return r.json()


def collect_known_uuids(node):
    """Recursively collect UIDs of skipped/xfail/xpass tests"""
    uuids = []
    if "children" in node:
        for child in node["children"]:
            uuids.extend(collect_known_uuids(child))
    else:
        status = node.get("status", "").lower()
        tags = [t.lower() for t in node.get("tags", [])]
        if status in ("skipped", "xfail", "xpass") or any(t in ("xfail", "xpass") for t in tags):
            uuids.append(node["uid"])
    return uuids


def get_test_details(uuid, allure_base):
    """Fetch detailed test case JSON for a given UUID."""
    url = f"{allure_base}data/test-cases/{uuid}.json"
    data = fetch_json(url)
    status_message = data.get("statusMessage", "")
    return {
        "name": data.get("name"),
        "status": data.get("status"),
        "statusMessage": linkify_jira_ticket(data.get("statusMessage", "")),
        "pytest_label": [
            l.get("value")
            for l in data.get("labels", [])
            if "pytest.mark" in l.get("value", "")
        ],
    }


def get_known_issues_from_allure_report(allure_report_url, keywords=None, excludes=None):
    """Extract known issues from a single Allure report URL.

    keywords/excludes default to the module constants; the dashboard passes the
    UI-configured report settings instead (Configuration → Advanced → Report).
    """
    allure_base = allure_report_url.replace("index.html", "")

    if not keywords:
        keywords = KNOWN_ISSUE_KEYWORDS_DEFAULT
    if excludes is None:
        excludes = EXCLUDE_KNOWN_ISSUE_KEYWORDS

    # Fetch suites.json
    suites_url = allure_base + "data/suites.json"
    suites_json = fetch_json(suites_url)

    # Collect known issue UIDs
    uuids = collect_known_uuids(suites_json)
    known_issues = []

    for uid in uuids:
        t = get_test_details(uid, allure_base)
        msg = (t.get("statusMessage") or "").lower()

        if any(kw.lower() in msg for kw in keywords) and not any(
            bad.lower() in msg for bad in excludes
        ):
            known_issues.append(t)
    return known_issues


def linkify_jira_ticket(text: str) -> str:
    """Convert Jira ticket IDs (e.g. PROJ-123) inside any text into HTML hyperlinks."""
    if not text:
        return ""

    full_match = URL_TICKET_PATTERN.search(text)
    id_match = TICKET_PATTERN.search(text)

    if full_match:
        ticket_id = full_match.group(1)
        before = text[:full_match.start()]  # keep text before link
        if not BASE_TICKET_URL:
            return f'{before}{ticket_id}'
        return f'{before}<a href="{BASE_TICKET_URL}{ticket_id}">{ticket_id}</a>'

    if id_match:
        ticket_id = id_match.group(1)
        before = text[:id_match.start()]
        if not BASE_TICKET_URL:
            return f'{before}{ticket_id}'
        return f'{before}<a href="{BASE_TICKET_URL}{ticket_id}">{ticket_id}</a>'

    return text


# -----------------------------
# MAIN
# -----------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Extract known issues from Allure reports."
    )
    parser.add_argument(
        "--url",
        required=True,
        help="URL of the Allure report (required).",
    )
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=KNOWN_ISSUE_KEYWORDS_DEFAULT,
        help="List of keywords to filter known issues (default: %(default)s).",
    )

    args = parser.parse_args()

    try:
        issues = get_known_issues_from_allure_report(
            allure_report_url=args.url,
            keywords=args.keywords
        )

        if not issues:
            print("No known issues found.")
            sys.exit(0)

        print(f"Found {len(issues)} known issues:\n")
        for idx, issue in enumerate(issues, 1):
            print(f"{idx}. {issue['name']}")
            print(f"   Status: {issue['status']}")
            if issue.get("statusMessage"):
                print(f"   Message: {issue['statusMessage']}")
            if issue.get("pytest_label"):
                print(f"   Labels: {issue['pytest_label']}")
            print("")

    except Exception as e:
        print(f"ERROR: Failed to process Allure report: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
