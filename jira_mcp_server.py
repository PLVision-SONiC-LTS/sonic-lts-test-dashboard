"""
Standalone Jira MCP server — search issues with JQL or log snippets (no DB, no dashboard).

Use with the same credentials as the web app (.env): JIRA_BASE_URL, JIRA_USERNAME,
JIRA_PASSWORD. TLS verification is off by default (self-signed); set JIRA_VERIFY_SSL=true
to enforce certificate checks.

Run:
    .venv/bin/python jira_mcp_server.py                    # stdio (default)
    .venv/bin/python jira_mcp_server.py --http             # SSE on 0.0.0.0:8002
    .venv/bin/python jira_mcp_server.py --http --port 9000

Connect (HTTP/SSE), e.g. Claude Code:
    claude mcp add --transport sse jira-search http://127.0.0.1:8002/sse
"""

from __future__ import annotations

import argparse
import os
import re
import time
from typing import Any, Optional

import requests as _requests
import urllib3
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_USERNAME = os.getenv("JIRA_USERNAME", "")
JIRA_PASSWORD = os.getenv("JIRA_PASSWORD", "")
JIRA_VERIFY_SSL = os.getenv("JIRA_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
JIRA_SEARCH_MAX_RESULTS_CAP = int(os.getenv("JIRA_SEARCH_MAX_RESULTS_CAP", "50"))
JIRA_SNIPPET_MAX_LEN = int(os.getenv("JIRA_SNIPPET_MAX_LEN", "200"))
JIRA_HTTP_TIMEOUT = float(os.getenv("JIRA_HTTP_TIMEOUT", "15"))
# Transient blips (VPN/Wi‑Fi, errno 113/110): retry connection-level failures a few times
JIRA_HTTP_RETRIES = max(1, int(os.getenv("JIRA_HTTP_RETRIES", "3")))
JIRA_HTTP_RETRY_BACKOFF = float(os.getenv("JIRA_HTTP_RETRY_BACKOFF", "0.6"))
JIRA_COMMENTS_MAX = max(1, int(os.getenv("JIRA_COMMENTS_MAX", "100")))
JIRA_WORKLOG_MAX = max(1, int(os.getenv("JIRA_WORKLOG_MAX", "100")))
JIRA_CHANGELOG_MAX = max(1, int(os.getenv("JIRA_CHANGELOG_MAX", "50")))
JIRA_COMMENT_BODY_MAX = max(256, int(os.getenv("JIRA_COMMENT_BODY_MAX", "8000")))

_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")
_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{0,9}$")

# Default project key for jira_search_from_failure_text when project_key is omitted (JQL uses the key, e.g. SL, not the numeric id)
_raw_def_proj = (os.getenv("JIRA_DEFAULT_PROJECT") or "").strip().upper()
JIRA_DEFAULT_PROJECT_KEY: Optional[str] = (
    _raw_def_proj if _raw_def_proj and _PROJECT_KEY_RE.match(_raw_def_proj) else None
)

_args = argparse.ArgumentParser(description="Standalone Jira MCP server")
_args.add_argument("--http", action="store_true", help="Run as HTTP/SSE server")
_args.add_argument("--host", default="0.0.0.0")
_args.add_argument(
    "--port",
    type=int,
    default=int(os.getenv("JIRA_MCP_PORT", "8002")),
)
_parsed = _args.parse_args()

mcp = FastMCP("Jira Search", host=_parsed.host, port=_parsed.port)


def _jira_configured() -> bool:
    return bool(JIRA_BASE_URL and JIRA_USERNAME and JIRA_PASSWORD)


def _friendly_connection_error(exc: BaseException) -> dict[str, Any]:
    """Turn long urllib3/requests errors into a short message + optional hint (network vs app)."""
    msg = str(exc)
    hint: Optional[str] = None
    if "No route to host" in msg or "Errno 113" in msg:
        short = "Cannot reach Jira host (no route to host — errno 113)."
        hint = (
            "Your machine had no network path to JIRA_BASE_URL at that moment. If this is occasional, it is often a brief "
            "VPN/Wi‑Fi drop or routing flap; the client retries a few times (JIRA_HTTP_RETRIES). Persistent failures: connect VPN, "
            "check firewall outbound TCP 8443, `curl -vI \"$JIRA_BASE_URL\"` from the same host that runs the MCP server."
        )
    elif (
        "Name or service not known" in msg
        or "Failed to resolve" in msg
        or "getaddrinfo failed" in msg
        or "nodename nor servname" in msg
    ):
        short = "Cannot resolve Jira hostname (DNS)."
        hint = "Check JIRA_BASE_URL spelling; internal hostnames often need VPN or correct DNS."
    elif "Connection refused" in msg or "Errno 111" in msg:
        short = "Connection refused by Jira host/port."
        hint = "Host is reachable but nothing accepts connections on that port — verify URL/port (e.g. :8443) and that Jira is running."
    elif "timed out" in msg.lower() or "Timeout" in msg or "Errno 110" in msg:
        short = "Jira request timed out."
        hint = "Try JIRA_HTTP_TIMEOUT higher; check VPN stability or server load."
    else:
        short = msg if len(msg) < 400 else msg[:397] + "…"
    out: dict[str, Any] = {"ok": False, "error": short, "detail": msg[:1200]}
    if hint:
        out["hint"] = hint
    return out


def _transient_jira_connection_error(exc: BaseException) -> bool:
    """True if retrying the same request might succeed (network blip, not bad credentials in response)."""
    if isinstance(exc, _requests.Timeout):
        return True
    if isinstance(exc, _requests.ConnectionError):
        return True
    return False


def _jira_request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
) -> tuple[bool, Any]:
    if not _jira_configured():
        return False, {
            "ok": False,
            "error": "Jira not configured: set JIRA_BASE_URL, JIRA_USERNAME, JIRA_PASSWORD",
        }
    url = f"{JIRA_BASE_URL}{path}"
    for attempt in range(JIRA_HTTP_RETRIES):
        try:
            r = _requests.request(
                method,
                url,
                params=params,
                auth=(JIRA_USERNAME, JIRA_PASSWORD),
                timeout=JIRA_HTTP_TIMEOUT,
                verify=JIRA_VERIFY_SSL,
            )
            if not r.ok:
                body = (r.text or "")[:800]
                return False, {
                    "ok": False,
                    "error": f"Jira HTTP {r.status_code}",
                    "detail": body or r.reason,
                }
            return True, r.json()
        except _requests.RequestException as e:
            if attempt < JIRA_HTTP_RETRIES - 1 and _transient_jira_connection_error(e):
                time.sleep(JIRA_HTTP_RETRY_BACKOFF * (2**attempt))
                continue
            err = _friendly_connection_error(e)
            if JIRA_HTTP_RETRIES > 1 and attempt > 0:
                err = {**err, "connection_attempts": attempt + 1}
            return False, err
    return False, {"ok": False, "error": "internal: Jira retry loop exhausted"}


