"""Shared, DB-backed configuration for the Allure dashboard.

Single source of truth for how project IDs are parsed into dimensions and how
those dimension values are labelled. Imported by app.py, mcp_server.py and
scraper.py so the web UI, the MCP server and the scraper all agree.

The config lives in SQLite tables (so it can be edited from the UI in later
phases). A monotonic ``config_meta.version`` counter lets each process cache the
parsed config and reload only when it actually changes.

Phase 1: tables are seeded with the original SONiC values, so behaviour is
identical to the previous hardcoded parser. Later phases move metrics, views and
auto-map rules onto the same config and expose an admin editor.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

DB_PATH = os.getenv("DB_PATH", "allure_data.db")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    version    INTEGER NOT NULL DEFAULT 1,
    seed_level INTEGER NOT NULL DEFAULT 0   -- highest applied default-seed migration
);

CREATE TABLE IF NOT EXISTS config_dimensions (
    key        TEXT PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 0,
    role       TEXT NOT NULL DEFAULT ''   -- 'title' marks the card-title dimension
);

CREATE TABLE IF NOT EXISTS config_patterns (
    name       TEXT PRIMARY KEY,
    regex      TEXT NOT NULL,
    group_map  TEXT NOT NULL DEFAULT '{}',   -- JSON {"1": "variant", ...}
    constants  TEXT NOT NULL DEFAULT '{}',   -- JSON {"variant": "debug", ...}
    transforms TEXT NOT NULL DEFAULT '{}',   -- JSON {"variant": "upper", ...}
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config_labels (
    dimension_key TEXT NOT NULL,
    value         TEXT NOT NULL,
    label         TEXT NOT NULL,
    PRIMARY KEY (dimension_key, value)
);

-- Created now so later phases can populate them without a migration.
CREATE TABLE IF NOT EXISTS config_views (
    name       TEXT PRIMARY KEY,
    scope_key  TEXT NOT NULL DEFAULT '',
    filter     TEXT NOT NULL DEFAULT '{}',   -- JSON column-filter predicate
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS config_metrics (
    name            TEXT PRIMARY KEY,
    test_names      TEXT NOT NULL DEFAULT '[]',   -- JSON list of candidate test names
    attachment_name TEXT NOT NULL DEFAULT '',
    value_keys      TEXT NOT NULL DEFAULT '[]',   -- JSON list of attachment keys
    label           TEXT NOT NULL DEFAULT '',     -- display name (falls back to name)
    unit            TEXT NOT NULL DEFAULT '',     -- display unit, e.g. "s"
    sort_order      INTEGER NOT NULL DEFAULT 0
);

-- Scraped metric values (data, not config — lives here because this module is
-- the one shared by app/scraper/mcp and owns the run-once migrations).
-- One row per (run, metric, series key). NULL value = the metric's test ran but
-- the value was absent/unparseable; the row still marks the run as scraped.
CREATE TABLE IF NOT EXISTS metric_values (
    project_id     TEXT    NOT NULL,
    report_number  INTEGER NOT NULL,
    metric_name    TEXT    NOT NULL,
    value_key      TEXT    NOT NULL,
    value          REAL,
    test_uid       TEXT,
    test_status    TEXT,
    scraped_at     TEXT,
    PRIMARY KEY (project_id, report_number, metric_name, value_key)
);
CREATE INDEX IF NOT EXISTS idx_metric_values_project
    ON metric_values(project_id, metric_name, report_number);

CREATE TABLE IF NOT EXISTS config_automap (
    suite_substring TEXT PRIMARY KEY,
    feature_name    TEXT NOT NULL
);

-- HTML-report settings (branding strings, image-server URL, known-issue
-- keywords, …). One row per key; values JSON-encoded. Missing keys fall back
-- to REPORT_DEFAULTS so older DBs and partial configs keep working.
CREATE TABLE IF NOT EXISTS config_report (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Direct project-ID → dimension-values mapping. Takes precedence over patterns,
-- so an admin can classify existing projects without writing any regex.
CREATE TABLE IF NOT EXISTS config_project_map (
    project_id TEXT PRIMARY KEY,
    dims       TEXT NOT NULL DEFAULT '{}'   -- JSON {"variant":"SL", ...}
);
"""

# ---------------------------------------------------------------------------
# Default seed — the original SONiC behaviour (Phase 1: behaviour-preserving)
# ---------------------------------------------------------------------------

_SEED_DIMENSIONS = [
    # (key, label, sort_order, role)
    ("cadence", "Cadence", 0, ""),
    ("platform", "Platform", 1, "title"),
    ("variant", "Variant", 2, ""),
    ("topo", "Topology", 3, ""),
]

_SEED_PATTERNS = [
    {
        "name": "regression",
        "regex": (
            r"^regression-(lts)"
            r"-(daily|weekly|release|healthcheck)"
            r"-(.+)-(t0|t1)$"
        ),
        "group_map": {"1": "variant", "2": "cadence", "3": "platform", "4": "topo"},
        "constants": {},
        "transforms": {"variant": "upper"},
        "sort_order": 0,
    },
    {
        "name": "debug",
        "regex": r"^debug-(.+)-(t0|t1)$",
        "group_map": {"1": "platform", "2": "topo"},
        "constants": {"variant": "debug", "cadence": "debug"},
        "transforms": {},
        "sort_order": 1,
    },
]

