#!/bin/sh
set -e

# One-shot scrape on startup so the dashboard has data immediately.
# Failure is non-fatal (Allure server may not be reachable yet).
echo "[entrypoint] Running initial scrape..."
python scraper.py || echo "[entrypoint] Initial scrape failed — dashboard will show data once the scraper succeeds."

# Background watch loop (uses SCRAPE_INTERVAL, default 300 s)
echo "[entrypoint] Starting background scraper (watch mode)..."
python scraper.py --watch &

# MCP HTTP/SSE — test dashboard (background)
echo "[entrypoint] Starting test-dashboard MCP on :${MCP_PORT:-8001}..."
python mcp_server.py --http --port "${MCP_PORT:-8001}" &

# MCP HTTP/SSE — Jira search (background; uses JIRA_* from env)
echo "[entrypoint] Starting Jira MCP on :${JIRA_MCP_PORT:-8002}..."
python jira_mcp_server.py --http --port "${JIRA_MCP_PORT:-8002}" &

# Web server — PID 1
if [ -n "${SSL_CERTFILE:-}" ] && [ -n "${SSL_KEYFILE:-}" ]; then
  echo "[entrypoint] Starting web server (HTTPS) on :8080..."
  exec uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1 \
    --ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE"
else
  echo "[entrypoint] Starting web server (HTTP) on :8080..."
  exec uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1
fi