def _escape_jql_string(value: str) -> str:
    v = " ".join(value.split())
    if len(v) > JIRA_SNIPPET_MAX_LEN:
        v = v[-JIRA_SNIPPET_MAX_LEN:]
    return v.replace("\\", "\\\\").replace('"', '\\"')


def _user_display(u: Any) -> Optional[str]:
    if not u or not isinstance(u, dict):
        return None
    return u.get("displayName") or u.get("name") or u.get("emailAddress")


def _description_to_text(desc: Any, max_len: int) -> str:
    if desc is None:
        return ""
    if isinstance(desc, str):
        text = desc
    elif isinstance(desc, dict):
        text = _adf_plain_text(desc)
    else:
        text = str(desc)
    text = " ".join(text.split())
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


def _adf_plain_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text") or ""
        parts: list[str] = []
        for c in node.get("content") or []:
            parts.append(_adf_plain_text(c))
        if node.get("type") == "hardBreak":
            parts.append("\n")
        return "".join(parts)
    if isinstance(node, list):
        return "".join(_adf_plain_text(x) for x in node)
    return ""


def _run_jql_search(
    jql: str,
    max_results: int,
    include_description: bool,
    start_at: int,
) -> dict[str, Any]:
    if not jql or not jql.strip():
        return {"ok": False, "error": "jql must be non-empty"}
    mr = max(1, min(int(max_results), JIRA_SEARCH_MAX_RESULTS_CAP))
    sa = max(0, int(start_at))
    fields = "summary,status,assignee,reporter,created,updated,issuetype,priority"
    if include_description:
        fields += ",description"
    ok, data = _jira_request(
        "GET",
        "/rest/api/2/search",
        params={
            "jql": jql.strip(),
            "fields": fields,
            "maxResults": mr,
            "startAt": sa,
        },
    )
    if not ok:
        return data if isinstance(data, dict) else {"ok": False, "error": str(data)}
    issues_raw = data.get("issues") or []
    issues = [
        _issue_compact(iss, include_description=include_description)
        for iss in issues_raw
    ]
    return {
        "ok": True,
        "jql": jql.strip(),
        "total": data.get("total", len(issues)),
        "start_at": sa,
        "max_results": mr,
        "returned": len(issues),
        "issues": issues,
    }


