"""Detects SCPs managed by AWS Control Tower.

Control Tower attaches SCPs whose names follow well-known patterns. ZeroShift
never proposes modifications to these — doing so would cause CT drift and
potentially break the landing zone. The guard is consulted before any SCP
mutation (currently only relevant for Part 3 Component 2, but the check is
established now so the invariant is single-sourced).
"""
from __future__ import annotations

import re

# Control Tower–managed SCP name patterns. AWS uses these prefixes for both
# preventive guardrails and the mandatory FullAWSAccess baseline.
_CT_NAME_PATTERNS = (
    re.compile(r"^aws-guardrails-"),
    re.compile(r"^AWSControlTower"),
)


def is_ct_managed(policy_name: str, policy_id: str = "") -> bool:
    """True if the SCP is owned by AWS Control Tower and must not be modified."""
    for pattern in _CT_NAME_PATTERNS:
        if pattern.match(policy_name):
            return True
    return False
