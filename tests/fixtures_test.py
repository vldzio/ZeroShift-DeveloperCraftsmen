"""Schema validation for the demo fixture data."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _REPO_ROOT / "fixtures"


def _walk_tree(node, visited=None):
    visited = visited or set()
    yield node
    for child in node.get("children", []) or []:
        yield from _walk_tree(child, visited)


def test_org_tree_shape():
    tree = json.loads((_FIXTURES / "scp_org_tree.json").read_text(encoding="utf-8"))
    for node in _walk_tree(tree):
        assert "id" in node
        assert "type" in node
        assert node["type"] in {"ROOT", "ORGANIZATIONAL_UNIT", "ACCOUNT"}
        assert "name" in node
        assert isinstance(node.get("scps", []), list)


def test_every_referenced_scp_has_a_fixture_file():
    tree = json.loads((_FIXTURES / "scp_org_tree.json").read_text(encoding="utf-8"))
    referenced_ids: set[str] = set()
    for node in _walk_tree(tree):
        for policy_id in node.get("scps", []) or []:
            referenced_ids.add(policy_id)

    scp_dir = _FIXTURES / "scp_policies"
    for policy_id in referenced_ids:
        assert (scp_dir / f"{policy_id}.json").exists(), f"missing SCP fixture: {policy_id}"


@pytest.mark.parametrize(
    "scp_file",
    sorted((Path(__file__).resolve().parent.parent / "fixtures" / "scp_policies").glob("*.json")),
    ids=lambda p: p.name,
)
def test_scp_document_shape(scp_file):
    data = json.loads(scp_file.read_text(encoding="utf-8"))
    assert "policyId" in data
    assert "name" in data
    assert "document" in data
    doc = data["document"]
    assert doc["Version"] == "2012-10-17"
    assert isinstance(doc["Statement"], list)
    for stmt in doc["Statement"]:
        assert stmt["Effect"] in {"Allow", "Deny"}


@pytest.mark.parametrize(
    "event_file",
    sorted((Path(__file__).resolve().parent.parent / "fixtures" / "denial_events").glob("*.json")),
    ids=lambda p: p.name,
)
def test_denial_event_shape(event_file):
    event = json.loads(event_file.read_text(encoding="utf-8"))
    assert event.get("errorCode") == "AccessDenied"
    assert "eventSource" in event
    assert "eventName" in event
    assert "awsRegion" in event
    user_identity = event.get("userIdentity")
    assert isinstance(user_identity, dict)
    assert user_identity.get("arn")
    assert user_identity.get("accountId") or event.get("recipientAccountId")