def _issue_compact(
    issue: dict[str, Any],
    *,
    include_description: bool = False,
    include_slts_description: bool = False,
    desc_max: int = 2000,
) -> dict[str, Any]:
    key = issue["key"]
    fields = issue.get("fields") or {}
    st = fields.get("status") or {}
    it = fields.get("issuetype") or {}
    pr = fields.get("priority") or {}
    out: dict[str, Any] = {
        "key": key,
        "summary": fields.get("summary") or "",
        "status": st.get("name", ""),
        "status_category": (st.get("statusCategory") or {}).get("name", ""),
        "issuetype": it.get("name", ""),
        "priority": pr.get("name", ""),
        "assignee": _user_display(fields.get("assignee")),
        "reporter": _user_display(fields.get("reporter")),
        "created": fields.get("created"),
        "updated": fields.get("updated"),
        "url": f"{JIRA_BASE_URL}/browse/{key}",
    }
    if include_description:
        out["description"] = _description_to_text(fields.get("description"), desc_max)
    if include_slts_description:
        out["slts_description"] = _description_to_text(fields.get("customfield_20000"), desc_max)
    return out


def _attachment_compact(att: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": att.get("id"),
        "filename": att.get("filename"),
        "size": att.get("size"),
        "mimeType": att.get("mimeType"),
        "created": att.get("created"),
        "author": _user_display(att.get("author")),
        "content_url": att.get("content"),
        "thumbnail_url": att.get("thumbnail"),
    }


def _subtask_compact(st: dict[str, Any]) -> dict[str, Any]:
    fields = st.get("fields") or {}
    status = fields.get("status") or {}
    return {
        "key": st.get("key"),
        "summary": fields.get("summary") or "",
        "status": status.get("name", ""),
        "issuetype": (fields.get("issuetype") or {}).get("name", ""),
    }


def _issuelink_compact(link: dict[str, Any]) -> dict[str, Any]:
    typ = link.get("type") or {}
    out: dict[str, Any] = {
        "link_type": typ.get("name") or typ.get("inward") or typ.get("outward"),
    }
    if link.get("outwardIssue"):
        oi = link["outwardIssue"]
        ofs = oi.get("fields") or {}
        out["outward"] = {
            "key": oi.get("key"),
            "summary": ofs.get("summary") or "",
            "status": (ofs.get("status") or {}).get("name", ""),
        }
    if link.get("inwardIssue"):
        ii = link["inwardIssue"]
        ifs = ii.get("fields") or {}
        out["inward"] = {
            "key": ii.get("key"),
            "summary": ifs.get("summary") or "",
            "status": (ifs.get("status") or {}).get("name", ""),
        }
    return out


def _comment_compact(c: dict[str, Any], body_max: int) -> dict[str, Any]:
    body = c.get("body")
    text = _description_to_text(body, body_max) if body is not None else ""
    return {
        "id": c.get("id"),
        "author": _user_display(c.get("author")),
        "created": c.get("created"),
        "updated": c.get("updated"),
        "body": text,
    }


def _worklog_compact(w: dict[str, Any], comment_max: int) -> dict[str, Any]:
    note = w.get("comment")
    comment_txt = ""
    if note:
        comment_txt = _description_to_text(note, comment_max)
    return {
        "id": w.get("id"),
        "author": _user_display(w.get("author")),
        "started": w.get("started"),
        "created": w.get("created"),
        "updated": w.get("updated"),
        "timeSpent": w.get("timeSpent"),
        "timeSpentSeconds": w.get("timeSpentSeconds"),
        "comment": comment_txt,
    }


