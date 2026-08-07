"""Deterministic LOW / MEDIUM / HIGH risk scorer for IAM drift proposals.

Risk tier drives the Step Functions Choice state:
- LOW auto-applies (calls iam:CreatePolicyVersion + SetDefaultPolicyVersion for
  real roles; no-op for fixture roles)
- MEDIUM waits out a grace period before applying (default 48h;
  GRACE_PERIOD_SECONDS env var lets demos shrink it to 60s)
- HIGH stops and writes an approval-required audit record

Rules deliberately live outside the LLM path so a reviewer can audit exactly
what triggers each tier.
"""
from __future__ import annotations

from typing import Iterable

ADMIN_ADJACENT_PREFIXES = ("iam:", "organizations:", "sts:AssumeRole")
SENSITIVE_ACTIONS = frozenset(
    {
        "iam:AssumeRolePolicyUpdate",
        "iam:CreateAccessKey",
        "iam:UpdateAccessKey",
        "iam:PutRolePolicy",
        "iam:AttachRolePolicy",
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePermissionsBoundary",
        "iam:DeleteRolePermissionsBoundary",
        "iam:CreateLoginProfile",
    }
)

ENV_PROD = "prod"
ENV_PROD_ADJACENT = "prod-adjacent"
ENV_NON_PROD = "non-prod"


def score(
    *,
    environment: str | None,
    removed_actions: Iterable[str],
    is_wildcard_scope_reduction: bool = False,
) -> str:
    """Return the risk tier for this proposal.

    HIGH beats MEDIUM beats LOW when multiple signals fire.
    """
    removed = list(removed_actions or [])
    env = (environment or "").lower()

    # HIGH conditions
    if env == ENV_PROD:
        return "HIGH"
    if not env or env not in {ENV_PROD, ENV_PROD_ADJACENT, ENV_NON_PROD}:
        # Missing or unknown environment tag => conservative HIGH.
        return "HIGH"
    if any(a in SENSITIVE_ACTIONS for a in removed):
        return "HIGH"

    # MEDIUM conditions
    if env == ENV_PROD_ADJACENT:
        return "MEDIUM"
    if is_wildcard_scope_reduction:
        return "MEDIUM"
    if any(a.startswith(prefix) for a in removed for prefix in ADMIN_ADJACENT_PREFIXES):
        return "MEDIUM"

    # Otherwise LOW.
    return "LOW"
