"""Seed the ``zeroshift-intent-registry`` DynamoDB table with fixture entries.

Reads ``fixtures/intent_registry_seed.json`` and writes each entry. The
ACCOUNT placeholder in role ARNs is rewritten to the current account ID
(the same substitution the fixture-mode iam_role_client applies).

Usage:
    python scripts/seed_intent_registry.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import boto3

TABLE_NAME = "zeroshift-intent-registry"
FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "intent_registry_seed.json"


def main() -> int:
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    entries = payload.get("entries", [])
    if not entries:
        print("No entries in fixture file.")
        return 0

    ddb = boto3.resource("dynamodb").Table(TABLE_NAME)
    written = 0
    for entry in entries:
        item = {k: v for k, v in entry.items() if v is not None}
        if isinstance(item.get("roleArn"), str):
            item["roleArn"] = item["roleArn"].replace("ACCOUNT", account_id)
        ddb.put_item(Item=item)
        written += 1
        print(f"  wrote {item['roleArn']} :: {item['permission']}")

    print(f"Seeded {written} intent-registry entries into {TABLE_NAME}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
