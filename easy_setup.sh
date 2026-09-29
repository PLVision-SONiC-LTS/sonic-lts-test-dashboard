#!/bin/sh
set -e

IMAGE=test-dashboard:latest
CONTAINER=test-dashboard
VOLUME=dashboard-data
CERT_DIR="$(pwd)/certs"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"

usage() {
  echo "Usage: $0 [deploy|clean]"
  echo "  deploy  Build the image and start the dashboard (HTTPS) [default]"
  echo "  clean   Stop/remove the container, image, build cache, and drop the DB volume"
  echo "  -h, --help  Show this help"
  exit 0
}

cmd_deploy() {

  # Generate a self-signed cert on first run so the UI can be served over HTTPS.
  CERT_DIR="$CERT_DIR" sh "$SCRIPT_DIR/gen-cert.sh"

  echo "[deploy] Building image..."
  docker build -t "$IMAGE" .
  
  echo "[deploy] Removing existing container (if any)..."
  docker rm -f "$CONTAINER" 2>/dev/null || true

  echo "[deploy] Starting container..."
  docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    --network host \
    -v "${VOLUME}:/data" \
    -v "$(pwd)/admins.json:/app/admins.json:ro" \
    -v "$CERT_DIR:/app/certs:ro" \
    --env-file .env \
    -e DB_PATH=/data/allure_data.db \
    -e SSL_CERTFILE=/app/certs/cert.pem \
    -e SSL_KEYFILE=/app/certs/key.pem \
    "$IMAGE"

  echo "[deploy] Container started. Dashboard at https://localhost:8080"
  echo "[deploy] Logs: docker logs -f $CONTAINER"
}

cmd_clean() {
  printf "[clean] This will remove the container, image, and DB volume '%s'. Continue? [y/N] " "$VOLUME"
  read -r answer
  case "$answer" in
    y|Y) ;;
    *)
      echo "[clean] Aborted."
      exit 1
      ;;
  esac

  echo "[clean] Stopping and removing container..."
  docker stop "$CONTAINER" 2>/dev/null || true
  docker rm -f "$CONTAINER" 2>/dev/null || true

  echo "[clean] Removing image..."
  docker rmi "$IMAGE" 2>/dev/null || true

  # echo "[clean] Pruning build cache..."
  # docker builder prune -f

  echo "[clean] Removing DB volume..."
  docker volume rm "$VOLUME" 2>/dev/null || true

  echo "[clean] Done."
}

case "${1:-deploy}" in
  deploy)       cmd_deploy ;;
  clean)        cmd_clean ;;
  -h|--help)    usage ;;
  *)
    echo "Unknown command: $1" >&2
    echo "Try '$0 --help' for usage." >&2
    exit 1
    ;;
esac
