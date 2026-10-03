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

# Internal Docker network only; Compose never publishes the API port.
uvicorn app.main:app --host 0.0.0.0 --port 8000 &
python - <<'PY'
import os
import re
from pathlib import Path
target = os.environ.get("PRISMA_VIEWER_URL", "http://127.0.0.1:8081")
if not re.fullmatch(r"http://[A-Za-z0-9.-]+:[0-9]{1,5}", target):
    raise SystemExit("Invalid PRISMA viewer upstream")
rendered = Path("/etc/nginx/nginx.conf").read_text().replace("http://127.0.0.1:8081", target)
Path("/run/nginx.conf").write_text(rendered)
PY
exec nginx -c /run/nginx.conf -g 'daemon off;'
