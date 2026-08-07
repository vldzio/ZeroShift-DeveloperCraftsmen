"""Part 3 — Govern Right.

Component 1: SCP Denial Root-Cause Analyzer.
Component 2: SCP Refactoring (Step Functions + Change Manager + safe-mode apply).
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct


class Part3Stack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        intent_table: dynamodb.Table,
        scp_audit_table: dynamodb.Table,
        denial_alerts_topic: sns.Topic,
        approval_requests_topic: sns.Topic,
        llm_credentials_secret: secretsmanager.Secret,
        shared_kms_key: kms.Key,
        agent_layer: lambda_.LayerVersion,
        agent_reasoning_table: dynamodb.Table,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Lambda deployment package assembled by `make package-lambda`.
        repo_root = Path(__file__).resolve().parent.parent.parent
        lambda_package_dir = repo_root / "build" / "lambda-package"

        analyzer_role = iam.Role(
            self,
            "DenialAnalyzerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart3AnalyzerRole",
            description="Execution role for the ZeroShift SCP Denial Root-Cause Analyzer Lambda.",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )

        # Organizations read-only (denial attribution).
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "organizations:ListPoliciesForTarget",
                    "organizations:DescribePolicy",
                    "organizations:DescribeOrganizationalUnit",
                    "organizations:DescribeAccount",
                    "organizations:ListParents",
                    "organizations:ListRoots",
                ],
                resources=["*"],
            )
        )

        # IAM Policy Simulator for per-SCP evaluation.
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:SimulateCustomPolicy"],
                resources=["*"],
            )
        )

        # CloudTrail Lake lookups (eventId-based analysis, future work).
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudtrail:LookupEvents",
                    "cloudtrail:StartQuery",
                    "cloudtrail:GetQueryResults",
                ],
                resources=["*"],
            )
        )

        # Bedrock — scoped to Moonshot AI Kimi K2.5. Invocation goes through
        # the provider-neutral Converse API, so both `InvokeModel` and
        # `Converse` are required. Confirm the exact model ID via
        # `aws bedrock list-foundation-models --by-provider moonshotai
        # --region us-east-1` after model access is granted.
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:Converse"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/moonshotai.kimi-k2.5*",
                    # Cross-region inference profile ARNs — allowed for
                    # multi-region failover of the same model.
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/moonshotai.kimi-k2.5*",
                ],
            )
        )

        # Read-only access to the LLM credentials fallback secret. Use explicit
        # PolicyStatements referencing ARNs to avoid cross-stack circular refs
        # that ``grant_*`` helpers introduce when the resource lives in a
        # different stack than the role.
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[llm_credentials_secret.secret_arn],
            )
        )
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:GenerateDataKey",
                    "kms:GenerateDataKeyWithoutPlaintext",
                ],
                resources=[shared_kms_key.key_arn],
            )
        )
        # DynamoDB — write to audit, read-only intent registry (future use).
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:UpdateItem",
                    "dynamodb:BatchWriteItem",
                ],
                resources=[scp_audit_table.table_arn],
            )
        )
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:BatchGetItem",
                ],
                resources=[intent_table.table_arn],
            )
        )
        # SNS — publish denial alerts.
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=[denial_alerts_topic.topic_arn],
            )
        )

        denial_analyzer = lambda_.Function(
            self,
            "DenialAnalyzerFunction",
            function_name="zeroshift-part3-denial-analyzer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="part3.denial_analyzer.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=analyzer_role,
            timeout=Duration.seconds(60),
            memory_size=512,
            environment={
                "LOG_LEVEL": "INFO",
                "LLM_PROVIDER": "bedrock",
                "LLM_MODEL_ID": "moonshotai.kimi-k2.5",
                "LLM_CREDENTIALS_SECRET_ID": llm_credentials_secret.secret_name,
                "ZEROSHIFT_FIXTURE_MODE": "true",
                "ZEROSHIFT_INTENT_TABLE": intent_table.table_name,
                "ZEROSHIFT_SCP_AUDIT_TABLE": scp_audit_table.table_name,
                "DENIAL_ALERTS_TOPIC_ARN": denial_alerts_topic.topic_arn,
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # HTTP API — public POST /analyze-denial endpoint. CORS is enabled so
        # the local frontend (served from http://localhost:8000 during dev, or
        # any origin the demo is hosted on) can call the API from a browser.
        http_api = apigw.HttpApi(
            self,
            "DenialAnalyzerApi",
            api_name="zeroshift-part3-api",
            description="ZeroShift Part 3 — Denial Analyzer HTTP API",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[
                    apigw.CorsHttpMethod.GET,
                    apigw.CorsHttpMethod.POST,
                    apigw.CorsHttpMethod.OPTIONS,
                ],
                allow_headers=["Content-Type", "Authorization"],
                max_age=Duration.hours(1),
            ),
        )
        http_api.add_routes(
            path="/analyze-denial",
            methods=[apigw.HttpMethod.POST],
            integration=apigw_integrations.HttpLambdaIntegration(
                "DenialAnalyzerIntegration",
                handler=denial_analyzer,
            ),
        )

        CfnOutput(
            self,
            "DenialAnalyzerApiEndpoint",
            value=http_api.api_endpoint,
            description="Base URL for the ZeroShift Part 3 HTTP API. Paste this into the frontend's API endpoint field.",
            export_name="ZeroShiftPart3ApiEndpoint",
        )

        self.denial_analyzer = denial_analyzer
        self.http_api = http_api

        # =====================================================================
        # Component 2 — SCP Refactoring
        # =====================================================================

        # Apply-runbook Lambda: invoked by Change Manager after two approvals.
        # Safe-mode v1 — writes an audit row and publishes SNS; never mutates SCPs.
        apply_runbook_role = iam.Role(
            self,
            "ScpApplyRunbookRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftScpApplyRunbookRole",
            description="Execution role for the safe-mode apply runbook. Records approval; does NOT call organizations:UpdatePolicy.",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        apply_runbook_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                resources=[scp_audit_table.table_arn],
            )
        )
        apply_runbook_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=[approval_requests_topic.topic_arn],
            )
        )
        apply_runbook_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:GenerateDataKey",
                    "kms:GenerateDataKeyWithoutPlaintext",
                ],
                resources=[shared_kms_key.key_arn],
            )
        )

        apply_runbook_fn = lambda_.Function(
            self,
            "ScpApplyRunbookFunction",
            function_name="zeroshift-scp-refactor-apply-runbook",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="part3.refactorer.apply_runbook.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=apply_runbook_role,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "LOG_LEVEL": "INFO",
                "ZEROSHIFT_SCP_AUDIT_TABLE": scp_audit_table.table_name,
                "APPROVAL_REQUESTS_TOPIC_ARN": approval_requests_topic.topic_arn,
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # Refactorer role — dispatches all Step Functions task lambdas via a
        # single function; needs the union of permissions the tasks require.
        refactorer_role = iam.Role(
            self,
            "RefactorerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart3RefactorerRole",
            description="Execution role for the ZeroShift SCP Refactor task Lambda.",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        refactorer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "organizations:DescribePolicy",
                    "organizations:ListPolicies",
                    "organizations:ListPoliciesForTarget",
                ],
                resources=["*"],
            )
        )
        refactorer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:SimulateCustomPolicy"],
                resources=["*"],
            )
        )
        refactorer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:Converse"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/moonshotai.kimi-k2.5*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/moonshotai.kimi-k2.5*",
                ],
            )
        )
        refactorer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[llm_credentials_secret.secret_arn],
            )
        )
        refactorer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:GenerateDataKey",
                    "kms:GenerateDataKeyWithoutPlaintext",
                ],
                resources=[shared_kms_key.key_arn],
            )
        )
        refactorer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:UpdateItem",
                ],
                resources=[scp_audit_table.table_arn],
            )
        )
        refactorer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=[approval_requests_topic.topic_arn],
            )
        )
        refactorer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ssm:StartChangeRequestExecution",
                    "ssm:DescribeDocument",
                ],
                resources=["*"],
            )
        )

        refactorer_fn = lambda_.Function(
            self,
            "RefactorerFunction",
            function_name="zeroshift-part3-refactorer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="part3.refactorer.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=refactorer_role,
            timeout=Duration.seconds(120),
            memory_size=768,
            environment={
                "LOG_LEVEL": "INFO",
                "LLM_PROVIDER": "bedrock",
                "LLM_MODEL_ID": "moonshotai.kimi-k2.5",
                "LLM_CREDENTIALS_SECRET_ID": llm_credentials_secret.secret_name,
                "ZEROSHIFT_FIXTURE_MODE": "true",
                "ZEROSHIFT_INTENT_TABLE": intent_table.table_name,
                "ZEROSHIFT_SCP_AUDIT_TABLE": scp_audit_table.table_name,
                "APPROVAL_REQUESTS_TOPIC_ARN": approval_requests_topic.topic_arn,
                "ZEROSHIFT_CHANGE_TEMPLATE_NAME": "ZeroShiftScpMutationTwoApprover",
                "ZEROSHIFT_APPLY_RUNBOOK_NAME": "ZeroShiftScpApplyRunbook",
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # SCP Governance Agent (LangGraph, Kimi K2.5 on Bedrock).
        scp_agent_role = iam.Role(
            self,
            "ScpAgentRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart3ScpAgentRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        scp_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "organizations:DescribePolicy",
                    "organizations:ListTargetsForPolicy",
                    "organizations:UpdatePolicy",
                    "iam:SimulateCustomPolicy",
                ],
                resources=["*"],
            )
        )
        scp_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:Converse"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/moonshotai.kimi-k2.5*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/moonshotai.kimi-k2.5*",
                ],
            )
        )
        scp_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=[approval_requests_topic.topic_arn],
            )
        )
        scp_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:GenerateDataKey",
                    "kms:GenerateDataKeyWithoutPlaintext",
                ],
                resources=[shared_kms_key.key_arn],
            )
        )
        scp_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem"],
                resources=[agent_reasoning_table.table_arn],
            )
        )

        scp_agent_fn = lambda_.Function(
            self,
            "ScpGovernanceAgentFunction",
            function_name="zeroshift-part3-scp-agent",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="agents.scp_governance.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=scp_agent_role,
            timeout=Duration.seconds(120),
            memory_size=1024,
            layers=[agent_layer],
            environment={
                "LOG_LEVEL": "INFO",
                "LLM_MODEL_ID": "moonshotai.kimi-k2.5",
                "APPROVAL_REQUESTS_TOPIC_ARN": approval_requests_topic.topic_arn,
                "ZEROSHIFT_AGENT_REASONING_TABLE": agent_reasoning_table.table_name,
                "ZEROSHIFT_FIXTURE_MODE": "true",
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # Step Functions state machine — the refactor orchestration.
        asl_path = repo_root / "infrastructure" / "step_functions" / "scp_refactor.json"
        asl_definition = (
            asl_path.read_text(encoding="utf-8")
            .replace("${RefactorerFunctionArn}", refactorer_fn.function_arn)
            .replace("${ScpAgentFunctionArn}", scp_agent_fn.function_arn)
        )

        state_machine_role = iam.Role(
            self,
            "ScpRefactorStateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            role_name="ZeroShiftScpRefactorStateMachineRole",
        )
        state_machine_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[refactorer_fn.function_arn, scp_agent_fn.function_arn],
            )
        )

        state_machine = sfn.CfnStateMachine(
            self,
            "ScpRefactorStateMachine",
            state_machine_name="zeroshift-scp-refactor",
            role_arn=state_machine_role.role_arn,
            definition_string=asl_definition,
        )
        state_machine.add_dependency(refactorer_fn.node.default_child)
        state_machine.add_dependency(scp_agent_fn.node.default_child)

        # POST /refactor-scp — starts a Step Functions execution.
        refactor_starter_role = iam.Role(
            self,
            "RefactorStarterRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart3RefactorStarterRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        refactor_starter_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[state_machine.attr_arn],
            )
        )
        # Inline starter Lambda — accepts {"scpId": "..."}, kicks off the state
        # machine execution, returns the execution ARN so the caller can poll.
        refactor_starter_fn = lambda_.Function(
            self,
            "RefactorStarterFunction",
            function_name="zeroshift-part3-refactor-starter",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                """
