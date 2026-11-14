# ZeroShift — Claude Code project instructions

## AWS grounding rule (mandatory)

Two AWS plugins are installed in this project and provide authoritative, up-to-date AWS information:

- `aws-core` — MCP proxy for AWS. Tools: `aws___read_documentation`, `aws___search_documentation`, `aws___call_aws`, `aws___list_regions`, `aws___get_regional_availability`, `aws___run_script`, `aws___retrieve_skill`, `aws___get_presigned_url`, `aws___get_tasks`. Also exposes AWS skills: `amazon-bedrock`, `aws-cdk`, `aws-cloudformation`, `aws-iam`, `aws-serverless`, `aws-database`, `aws-containers`, `aws-messaging-and-streaming`, `aws-observability`, `aws-secrets-manager`, `aws-billing-and-cost-management`, `aws-sdk-python-usage`, `aws-sdk-js-v3-usage`, `signing-in-to-aws`, and more.
- `deploy-on-aws` — the AWS IaC and Pricing MCP servers. Tools: `search_cdk_documentation`, `search_cdk_samples_and_constructs`, `cdk_best_practices`, `validate_cloudformation_template`, `check_cloudformation_template_compliance`, `get_cloudformation_pre_deploy_validation_instructions`, `troubleshoot_cloudformation_deployment`, `search_cloudformation_documentation`, `read_iac_documentation_page`, `get_pricing`, `get_pricing_service_codes`, `get_pricing_service_attributes`, `get_pricing_attribute_values`, `get_price_list_urls`, `get_bedrock_patterns`, `analyze_cdk_project`, `analyze_terraform_project`, `generate_cost_report`. Also exposes the `deploy` and `aws-architecture-diagram` skills.

**Rule.** Before making any non-trivial AWS decision or recommendation — service selection, IAM policy authoring, CDK construct choice, model ID or model region, pricing estimate, regional availability, deployment step, error diagnosis, best-practice claim — you MUST first ground the answer using one of the plugin tools or skills above. Do not answer AWS questions from training memory alone. This project has already been bitten once by a model-availability answer produced from stale training data (initial claim that Bedrock does not host OpenAI models — wrong; the aws-core plugin would have corrected that immediately).

**Which tool for which question:**

- Model availability, exact model IDs, model regions, model pricing on Bedrock → `aws___search_documentation` or `aws___read_documentation` (docs.aws.amazon.com/bedrock), or invoke the `amazon-bedrock` skill.
- CDK construct APIs, imports, prop shapes → `search_cdk_documentation` first; then `search_cdk_samples_and_constructs` for working examples. Invoke the `aws-cdk` skill for architectural questions.
- CDK best practices, security defaults, stack layout → `cdk_best_practices` tool, or the `aws-cdk` skill.
- CloudFormation template authoring, resource properties → `search_cloudformation_documentation`, `validate_cloudformation_template` (cfn-lint), `check_cloudformation_template_compliance` (cfn-guard).
- Pre-deployment validation → `get_cloudformation_pre_deploy_validation_instructions`.
- Deployment failure diagnosis → `troubleshoot_cloudformation_deployment` (includes CloudTrail correlation).
- IAM policy authoring, trust policies, condition-key edge cases → the `aws-iam` skill.
- Pricing, cost estimates, savings-plans, right-sizing → `get_pricing` (with `get_pricing_service_codes` → `get_pricing_service_attributes` → `get_pricing_attribute_values` for discovery), `analyze_cdk_project` for a whole-stack cost estimate, or the `aws-billing-and-cost-management` skill. For Bedrock cost patterns, use `get_bedrock_patterns`.
- Regional availability of a service → `aws___list_regions`, `aws___get_regional_availability`.
- Running an AWS CLI command that isn't already automated → `aws___call_aws` or `aws___run_script`.
- Architecture diagrams → the `aws-architecture-diagram` skill.

**How to ground:**