_SEED_LABELS = {
    "cadence": {
        "daily": "Daily",
        "weekly": "Weekly",
        "release": "Release",
        "healthcheck": "Healthcheck",
        "debug": "Debug",
        "other": "Other",
    },
    "variant": {
        "SL": "Sonic Lite",
        "LTS": "Sonic LTS",
        "debug": "Debug",
        "other": "Other",
    },
    "platform": {
        "vms-kvm": "VS",
        "ufispace-s9321-64e": "UfiSpace S9321-64E",
        "ufispace-s9300-32d": "UfiSpace S9300-32D",
        "ufispace-s9301-32db": "UfiSpace S9301-32DB",
        "ufispace-s9110-32x": "UfiSpace S9110-32X",
        "ufispace-s8901-54xc": "UfiSpace S8901-54XC",
        "edgecore-as7726-32x": "Edgecore AS7726-32X",
        "edgecore-as7816-64x": "Edgecore AS7816-64X",
        "edgecore-as9726-32d": "Edgecore AS9726-32D",
        "edgecore-as7326-56x": "Edgecore AS7326-56X",
        "celestica-ds3000": "Celestica DS3000",
        "celestica-ds2000": "Celestica DS2000",
        "celestica-ds4000": "Celestica DS4000",
        "celestica-ds4101": "Celestica DS4101",
        "celestica-ds5000": "Celestica DS5000",
    },
}

# The dimension that receives the raw project ID when nothing matches, and the
# legacy "debug if 'debug' in id else other" fallback values. Kept as code for
# now (becomes configurable in a later phase); seeded to match the old parser.
_FALLBACK_ID_DIMENSION = "platform"
_FALLBACK_DIMENSIONS = ["variant", "cadence"]

# Feature-matrix views. Each has a feature scope and a column-filter over
# dimensions, replacing the previously hardcoded OLS cadence special-casing.
# (name, scope, filter, sort_order)
_SEED_VIEWS = [
    ("Feature Matrix", "standard",
     {"rules": [{"dim": "cadence", "op": "not_in", "values": ["ols-daily", "ols-weekly"]}]}, 0),
    ("OLS", "ols",
     {"rules": [{"dim": "cadence", "op": "in", "values": ["ols-daily", "ols-weekly"]}]}, 1),
]

# suite_name → feature name heuristics for the "auto-map" button (was hardcoded
# in app.py). Seeded once; editable in the Configuration tab thereafter.
_SEED_AUTOMAP = {
    "tacacs": "TACACS+", "radius": "RADIUS", "klish_tests.macsec": "MACsec",
    "klish_tests.ols": "OLS Client",
    "lldp": "LLDP", "stp.pvst": "PVST", "klish_tests.stp.pvst": "PVST",
    "vlan": "VLAN", "klish_tests.l2_config": "VLAN", "fdb": "VLAN",
    "pc": "LAG/LACP", "dhcp_relay": "DHCP", "dhcp_server": "DHCP",
    "klish_tests.dhcp_client": "DHCP", "klish_tests.igmp": "IGMP Snooping",
    "mclag_tests.L2": "MCLAG", "mclag_tests.L3": "MCLAG",
    "arp": "IPv4/IPv6 Routing", "ip": "IPv4/IPv6 Routing", "ipfwd": "IPv4/IPv6 Routing",
    "route": "IPv4/IPv6 Routing", "klish_tests.route": "Static Routing",
    "bgp": "BGP", "klish_bgp": "BGP", "klish_tests.ospf": "OSPFv2", "klish_tests.isis": "IS-IS",
    "klish_tests.qos": "QoS", "storm_control": "Storm Control",
    "klish_tests.storm_control": "Storm Control", "klish_tests.poe": "PoE++",
    "acl": "ACL", "acl.null_route": "ACL", "klish_tests.acl": "ACL", "cacl": "ACL",
    "platform_tests": "Telemetry", "platform_tests.api": "Telemetry",
    "platform_tests.cli": "Telemetry",
}

# Numeric metrics scraped from a named test's attachment, used by
# scraper.scrape_run_details. Every configured metric is scraped into the generic
# metric_values table — add a new metric here (or in the UI) and the scraper,
# dashboard charts and MCP pick it up with no code changes. A metric is:
#   name            identifier (also the metric_name stored in metric_values)
#   test_names      candidate test names; the first one found in a run wins (lets
#                   old/renamed tests still resolve)
#   attachment_name the attachment on that test whose JSON holds the values
#   value_keys      mapping { series_key : [candidate attachment keys] }; each
#                   series_key becomes one stored series (first present source
#                   key wins, handling renamed keys). The FIRST series_key is
#                   the metric's primary series (tile sparkline / runs column).
#   label / unit    display name and unit for charts (label falls back to name)
_SEED_METRICS = [
    {
        "name": "boot_time",
        "test_names": [
            "test_boot_time_sonic_bpa_after_reboot", "test_reboot_klish",
            "test_boot_time_sonic_bpa_after_reboot_ols", "test_ols_reboot",
        ],
        "attachment_name": "boot-times",
        "value_keys": {"proc_uptime": ["port_oper", "proc_uptime"], "sonic_bpa": ["sonic_bpa"]},
        "label": "Boot Time",
        "unit": "s",
    },
]