def _changelog_compact(changelog: Optional[dict[str, Any]], max_histories: int) -> list[dict[str, Any]]:
    if not changelog or not isinstance(changelog, dict):
        return []
    histories = changelog.get("histories") or []
    out: list[dict[str, Any]] = []
    for h in histories[:max_histories]:
        items_out = []
        for it in h.get("items") or []:
            items_out.append({
                "field": it.get("field"),
                "from": it.get("fromString"),
                "to": it.get("toString"),
            })
        out.append({
            "id": h.get("id"),
            "author": _user_display(h.get("author")),
            "created": h.get("created"),
            "items": items_out,
        })
    return out


def _fetch_comments_paginated(key: str, max_total: int, body_max: int) -> tuple[bool, Any]:
    comments: list[dict[str, Any]] = []
    start = 0
    page_size = 50
    reported_total: Optional[int] = None
    while len(comments) < max_total:
        ok, data = _jira_request(
            "GET",
            f"/rest/api/2/issue/{key}/comment",
            params={
                "startAt": start,
                "maxResults": min(page_size, max_total - len(comments)),
            },
        )
        if not ok:
            return False, data
        if isinstance(data, dict):
            reported_total = data.get("total", reported_total)
        batch = (data.get("comments") or []) if isinstance(data, dict) else []
        if not batch:
            break
        for c in batch:
            comments.append(_comment_compact(c, body_max))
            if len(comments) >= max_total:
                break
        start += len(batch)
        if reported_total is not None and start >= reported_total:
            break
        if len(batch) < page_size:
            break
    return True, {
        "comments": comments,
        "total": reported_total if reported_total is not None else len(comments),
    }


def _fetch_worklog_paginated(key: str, max_total: int, note_max: int) -> tuple[bool, Any]:
    entries: list[dict[str, Any]] = []
    start = 0
    page_size = 50
    last_total = 0
    while len(entries) < max_total:
        ok, data = _jira_request(
            "GET",
            f"/rest/api/2/issue/{key}/worklog",
            params={
                "startAt": start,
                "maxResults": min(page_size, max_total - len(entries)),
            },
        )
        if not ok:
            return False, data
        batch = data.get("worklogs") or []
        last_total = int(data.get("total") or 0)
        for w in batch:
            entries.append(_worklog_compact(w, note_max))
            if len(entries) >= max_total:
                break
        if not batch:
            break
        start += len(batch)
        if last_total and start >= last_total:
            break
        if len(batch) < page_size:
            break
    return True, {"worklogs": entries, "total": last_total or len(entries)}


@mcp.tool()
def jira_ping() -> dict[str, Any]:
    """Check Jira credentials and reachability (GET /rest/api/2/myself). No JQL."""
    ok, data = _jira_request("GET", "/rest/api/2/myself")
    if not ok:
        return data if isinstance(data, dict) else {"ok": False, "error": str(data)}
    return {
        "ok": True,
        "server": JIRA_BASE_URL,
        "user": data.get("displayName") or data.get("name"),
        "email": data.get("emailAddress"),
    }


@mcp.tool()
def jira_jql_search(
    jql: str,
    max_results: int = 25,
    include_description: bool = False,
    start_at: int = 0,
) -> dict[str, Any]:
    """Run arbitrary Jira JQL (same as Advanced search in the UI).

    Examples:
        text ~ "\\"Connection refused\\"" AND status != Closed
        summary ~ "BGP" ORDER BY updated DESC
        project = PROJ AND issuetype = Bug AND status = Open

    Use for precise queries after you extract keywords from a test failure log.
    """
    return _run_jql_search(
        jql,
        max_results=max_results,
        include_description=include_description,
        start_at=start_at,
    )