import json
import os
import boto3

SM_ARN = os.environ["STATE_MACHINE_ARN"]
sfn = boto3.client("stepfunctions")


def handler(event, context):
    body = event.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    body = body or event
    scp_id = body.get("scpId")
    if not scp_id:
        return {"statusCode": 400, "body": json.dumps({"error": "scpId is required"})}
    use_agent = bool(body.get("useAgent"))
    resp = sfn.start_execution(
        stateMachineArn=SM_ARN,
        input=json.dumps({"scpId": scp_id, "useAgent": use_agent}),
    )
    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "executionArn": resp["executionArn"],
            "startDate": resp["startDate"].isoformat() if hasattr(resp["startDate"], "isoformat") else str(resp["startDate"]),
            "scpId": scp_id,
        }),
    }
                """
            ),
            role=refactor_starter_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"STATE_MACHINE_ARN": state_machine.attr_arn},
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        http_api.add_routes(
            path="/refactor-scp",
            methods=[apigw.HttpMethod.POST],
            integration=apigw_integrations.HttpLambdaIntegration(
                "RefactorScpIntegration",
                handler=refactor_starter_fn,
            ),
        )

        # Refactor status endpoint — describes a running or completed Step
        # Functions execution and parses its history into a step-by-step
        # timeline the frontend can render as a checklist.
        refactor_status_role = iam.Role(
            self,
            "RefactorStatusRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart3RefactorStatusRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        refactor_status_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution", "states:GetExecutionHistory"],
                resources=[
                    state_machine.attr_arn,
                    f"arn:aws:states:{self.region}:{self.account}:execution:zeroshift-scp-refactor:*",
                ],
            )
        )
        refactor_status_fn = lambda_.Function(
            self,
            "RefactorStatusFunction",
            function_name="zeroshift-part3-refactor-status",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                """
