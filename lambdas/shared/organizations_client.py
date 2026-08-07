"""AWS Organizations client with a fixture-mode fallback.

When ``ZEROSHIFT_FIXTURE_MODE=true`` (default in dev and demo), all Organizations
API calls resolve from ``fixtures/scp_org_tree.json`` and ``fixtures/scp_policies/``.
When false, boto3 calls the real AWS Organizations API.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import boto3

from .logging_config import get_logger

log = get_logger(__name__)


def is_fixture_mode() -> bool:
    return os.environ.get("ZEROSHIFT_FIXTURE_MODE", "true").lower() == "true"


def _fixtures_dir() -> Path:
    override = os.environ.get("ZEROSHIFT_FIXTURES_DIR")
    if override:
        return Path(override)
    # Lambda runtime: fixtures shipped inside the deployment package under /var/task/fixtures
    packaged = Path("/var/task/fixtures")
    if packaged.exists():
        return packaged
    # Local dev: repo root / fixtures
    return Path(__file__).resolve().parents[2] / "fixtures"


@dataclass
class HierarchyNode:
    node_id: str
    node_type: str  # "ROOT" | "ORGANIZATIONAL_UNIT" | "ACCOUNT"
    name: str
    attached_scp_ids: list[str] = field(default_factory=list)


@dataclass
class ScpPolicy:
    policy_id: str
    name: str
    document: dict[str, Any]
    aws_managed: bool = False
    control_tower_managed: bool = False


class OrganizationsClient:
    def __init__(self) -> None:
        self._fixture_mode = is_fixture_mode()
        self._org = None if self._fixture_mode else boto3.client("organizations")

    def get_hierarchy_for_account(self, account_id: str) -> list[HierarchyNode]:
        """Return the ordered path Root -> OU_1 -> ... -> Account for the given account."""
        if self._fixture_mode:
            return self._hierarchy_from_fixtures(account_id)
        return self._hierarchy_from_aws(account_id)

    def get_policy(self, policy_id: str) -> ScpPolicy:
        if self._fixture_mode:
            return self._policy_from_fixture(policy_id)
        return self._policy_from_aws(policy_id)

    # ---- fixture path ----

    @lru_cache(maxsize=1)
    def _load_org_tree(self) -> dict[str, Any]:
        path = _fixtures_dir() / "scp_org_tree.json"
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _hierarchy_from_fixtures(self, account_id: str) -> list[HierarchyNode]:
        tree = self._load_org_tree()
        path = _find_path(tree, account_id)
        if path is None:
            raise ValueError(f"Account {account_id} not found in fixture org tree")
        return [
            HierarchyNode(
                node_id=n["id"],
                node_type=n["type"],
                name=n["name"],
                attached_scp_ids=list(n.get("scps", [])),
            )
            for n in path
        ]

    def _policy_from_fixture(self, policy_id: str) -> ScpPolicy:
        path = _fixtures_dir() / "scp_policies" / f"{policy_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"SCP fixture not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return ScpPolicy(
            policy_id=data["policyId"],
            name=data["name"],
            document=data["document"],
            aws_managed=data.get("awsManaged", False),
            control_tower_managed=data.get("controlTowerManaged", False),
        )

    # ---- real AWS path ----

    def _hierarchy_from_aws(self, account_id: str) -> list[HierarchyNode]:
        assert self._org is not None
        nodes: list[HierarchyNode] = []
        current_id = account_id
        current_type = "ACCOUNT"
        # Walk up from the Account through parents until we reach the Root.
        # AWS Organizations exposes `list_parents` to walk one level at a time.
        while True:
            attached = self._org.list_policies_for_target(
                TargetId=current_id, Filter="SERVICE_CONTROL_POLICY"
            ).get("Policies", [])
            name = self._describe_target_name(current_id, current_type)
            nodes.append(
                HierarchyNode(
                    node_id=current_id,
                    node_type=current_type,
                    name=name,
                    attached_scp_ids=[p["Id"] for p in attached],
                )
            )
            if current_type == "ROOT":
                break
            parents = self._org.list_parents(ChildId=current_id).get("Parents", [])
            if not parents:
                break
            current_id = parents[0]["Id"]
            current_type = parents[0]["Type"]
        return list(reversed(nodes))  # Root -> ... -> Account

    def _describe_target_name(self, target_id: str, target_type: str) -> str:
        assert self._org is not None
        if target_type == "ROOT":
            roots = self._org.list_roots().get("Roots", [])
            for r in roots:
                if r["Id"] == target_id:
                    return r["Name"]
            return target_id
        if target_type == "ORGANIZATIONAL_UNIT":
            return self._org.describe_organizational_unit(
                OrganizationalUnitId=target_id
            )["OrganizationalUnit"]["Name"]
        if target_type == "ACCOUNT":
            return self._org.describe_account(AccountId=target_id)["Account"]["Name"]
        return target_id

    def _policy_from_aws(self, policy_id: str) -> ScpPolicy:
        assert self._org is not None
        resp = self._org.describe_policy(PolicyId=policy_id)["Policy"]
        summary = resp["PolicySummary"]
        return ScpPolicy(
            policy_id=summary["Id"],
            name=summary["Name"],
            document=json.loads(resp["Content"]),
            aws_managed=summary.get("AwsManaged", False),
            control_tower_managed=False,  # detected separately via control_tower_guard
        )


def _find_path(tree: dict[str, Any], target_account_id: str) -> list[dict[str, Any]] | None:
    """Depth-first search for the account and return the ordered path from root."""

    def walk(node: dict[str, Any], path: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        new_path = path + [node]
        if node.get("type") == "ACCOUNT" and node["id"] == target_account_id:
            return new_path
        for child in node.get("children", []) or []:
            result = walk(child, new_path)
            if result is not None:
                return result
        return None

    return walk(tree, [])
