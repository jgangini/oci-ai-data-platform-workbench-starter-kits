#!/bin/sh
set -eu

TLS_DIR=/etc/aidp-lab/tls
mkdir -p "$TLS_DIR" /var/lib/aidp-lab
if [ ! -s "$TLS_DIR/tls.crt" ] || [ ! -s "$TLS_DIR/tls.key" ]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 30 \
    -subj "/CN=localhost" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    -keyout "$TLS_DIR/tls.key" -out "$TLS_DIR/tls.crt"
  chmod 0600 "$TLS_DIR/tls.key"
else
  chmod 0600 "$TLS_DIR/tls.key" 2>/dev/null || true
fi

uvicorn app.main:app --host 127.0.0.1 --port 8000 &
exec nginx -c /etc/nginx/nginx.conf -g 'daemon off;'
