"""CloudTrail Lake activity-count wrapper with fixture-mode fallback.

Used by Part 3 Component 3 (Stale SCP Detection) and — when Part 2 lands —
by the drift-remediation loop that needs to confirm an action has been
attempted recently before removing its permission.

Fixture mode reads pre-populated activity counts from
``fixtures/cloudtrail_lake_responses/counts.json``. Real mode issues
``cloudtrail:StartQuery`` against a pre-provisioned event data store whose
ARN is supplied via ``CLOUDTRAIL_EVENT_DATA_STORE_ARN``.
"""
from __future__ import annotations

import json
import os
import time
from functools import lru_cache
from pathlib import Path

import boto3

from .logging_config import get_logger

log = get_logger(__name__)

EVENT_DATA_STORE_ENV = "CLOUDTRAIL_EVENT_DATA_STORE_ARN"


def _fixture_path() -> Path:
    override = os.environ.get("ZEROSHIFT_FIXTURES_DIR")
    if override:
        return Path(override) / "cloudtrail_lake_responses" / "counts.json"
    packaged = Path("/var/task/fixtures/cloudtrail_lake_responses/counts.json")
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[2] / "fixtures" / "cloudtrail_lake_responses" / "counts.json"


def _is_fixture_mode() -> bool:
    return os.environ.get("ZEROSHIFT_FIXTURE_MODE", "true").lower() == "true"


@lru_cache(maxsize=1)
def _load_fixture_counts() -> dict[str, int]:
    with _fixture_path().open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("counts", {})


def count_activity(actions: list[str], *, lookback_days: int = 180) -> dict[str, int]:
    """Return ``{action: count}`` for each action over the lookback window.

    Fixture mode returns pre-populated counts; unknown actions default to 0.
    Real mode issues one CloudTrail Lake query for the whole batch.
    """
    if not actions:
        return {}
    if _is_fixture_mode():
        table = _load_fixture_counts()
        return {a: int(table.get(a, 0)) for a in actions}
    return _query_cloudtrail_lake(actions=actions, lookback_days=lookback_days)


def _query_cloudtrail_lake(*, actions: list[str], lookback_days: int) -> dict[str, int]:
    event_data_store = os.environ.get(EVENT_DATA_STORE_ENV)
    if not event_data_store:
        raise RuntimeError(
            f"{EVENT_DATA_STORE_ENV} must be set for real-mode CloudTrail Lake queries"
        )
    client = boto3.client("cloudtrail")
    quoted_actions = ",".join(f"'{a}'" for a in actions)
    # eventName in Lake is the API method name without the service prefix, and
    # eventSource is the '<service>.amazonaws.com' form. The tuple form below
    # matches both in a single query.
    action_tuples = ", ".join(
        f"('{a.split(':', 1)[0]}.amazonaws.com', '{a.split(':', 1)[1]}')"
        for a in actions if ":" in a
    )
    query_statement = (
        f"SELECT eventSource, eventName, count(*) as cnt "
        f"FROM {event_data_store} "
        f"WHERE eventTime > timestamp '{_since_iso(lookback_days)}' "
        f"AND (eventSource, eventName) IN ({action_tuples or quoted_actions}) "
        f"GROUP BY eventSource, eventName"
    )
    log.info("cloudtrail_lake_query_start", extra={"actionCount": len(actions), "lookbackDays": lookback_days})
    resp = client.start_query(QueryStatement=query_statement)
    query_id = resp["QueryId"]

    deadline = time.time() + 60
    while time.time() < deadline:
        status = client.describe_query(QueryId=query_id)
        state = status.get("QueryStatus")
        if state in ("FINISHED", "CANCELLED", "FAILED", "TIMED_OUT"):
            break
        time.sleep(1.5)
    if state != "FINISHED":
        raise RuntimeError(f"CloudTrail Lake query {query_id} ended in state {state}")

    results = client.get_query_results(QueryId=query_id, MaxQueryResults=1000)
    counts: dict[str, int] = {a: 0 for a in actions}
    for row in results.get("QueryResultRows", []) or []:
        row_map = {list(cell.keys())[0]: list(cell.values())[0] for cell in row}
        source = row_map.get("eventSource", "")
        name = row_map.get("eventName", "")
        cnt = int(row_map.get("cnt", 0))
        service = source.replace(".amazonaws.com", "")
        action = f"{service}:{name}"
        if action in counts:
            counts[action] = cnt
    return counts


def _since_iso(lookback_days: int) -> str:
    # Time math left deliberately simple — CloudTrail Lake expects UTC ISO.
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
