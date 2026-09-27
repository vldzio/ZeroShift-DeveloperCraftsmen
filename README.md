# ZeroShift — agentic IAM and SCP governance on AWS

**A team-built AWS Agentic AI Hackathon project exploring how AI agents can propose, inspect, and verify changes to IAM policies and AWS Organizations service control policies.** ZeroShift combines Python AWS Lambdas, Step Functions, CDK infrastructure, Amazon Bedrock, and two LangGraph agents with a five-tab browser interface. The repository includes local fixture data so its workflows can be explored without a live AWS Organization.

> **Project context:** Developer Craftsmen team project. The repository currently labels its license “Internal — Developer Craftsmen team.” Confirm the team's permission before redistributing or featuring its code as an individual portfolio project, and retain team attribution.

## What the system covers

| Area | Implemented approach | Where to look |
| --- | --- | --- |
| Code-to-permission intent | Analyze Python AWS SDK calls and maintain an intent registry | `lambdas/part1/` |
| IAM drift | Compare role permissions with usage/intent, propose scoped-down policies, score risk, and route remediation | `lambdas/part2/` |
| SCP denial analysis | Walk a policy hierarchy, simulate policies, and explain an access-denied result | `lambdas/part3/denial_analyzer/` |
| SCP refactoring | Propose a more compact SCP, check action behavior, and route a change request or agentic apply | `lambdas/part3/refactorer/` |
| Stale SCP review | Look for Deny statements without observed usage in a configured window | `lambdas/part3/stale_detector/` |

The interface is in `frontend/`; infrastructure for shared services and the three product parts lives in `infrastructure/stacks/`. `fixtures/` supplies sample policies, events, and organization data.

## Agent design

Two stateful LangGraph workflows live in `lambdas/agents/`:

- **IAM remediation** (`iam_remediator/graph.py`): assesses a proposed policy, optionally simulates removals, applies a policy version, checks the result, and has a rollback path. The Bedrock-backed model participates in assessment and verification decisions.
- **SCP governance** (`scp_governance/graph.py`): checks whether the SCP is Control Tower–managed, snapshots the current document, spot-checks proposed policy behavior, counts attached targets, asks the model to deliberate, then conditionally applies, verifies, or rolls back the change.

Shared AWS-facing tools, the Bedrock client, and decision logging are in `lambdas/agents/common/`. These are orchestration and tool-use graphs, not a trained or fine-tuned LLM.

```text
Frontend → API Gateway → Lambda / Step Functions
                              ├─ intent + drift workflows
                              ├─ denial / refactoring workflows
                              └─ LangGraph agents → Bedrock + AWS APIs
                         DynamoDB / fixtures / CloudTrail-related inputs
```

## Explore locally

Prerequisites: Python 3.12, `pip`, and—only for CDK deployment—the AWS CLI, credentials, Node.js, and the AWS CDK CLI.

```bash
python -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
make install-dev
make install
make test                            # pytest suite; AWS services mocked/fixture-backed where configured
make seed-fixtures                   # list bundled fixture JSON files
make serve-frontend                  # static UI at http://localhost:8000
```

The frontend's API-backed flows need a deployed API endpoint; serving static files alone does **not** start an API or provision AWS resources. Consult `Makefile` and `infrastructure/` before deploying. Deployment can incur AWS charges and requires permissions and Bedrock model access:

```bash
npm install -g aws-cdk
make bootstrap
make deploy                         # packages Lambdas and agent layer; deploys four stacks
```

Use `make diff`/`make synth` to inspect infrastructure first. After deployment, supply the API Gateway endpoint from the CDK output in the UI. **Do not run mutation paths against production IAM or SCPs without a separate security review and explicit human approval.**

## Repository map

```text
frontend/             Browser UI (HTML, CSS, JavaScript)
infrastructure/       Python AWS CDK app and stacks
lambdas/              Product Lambdas and LangGraph agents
fixtures/             Demonstration inputs and sample policies
scripts/              Packaging, invocation, and seed helpers
tests/                Unit and integration-oriented tests
requirements-dev.txt  Test and lint dependencies
Makefile              Local and deployment tasks
```

## Maturity and guardrails

ZeroShift is a substantial **hackathon prototype**, not a verified production security control. The CDK stacks set fixture mode as the default for several Lambdas. The code includes real AWS API integrations and mutation calls, but this README does not imply that an end-to-end live-organization deployment has been independently verified.

Before enabling unattended writes, make safety conditions deterministic at the mutation boundary: the IAM graph currently allows an LLM `APPLY` decision to set `simulator_ok=True` without running simulations; the SCP graph relies on its prompt to enforce the spot-check and blast-radius conditions, and falls back to `target_count=0` if listing targets fails. Add hard validation, fail-closed error handling, human approval for sensitive changes, and live-environment tests. Local tests and CDK deployment were **not** executed as part of this README audit.

## Attribution

Developer Craftsmen team · AWS Agentic AI Hackathon (2026). Internal/team licensing notice is present in the original repository; ask the team before changing its license, publishing copies, or claiming sole authorship.
