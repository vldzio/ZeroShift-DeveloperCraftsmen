.PHONY: help bootstrap install install-dev test test-unit test-integration lint deploy diff synth seed-fixtures invoke-denial destroy

PYTHON ?= python
PIP ?= pip
CDK ?= cdk
AWS_REGION ?= us-east-1
FIXTURE ?= production-region-deny
LOOKBACK ?= 180

help:
	@echo "ZeroShift Part 3 - Make targets"
	@echo ""
	@echo "  install-dev       Install dev dependencies (pytest, moto, boto3, ruff)"
	@echo "  install           Install CDK app dependencies"
	@echo "  bootstrap         Run cdk bootstrap for the target account/region"
	@echo "  synth             cdk synth (renders CloudFormation)"
	@echo "  diff              cdk diff against deployed stacks"
	@echo "  deploy            cdk deploy ZeroShiftShared and ZeroShiftPart3"
	@echo "  destroy           cdk destroy (irreversible)"
	@echo "  test              Run the full pytest suite"
	@echo "  test-unit         Run only unit tests"
	@echo "  test-integration  Run only integration tests (fixture-mode)"
	@echo "  lint              Run ruff on lambdas/ and infrastructure/"
	@echo "  invoke-denial     Invoke the deployed denial analyzer with FIXTURE=<name>"
	@echo "  invoke-refactor   Start the SCP refactor state machine with SCP=<policy-id>"
	@echo "  invoke-stale-scan Invoke the stale-SCP detector with LOOKBACK=<days>"
	@echo "  invoke-drift-scan Invoke the Part 2 IAM drift detector with LOOKBACK=<days>"
	@echo "  seed-intent-registry Load fixture entries into the intent-registry table"
	@echo "  seed-fixtures     Print the fixture inventory (sanity check)"
	@echo "  serve-frontend    Serve the browser UI on http://localhost:8000"

install-dev:
	$(PIP) install -r requirements-dev.txt

install:
	$(PIP) install -r infrastructure/requirements.txt

bootstrap:
	cd infrastructure && $(CDK) bootstrap

synth: package-lambda
	cd infrastructure && $(CDK) synth

diff:
	cd infrastructure && $(CDK) diff

package-lambda:
	@echo "Assembling Lambda deployment package at build/lambda-package/"
	$(PYTHON) scripts/package_lambda.py

build-agent-layer:
	@echo "Building agent Lambda Layer (LangGraph + langchain-aws) at build/agent-layer/"
	$(PYTHON) scripts/build_agent_layer.py

deploy: package-lambda build-agent-layer
	cd infrastructure && $(CDK) deploy ZeroShiftShared ZeroShiftPart3 ZeroShiftPart2 ZeroShiftPart1 --require-approval never

destroy:
	cd infrastructure && $(CDK) destroy ZeroShiftPart3 ZeroShiftShared

test:
	$(PYTHON) -m pytest tests/ -v

test-unit:
	$(PYTHON) -m pytest tests/unit/ -v

test-integration:
	$(PYTHON) -m pytest tests/integration/ -v

lint:
	$(PYTHON) -m ruff check lambdas/ infrastructure/

invoke-denial:
	@echo "Invoking denial analyzer with fixture: $(FIXTURE)"
	@$(PYTHON) -c "from pathlib import Path; Path('build').mkdir(exist_ok=True)"
	aws lambda invoke --function-name zeroshift-part3-denial-analyzer --payload fileb://fixtures/denial_events/$(FIXTURE).json --cli-binary-format raw-in-base64-out build/response.json
	@$(PYTHON) -c "import json; print(json.dumps(json.load(open('build/response.json')), indent=2))"

invoke-refactor:
	@echo "Starting SCP refactor state machine for SCP: $(SCP)"
	@$(PYTHON) -c "from pathlib import Path; Path('build').mkdir(exist_ok=True)"
	@$(PYTHON) scripts/invoke_refactor.py $(SCP)

invoke-stale-scan:
	@echo "Invoking stale-SCP detector (lookback: $(LOOKBACK) days)"
	@$(PYTHON) -c "from pathlib import Path; Path('build').mkdir(exist_ok=True)"
	@$(PYTHON) scripts/invoke_stale_scan.py $(LOOKBACK)

invoke-drift-scan:
	@echo "Invoking IAM drift detector (lookback: $(LOOKBACK) days)"
	@$(PYTHON) scripts/invoke_drift_scan.py $(LOOKBACK)

seed-intent-registry:
	@echo "Seeding zeroshift-intent-registry from fixtures/intent_registry_seed.json"
	@$(PYTHON) scripts/seed_intent_registry.py

seed-fixtures:
	@$(PYTHON) -c "from pathlib import Path; [print(p) for p in sorted(Path('fixtures').rglob('*.json'))]"

serve-frontend:
	$(PYTHON) scripts/sync_frontend_fixtures.py
	@echo "Serving frontend on http://localhost:8000"
	@echo "After deploy, paste the API URL (from CfnOutput DenialAnalyzerApiEndpoint) into the header field."
	cd frontend && $(PYTHON) -m http.server 8000