# HTML-report settings: branding strings, image-server URL, version parsing and
# known-issue keywords used by report_generator / get_known_issues / the
# test_report.html template. All UI-editable (Configuration → Advanced →
# Report); these defaults reproduce the original SONiC report and are also the
# fallback for keys missing from the DB (and for standalone CLI use).
#   title_prefix          report heading / page title
#   unit_label            what one column-under-test is called (e.g. DUT)
#   image_server_url      base URL for build-image links ('' disables the link)
#   version_prefix        prefix stripped from the version to get the image dir
#   version_env_key       environment.json entry holding the build version
#   unit_env_key          environment.json entry naming the unit under test
#   family_map            ordered [{contains, family}] — first version substring
#                         match wins; family_default when none match
#   tag_map               ordered [{contains: [substrings], tag}] — all must
#                         match; used for the CLI summary line
#   flag_keyword          substring marking a special run flavor ('' disables)
#   flag_label            short label for flagged runs (chips, "X image")
#   flag_description      long label chip for flagged runs
#   cadence_map           ordered {version substring: cadence label}; falls back
#                         to "Custom"
#   known_issue_keywords  statusMessage substrings marking a known issue
#   known_issue_excludes  statusMessage substrings that veto the match
REPORT_DEFAULTS = {
    "title_prefix": "SONiC Regression Report",
    "unit_label": "DUT",
    "image_server_url": "http://172.20.10.124/images/",
    "version_prefix": "SONiC.",
    "version_env_key": "Version",
    "unit_env_key": "HwSKU",
    "family_map": [
        {"contains": "lts", "family": "SONIC-LTS"},
        {"contains": "lite", "family": "SONiC Lite"},
    ],
    "family_default": "SONiC LTS",
    "tag_map": [
        {"contains": ["lts"], "tag": "LTS"},
        {"contains": ["lite", "ols"], "tag": "SL-OLS"},
        {"contains": ["lite"], "tag": "SL"},
    ],
    "flag_keyword": "ols",
    "flag_label": "OLS",
    "flag_description": "OpenWiFi Local Stack",
    "cadence_map": {"weekly": "Weekly", "daily": "Daily"},
    "known_issue_keywords": ["SLE-", "jira.plvision"],
    "known_issue_excludes": ["not implemented", "under investigation"],
}

# Default-seed migration target. Each level seeds a table introduced later, applied
# exactly once per DB (tracked by config_meta.seed_level) so user deletions stick.
#   1 → views, 2 → auto-map, 3 → metrics, 4 → generic metric_values storage,
#   5 → report settings
_SEED_LEVEL = 5


def metric_by_name(cfg: "Config", name: str) -> Optional[dict]:
    """Return the configured metric with this name (or the first one as a fallback)."""
    for m in cfg.metrics:
        if m.get("name") == name:
            return m
    return cfg.metrics[0] if cfg.metrics else None


def metric_display(m: dict) -> dict:
    """Display info for a configured metric: label/unit plus the series keys.

    The first value_keys entry is the metric's primary series (used for the
    project-tile sparkline and the runs-table column).
    """
    keys = list((m.get("value_keys") or {}).keys())
    return {
        "name": m.get("name") or "",
        "label": m.get("label") or m.get("name") or "",
        "unit": m.get("unit") or "",
        "keys": keys,
        "primary_key": keys[0] if keys else "",
    }


def matches_filter(dims: dict, flt: Optional[dict]) -> bool:
    """True if dimension values satisfy every rule in a view's column filter.

    A rule is {dim, op: 'in'|'not_in', values}. An empty/missing filter matches all.
    """
    for rule in (flt or {}).get("rules", []):
        value = dims.get(rule.get("dim"))
        values = rule.get("values") or []
        if rule.get("op") == "not_in":
            if value in values:
                return False
        else:  # default 'in'
            if value not in values:
                return False
    return True


# ---------------------------------------------------------------------------
# Parsed config object
# ---------------------------------------------------------------------------

@dataclass
class _Pattern:
    name: str
    compiled: re.Pattern
    group_map: dict[str, str]
    constants: dict[str, str]
    transforms: dict[str, str]


@dataclass
class Config:
    version: int
    dimensions: list[str]                      # ordered dimension keys
    patterns: list[_Pattern]
    labels: dict[str, dict[str, str]]          # dim_key -> {value -> label}
    raw_dimensions: list[dict] = field(default_factory=list)
    project_map: dict[str, dict[str, str]] = field(default_factory=dict)  # pid -> {dim: value}
    views: list[dict] = field(default_factory=list)  # [{name, scope, filter, sort_order}]
    automap: dict[str, str] = field(default_factory=dict)  # suite_name -> feature_name
    metrics: list[dict] = field(default_factory=list)  # [{name, test_names, attachment_name, value_keys, label, unit}]
    report: dict = field(default_factory=dict)         # REPORT_DEFAULTS merged with stored overrides


