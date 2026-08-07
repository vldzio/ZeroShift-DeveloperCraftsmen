# ZeroShift

**Autonomous IAM and SCP Lifecycle Platform for AWS — an agentic AI application built on real Amazon Web Services.**

ZeroShift is a dual-purpose platform. It manages the full lifecycle of both **AWS IAM identity policies** (what individual roles are permitted to do) and **AWS Organizations Service Control Policies** (what accounts, OUs, and the whole organization are permitted to do). AWS's native tooling detects problems in both areas but leaves remediation entirely manual. ZeroShift closes that loop for both — detects, reasons about, and (where safe) autonomously fixes both IAM drift and SCP problems — using **real LangGraph agents driven by Kimi K2.5 on Amazon Bedrock**.

The agents pick their own tool sequences, verify their own work, and roll back their own mistakes.

---

## Why this matters — two real problems, one platform

Over-permissioning is the #1 root cause in published AWS incident post-mortems, and it comes from two directions:

**IAM drift (per-role).** Developers over-permission their application roles at deploy time and never clean them up. `PowerUserAccess` gets attached "just for now." A code change removes an S3 call but the permission stays. Six months later that role can do things nobody remembers granting it — and no tool tells you which permissions the role no longer needs.

**SCP problems (org-wide).** Service Control Policies bloat toward AWS's hard 10,240-character limit as security teams add guardrails over the years. Access-denied errors caused by SCPs don't tell developers which of a dozen SCPs fired. Deny statements added in response to a one-time incident linger forever, blocking things nobody was going to do anyway.

ZeroShift automates the full lifecycle for both:

| Stage | Traditional workflow | ZeroShift |
|---|---|---|
| **Detect IAM drift** | Manual quarterly audit | Weekly CloudTrail Lake scan (Tuesday 04:00 UTC) |
| **Propose scoped-down IAM policy** | Human writes it by hand | Kimi K2.5 on Bedrock generates the minimal replacement |
| **Apply the IAM change** | Weeks-long change management | LangGraph agent invokes `iam:CreatePolicyVersion` + verifies + can auto-rollback |
| **Sync IAM to code changes** | Ticket → security engineer → weeks | Python AST + Kimi extract SDK calls on every commit; intent registry updates in real time |
| **Diagnose SCP denial** | Grep 15 SCPs by hand | Walk Root → OU → Account, simulate each with IAM Policy Simulator, Kimi explains the offender |
| **Refactor a bloated SCP** | Manual JSON surgery | Kimi compresses, Policy Simulator verifies equivalence across ~500 actions, LangGraph agent applies via `organizations:UpdatePolicy` |
| **Detect stale SCP statements** | Never happens in practice | Weekly CloudTrail Lake query flags Deny statements with zero activity in 180 days |
| **Verify safety of any change** | Manual eyeballing | AWS IAM Policy Simulator checks every action against both original and proposed policies |

Every LLM decision is surrounded by deterministic verifiers. IAM Permissions Boundaries cap the blast radius of any mistake. Every mutation is audited to DynamoDB. The autonomous action layer runs bounded — real agency where safe, hard refusal where risky (Control Tower–managed SCPs are never touched, `HIGH`-risk IAM changes require human approval).

---

## What makes it agentic

Two real LangGraph state graphs live under `lambdas/agents/`. Each is a bounded autonomous agent driven by Kimi K2.5 on Amazon Bedrock. Neither is a fixed pipeline — the LLM controls routing at decision nodes and gathers evidence via AWS API calls before committing to actions.

**IAM Remediation Agent** — `lambdas/agents/iam_remediator/graph.py`
- 7-node graph: `assess_proposal → run_simulations → apply → verify → rollback/success/failure`
- Kimi picks the next node at `assess_proposal` (SIMULATE / APPLY / REJECT) and at `verify` (verified / not verified)
- Real AWS actions: `iam:SimulateCustomPolicy` in a loop, `iam:CreatePolicyVersion` + `SetDefaultPolicyVersion` for the mutation, `iam:GetPolicyVersion` for verification, `iam:DeletePolicyVersion` for autonomous rollback

