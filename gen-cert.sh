#!/bin/sh
set -e

# Generate a self-signed TLS certificate for the dashboard web UI.
# Reuses an existing cert/key pair if both are already present.
#
# Override the hostname/IP baked into the cert via CERT_CN / CERT_SAN, e.g.:
#   CERT_CN=dashboard.example.com \
#   CERT_SAN="DNS:dashboard.example.com,IP:10.0.0.5" ./gen-cert.sh

CERT_DIR="${CERT_DIR:-$(pwd)/certs}"
CERT_FILE="$CERT_DIR/cert.pem"
KEY_FILE="$CERT_DIR/key.pem"
CERT_CN="${CERT_CN:-localhost}"
CERT_SAN="${CERT_SAN:-DNS:localhost,IP:127.0.0.1}"

if [ -f "$CERT_FILE" ] && [ -f "$KEY_FILE" ]; then
  echo "[gen-cert] Reusing existing certificate in $CERT_DIR."
  exit 0
fi

echo "[gen-cert] Generating self-signed certificate in $CERT_DIR..."
mkdir -p "$CERT_DIR"
openssl req -x509 -newkey rsa:4096 -nodes \
  -keyout "$KEY_FILE" -out "$CERT_FILE" \
  -days 365 -subj "/CN=$CERT_CN" \
  -addext "subjectAltName=$CERT_SAN"
echo "[gen-cert] Done."