# ---------------------------------------------------------------------------
# Module-level cache (per process)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cached: Optional[Config] = None


def _connect(db: Optional[sqlite3.Connection]) -> tuple[sqlite3.Connection, bool]:
    """Return (connection, owns_it). If db is given, reuse it; else open one."""
    if db is not None:
        return db, False
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn, True


def _schema_ready(conn: sqlite3.Connection) -> bool:
    """True if the config tables already exist and are seeded — read-only check.

    Lets ensure_schema short-circuit without taking any write lock on the common
    path (every startup after the first), avoiding lock contention with the
    scraper's concurrent writes.
    """
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(config_dimensions)")]
        if "role" not in cols:
            return False
        if conn.execute("SELECT 1 FROM config_dimensions LIMIT 1").fetchone() is None:
            return False
        meta_cols = [r[1] for r in conn.execute("PRAGMA table_info(config_meta)")]
        if "seed_level" not in meta_cols:
            return False
        return _read_seed_level(conn) >= _SEED_LEVEL
    except sqlite3.OperationalError:
        return False


def _read_seed_level(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT seed_level FROM config_meta WHERE id = 1").fetchone()
    return int(row["seed_level"]) if row and row["seed_level"] is not None else 0


def ensure_schema(db: Optional[sqlite3.Connection] = None) -> None:
    """Create config tables (idempotent) and apply pending default-seed migrations."""
    conn, owns = _connect(db)
    try:
        if _schema_ready(conn):
            return  # nothing to do — avoid DDL / write locks on the hot path
        conn.executescript(_SCHEMA)
        # Add columns missing on pre-existing config tables (only when absent —
        # an unconditional ALTER takes a write lock each boot).
        cols = [r[1] for r in conn.execute("PRAGMA table_info(config_dimensions)")]
        if "role" not in cols:
            conn.execute("ALTER TABLE config_dimensions ADD COLUMN role TEXT NOT NULL DEFAULT ''")
        meta_cols = [r[1] for r in conn.execute("PRAGMA table_info(config_meta)")]
        if "seed_level" not in meta_cols:
            conn.execute("ALTER TABLE config_meta ADD COLUMN seed_level INTEGER NOT NULL DEFAULT 0")
        metric_cols = [r[1] for r in conn.execute("PRAGMA table_info(config_metrics)")]
        if "label" not in metric_cols:
            conn.execute("ALTER TABLE config_metrics ADD COLUMN label TEXT NOT NULL DEFAULT ''")
        if "unit" not in metric_cols:
            conn.execute("ALTER TABLE config_metrics ADD COLUMN unit TEXT NOT NULL DEFAULT ''")

        _seed_if_empty(conn)  # fresh DB: dimensions/patterns/labels

        # Apply seed migrations exactly once per DB (so user deletions stick).
        conn.execute("INSERT OR IGNORE INTO config_meta (id, version, seed_level) VALUES (1, 1, 0)")
        level = _read_seed_level(conn)
        if level < 1:
            _seed_default_views(conn)
        if level < 2:
            _seed_default_automap(conn)
        if level < 3:
            _seed_default_metrics(conn)
        if level < 4:
            _migrate_metric_values(conn)
        if level < 5:
            _seed_default_report(conn)
        if level < _SEED_LEVEL:
            conn.execute("UPDATE config_meta SET seed_level = ? WHERE id = 1", (_SEED_LEVEL,))
        if owns:
            conn.commit()
    finally:
        if owns:
            conn.close()


def _seed_if_empty(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM config_dimensions").fetchone()
    if row and row["n"]:
        return  # already configured

    conn.execute("INSERT OR IGNORE INTO config_meta (id, version) VALUES (1, 1)")
    conn.executemany(
        "INSERT OR IGNORE INTO config_dimensions (key, label, sort_order, role) VALUES (?, ?, ?, ?)",
        _SEED_DIMENSIONS,
    )
    for p in _SEED_PATTERNS:
        conn.execute(
            "INSERT OR IGNORE INTO config_patterns "
            "(name, regex, group_map, constants, transforms, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                p["name"],
                p["regex"],
                json.dumps(p["group_map"]),
                json.dumps(p["constants"]),
                json.dumps(p["transforms"]),
                p["sort_order"],
            ),
        )
    for dim, mapping in _SEED_LABELS.items():
        conn.executemany(
            "INSERT OR IGNORE INTO config_labels (dimension_key, value, label) VALUES (?, ?, ?)",
            [(dim, value, label) for value, label in mapping.items()],
        )


def _seed_default_views(conn: sqlite3.Connection) -> None:
    """Seed the default matrix views (seed-level 1)."""
    conn.executemany(
        "INSERT OR IGNORE INTO config_views (name, scope_key, filter, sort_order) VALUES (?, ?, ?, ?)",
        [(name, scope, json.dumps(flt), order) for name, scope, flt, order in _SEED_VIEWS],
    )


def _seed_default_automap(conn: sqlite3.Connection) -> None:
    """Seed the default suite→feature auto-map heuristics (seed-level 2)."""
    conn.executemany(
        "INSERT OR IGNORE INTO config_automap (suite_substring, feature_name) VALUES (?, ?)",
        list(_SEED_AUTOMAP.items()),
    )


def _seed_default_metrics(conn: sqlite3.Connection) -> None:
    """Seed the default scraped metrics, e.g. boot-time (seed-level 3)."""
    conn.executemany(
        "INSERT OR IGNORE INTO config_metrics "
        "(name, test_names, attachment_name, value_keys, label, unit, sort_order) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (m["name"], json.dumps(m["test_names"]), m["attachment_name"],
             json.dumps(m["value_keys"]), m.get("label", ""), m.get("unit", ""), i)
            for i, m in enumerate(_SEED_METRICS)
        ],
    )


