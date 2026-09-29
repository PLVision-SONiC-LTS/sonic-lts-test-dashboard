# Test Dashboard — User Guide

## Overview

Test Dashboard is a web UI that aggregates test results from an Allure Docker Service instance and presents them across several tabs. How projects are grouped (the **dimensions**), the labels, the matrix **views**, and the scraped metrics are all configurable by an admin in the **Configuration** tab — the examples below use the bundled PLVision SONiC preset (cadence / platform / variant, with "Feature Matrix" and "OLS" views).

| Tab | Purpose |
|-----|---------|
| **Dashboard** | Card-per-project overview with pass rate, sparkline trends, and a detail panel |
| **_(one tab per configured view)_** | Features × project-column coverage matrix — the example preset seeds "Feature Matrix" and "OLS" |
| **Scraper** | Coverage summary showing how many test results have been scraped per run |
| **Manage Features** | Add/remove features and test-case mappings per view scope |
| **Configuration** | _(admin only)_ Edit dimensions, parsing patterns, labels, project mappings, views, auto-map rules, metrics, and report settings — see the README "Configuration tab" section |

---

## Logging in

If LDAP authentication is enabled, a sign-in screen appears on first visit.
Enter your domain username and password, then click **Sign in**.
Sessions last 8 hours. Click **Sign out** (top-right) to end the session early.

> If `LDAP_URI` is not configured by the administrator, the dashboard is open without login.

---

## Dashboard tab

### Project cards

Each card shows one test project (a unique platform + variant + cadence combination):

| Element | Meaning |
|---------|---------|
| **Title** | Hardware platform name |
| **Blue badge** | Variant — SonicLite (SL) or SonicX (SX) |
| **Grey badge** | Cadence — daily, weekly, release, healthcheck |
| **Large %** | Pass rate of the latest run, coloured green ≥ 90 %, yellow ≥ 70 %, orange ≥ 50 %, red < 50 % |
| **Top-right sparkline** | Pass rate trend over the last 15 runs (same colour scale as the %) |
| **Run info** | Report number and start timestamp of the latest run |
| **Colour bar** | Breakdown of latest run: green = passed, red = failed, orange = broken, grey = skipped |
| **Counts** | Passed ✓ / Failed ✗ / Broken ⚠ / Skipped ⊘ |
| **Bottom-right sparkline** | Total test count trend over the last 15 runs (slate) |
| **Duration** | Wall-clock duration of the latest run |

### Trend sparklines

Each card displays two mini sparklines showing history across the last 15 runs (oldest on the left, newest on the right):

**Pass rate sparkline** (top-right, next to the large %)
- The line is coloured by the current pass rate: green ≥ 90 %, yellow ≥ 70 %, orange ≥ 50 %, red < 50 %
- A rising line means pass rate is improving; a falling line means it is degrading
- A flat line means results are stable

**Test count sparkline** (bottom-right, next to the counts row)
- Shown in slate grey — colour has no health meaning here
- A rising line means more tests are being run over time (suite is growing)
- A falling line means fewer tests ran in recent runs (tests removed, or some suites skipped)
- Sudden drops can indicate a partial run or infrastructure issue

### Filtering

Three rows of filter pills appear above the grid:

- **Cadence** — click a pill to show only that cadence; click again to clear
- **Platform** — filter by hardware platform
- **Variant** — filter by SL / SX

Clicking an already-active pill resets it to "all". Multiple filters combine (AND logic).

### Detail panel

Click any card to open a slide-in detail panel for that project. It shows:

- Latest run stats (Total / Passed / Failed / Broken / Skipped)
- **Test history** bar chart — stacked pass/fail/broken/skipped per run (up to 60 runs)
- **Duration** line chart — run wall-clock time in hours
- **Metric** charts — one per configured metric (e.g. boot time), scraped from test attachments (if available)
- **Run table** — one row per run; click a row to open the corresponding Allure report in a new tab

Close the panel by clicking **✕** or clicking outside it.

### Settings

Click **⚙ Settings** (top-right, next to your username) to open display settings.
Toggle any **Cadence**, **Platform**, or **Variant** off to hide those cards globally.
Settings are saved per user and persist across sessions.

### Refresh

Click **↺ Refresh** (top-right) to reload all project data without reloading the page.

---

## Feature Matrix tab

Each configured **view** gets its own matrix tab (in the example preset: "Feature Matrix" and "OLS"). A view owns a feature **scope** and a column filter, both editable in Configuration → Advanced → Views — adding a view there adds a tab.

### Reading the table

- **Rows** — features of the view's scope, grouped by category
- **Columns** — one project per column, filtered by the view's column filter
- **Cell colour** — pass rate for that feature on that platform: green ≥ 90 %, yellow ≥ 70 %, orange ≥ 50 %, red < 50 %, grey = no data

### Cell drill-down

Click any coloured percentage badge to open a **test detail modal** for that cell. It shows:

- Feature name, platform, and run number
- `passed / total` summary
- Table of every mapped test with its status, sorted: failed → broken → skipped → passed
- Each test name is a **clickable link** (↗) that opens the test directly in the Allure report

### Column header controls

Each column header shows:

| Control | Action |
|---------|--------|
| **Run dropdown** | Select which report number to display for that column; shows `#run · date · build version · N tests` |
| **Open Allure** link | Opens the currently selected run's Allure report in a new tab |

Changing the run in one column does **not** affect other columns.

### Filtering columns

Use the **Variant** and **Cadence** pills in the toolbar to show only matching columns.
Clicking an active pill resets it to "all".

### Manage Features

Click **⚙ Manage Features** to switch to the **Manage Features** tab with the current view's scope pre-selected.

---

## Manage Features tab

The **Manage Features** tab replaces the old side-panel and provides a full-page editor for feature → test mappings.

### Scope toggle

The buttons at the top show one scope per configured view — use them to switch between feature sets.

### Adding a feature

Fill in **Feature name** and **Group** (e.g. `Layer 3`) in the toolbar form and click **Add**.

### Seed from suites

Click **🌱 Seed from suites** to create one feature per test suite found in the current scope's projects (with all its tests mapped). Useful to bootstrap a new view's feature set; existing features and mappings are preserved.

### Auto-map

Click **✨ Auto-map** to automatically map test cases to features based on the configured suite-name auto-map rules. This is a one-time setup step; mappings are saved and reused.

### Managing mappings

Click any feature row to expand it and see its mapped test cases:

- Type a test name in the input box and press **Add mapping** to link a test
- Click **✕** next to a mapping to remove it
- The autocomplete dropdown suggests test names from scraped results as you type

### Deleting a feature

Click **Delete** (🗑) on a feature row. This removes the feature and all its mappings.

---

## Scraper tab

Shows a table of all projects with per-run coverage:

| Column | Meaning |
|--------|---------|
| **Project** | Project ID |
| **Run #** | Report number |
| **Allure total** | Total tests according to the Allure summary widget |
| **Stored** | Rows in the `test_results` table for that run |
| **Coverage** | Stored ÷ Allure total as a percentage |

Runs with low coverage may have been scraped before the `test_results` feature was added; re-running the scraper will backfill them.

---

## Colour reference

| Colour | Pass rate |
|--------|-----------|
| Green | ≥ 90 % |
| Yellow | ≥ 70 % |
| Orange | ≥ 50 % |
| Red | < 50 % |
| Grey | No data |