**SCP Governance Agent** — `lambdas/agents/scp_governance/graph.py`
- 10-node graph with a Control Tower hard-refuse guard, snapshot for rollback, spot-check equivalence, blast-radius check, LLM deliberation, apply, verify, autonomous rollback
- Kimi decides at the `deliberate` node — reject if CT-managed, spot-check failed, or blast radius > 50; otherwise apply
- Real AWS actions: `organizations:DescribePolicy`, `organizations:ListTargetsForPolicy`, `iam:SimulateCustomPolicy` × 5, `organizations:UpdatePolicy` for the real org-wide SCP mutation, and again for autonomous rollback if verification fails

Both agents write their decisions to `zeroshift-agent-reasoning` (DynamoDB), and the frontend fetches this trace via `GET /agent-reasoning` to render each node visit live. If an agent errors, the Step Functions state machine automatically falls back to a deterministic path — the demo never hangs.

---

## Real AWS services (not local, not simulated)

Every backend flow runs inside AWS. The browser frontend on `localhost:8000` calls a real API Gateway endpoint. CloudTrail activity data is fixture-backed only because we didn't have a live AWS Organization to test against — the same Lambdas query CloudTrail Lake directly when `ZEROSHIFT_FIXTURE_MODE=false`.

- **AWS Lambda** — every unit of logic; Python 3.12
- **AWS Step Functions** — 10-state IAM remediation workflow, 7-state SCP refactor workflow
- **Amazon Bedrock** — Kimi K2.5 via the Converse API for every LLM call
- **Amazon API Gateway (HTTP API)** — public routes for the frontend
- **Amazon DynamoDB** — three tables: intent registry (Parts 1/2), SCP audit trail (Part 3), agent reasoning log (agentic layer)
- **Amazon S3** — policy snapshots at onboarding
- **Amazon SNS** — denial alerts, approval requests
- **Amazon EventBridge** — weekly scheduled scans (Sun/Mon/Tue crons for stale SCP / SCP refactor / IAM drift respectively)
- **AWS IAM** — real policy version mutations on the demo role
- **AWS Organizations** — SCP hierarchy traversal + real `UpdatePolicy` mutations via the SCP agent
- **AWS IAM Policy Simulator** — the deterministic verifier that surrounds every LLM proposal
- **AWS CloudTrail Lake** — usage-based drift signal for both IAM and SCP paths
- **AWS KMS** — customer-managed key for encryption at rest
- **AWS Secrets Manager** — LLM fallback credential scaffold (unused at v1; IAM handles Bedrock auth)
- **AWS CloudWatch Logs** — structured JSON logs from every Lambda
- **AWS CloudFormation** (via AWS CDK) — infrastructure-as-code deployment

---

## The three parts

ZeroShift is organized as three parts, each solving a piece of the IAM+SCP lifecycle. All three ship in this repo; all three are deployable via one `make deploy`.

**Part 1 — Born Right (IAM).** Code analyzer. Parses Python source with the standard-library `ast` module, extracts every AWS SDK call, LLM-augments to catch dynamic dispatches, and writes intent-registry deltas — `ACTIVE` for new SDK usage, `PENDING_REMOVAL` with a grace period for actions no longer called. This is what grounds Part 2's drift decisions in real developer intent instead of guessing.

**Part 2 — Stay Right (IAM).** Continuous IAM drift remediation. Weekly cron plus an on-demand API. Cross-references each ZeroShift-managed role's attached policy against CloudTrail Lake activity and the intent registry to identify drift. Kimi K2.5 proposes minimal replacement policies. A deterministic risk scorer assigns `LOW` / `MEDIUM` / `HIGH`. `LOW` auto-applies (real IAM policy version mutation); `MEDIUM` waits out a grace period; `HIGH` requires human approval. The apply step routes to either a deterministic Lambda or the LangGraph IAM Remediation Agent based on a per-execution flag.