def _seed_default_report(conn: sqlite3.Connection) -> None:
    """Seed the HTML-report settings with the defaults (seed-level 5)."""
    conn.executemany(
        "INSERT OR IGNORE INTO config_report (key, value) VALUES (?, ?)",
        [(k, json.dumps(v)) for k, v in REPORT_DEFAULTS.items()],
    )


def _migrate_metric_values(conn: sqlite3.Connection) -> None:
    """One-time copy of the legacy boot_times columns into metric_values (level 4).

    Historical rows are attributed to the first configured metric (the seeded
    "boot_time"); its proc_uptime/sonic_bpa columns become value_key series.
    NULL values are copied too — they mark runs as already scraped. The legacy
    boot_times table is left in place (no longer read or written).
    """
    has_legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'boot_times'"
    ).fetchone()
    if not has_legacy:
        return
    row = conn.execute(
        "SELECT name FROM config_metrics ORDER BY sort_order, name LIMIT 1"
    ).fetchone()
    metric_name = row["name"] if row else "boot_time"
    for value_key in ("proc_uptime", "sonic_bpa"):
        conn.execute(
            f"""
            INSERT OR IGNORE INTO metric_values
                (project_id, report_number, metric_name, value_key, value,
                 test_uid, test_status, scraped_at)
            SELECT project_id, report_number, ?, ?, {value_key},
                   test_uid, test_status, scraped_at
            FROM boot_times
            """,
            (metric_name, value_key),
        )
    # Backfill display fields for the legacy metric so charts keep their labels.
    conn.execute(
        "UPDATE config_metrics SET label = 'Boot Time', unit = 's' "
        "WHERE name = 'boot_time' AND label = ''"
    )


def _read_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM config_meta WHERE id = 1").fetchone()
    return int(row["version"]) if row else 1


def _load(conn: sqlite3.Connection) -> Config:
    version = _read_version(conn)

    dim_rows = conn.execute(
        "SELECT key, label, sort_order, role FROM config_dimensions ORDER BY sort_order, key"
    ).fetchall()
    dimensions = [r["key"] for r in dim_rows]
    raw_dimensions = [dict(r) for r in dim_rows]

    patterns: list[_Pattern] = []
    for r in conn.execute(
        "SELECT name, regex, group_map, constants, transforms, sort_order "
        "FROM config_patterns ORDER BY sort_order, name"
    ).fetchall():
        patterns.append(
            _Pattern(
                name=r["name"],
                compiled=re.compile(r["regex"]),
                group_map=json.loads(r["group_map"]),
                constants=json.loads(r["constants"]),
                transforms=json.loads(r["transforms"]),
            )
        )

    labels: dict[str, dict[str, str]] = {}
    for r in conn.execute(
        "SELECT dimension_key, value, label FROM config_labels"
    ).fetchall():
        labels.setdefault(r["dimension_key"], {})[r["value"]] = r["label"]

    project_map: dict[str, dict[str, str]] = {}
    for r in conn.execute("SELECT project_id, dims FROM config_project_map").fetchall():
        try:
            project_map[r["project_id"]] = json.loads(r["dims"])
        except (TypeError, ValueError):
            pass

    views: list[dict] = []
    for r in conn.execute(
        "SELECT name, scope_key, filter, sort_order FROM config_views ORDER BY sort_order, name"
    ).fetchall():
        try:
            flt = json.loads(r["filter"])
        except (TypeError, ValueError):
            flt = {}
        views.append({"name": r["name"], "scope": r["scope_key"], "filter": flt,
                      "sort_order": r["sort_order"]})

    automap: dict[str, str] = {
        r["suite_substring"]: r["feature_name"]
        for r in conn.execute("SELECT suite_substring, feature_name FROM config_automap").fetchall()
    }

    metrics: list[dict] = []
    for r in conn.execute(
        "SELECT name, test_names, attachment_name, value_keys, label, unit "
        "FROM config_metrics ORDER BY sort_order, name"
    ).fetchall():
        try:
            test_names = json.loads(r["test_names"])
        except (TypeError, ValueError):
            test_names = []
        try:
            value_keys = json.loads(r["value_keys"])
        except (TypeError, ValueError):
            value_keys = {}
        metrics.append({"name": r["name"], "test_names": test_names,
                        "attachment_name": r["attachment_name"], "value_keys": value_keys,
                        "label": r["label"], "unit": r["unit"]})

    return Config(
        version=version,
        dimensions=dimensions,
        patterns=patterns,
        labels=labels,
        raw_dimensions=raw_dimensions,
        project_map=project_map,
        views=views,
        automap=automap,
        metrics=metrics,
        report=_load_report(conn),
    )