import json
import os
import boto3

sfn = boto3.client("stepfunctions")

TASK_STATES = [
    "LoadScp",
    "CheckControlTower",
    "CheckSize",
    "ProposeRefactor",
    "VerifyEquivalence",
    "CreateChangeRequest",
]
TERMINAL_STATES = {
    "Accepted": "SUCCEEDED",
    "Skipped_ControlTower": "SKIPPED_CONTROL_TOWER",
    "Skipped_UnderThreshold": "SKIPPED_UNDER_THRESHOLD",
    "Rejected_NotEquivalent": "REJECTED_NOT_EQUIVALENT",
}


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    execution_arn = params.get("executionArn")
    if not execution_arn:
        return _resp(400, {"error": "executionArn query parameter is required"})

    try:
        desc = sfn.describe_execution(executionArn=execution_arn)
    except sfn.exceptions.ExecutionDoesNotExist:
        return _resp(404, {"error": "execution not found", "executionArn": execution_arn})
    except Exception as exc:
        return _resp(500, {"error": str(exc)})

    steps = {name: {"name": name, "status": "PENDING"} for name in TASK_STATES}
    terminal = None

    history = sfn.get_execution_history(executionArn=execution_arn, maxResults=1000, reverseOrder=False)
    for evt in history.get("events", []):
        t = evt.get("type", "")
        details = evt.get("stateEnteredEventDetails") or evt.get("stateExitedEventDetails") or {}
        name = details.get("name")
        if not name:
            continue
        if name in steps:
            if t == "TaskStateEntered":
                steps[name]["status"] = "RUNNING"
                steps[name]["startedAt"] = evt.get("timestamp")
            elif t == "TaskStateExited":
                steps[name]["status"] = "SUCCEEDED"
                steps[name]["endedAt"] = evt.get("timestamp")
                try:
                    steps[name]["output"] = json.loads(details.get("output") or "{}")
                except Exception:
                    steps[name]["output"] = None
        if name in TERMINAL_STATES and t == "PassStateEntered":
            terminal = TERMINAL_STATES[name]

    # Mark any remaining PENDING steps as SKIPPED if the execution short-circuited.
    if desc["status"] != "RUNNING":
        for step in steps.values():
            if step["status"] == "PENDING":
                step["status"] = "SKIPPED"

    output = None
    if desc.get("output"):
        try:
            output = json.loads(desc["output"])
        except Exception:
            output = None

    return _resp(200, {
        "executionArn": execution_arn,
        "status": desc["status"],
        "terminal": terminal,
        "startDate": desc.get("startDate"),
        "stopDate": desc.get("stopDate"),
        "steps": [steps[n] for n in TASK_STATES],
        "finalOutput": output,
    })
                """
            ),
            role=refactor_status_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        http_api.add_routes(
            path="/refactor-status",
            methods=[apigw.HttpMethod.GET],
            integration=apigw_integrations.HttpLambdaIntegration(
                "RefactorStatusIntegration",
                handler=refactor_status_fn,
            ),
        )

        # EventBridge weekly cron — Monday 06:00 UTC. Triggers the refactorer
        # to scan all SCPs. In the safe-mode / fixture-mode v1 the target is
        # the starter Lambda with a canned payload; a future iteration replaces
        # this with a "discover candidates" step that fans out one execution
        # per SCP over the size threshold.
        events.Rule(
            self,
            "ScpRefactorWeeklyScan",
            rule_name="zeroshift-scp-refactor-weekly",
            description="Weekly SCP refactor candidate scan (Mon 06:00 UTC).",
            schedule=events.Schedule.cron(minute="0", hour="6", week_day="MON"),
            targets=[
                events_targets.LambdaFunction(
                    handler=refactor_starter_fn,
                    event=events.RuleTargetInput.from_object(
                        {"scpId": "p-prod-oversized-deny"}
                    ),
                )
            ],
        )

        # SSM Change Manager change-template registration is deferred to
        # real-mode deployment. In safe-mode / fixture-mode (v1), the
        # refactorer's change_request_creator returns a synthetic change
        # request identifier without calling ssm:StartChangeRequestExecution,
        # so the template document does not need to exist for the demo path.
        #
        # To enable real-mode:
        #   1. Enable Change Manager in AWS Systems Manager (one-time,
        #      per-account, in the console). This creates the required
        #      service-linked resources.
        #   2. Register the template manually — the CFN resource type
        #      Automation.ChangeTemplate has a specific schema that AWS
        #      revises frequently, so avoid vendoring it in CDK:
        #        aws ssm create-document \
        #          --name ZeroShiftScpMutationTwoApprover \
        #          --document-type Automation.ChangeTemplate \
        #          --document-format YAML \
        #          --content file://infrastructure/change_manager_templates/scp_mutation_two_approver.yaml
        #   3. Also register the runbook document ZeroShiftScpApplyRunbook
        #      pointing at the apply-runbook Lambda.
        #   4. Flip ZEROSHIFT_FIXTURE_MODE=false on the refactorer Lambda.

        # IAM group referenced by the change template. Deployer manually adds
        # the two approver principals after `make deploy`.
        iam.Group(
            self,
            "ZeroShiftApproversGroup",
            group_name="ZeroShift-Approvers",
        )

        CfnOutput(
            self,
            "ScpRefactorStateMachineArn",
            value=state_machine.attr_arn,
            description="ARN of the ZeroShift SCP Refactor Step Functions state machine.",
            export_name="ZeroShiftScpRefactorStateMachineArn",
        )
        CfnOutput(
            self,
            "RefactorerFunctionName",
            value=refactorer_fn.function_name,
            description="Name of the ZeroShift SCP Refactorer Lambda.",
        )

        self.refactorer_fn = refactorer_fn
        self.apply_runbook_fn = apply_runbook_fn
        self.state_machine = state_machine
        self.refactor_starter_fn = refactor_starter_fn

        # =====================================================================
        # Component 3 — Stale SCP Detection
        # =====================================================================

        stale_detector_role = iam.Role(
            self,
            "StaleDetectorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart3StaleDetectorRole",
            description="Execution role for the ZeroShift SCP Stale-Statement Detector Lambda.",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        stale_detector_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "organizations:ListPolicies",
                    "organizations:ListPoliciesForTarget",
                    "organizations:DescribePolicy",
                    "organizations:ListRoots",
                    "organizations:ListChildren",
                    "organizations:ListParents",
                ],
                resources=["*"],
            )
        )
        stale_detector_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudtrail:StartQuery",
                    "cloudtrail:DescribeQuery",
                    "cloudtrail:GetQueryResults",
                    "cloudtrail:CancelQuery",
                ],
                resources=["*"],
            )
        )
        stale_detector_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                ],
                resources=[scp_audit_table.table_arn],
            )
        )
        stale_detector_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Publish"],
                resources=[approval_requests_topic.topic_arn],
            )
        )
        stale_detector_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:GenerateDataKey",
                    "kms:GenerateDataKeyWithoutPlaintext",
                ],
                resources=[shared_kms_key.key_arn],
            )
        )

        stale_detector_fn = lambda_.Function(
            self,
            "StaleDetectorFunction",
            function_name="zeroshift-part3-stale-detector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="part3.stale_detector.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=stale_detector_role,
            timeout=Duration.seconds(90),
            memory_size=512,
            environment={
                "LOG_LEVEL": "INFO",
                "ZEROSHIFT_FIXTURE_MODE": "true",
                "ZEROSHIFT_SCP_AUDIT_TABLE": scp_audit_table.table_name,
                "APPROVAL_REQUESTS_TOPIC_ARN": approval_requests_topic.topic_arn,
                "CLOUDTRAIL_EVENT_DATA_STORE_ARN": "",
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        http_api.add_routes(
            path="/detect-stale-scps",
            methods=[apigw.HttpMethod.POST],
            integration=apigw_integrations.HttpLambdaIntegration(
                "StaleDetectorIntegration",
                handler=stale_detector_fn,
            ),
        )

        events.Rule(
            self,
            "ScpStaleDetectorWeeklyScan",
            rule_name="zeroshift-scp-stale-weekly",
            description="Weekly stale-SCP scan (Sun 03:00 UTC).",
            schedule=events.Schedule.cron(minute="0", hour="3", week_day="SUN"),
            targets=[
                events_targets.LambdaFunction(
                    handler=stale_detector_fn,
                    event=events.RuleTargetInput.from_object({"lookbackDays": 180}),
                )
            ],
        )

        CfnOutput(
            self,
            "StaleDetectorFunctionName",
            value=stale_detector_fn.function_name,
            description="Name of the ZeroShift Stale-SCP Detector Lambda.",
        )

        self.stale_detector_fn = stale_detector_fn
