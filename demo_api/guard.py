"""Abuse protection for the public demo.

* Per-IP daily credit budget and a global daily cap, stored in Upstash Redis
  (REST) so counters survive restarts and are shared across instances.
  Falls back to in-process counters when Upstash is not configured (local dev).
* Bring-your-own-key: a visitor may send ``X-LLM-Api-Key`` (and optionally
  ``X-LLM-Base-Url`` from an allow-list). Such requests spend the visitor's own
  quota, so they bypass the shared limits. The key is only held in memory for
  the duration of the request and is never logged.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

PROJECT = os.environ.get("DEMO_PROJECT", "zeroshift")
PER_IP_DAILY = int(os.environ.get("RL_PER_IP_DAILY", "20"))
GLOBAL_DAILY = int(os.environ.get("RL_GLOBAL_DAILY", "1000"))
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

ALLOWED_BYO_HOSTS = {
    "api.openai.com",
    "api.groq.com",
    "openrouter.ai",
    "api.mistral.ai",
    "api.cerebras.ai",
    "generativelanguage.googleapis.com",
    *[h.strip() for h in os.environ.get("LLM_BYO_ALLOWED_HOSTS", "").split(",") if h.strip()],
}
DEFAULT_BYO_BASE_URL = "https://api.openai.com/v1"

_mem: dict[str, tuple[int, float]] = {}


@dataclass
class Decision:
    allowed: bool
    message: str = ""
    remaining: int | None = None
    byo: bool = False


def _day() -> str:
    return time.strftime("%Y%m%d", time.gmtime())


def _seconds_to_midnight_utc() -> int:
    return int(86400 - (time.time() % 86400)) + 1


def _incr(key: str, amount: int, ttl: int) -> int:
    if UPSTASH_URL and UPSTASH_TOKEN:
        r = httpx.post(
            f"{UPSTASH_URL}/pipeline",
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
            json=[["INCRBY", key, amount], ["EXPIRE", key, ttl]],
            timeout=5,
        )
        r.raise_for_status()
        return int(r.json()[0]["result"])
    count, exp = _mem.get(key, (0, time.time() + ttl))
    if time.time() > exp:
        count, exp = 0, time.time() + ttl
    count += amount
    _mem[key] = (count, exp)
    return count


def validate_byo(api_key: str | None, base_url: str | None) -> tuple[str, str] | None:
    """Return (key, base_url) for a usable BYO key; raise ValueError if the URL is not allowed."""
    if not api_key:
        return None
    url = (base_url or DEFAULT_BYO_BASE_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "") not in ALLOWED_BYO_HOSTS:
        raise ValueError("Base URL not allowed. Use one of: " + ", ".join(sorted(ALLOWED_BYO_HOSTS)))
    return api_key.strip(), url


def check(ip: str, cost: int, byo: bool) -> Decision:
    if byo or cost <= 0:
        return Decision(True, byo=byo)
    try:
        ttl = _seconds_to_midnight_utc()
        ip_used = _incr(f"rl:{PROJECT}:ip:{_day()}:{ip}", cost, ttl)
        if ip_used > PER_IP_DAILY:
            return Decision(
                False,
                f"Daily free-demo limit reached ({PER_IP_DAILY} credits per visitor). "
                "Add your own OpenAI-compatible API key in the header to keep going, or come back tomorrow (UTC).",
                0,
            )
        global_used = _incr(f"rl:{PROJECT}:global:{_day()}", cost, ttl)
        if global_used > GLOBAL_DAILY:
            return Decision(
                False,
                "The shared free LLM quota for today is used up. Add your own API key to continue, or try again after 00:00 UTC.",
                0,
            )
        return Decision(True, remaining=max(PER_IP_DAILY - ip_used, 0))
    except Exception as exc:  # fail closed: a broken limiter must never burn the shared quota
        return Decision(False, f"Rate limiter unavailable ({type(exc).__name__}); try again shortly.")