def _load_report(conn: sqlite3.Connection) -> dict:
    """Stored report settings merged over REPORT_DEFAULTS (missing keys fall back)."""
    report = dict(REPORT_DEFAULTS)
    try:
        rows = conn.execute("SELECT key, value FROM config_report").fetchall()
    except sqlite3.OperationalError:
        return report  # table not created yet (read-only consumer on an old DB)
    for r in rows:
        try:
            report[r["key"]] = json.loads(r["value"])
        except (TypeError, ValueError):
            pass
    return report


def get_config(db: Optional[sqlite3.Connection] = None) -> Config:
    """Return the parsed config, reloading only when the DB version changed.

    Hot path (``db is None`` and already loaded): returns the cached config
    without touching the database, so per-project-ID parsing stays cheap.

    Freshness path (``db`` provided, e.g. at a request or scrape boundary):
    does a single indexed version lookup on the caller's connection and reloads
    only when an admin actually saved a change — picking up edits made by other
    processes without a restart.
    """
    global _cached
    if db is None and _cached is not None:
        return _cached

    conn, owns = _connect(db)
    try:
        with _lock:
            if _cached is None:
                # First use in this process: ensure tables exist (fresh DB /
                # read-only process that never ran _init_db) then load.
                ensure_schema(conn)
                if owns:
                    conn.commit()
                _cached = _load(conn)
                return _cached
            version = _read_version(conn)
            if _cached.version != version:
                _cached = _load(conn)
            return _cached
    finally:
        if owns:
            conn.close()


def reload(db: Optional[sqlite3.Connection] = None) -> Config:
    """Force a reload (used after a config write)."""
    global _cached
    with _lock:
        _cached = None
    return get_config(db)


def bump_version(db: sqlite3.Connection) -> None:
    """Increment the config version so other processes reload. Caller commits."""
    db.execute(
        "INSERT INTO config_meta (id, version) VALUES (1, 2) "
        "ON CONFLICT(id) DO UPDATE SET version = version + 1"
    )


# ---------------------------------------------------------------------------
# Editing (admin UI) — dump / validate+replace / pattern preview
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """Raised when a submitted config is invalid (mapped to HTTP 400)."""


def dump_config(db: Optional[sqlite3.Connection] = None) -> dict:
    """Return the full editable config as plain JSON-able data (editor + export)."""
    conn, owns = _connect(db)
    try:
        ensure_schema(conn)
        dims = [
            {"key": r["key"], "label": r["label"], "sort_order": r["sort_order"], "role": r["role"]}
            for r in conn.execute(
                "SELECT key, label, sort_order, role FROM config_dimensions ORDER BY sort_order, key"
            )
        ]
        patterns = [
            {
                "name": r["name"],
                "regex": r["regex"],
                "group_map": json.loads(r["group_map"]),
                "constants": json.loads(r["constants"]),
                "transforms": json.loads(r["transforms"]),
                "sort_order": r["sort_order"],
            }
            for r in conn.execute(
                "SELECT name, regex, group_map, constants, transforms, sort_order "
                "FROM config_patterns ORDER BY sort_order, name"
            )
        ]
        labels: dict[str, dict[str, str]] = {}
        for r in conn.execute("SELECT dimension_key, value, label FROM config_labels"):
            labels.setdefault(r["dimension_key"], {})[r["value"]] = r["label"]
        project_map: dict[str, dict[str, str]] = {}
        for r in conn.execute("SELECT project_id, dims FROM config_project_map"):
            try:
                project_map[r["project_id"]] = json.loads(r["dims"])
            except (TypeError, ValueError):
                pass
        views = []
        for r in conn.execute(
            "SELECT name, scope_key, filter, sort_order FROM config_views ORDER BY sort_order, name"
        ):
            try:
                flt = json.loads(r["filter"])
            except (TypeError, ValueError):
                flt = {}
            views.append({"name": r["name"], "scope": r["scope_key"], "filter": flt,
                          "sort_order": r["sort_order"]})
        automap = {
            r["suite_substring"]: r["feature_name"]
            for r in conn.execute("SELECT suite_substring, feature_name FROM config_automap")
        }
        metrics = []
        for r in conn.execute(
            "SELECT name, test_names, attachment_name, value_keys, label, unit "
            "FROM config_metrics ORDER BY sort_order, name"
        ):
            try:
                tn = json.loads(r["test_names"])
            except (TypeError, ValueError):
                tn = []
            try:
                vk = json.loads(r["value_keys"])
            except (TypeError, ValueError):
                vk = {}
            metrics.append({"name": r["name"], "test_names": tn,
                            "attachment_name": r["attachment_name"], "value_keys": vk,
                            "label": r["label"], "unit": r["unit"]})
        return {
            "version": _read_version(conn),
            "dimensions": dims,
            "patterns": patterns,
            "labels": labels,
            "project_map": project_map,
            "views": views,
            "automap": automap,
            "metrics": metrics,
            "report": _load_report(conn),
        }
    finally:
        if owns:
            conn.close()