@mcp.tool()
def jira_search_from_failure_text(
    log_snippet: str,
    project_key: Optional[str] = None,
    max_results: int = 20,
    use_error_tail: bool = True,
    include_description: bool = False,
) -> dict[str, Any]:
    """Search Jira for issues matching a failure log fragment (JQL text ~ \"…\").

    Collapses whitespace, escapes quotes, truncates to JIRA_SNIPPET_MAX_LEN (default 200).
    When use_error_tail=true, uses the last non-empty line first (typical assertion/error line).

    Optionally pass project_key (e.g. PROJ), or set JIRA_DEFAULT_PROJECT in .env to apply the same
    scope when project_key is omitted. To search every project, use jira_jql_search with JQL
    `text ~ "…"` and no `project =` clause (or unset JIRA_DEFAULT_PROJECT).
    """
    raw = (log_snippet or "").strip()
    if not raw:
        return {"ok": False, "error": "log_snippet is empty"}
    if use_error_tail:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if lines:
            raw = lines[-1]
            if len(raw) < 8 and len(lines) >= 2:
                raw = f"{lines[-2]} {lines[-1]}"
    escaped = _escape_jql_string(raw)
    if not escaped:
        return {"ok": False, "error": "snippet empty after normalization"}
    parts = [f'text ~ "{escaped}"']
    pk_source = (project_key or "").strip() or (JIRA_DEFAULT_PROJECT_KEY or "")
    if pk_source:
        pk = pk_source.strip().upper()
        if not _PROJECT_KEY_RE.match(pk):
            return {
                "ok": False,
                "error": f"invalid project_key {pk_source!r} (expected letters/digits, e.g. PROJ)",
            }
        parts.insert(0, f"project = {pk}")
    jql = " AND ".join(parts) + " ORDER BY updated DESC"
    return _run_jql_search(
        jql,
        max_results=max_results,
        include_description=include_description,
        start_at=0,
    )


@mcp.tool()
def jira_get_issue(
    issue_key: str,
    include_description: bool = True,
    description_max_chars: int = 4000,
    include_all_details: bool = False,
    include_attachments: bool = False,
    include_comments: bool = False,
    include_worklog: bool = False,
    include_issue_links: bool = False,
    include_subtasks: bool = False,
    include_watchers: bool = False,
    include_changelog: bool = False,
    include_slts_description: bool = False,
    comment_body_max_chars: Optional[int] = None,
) -> dict[str, Any]:
    """Fetch one issue by key (e.g. PROJ-1234).

    Default: core fields + description. Set include_all_details=true to also load
    attachments (metadata + download URLs), comments, worklog, issue links, subtasks,
    watchers, and changelog (subject to JIRA_COMMENTS_MAX / JIRA_WORKLOG_MAX / JIRA_CHANGELOG_MAX).

    Attachment and comment bodies use the same API user; downloading content_url still requires auth.
    """
    key = (issue_key or "").strip().upper()
    if not _ISSUE_KEY_RE.match(key):
        return {
            "ok": False,
            "error": f"invalid issue_key {issue_key!r} (expected PROJ-123)",
        }
    if include_all_details:
        include_description = True
        include_attachments = True
        include_comments = True
        include_worklog = True
        include_issue_links = True
        include_subtasks = True
        include_watchers = True
        include_changelog = True
        include_slts_description = True

    field_list = [
        "summary",
        "status",
        "assignee",
        "reporter",
        "created",
        "updated",
        "issuetype",
        "priority",
        "labels",
    ]
    if include_description:
        field_list.append("description")
    if include_attachments:
        field_list.append("attachment")
    if include_subtasks:
        field_list.append("subtasks")
    if include_issue_links:
        field_list.append("issuelinks")
    if include_slts_description:
        field_list.append("customfield_20000")  # SLTS description (if present in this Jira instance)

    params: dict[str, Any] = {"fields": ",".join(field_list)}
    if include_changelog:
        params["expand"] = "changelog"

    ok, data = _jira_request("GET", f"/rest/api/2/issue/{key}", params=params)
    if not ok:
        return data if isinstance(data, dict) else {"ok": False, "error": str(data)}

    desc_max = max(200, min(int(description_max_chars), 50_000))
    body_max = comment_body_max_chars if comment_body_max_chars is not None else JIRA_COMMENT_BODY_MAX
    body_max = max(256, min(int(body_max), 500_000))

    fields = data.get("fields") or {}
    issue = _issue_compact(
        {"key": data.get("key", key), "fields": fields},
        include_description=include_description,
        include_slts_description=include_slts_description,
        desc_max=desc_max,
    )
    labels = fields.get("labels")
    if isinstance(labels, list):
        issue["labels"] = labels

    fetched: list[str] = []

    if include_attachments:
        atts = fields.get("attachment") or []
        if isinstance(atts, list):
            issue["attachments"] = [_attachment_compact(a) for a in atts if isinstance(a, dict)]
            fetched.append("attachments")

    if include_subtasks:
        sts = fields.get("subtasks") or []
        if isinstance(sts, list):
            issue["subtasks"] = [_subtask_compact(s) for s in sts if isinstance(s, dict)]
            fetched.append("subtasks")

    if include_issue_links:
        links = fields.get("issuelinks") or []
        if isinstance(links, list):
            issue["issue_links"] = [_issuelink_compact(ln) for ln in links if isinstance(ln, dict)]
            fetched.append("issue_links")

    if include_changelog:
        issue["changelog"] = _changelog_compact(data.get("changelog"), JIRA_CHANGELOG_MAX)
        fetched.append("changelog")

    warnings: list[str] = []

    if include_comments:
        cok, cdata = _fetch_comments_paginated(key, JIRA_COMMENTS_MAX, body_max)
        if cok and isinstance(cdata, dict):
            issue["comments"] = cdata.get("comments", [])
            issue["comments_total_reported"] = cdata.get("total")
            fetched.append("comments")
        else:
            err = cdata.get("error", cdata) if isinstance(cdata, dict) else str(cdata)
            warnings.append(f"comments not loaded: {err}")

    if include_worklog:
        wok, wdata = _fetch_worklog_paginated(key, JIRA_WORKLOG_MAX, body_max)
        if wok and isinstance(wdata, dict):
            issue["worklog"] = wdata.get("worklogs", [])
            issue["worklog_total_reported"] = wdata.get("total")
            fetched.append("worklog")
        else:
            err = wdata.get("error", wdata) if isinstance(wdata, dict) else str(wdata)
            warnings.append(f"worklog not loaded: {err}")

    if include_watchers:
        wok, wdata = _jira_request("GET", f"/rest/api/2/issue/{key}/watchers")
        if wok and isinstance(wdata, dict):
            watchers_raw = wdata.get("watchers")
            if isinstance(watchers_raw, list):
                issue["watchers"] = [_user_display(w) or "" for w in watchers_raw]
            else:
                issue["watchers"] = []
            issue["watch_count"] = wdata.get("watchCount", len(issue["watchers"]))
            fetched.append("watchers")
        else:
            err = wdata.get("error", wdata) if isinstance(wdata, dict) else str(wdata)
            warnings.append(f"watchers not loaded: {err}")

    if fetched:
        issue["details_included"] = fetched

    out: dict[str, Any] = {"ok": True, "issue": issue}
    if warnings:
        out["warnings"] = warnings
    return out


