"""Invoke the deployed IAM Drift Detector Lambda directly.

Usage:
    python scripts/invoke_drift_scan.py [lookback_days]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import boto3

FUNCTION_NAME = "zeroshift-part2-drift-detector"


def main(lookback_days: int = 90) -> int:
    build = Path("build")
    build.mkdir(exist_ok=True)
    payload = json.dumps({"lookbackDays": lookback_days})
    print(f"Invoking {FUNCTION_NAME} with lookbackDays={lookback_days}")
    resp = boto3.client("lambda").invoke(
        FunctionName=FUNCTION_NAME,
        Payload=payload.encode("utf-8"),
    )
    body = resp["Payload"].read().decode("utf-8")
    (build / "drift_response.json").write_text(body, encoding="utf-8")
    try:
        outer = json.loads(body)
    except json.JSONDecodeError:
        print(body)
        return 1
    inner = outer.get("body")
    if isinstance(inner, str):
        try:
            inner = json.loads(inner)
        except json.JSONDecodeError:
            pass
    print(json.dumps(inner or outer, indent=2))
    return 0 if resp.get("StatusCode", 500) == 200 else 1


if __name__ == "__main__":
    days = 90
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print(f"Invalid lookback_days: {sys.argv[1]}", file=sys.stderr)
            sys.exit(2)
    sys.exit(main(days))
