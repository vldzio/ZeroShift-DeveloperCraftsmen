"""Bridge to the private FreeLLMAPI gateway running in the same container.

The gateway generates its unified API key into its own SQLite database on first
boot, so after any restart the key is different. The gateway only listens on
127.0.0.1 inside the container; this module waits for it, reads the current key
straight from the (local) database and exports ``LLM_BASE_URL`` / ``LLM_API_KEY``
for ``shared.llm_client``. Nothing here is reachable from the internet.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("demo_api.gateway")

GATEWAY_PORT = int(os.environ.get("FREELLMAPI_PORT", "3001"))
DB_PATH = Path(os.environ.get("FREEAPI_DB_PATH") or "/app/server/data/freeapi.db")


def _read_key() -> str | None:
    if not DB_PATH.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute("SELECT value FROM settings WHERE key = 'unified_api_key'").fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _ping() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{GATEWAY_PORT}/api/ping", timeout=2) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def connect(wait_seconds: float = 90.0) -> bool:
    """Wait for the local gateway, then export the LLM_* env vars. Returns True on success."""
    if os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_API_KEY"):
        return True  # explicitly configured (e.g. local development against another endpoint)
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        key = _read_key()
        if key and _ping():
            os.environ["LLM_BASE_URL"] = f"http://127.0.0.1:{GATEWAY_PORT}/v1"
            os.environ["LLM_API_KEY"] = key
            log.info("gateway connected")
            return True
        time.sleep(1.5)
    log.error("gateway did not become ready within %ss", wait_seconds)
    return False
