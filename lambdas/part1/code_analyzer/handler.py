"""Part 1 code analyzer handler.

Routes:
- POST /analyze-code — {"code": "<python source>", "targetRoleArn": "...",
  "commitHash": "abc123", "apply": false} → static extract + LLM augment +
  intent-registry diff (+ optional apply).
- POST /analyze-github — {"repoUrl": "https://github.com/.../.../blob/main/x.py",
  "targetRoleArn": "..."} → fetches the file via the GitHub raw URL and runs
  the same analysis. Public repos only; no PAT support at v1.

Both routes return the same response shape so the frontend can consume either.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from part1.code_analyzer import llm_augmenter
from part1.code_analyzer.intent_writer import apply_diff, diff
from part1.code_analyzer.static_extractor import extract_full
from shared.logging_config import get_logger, new_correlation_id

log = get_logger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    correlation_id = new_correlation_id()
    path = event.get("rawPath") or event.get("path") or ""
    body = _extract_body(event)

    if path.endswith("/analyze-github"):
        code = _fetch_github(body.get("repoUrl") or "")
    else:
        code = body.get("code") or ""

    target_role = body.get("targetRoleArn")
    commit_hash = body.get("commitHash")
    apply = bool(body.get("apply"))
    grace_days = int(body.get("graceDays") or 7)

    if not code:
        return _response(400, {"error": "code (or repoUrl for /analyze-github) is required"})
    if not target_role:
        return _response(400, {"error": "targetRoleArn is required"})

    log.info(
        "analyze_code_start",
        extra={"targetRole": target_role, "codeLen": len(code), "apply": apply},
    )

    # Static AST pass.
    calls = extract_full(code)
    static_actions = sorted({c.iam_action for c in calls})

    # LLM augmentation pass.
    aug = llm_augmenter.augment(source=code, static_actions=static_actions)
    additional = [a for a in aug.get("additional_actions", []) or [] if a not in static_actions]

    all_actions = sorted(set(static_actions) | set(additional))

    # Diff against the intent registry.
    delta = diff(role_arn=target_role, inferred_actions=all_actions)

    apply_result = None
    if apply:
        apply_result = apply_diff(
            role_arn=target_role,
            delta=delta,
            commit_hash=commit_hash,
            grace_days=grace_days,
        )

    return _response(
        200,
        {
            "correlationId": correlation_id,
            "targetRoleArn": target_role,
            "commitHash": commit_hash,
            "staticExtraction": {
                "actions": static_actions,
                "callDetails": [
                    {
                        "iamAction": c.iam_action,
                        "service": c.service,
                        "method": c.method,
                        "line": c.line,
                        "viaResource": c.via_resource,
                    }
                    for c in calls
                ],
            },
            "llmAugmentation": {
                "additionalActions": additional,
                "confidence": aug.get("confidence", "low"),
                "notes": aug.get("notes", ""),
            },
            "delta": delta,
            "applyResult": apply_result,
        },
    )


def _fetch_github(url: str) -> str:
    """Fetch a single Python file from a public GitHub URL via the raw endpoint.

    Accepts either a `blob` URL or a raw URL. No auth — public repos only.
    """
    if not url:
        return ""
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    req = urllib.request.Request(
        url, headers={"User-Agent": "ZeroShift-Part1/1.0", "Accept": "text/plain"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_body(event: dict[str, Any]) -> dict[str, Any]:
    if "body" in event and isinstance(event["body"], str):
        try:
            return json.loads(event["body"])
        except json.JSONDecodeError:
            return {}
    return event


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }
