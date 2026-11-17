#!/bin/sh
# Starts the private gateway, then the public demo API. Runs as the unprivileged `node` user.
set -e

# Gateway: localhost only. Provider keys / admin account come from FREEAPI_CONFIG_JSON (Space secret).
( cd /app && HOST=127.0.0.1 PORT=3001 exec node server/dist/index.js ) &

# Demo API: public port 7860 (HF Spaces default).
cd /srv/app
exec /opt/venv/bin/python -m uvicorn demo_api.app:app --host 0.0.0.0 --port 7860 --proxy-headers --forwarded-allow-ips='*'