def _validate_config(dimensions: list[dict], patterns: list[dict], labels: dict) -> list[str]:
    """Return the ordered list of dimension keys, or raise ConfigError."""
    if not dimensions:
        raise ConfigError("At least one dimension is required")
    dim_keys: list[str] = []
    for d in dimensions:
        key = (d.get("key") or "").strip()
        if not key:
            raise ConfigError("Dimension key cannot be empty")
        if key in dim_keys:
            raise ConfigError(f"Duplicate dimension key: {key}")
        dim_keys.append(key)
    dim_set = set(dim_keys)

    seen_names: set[str] = set()
    for p in patterns:
        name = (p.get("name") or "").strip()
        if not name:
            raise ConfigError("Pattern name cannot be empty")
        if name in seen_names:
            raise ConfigError(f"Duplicate pattern name: {name}")
        seen_names.add(name)
        try:
            re.compile(p.get("regex") or "")
        except re.error as exc:
            raise ConfigError(f"Pattern '{name}': invalid regex: {exc}") from exc
        for grp, dim in (p.get("group_map") or {}).items():
            if dim not in dim_set:
                raise ConfigError(
                    f"Pattern '{name}': group {grp} maps to unknown dimension '{dim}'"
                )
        for dim in (p.get("constants") or {}):
            if dim not in dim_set:
                raise ConfigError(
                    f"Pattern '{name}': constant set for unknown dimension '{dim}'"
                )
    for dim in labels or {}:
        if dim not in dim_set:
            raise ConfigError(f"Labels set for unknown dimension '{dim}'")
    return dim_keys


def replace_config(
    db: Optional[sqlite3.Connection],
    *,
    dimensions: list[dict],
    patterns: list[dict],
    labels: dict[str, dict[str, str]],
    project_map: Optional[dict[str, dict[str, str]]] = None,
    views: Optional[list[dict]] = None,
    automap: Optional[dict[str, str]] = None,
    metrics: Optional[list[dict]] = None,
    report: Optional[dict] = None,
) -> int:
    """Validate and atomically replace the parsing config, bumping the version.

    Returns the new config version. Raises ConfigError on invalid input.
    """
    dim_keys = _validate_config(dimensions, patterns, labels)
    dim_set = set(dim_keys)
    project_map = project_map or {}
    views = views if views is not None else None  # None => keep existing views
    # Keep only known dimension keys with a non-empty value. A mapping overrides
    # just the dimensions it sets; empty selections are dropped (not stored as
    # blanks), and a project with no non-empty values is left unmapped.
    clean_map = {}
    for pid, dims in project_map.items():
        kept = {k: v for k, v in (dims or {}).items() if k in dim_set and v}
        if kept:
            clean_map[pid] = kept

    conn, owns = _connect(db)
    try:
        ensure_schema(conn)
        conn.execute("BEGIN")
        conn.execute("DELETE FROM config_dimensions")
        conn.execute("DELETE FROM config_patterns")
        conn.execute("DELETE FROM config_labels")
        conn.execute("DELETE FROM config_project_map")
        conn.executemany(
            "INSERT INTO config_dimensions (key, label, sort_order, role) VALUES (?, ?, ?, ?)",
            [
                (d["key"].strip(), (d.get("label") or "").strip(),
                 int(d.get("sort_order", i)), (d.get("role") or "").strip())
                for i, d in enumerate(dimensions)
            ],
        )
        conn.executemany(
            "INSERT INTO config_patterns "
            "(name, regex, group_map, constants, transforms, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    p["name"].strip(),
                    p["regex"],
                    json.dumps(p.get("group_map") or {}),
                    json.dumps(p.get("constants") or {}),
                    json.dumps(p.get("transforms") or {}),
                    int(p.get("sort_order", i)),
                )
                for i, p in enumerate(patterns)
            ],
        )
        conn.executemany(
            "INSERT INTO config_labels (dimension_key, value, label) VALUES (?, ?, ?)",
            [
                (dim, value, label)
                for dim, mapping in (labels or {}).items()
                for value, label in mapping.items()
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO config_project_map (project_id, dims) VALUES (?, ?)",
            [(pid, json.dumps(dims)) for pid, dims in clean_map.items() if dims],
        )
        if views is not None:
            conn.execute("DELETE FROM config_views")
            conn.executemany(
                "INSERT INTO config_views (name, scope_key, filter, sort_order) VALUES (?, ?, ?, ?)",
                [
                    ((v.get("name") or "").strip(), (v.get("scope") or "").strip(),
                     json.dumps(v.get("filter") or {}), int(v.get("sort_order", i)))
                    for i, v in enumerate(views)
                    if (v.get("name") or "").strip()
                ],
            )
        if automap is not None:
            conn.execute("DELETE FROM config_automap")
            conn.executemany(
                "INSERT OR REPLACE INTO config_automap (suite_substring, feature_name) VALUES (?, ?)",
                [(str(s).strip(), str(f).strip()) for s, f in automap.items()
                 if str(s).strip() and str(f).strip()],
            )
        if metrics is not None:
            conn.execute("DELETE FROM config_metrics")
            conn.executemany(
                "INSERT INTO config_metrics "
                "(name, test_names, attachment_name, value_keys, label, unit, sort_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ((m.get("name") or "").strip(), json.dumps(m.get("test_names") or []),
                     (m.get("attachment_name") or "").strip(),
                     json.dumps(m.get("value_keys") or {}),
                     (m.get("label") or "").strip(), (m.get("unit") or "").strip(),
                     int(m.get("sort_order", i)))
                    for i, m in enumerate(metrics)
                    if (m.get("name") or "").strip()
                ],
            )
        if report is not None:
            # Store only known keys; unknown ones are dropped so typos in an
            # imported config don't linger invisibly.
            conn.execute("DELETE FROM config_report")
            conn.executemany(
                "INSERT INTO config_report (key, value) VALUES (?, ?)",
                [(k, json.dumps(report[k])) for k in REPORT_DEFAULTS if k in report],
            )
        bump_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns:
            conn.close()

    return reload(db).version


