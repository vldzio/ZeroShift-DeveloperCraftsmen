from part3.denial_analyzer.scp_traverser import traverse
from shared.organizations_client import OrganizationsClient


def test_traverse_production_account_path():
    scps = traverse("111111111111", OrganizationsClient())
    names = [s.policy.name for s in scps]

    # Ordered Root -> Production OU -> Account
    assert "DenyExpensiveInstances" in names
    assert "aws-guardrails-DenyRootUser" in names
    assert "DenyNonApprovedRegions" in names
    assert "DenyIAMWildcards" in names

    # Root SCPs come before OU SCPs before Account SCPs.
    root_index = names.index("DenyExpensiveInstances")
    ou_index = names.index("DenyNonApprovedRegions")
    account_index = names.index("DenyIAMWildcards")
    assert root_index < ou_index < account_index


def test_traverse_flags_control_tower_managed():
    scps = traverse("111111111111", OrganizationsClient())
    ct_flagged = [s for s in scps if s.control_tower_managed]
    assert any(s.policy.name.startswith("aws-guardrails-") for s in ct_flagged)


def test_traverse_sandbox_account_has_no_scps():
    scps = traverse("333333333333", OrganizationsClient())
    # Sandbox OU has no SCPs of its own, but Root SCPs still apply.
    names = [s.policy.name for s in scps]
    assert "DenyNonApprovedRegions" not in names  # not in Sandbox OU
    assert "DenyExpensiveInstances" in names  # root