1. Identify the exact factual claim you're about to make.
2. Pick the tool or skill above that maps to that claim.
3. Invoke it. Read the result.
4. Base the answer on what the tool returned. If the tool says something different from what you were about to assert, believe the tool.
5. In the response, briefly state which tool or skill grounded the answer (e.g. "per `search_cdk_documentation`, the current construct name is `HttpApi`, not `HttpApiV2`") so the user can audit whether this rule was followed.

**Scope.** This rule applies to any AWS-related output — code, IAM JSON, CDK constructs, model IDs, region names, service quotas, deployment steps, error explanations, cost estimates. It does not apply to trivial mechanical operations (renaming a variable, running an existing Make target, editing a comment) where no new AWS fact is being asserted.

**Exception.** If none of the tools cover the question, fall back to `aws___search_documentation` with a targeted query. If that also comes up empty, say so explicitly rather than fabricating.

## Project layout reminder

Repository conventions (see `README.md` for full detail):

- `lambdas/` is the Lambda deployment root — imports use `from shared.*` and `from part3.*`, not `from lambdas.shared.*`. `tests/conftest.py` adds `lambdas/` to `sys.path` so tests run with the same import paths.
- `fixtures/` is the canonical fixture source. `make package-lambda` copies it into the deploy bundle; `make serve-frontend` copies denial events into `frontend/fixtures/`.
- Any change touching the LLM invocation path (`llm_client.py`) or Bedrock IAM (`part3_stack.py`) must be re-verified with `make test` (25 tests should pass) plus `cd infrastructure && python app.py` (CDK synth should exit 0).

## Demo deployment (branch `demo-ready`)

The console is hosted as a live demo that runs the REAL Lambda handlers on bundled fixtures (no AWS account):

```text
Browser (Vercel, `frontend/`) -> Hugging Face Space `demo_api/` (limits, BYO key, LangSmith)
   -> real handlers in `lambdas/` + `demo_api/sfn_emulator.py` (runs `infrastructure/step_functions/*.json`) + moto sandbox (`demo_api/sandbox.py`)
   -> private FreeLLMAPI gateway (127.0.0.1:3001, same container) -> free-tier LLMs
```

- LLM seam: `lambdas/shared/llm_client.py` has an `openai-compatible` provider (`lambdas/shared/openai_compat.py`, auto-selected when `LLM_BASE_URL` is set; JSON-schema validated with one retry). LangGraph agents use `agents/common/bedrock_client.py` -> `ChatOpenAI` in that mode. Per-request BYO key lives in `lambdas/shared/llm_context.py`. Default Bedrock behaviour is unchanged.
- API: `demo_api/app.py` (FastAPI, routes mirror API Gateway), `guard.py` (Upstash per-IP/global limits, BYO-key host allow-list), `gateway.py` (reads the gateway's unified key from its local SQLite DB). Client IP = LAST `X-Forwarded-For` entry.
- Frontend: `frontend/llm-settings.js` (banner + "LLM key" panel, key in sessionStorage), `vercel.json` build runs `scripts/sync_frontend_fixtures.py` and `scripts/write_frontend_config.py` (env `ZEROSHIFT_API_URL`).
- Space recipe: `deploy/space/`. Space `zeroshift-demo-api`, LangSmith project `zeroshift-demo`. Secrets: `ENCRYPTION_KEY` (64 hex), `FREEAPI_CONFIG_JSON`, `LANGSMITH_API_KEY`, `UPSTASH_REDIS_REST_URL/TOKEN`; optional `CORS_ORIGINS`, `RL_PER_IP_DAILY`, `RL_GLOBAL_DAILY`. The Space clones branch `demo-ready`.
- Steps: `hf auth login`, `vercel login`, create the Space from `deploy/space/`, set secrets, then `vercel deploy --prod --yes --build-env ZEROSHIFT_API_URL=<space url>` from the repo root.
- Tests: `make test` (105 pass: 92 original + 13 new). Never hard-code LLM output. Many repo files use CRLF; preserve line endings.
- Status: NOT verified - Space Docker build, real provider calls (esp. LangGraph tool use on free models), live deploy.
