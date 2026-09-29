FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies first (layer-cached until requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application source
COPY app.py config_store.py scraper.py new_fails_db.py report_generator.py parse_allure_results.py get_known_issues_from_allure_report.py mcp_server.py jira_mcp_server.py ./
COPY static/ static/
COPY templates/ templates/

# Persistent data lives on a volume mounted at /data
RUN mkdir -p /data
VOLUME ["/data"]

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 8080
# MCP HTTP/SSE: test dashboard (8001), Jira search (8002)
EXPOSE 8001
EXPOSE 8002

# Override in docker run / compose env to point elsewhere
ENV DB_PATH=/data/allure_data.db
ENV MCP_PORT=8001
ENV JIRA_MCP_PORT=8002

ENTRYPOINT ["./entrypoint.sh"]