**Part 3 — Govern Right (SCP).** Full SCP intelligence at the AWS Organizations level. Three components:
- **Denial Root-Cause Analyzer.** Given an access-denied event, walks the Root → OU → Account SCP hierarchy, simulates each attached SCP against the denied action, identifies the exact statement that fired, and returns a plain-English explanation with a suggested fix.
- **SCP Refactoring.** Detects SCPs approaching the 10,240-character limit. Kimi proposes a compressed replacement, IAM Policy Simulator verifies equivalence across the curated ~500-action catalog, and either the deterministic path records the change request or the LangGraph SCP Governance Agent autonomously mutates the SCP via `organizations:UpdatePolicy` (with self-rollback on verification failure).
- **Stale SCP Detection.** Weekly scan flags Deny statements whose enumerated actions have zero CloudTrail activity in the last 180 days — safe to remove without changing behavior.

**One frontend, five sidebar tabs.** Denial Analysis, SCP Refactoring, Stale SCP Detection, IAM Drift Detection, Code Analyzer. Every tab talks to the same real API Gateway. Vanilla HTML/CSS/JS, no build step, no framework.

---

## Quick start

**Prerequisites:**
- Python 3.12+
- Node.js (for the AWS CDK CLI: `npm install -g aws-cdk`)
- AWS CLI v2, authenticated to an account you can deploy into
- Amazon Bedrock model access for Kimi K2.5 (Bedrock console → Model access)

**Deploy and demo:**

```bash
make install-dev           # dev tools (pytest, moto, ruff, boto3)
make install               # CDK Python dependencies
make bootstrap             # one-time CDK bootstrap for the account/region
make build-agent-layer     # pip installs LangGraph + langchain-aws into the layer directory
make deploy                # deploys all four CDK stacks
make seed-intent-registry  # seed fixture rows so the Part 1 ↔ Part 2 feedback loop works

# In a second terminal:
make serve-frontend        # browser UI on http://localhost:8000
```

After deploy, paste the `DenialAnalyzerApiEndpoint` value from the `ZeroShiftPart3` stack outputs into the frontend's API endpoint field in the header.

**Run tests without AWS:**

```bash
make test        # 92 tests, all pass in ~3 seconds, no AWS calls (fixture mode + moto mocks)
make lint        # ruff on lambdas/, infrastructure/, scripts/
```

**Invoke deployed Lambdas from the CLI:**

```bash
make invoke-denial FIXTURE=production-region-deny
make invoke-refactor SCP=p-prod-oversized-deny
make invoke-drift-scan LOOKBACK=90
make invoke-stale-scan LOOKBACK=180
```

**Tear down:**

```bash
make destroy
```

Note that SharedStack resources (DynamoDB tables, S3 bucket, KMS key) have `RemovalPolicy.RETAIN` for safety and must be deleted manually if you want a full wipe.

---

## Architecture

Four CDK stacks:

- `ZeroShiftShared` — DynamoDB × 3, S3, SNS × 3, KMS, Secrets Manager, Permissions Boundary, agent Lambda Layer
- `ZeroShiftPart1` — code analyzer Lambda (IAM intent capture)
- `ZeroShiftPart2` — drift detector, remediator (Step Functions), IAM Remediation Agent, onboarding Lambda, demo role + policy
- `ZeroShiftPart3` — denial analyzer, refactorer (Step Functions), stale detector, SCP Governance Agent, HTTP API Gateway

---

## Repository layout