def test_patterns(patterns: list[dict], pids: list[str]) -> list[dict]:
    """Preview how a candidate pattern set matches a list of project IDs.

    Returns one row per pid: {"project_id", "match": {"pattern", "dimensions"} | None}.
    Compile errors are reported per pid against the offending pattern.
    """
    compiled: list[tuple[dict, Optional[re.Pattern], Optional[str]]] = []
    for p in patterns:
        try:
            compiled.append((p, re.compile(p.get("regex") or ""), None))
        except re.error as exc:
            compiled.append((p, None, str(exc)))

    results = []
    for pid in pids:
        match = None
        for p, rx, _err in compiled:
            if rx is None:
                continue
            m = rx.match(pid)
            if not m:
                continue
            dims = dict(p.get("constants") or {})
            transforms = p.get("transforms") or {}
            for grp, dim in (p.get("group_map") or {}).items():
                try:
                    value = m.group(int(grp))
                except (IndexError, ValueError):
                    value = None
                if transforms.get(dim) == "upper" and value is not None:
                    value = value.upper()
                dims[dim] = value
            match = {"pattern": p.get("name"), "dimensions": dims, "fallback": False}
            break
        if match is None:
            match = {"pattern": None, "dimensions": _fallback_dims(pid), "fallback": True}
        results.append({"project_id": pid, "match": match})
    return results


# ---------------------------------------------------------------------------
# Project-ID parsing (replaces the duplicated parsers in app.py / mcp_server.py)
# ---------------------------------------------------------------------------

def _label_for(cfg: Config, dim: str, value: str) -> str:
    return cfg.labels.get(dim, {}).get(value, value)


def _build_result(cfg: Config, dims: dict[str, str]) -> dict:
    """Flatten parsed dimension values into the legacy result shape.

    Always includes ``dimensions`` and ``labels`` maps, plus top-level
    ``<dim>`` / ``<dim>_label`` keys for every configured dimension so existing
    call sites (meta["variant"], meta["cadence_label"], …) keep working.
    """
    out: dict[str, Any] = {"dimensions": {}, "labels": {}}
    for dim in cfg.dimensions:
        value = dims.get(dim, "")
        label = _label_for(cfg, dim, value)
        out["dimensions"][dim] = value
        out["labels"][dim] = label
        out[dim] = value
        out[f"{dim}_label"] = label
    return out


def _fallback_dims(pid: str) -> dict:
    """Dimension values when no pattern matches (legacy SONiC behaviour)."""
    value = "debug" if "debug" in pid else "other"
    dims = {d: value for d in _FALLBACK_DIMENSIONS}
    dims[_FALLBACK_ID_DIMENSION] = pid
    return dims


def _match_compiled(patterns: list[_Pattern], pid: str) -> tuple[Optional[str], Optional[dict]]:
    """First matching compiled pattern → (name, dims), else (None, None)."""
    for pat in patterns:
        m = pat.compiled.match(pid)
        if not m:
            continue
        dims = dict(pat.constants)
        for grp, dim in pat.group_map.items():
            value = m.group(int(grp))
            if pat.transforms.get(dim) == "upper" and value is not None:
                value = value.upper()
            dims[dim] = value
        return pat.name, dims
    return None, None


def parse_project_id(pid: str, config: Optional[Config] = None) -> dict:
    cfg = config or get_config()

    # Base classification from regex patterns, or the fallback.
    _name, dims = _match_compiled(cfg.patterns, pid)
    if dims is None:
        dims = _fallback_dims(pid)

    # An explicit per-project mapping overrides only the dimensions it sets,
    # so a partial mapping keeps the rest from the pattern/fallback.
    override = cfg.project_map.get(pid)
    if override:
        dims = {**dims, **{k: v for k, v in override.items() if v}}

    return _build_result(cfg, dims)
