"""Curated IAM action catalog loader and wildcard expander.

Used by Part 3 Component 2 (equivalence checking) and Component 3 (stale
statement detection). The catalog is a JSON file mapping service prefixes
(e.g. ``s3``, ``ec2``) to lists of concrete IAM action strings.

Wildcards in SCP statements (``s3:*``, ``iam:PutRole*``) cannot be passed
directly to ``iam:SimulateCustomPolicy`` — the API requires named actions.
This module expands wildcards against the catalog.
"""
from __future__ import annotations

import fnmatch
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


def _catalog_path() -> Path:
    override = os.environ.get("ZEROSHIFT_FIXTURES_DIR")
    if override:
        return Path(override) / "iam_actions.json"
    packaged = Path("/var/task/fixtures/iam_actions.json")
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[2] / "fixtures" / "iam_actions.json"


@lru_cache(maxsize=1)
def _load_catalog() -> dict[str, list[str]]:
    with _catalog_path().open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, list)}


def all_actions() -> list[str]:
    """Every action string across every service in the catalog."""
    result: list[str] = []
    for actions in _load_catalog().values():
        result.extend(actions)
    return result


def expand_service_wildcard(service: str) -> list[str]:
    """Expand ``<service>:*`` into every catalogued action for the service.

    Returns an empty list if the service is not in the catalog. Callers should
    treat that as a coverage gap and extend the catalog.
    """
    return list(_load_catalog().get(service, []))


def expand_action_pattern(pattern: str) -> list[str]:
    """Expand any action string that may contain glob-style wildcards.

    Examples:
      ``s3:*``           -> all catalogued s3 actions
      ``s3:Get*``        -> catalogued s3 actions matching Get*
      ``*``              -> every catalogued action (all services)
      ``s3:GetObject``   -> that action only, if present in the catalog
    """
    if pattern == "*":
        return all_actions()
    if ":" not in pattern:
        return []
    service, action_part = pattern.split(":", 1)
    if service == "*":
        return all_actions()
    candidates = _load_catalog().get(service, [])
    if action_part == "*":
        return list(candidates)
    return [a for a in candidates if fnmatch.fnmatchcase(a, f"{service}:{action_part}")]


def list_all_actions_for_scp(document: dict[str, Any]) -> list[str]:
    """Return the concrete IAM actions the SCP applies to.

    Handles both ``Action`` (positive list) and ``NotAction`` (complement of
    the list within the services referenced anywhere in the SCP). The result
    is deduplicated and sorted for deterministic simulator ordering.
    """
    positive: set[str] = set()
    negative: set[str] = set()
    services_touched: set[str] = set()

    for stmt in document.get("Statement", []) or []:
        if "Action" in stmt:
            for pattern in _as_list(stmt["Action"]):
                for action in expand_action_pattern(pattern):
                    positive.add(action)
                for service in _services_referenced_by(pattern):
                    services_touched.add(service)
        if "NotAction" in stmt:
            for pattern in _as_list(stmt["NotAction"]):
                for action in expand_action_pattern(pattern):
                    negative.add(action)
                for service in _services_referenced_by(pattern):
                    services_touched.add(service)

    if negative:
        # Complement within the services actually referenced by the SCP.
        universe: set[str] = set()
        for service in services_touched:
            universe.update(_load_catalog().get(service, []))
        positive.update(universe - negative)

    if not positive:
        # SCP with no Action/NotAction at all -> defaults to every action in
        # the catalog. This is uncommon in real SCPs but handled defensively.
        positive = set(all_actions())

    return sorted(positive)


def _as_list(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    return list(v)


def _services_referenced_by(pattern: str) -> list[str]:
    if pattern == "*":
        return list(_load_catalog().keys())
    if ":" not in pattern:
        return []
    service = pattern.split(":", 1)[0]
    if service == "*":
        return list(_load_catalog().keys())
    return [service]
