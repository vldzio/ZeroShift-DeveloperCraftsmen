# ZeroShift — agentic IAM and SCP governance on AWS

**AI agents that propose, verify, and explain changes to IAM policies and AWS Organizations service control policies (SCPs).** ZeroShift combines Python AWS Lambdas, Step Functions, CDK infrastructure, Amazon Bedrock, and two LangGraph agents behind a five-tab browser console. Bundled fixture data lets you explore every workflow without a live AWS Organization.

**Live demo:** [zeroshift-demo.vercel.app](https://zeroshift-demo.vercel.app). It runs the real Lambda handlers on fixture data, with no AWS account involved.

## What it does

| Area | Approach | Code |
| --- | --- | --- |
| Code-to-permission intent | Analyze Python AWS SDK calls and maintain an intent registry | `lambdas/part1/` |
| IAM drift | Compare role permissions with usage and intent, propose scoped-down policies, score risk, and route remediation | `lambdas/part2/` |
| SCP denial analysis | Walk the policy hierarchy, simulate policies, and explain an access-denied result | `lambdas/part3/denial_analyzer/` |
| SCP refactoring | Compress an oversized SCP, prove decision equivalence by simulation, and open a change request or apply via the agent | `lambdas/part3/refactorer/` |
| Stale SCP review | Find Deny statements with no observed usage in a configured window | `lambdas/part3/stale_detector/` |

The console lives in `frontend/`; infrastructure for shared services and the three product parts lives in `infrastructure/stacks/`; `fixtures/` supplies sample policies, events, and organization data.

## Agent design

The language model reasons, but deterministic code decides whether a mutation is allowed. Two stateful LangGraph workflows live in `lambdas/agents/`:

- **IAM remediation** (`iam_remediator/graph.py`): rejects anything that is not LOW-risk drift before the model is called, has the model assess the proposal, simulates every removed action against the original and proposed policy, applies a new policy version only when every simulation passes, verifies the re-fetched document, and rolls back on mismatch.
- **SCP governance** (`scp_governance/graph.py`): refuses Control Tower–managed SCPs, snapshots the current document, spot-checks proposed policy behavior, counts attached targets, and enforces the spot-check and blast-radius limits in code before the model deliberates. It then applies, verifies, or rolls back. An unknown blast radius blocks the apply.

Shared AWS tools, the Bedrock client, and decision logging live in `lambdas/agents/common/`.

```text
Frontend → API Gateway → Lambda / Step Functions
                              ├─ intent + drift workflows
                              ├─ denial / refactoring workflows
                              └─ LangGraph agents → Bedrock + AWS APIs
                         DynamoDB / fixtures / CloudTrail inputs
```

## Run locally

Prerequisites: Python 3.12 and `pip`. For deployment you also need the AWS CLI with credentials, Node.js, and the AWS CDK CLI.

```bash
python -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
make install-dev
make install
make test                             # pytest suite with AWS services mocked by moto
make seed-fixtures                    # list bundled fixture JSON files
make serve-frontend                   # static console at http://localhost:8000
```

The console's API-backed flows talk to a deployed API endpoint (see below) or the hosted demo.

## Deploy to AWS

```bash
npm install -g aws-cdk
make synth                            # render CloudFormation
make diff                             # compare with deployed stacks
make bootstrap
make deploy                           # packages Lambdas and the agent layer; deploys four stacks
```

After deployment, enter the API Gateway endpoint from the CDK output in the console. The account needs Bedrock model access in the target region.

## Live demo

The hosted console runs the **real Lambda handlers** in fixture mode through `demo_api/`, a FastAPI adapter whose routes mirror API Gateway:

```text
Browser (Vercel, frontend/) ──► demo_api container
                                  ├─ rate limits, bring-your-own-key, CORS
                                  ├─ real Lambdas + Step Functions emulator (runs infrastructure/step_functions/*.json)
                                  ├─ moto sandbox for DynamoDB / SNS / IAM
                                  └─ OpenAI-compatible LLM gateway ──► free-tier models (LangSmith tracing)
```

- In demo mode the LLM layer (`lambdas/shared/llm_client.py`) switches from Bedrock to an OpenAI-compatible provider, with schema-validated JSON and one retry. Without `LLM_BASE_URL` set, Bedrock is used as before.
- Visitors get a daily allowance, or can enter their own OpenAI-compatible key under **LLM key**. The key stays in the browser tab and is never logged.
- Run it locally:

  ```bash
  pip install -r demo_api/requirements.txt
  LLM_BASE_URL=... LLM_API_KEY=... uvicorn demo_api.app:app --port 8130
  make serve-frontend                 # then open http://localhost:8000/index.html?api=http://localhost:8130
  ```

- Deployment: container recipe in `deploy/space/`, frontend via `vercel.json` (set `ZEROSHIFT_API_URL`).

## Repository map

```text
frontend/             Browser console (HTML, CSS, JavaScript)
demo_api/             FastAPI adapter, Step Functions emulator, and sandbox for the hosted demo
deploy/space/         Container recipe for the demo API
infrastructure/       Python AWS CDK app and stacks
lambdas/              Product Lambdas and LangGraph agents
fixtures/             Sample policies, events, and organization data
scripts/              Packaging, invocation, and seed helpers
tests/                Unit and integration tests
requirements-dev.txt  Test and lint dependencies
Makefile              Local and deployment tasks
```

## Author

Designed and built by [Hariharan S](https://github.com/vldzio).
