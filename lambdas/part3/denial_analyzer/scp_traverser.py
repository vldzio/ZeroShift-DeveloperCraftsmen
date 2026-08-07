"""Builds the ordered list of SCPs attached along the Root -> OU -> Account path."""
from __future__ import annotations

from dataclasses import dataclass

from shared.control_tower_guard import is_ct_managed
from shared.organizations_client import (
    HierarchyNode,
    OrganizationsClient,
    ScpPolicy,
)


@dataclass
class ScpAlongPath:
    policy: ScpPolicy
    attached_at_node: HierarchyNode
    control_tower_managed: bool


def traverse(account_id: str, org_client: OrganizationsClient) -> list[ScpAlongPath]:
    """Return every SCP attached anywhere along the Root->Account path,
    ordered from Root (index 0) down to the Account (last)."""
    hierarchy = org_client.get_hierarchy_for_account(account_id)
    result: list[ScpAlongPath] = []
    for node in hierarchy:
        for policy_id in node.attached_scp_ids:
            policy = org_client.get_policy(policy_id)
            ct = policy.control_tower_managed or is_ct_managed(policy.name, policy.policy_id)
            result.append(
                ScpAlongPath(
                    policy=policy,
                    attached_at_node=node,
                    control_tower_managed=ct,
                )
            )
    return result