@mcp.tool()
def jira_search_help() -> dict[str, Any]:
    """Static hints for JQL and workflow (no network)."""
    return {
        "workflow": [
            "1) Paste failure message / stack tail into jira_search_from_failure_text",
            "2) Refine with jira_jql_search (summary ~, text ~, status, project, ORDER BY)",
            "3) jira_get_issue(include_all_details=true) for comments, attachments, worklog, links, changelog",
        ],
        "jql_tips": [
            "Phrase in text: text ~ \"\\\"exact substring\\\"\"",
            "Combine: project = PROJ AND status != Closed AND text ~ \"AssertionError\"",
            "Recent first: ... ORDER BY updated DESC",
        ],
        "env": {
            "JIRA_BASE_URL": "required",
            "JIRA_USERNAME": "required",
            "JIRA_PASSWORD": "required",
            "JIRA_DEFAULT_PROJECT": "optional; project key (e.g. PROJ) for jira_search_from_failure_text when project_key arg omitted",
            "JIRA_MCP_PORT": "default 8002 for --http",
            "JIRA_SNIPPET_MAX_LEN": str(JIRA_SNIPPET_MAX_LEN),
            "JIRA_SEARCH_MAX_RESULTS_CAP": str(JIRA_SEARCH_MAX_RESULTS_CAP),
            "JIRA_VERIFY_SSL": str(JIRA_VERIFY_SSL),
            "JIRA_HTTP_RETRIES": f"{JIRA_HTTP_RETRIES} (connection/timeout failures; exponential backoff from JIRA_HTTP_RETRY_BACKOFF)",
            "JIRA_HTTP_RETRY_BACKOFF": str(JIRA_HTTP_RETRY_BACKOFF),
            "JIRA_COMMENTS_MAX": str(JIRA_COMMENTS_MAX),
            "JIRA_WORKLOG_MAX": str(JIRA_WORKLOG_MAX),
            "JIRA_CHANGELOG_MAX": str(JIRA_CHANGELOG_MAX),
            "JIRA_COMMENT_BODY_MAX": str(JIRA_COMMENT_BODY_MAX),
        },
        "default_project_active": JIRA_DEFAULT_PROJECT_KEY,
    }


if __name__ == "__main__":
    if _parsed.http:
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