```
devCraftsmen-platform/
├── zeroshift.md                       # original product spec
├── CLAUDE.md                          # AWS grounding rule for AI-assisted work
├── Makefile                           # deploy, test, invoke, serve targets
├── infrastructure/                    # CDK app (Python)
│   ├── app.py
│   ├── stacks/                        # SharedStack + Part1Stack + Part2Stack + Part3Stack
│   ├── step_functions/                # ASL definitions for the two state machines
│   ├── policies/                      # Permissions Boundary JSON
│   └── change_manager_templates/      # SSM Change Manager template (planned use)
├── lambdas/
│   ├── shared/                        # LLM client, DynamoDB helpers, Organizations client, IAM role client, CloudTrail Lake client, action catalog, logging
│   ├── part1/code_analyzer/           # Python AST extractor + Kimi augmenter + intent-registry writer
│   ├── part2/                         # IAM lifecycle
│   │   ├── drift_detector/            # role scan + Kimi proposal + deterministic risk scorer
│   │   ├── remediator/                # Step Functions task handlers + deterministic apply
│   │   └── onboarding/                # discover roles + baseline + tag + boundary attach
│   ├── part3/                         # SCP lifecycle
│   │   ├── denial_analyzer/           # SCP root-cause analysis
│   │   ├── refactorer/                # SCP refactor pipeline
│   │   └── stale_detector/            # weekly stale-SCP scan
│   └── agents/                        # LangGraph agents (agentic layer)
│       ├── common/                    # Bedrock client, boto3 tool functions, reasoning logger
│       ├── iam_remediator/            # Part 2 agentic apply
│       └── scp_governance/            # Part 3 agentic apply
├── frontend/                          # vanilla HTML/CSS/JS single-page UI (five tabs)
├── fixtures/                          # canned data for fixture mode (org tree, SCPs, denials, CloudTrail counts, role policies, code samples, IAM action catalog)
├── scripts/                           # build_agent_layer, package_lambda, seed_intent_registry, invoke_* helpers
├── tests/                             # 92 tests (unit + integration)
└── docs/
    ├── zeroshift-knowledge-transfer.html   # comprehensive team-onboarding guide
    └── demo-script.md                      # ~6-minute presenter walkthrough
```

---

## Tech stack

**Languages:** Python 3.12 (Lambda + CDK), JavaScript (vanilla, no framework), HTML, CSS, JSON (Step Functions ASL), YAML (SSM templates)

**AWS SDK:** `boto3` / `botocore`

**Agent framework:** `langgraph` + `langchain-aws` (`ChatBedrockConverse` for Kimi K2.5) + `langchain-core`. Shipped as an AWS Lambda Layer at `build/agent-layer/`.

**Infrastructure-as-code:** `aws-cdk-lib` (Python bindings) → CloudFormation → real AWS resources

**Dev tooling:** `pytest`, `moto` (AWS mocking), `ruff` (linter)

---

## Documentation

- **`docs/zeroshift-knowledge-transfer.html`** — comprehensive team-onboarding doc with a searchable sidebar, AWS-service primer, per-feature deep dives, and file references. Open directly in a browser; no build required.
- **`docs/demo-script.md`** — ~6-minute presenter walkthrough with timings, spoken lines, service name-drops, and a pre-flight cheat sheet.
- **`zeroshift.md`** — the original product spec that defined the three parts. Referenced throughout the codebase.

---

## Status

- 92 tests passing (unit + integration), ruff clean, CDK synth clean
- All four stacks deploy cleanly with `make deploy`
- Fixture mode is the default; real-mode toggles per Lambda via `ZEROSHIFT_FIXTURE_MODE=false`
- Real IAM mutation is enabled against the CDK-deployed demo role `zeroshift-demo-app-role`; other roles remain fixture-only until you onboard them
- Real SCP mutation via the SCP Governance Agent goes through `organizations:UpdatePolicy` when the "🤖 Autonomous apply" toggle is on

---

## License

Internal — Developer Craftsmen team.

## Team

Developer Craftsmen — built for the AWS Agentic AI Hackathon (2026).
