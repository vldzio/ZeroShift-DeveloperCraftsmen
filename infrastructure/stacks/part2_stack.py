"""Part 2 — Stay Right — continuous IAM drift remediation."""
from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import CfnOutput, Duration, Stack, Tags
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from stacks.part3_stack import Part3Stack
from stacks.shared_stack import SharedStack


class Part2Stack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        shared: SharedStack,
        part3: Part3Stack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._shared = shared

        repo_root = Path(__file__).resolve().parent.parent.parent
        lambda_package_dir = repo_root / "build" / "lambda-package"

        # =====================================================================
        # Demo target role + customer-managed policy
        # =====================================================================
        # The role LOW-risk remediation actually mutates. Empty assume-role
        # policy (no principal) so no workload can use it — it's purely a
        # mutation target for the demo.
        with (repo_root / "fixtures" / "role_policies" / "zeroshift-demo-app-policy.json").open("r", encoding="utf-8") as f:
            demo_policy_doc = json.load(f)

        demo_policy = iam.ManagedPolicy(
            self,
            "DemoAppPolicy",
            managed_policy_name="zeroshift-demo-app-policy",
            description="Overly-broad customer-managed policy attached to the ZeroShift demo role. Part 2 scopes this down.",
            document=iam.PolicyDocument.from_json(demo_policy_doc),
        )

        demo_role = iam.Role(
            self,
            "DemoAppRole",
            role_name="zeroshift-demo-app-role",
            assumed_by=iam.AccountRootPrincipal(),
            description="ZeroShift demo target role for Part 2 drift remediation. Never used by real workloads.",
            managed_policies=[demo_policy],
        )
        # Tags identify this role as ZeroShift-managed and mark its environment.
        # Part 2's scan uses these tags to (a) confirm the role is under
        # management and (b) drive the risk-tier decision.
        Tags.of(demo_role).add("ManagedBy", "ZeroShift")
        Tags.of(demo_role).add("Environment", "non-prod")

        # =====================================================================
        # Shared role config used by both drift-detector and remediator
        # =====================================================================
        common_env = {
            "LOG_LEVEL": "INFO",
            "LLM_PROVIDER": "bedrock",
            "LLM_MODEL_ID": "moonshotai.kimi-k2.5",
            "LLM_CREDENTIALS_SECRET_ID": shared.llm_credentials_secret.secret_name,
            "ZEROSHIFT_FIXTURE_MODE": "true",
            "ZEROSHIFT_INTENT_TABLE": shared.intent_table.table_name,
            "ZEROSHIFT_SCP_AUDIT_TABLE": shared.scp_audit_table.table_name,
            "ZEROSHIFT_ARTIFACTS_BUCKET": shared.artifacts_bucket.bucket_name,
            "ZEROSHIFT_PLATFORM_BOUNDARY_ARN": shared.platform_boundary.managed_policy_arn,
        }

        # =====================================================================
        # Drift detector Lambda (POST /detect-drift)
        # =====================================================================
        drift_detector_role = iam.Role(
            self,
            "DriftDetectorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart2DriftDetectorRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        _grant_common_lambda_permissions(drift_detector_role, shared, self.region, self.account)
        drift_detector_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:ListRoles",
                    "iam:GetRole",
                    "iam:ListRoleTags",
                    "iam:ListAttachedRolePolicies",
                    "iam:GetPolicy",
                    "iam:GetPolicyVersion",
                    "iam:SimulateCustomPolicy",
                ],
                resources=["*"],
            )
        )

        drift_detector_fn = lambda_.Function(
            self,
            "DriftDetectorFunction",
            function_name="zeroshift-part2-drift-detector",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="part2.drift_detector.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=drift_detector_role,
            timeout=Duration.seconds(120),
            memory_size=768,
            environment=common_env,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # =====================================================================
        # Remediator Lambda (Step Functions tasks)
        # =====================================================================
        remediator_role = iam.Role(
            self,
            "RemediatorRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart2RemediatorRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        _grant_common_lambda_permissions(remediator_role, shared, self.region, self.account)
        remediator_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:ListRoles",
                    "iam:GetRole",
                    "iam:ListRoleTags",
                    "iam:ListAttachedRolePolicies",
                    "iam:GetPolicy",
                    "iam:GetPolicyVersion",
                    "iam:SimulateCustomPolicy",
                    "iam:ListPolicyVersions",
                    "iam:DeletePolicyVersion",
                ],
                resources=["*"],
            )
        )
        # Only allow policy-version mutation on the specific demo policy —
        # this is the safety cap.
        remediator_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:CreatePolicyVersion",
                    "iam:SetDefaultPolicyVersion",
                ],
                resources=[demo_policy.managed_policy_arn],
            )
        )

        remediator_fn = lambda_.Function(
            self,
            "RemediatorFunction",
            function_name="zeroshift-part2-remediator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="part2.remediator.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=remediator_role,
            timeout=Duration.seconds(120),
            memory_size=768,
            environment=common_env,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # =====================================================================
        # IAM Remediation Agent (LangGraph, Kimi K2.5 on Bedrock)
        # =====================================================================
        iam_agent_role = iam.Role(
            self,
            "IamAgentRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart2IamAgentRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        _grant_common_lambda_permissions(iam_agent_role, shared, self.region, self.account)
        iam_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:GetPolicy",
                    "iam:GetPolicyVersion",
                    "iam:ListPolicyVersions",
                    "iam:SimulateCustomPolicy",
                ],
                resources=["*"],
            )
        )
        iam_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:CreatePolicyVersion",
                    "iam:SetDefaultPolicyVersion",
                    "iam:DeletePolicyVersion",
                ],
                resources=[demo_policy.managed_policy_arn],
            )
        )
        # Agent writes decision-log rows to the shared reasoning table.
        iam_agent_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:PutItem"],
                resources=[shared.agent_reasoning_table.table_arn],
            )
        )

        iam_agent_fn = lambda_.Function(
            self,
            "IamRemediationAgentFunction",
            function_name="zeroshift-part2-iam-agent",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="agents.iam_remediator.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=iam_agent_role,
            timeout=Duration.seconds(120),
            memory_size=1024,
            layers=[shared.agent_layer],
            environment={
                **common_env,
                "ZEROSHIFT_AGENT_REASONING_TABLE": shared.agent_reasoning_table.table_name,
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # =====================================================================
        # Step Functions state machine
        # =====================================================================
        asl_path = repo_root / "infrastructure" / "step_functions" / "iam_drift_remediation.json"
        asl_definition = (
            asl_path.read_text(encoding="utf-8")
            .replace("${RemediatorFunctionArn}", remediator_fn.function_arn)
            .replace("${IamAgentFunctionArn}", iam_agent_fn.function_arn)
        )

        sm_role = iam.Role(
            self,
            "DriftRemediationStateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            role_name="ZeroShiftPart2StateMachineRole",
        )
        sm_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[remediator_fn.function_arn, iam_agent_fn.function_arn],
            )
        )

        state_machine = sfn.CfnStateMachine(
            self,
            "IamDriftRemediationStateMachine",
            state_machine_name="zeroshift-iam-drift-remediation",
            role_arn=sm_role.role_arn,
            definition_string=asl_definition,
        )
        state_machine.add_dependency(remediator_fn.node.default_child)
        state_machine.add_dependency(iam_agent_fn.node.default_child)

        # =====================================================================
        # Onboarding Lambda (POST /discover-roles, POST /onboard-roles)
        # =====================================================================
        onboarding_role = iam.Role(
            self,
            "OnboardingRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart2OnboardingRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        _grant_common_lambda_permissions(onboarding_role, shared, self.region, self.account)
        onboarding_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:ListRoles",
                    "iam:GetRole",
                    "iam:ListRoleTags",
                    "iam:TagRole",
                    "iam:PutRolePermissionsBoundary",
                    "iam:ListAttachedRolePolicies",
                    "iam:GetPolicy",
                    "iam:GetPolicyVersion",
                    "resource-groups:ListGroupResources",
                    "tag:GetResources",
                ],
                resources=["*"],
            )
        )
        onboarding_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{shared.artifacts_bucket.bucket_arn}/policies/*"],
            )
        )

        onboarding_fn = lambda_.Function(
            self,
            "OnboardingFunction",
            function_name="zeroshift-part2-onboarding",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="part2.onboarding.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=onboarding_role,
            timeout=Duration.seconds(60),
            memory_size=512,
            environment=common_env,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # =====================================================================
        # Remediate-starter Lambda (POST /remediate-role) + Status Lambda
        # =====================================================================
        starter_role = iam.Role(
            self,
            "DriftStarterRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart2DriftStarterRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        starter_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[state_machine.attr_arn],
            )
        )
        starter_fn = lambda_.Function(
            self,
            "DriftStarterFunction",
            function_name="zeroshift-part2-remediate-starter",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                """
import json
import os
import boto3

SM_ARN = os.environ["STATE_MACHINE_ARN"]
DEFAULT_GRACE = int(os.environ.get("GRACE_PERIOD_SECONDS", "60"))
sfn = boto3.client("stepfunctions")


def handler(event, context):
    body = event.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = {}
    body = body or event
    role_arn = body.get("roleArn")
    if not role_arn:
        return {"statusCode": 400, "body": json.dumps({"error": "roleArn is required"})}
    grace = int(body.get("gracePeriodSecondsOverride") or DEFAULT_GRACE)
    use_agent = bool(body.get("useAgent"))
    resp = sfn.start_execution(
        stateMachineArn=SM_ARN,
        input=json.dumps({
            "roleArn": role_arn,
            "gracePeriodSecondsOverride": grace,
            "useAgent": use_agent,
        }),
    )
    return {
        "statusCode": 202,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "executionArn": resp["executionArn"],
            "startDate": resp["startDate"].isoformat() if hasattr(resp["startDate"], "isoformat") else str(resp["startDate"]),
            "roleArn": role_arn,
        }),
    }
                """
            ),
            role=starter_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"STATE_MACHINE_ARN": state_machine.attr_arn, "GRACE_PERIOD_SECONDS": "60"},
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        status_role = iam.Role(
            self,
            "DriftStatusRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart2DriftStatusRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        status_role.add_to_policy(
            iam.PolicyStatement(
                actions=["states:DescribeExecution", "states:GetExecutionHistory"],
                resources=[
                    state_machine.attr_arn,
                    f"arn:aws:states:{self.region}:{self.account}:execution:zeroshift-iam-drift-remediation:*",
                ],
            )
        )
        status_fn = lambda_.Function(
            self,
            "DriftStatusFunction",
            function_name="zeroshift-part2-remediation-status",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                """
import json
import boto3

sfn = boto3.client("stepfunctions")

TASK_STATES = [
    "LoadRole",
    "CheckManaged",
    "ScanUsage",
    "ReadIntentRegistry",
    "ProposeReplacement",
    "SimulateEquivalence",
    "ScoreRisk",
    "WaitGracePeriod",
    "ApplyPolicy",
    "UpdateIntentRegistry",
]


def _resp(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    execution_arn = params.get("executionArn")
    if not execution_arn:
        return _resp(400, {"error": "executionArn is required"})
    try:
        desc = sfn.describe_execution(executionArn=execution_arn)
    except sfn.exceptions.ExecutionDoesNotExist:
        return _resp(404, {"error": "execution not found"})
    steps = {name: {"name": name, "status": "PENDING"} for name in TASK_STATES}
    hist = sfn.get_execution_history(executionArn=execution_arn, maxResults=1000, reverseOrder=False)
    for evt in hist.get("events", []) or []:
        t = evt.get("type", "")
        details = evt.get("stateEnteredEventDetails") or evt.get("stateExitedEventDetails") or {}
        name = details.get("name")
        if not name or name not in steps:
            continue
        if t.endswith("StateEntered"):
            steps[name]["status"] = "RUNNING"
        elif t.endswith("StateExited"):
            steps[name]["status"] = "SUCCEEDED"
            try:
                steps[name]["output"] = json.loads(details.get("output") or "{}")
            except Exception:
                steps[name]["output"] = None
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
        "steps": [steps[n] for n in TASK_STATES],
        "finalOutput": output,
    })
                """
            ),
            role=status_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        # =====================================================================
        # HTTP API routes — reuse the Part 3 HttpApi
        # =====================================================================
        part3.http_api.add_routes(
            path="/detect-drift",
            methods=[apigw.HttpMethod.POST],
            integration=apigw_integrations.HttpLambdaIntegration(
                "DetectDriftIntegration", handler=drift_detector_fn
            ),
        )
        part3.http_api.add_routes(
            path="/remediate-role",
            methods=[apigw.HttpMethod.POST],
            integration=apigw_integrations.HttpLambdaIntegration(
                "RemediateRoleIntegration", handler=starter_fn
            ),
        )
        part3.http_api.add_routes(
            path="/remediation-status",
            methods=[apigw.HttpMethod.GET],
            integration=apigw_integrations.HttpLambdaIntegration(
                "RemediationStatusIntegration", handler=status_fn
            ),
        )
        part3.http_api.add_routes(
            path="/discover-roles",
            methods=[apigw.HttpMethod.POST],
            integration=apigw_integrations.HttpLambdaIntegration(
                "DiscoverRolesIntegration", handler=onboarding_fn
            ),
        )
        part3.http_api.add_routes(
            path="/onboard-roles",
            methods=[apigw.HttpMethod.POST],
            integration=apigw_integrations.HttpLambdaIntegration(
                "OnboardRolesIntegration", handler=onboarding_fn
            ),
        )

        # =====================================================================
        # Agent reasoning trace endpoint (GET /agent-reasoning)
        # =====================================================================
        reasoning_role = iam.Role(
            self,
            "AgentReasoningRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftAgentReasoningRole",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        reasoning_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[shared.agent_reasoning_table.table_arn],
            )
        )
        reasoning_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[shared.shared_kms_key.key_arn],
            )
        )
        reasoning_fn = lambda_.Function(
            self,
            "AgentReasoningFunction",
            function_name="zeroshift-agent-reasoning",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="index.handler",
            code=lambda_.Code.from_inline(
                """
import json
import os
import boto3

TABLE = os.environ["REASONING_TABLE"]
ddb = boto3.resource("dynamodb").Table(TABLE)


def _resp(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}


def handler(event, context):
    params = event.get("queryStringParameters") or {}
    execution_id = params.get("agentExecutionId") or params.get("executionId")
    if not execution_id:
        return _resp(400, {"error": "agentExecutionId query parameter is required"})
    resp = ddb.query(
        KeyConditionExpression="agentExecutionId = :e",
        ExpressionAttributeValues={":e": execution_id},
        ScanIndexForward=True,
        Limit=200,
    )
    items = resp.get("Items", [])
    return _resp(200, {"agentExecutionId": execution_id, "steps": items})
                """
            ),
            role=reasoning_role,
            timeout=Duration.seconds(10),
            memory_size=256,
            environment={"REASONING_TABLE": shared.agent_reasoning_table.table_name},
            log_retention=logs.RetentionDays.ONE_MONTH,
        )
        part3.http_api.add_routes(
            path="/agent-reasoning",
            methods=[apigw.HttpMethod.GET],
            integration=apigw_integrations.HttpLambdaIntegration(
                "AgentReasoningIntegration", handler=reasoning_fn
            ),
        )

        # =====================================================================
        # Weekly EventBridge scan
        # =====================================================================
        events.Rule(
            self,
            "IamDriftWeeklyScan",
            rule_name="zeroshift-iam-drift-weekly",
            description="Weekly IAM drift scan (Tue 04:00 UTC).",
            schedule=events.Schedule.cron(minute="0", hour="4", week_day="TUE"),
            targets=[
                events_targets.LambdaFunction(
                    handler=drift_detector_fn,
                    event=events.RuleTargetInput.from_object({"lookbackDays": 90}),
                )
            ],
        )

        CfnOutput(
            self,
            "DemoRoleArn",
            value=demo_role.role_arn,
            description="ARN of the ZeroShift demo target role Part 2 remediates.",
        )
        CfnOutput(
            self,
            "DemoPolicyArn",
            value=demo_policy.managed_policy_arn,
            description="ARN of the customer-managed policy Part 2 mutates on LOW-risk drift.",
        )
        CfnOutput(
            self,
            "IamDriftStateMachineArn",
            value=state_machine.attr_arn,
            description="ARN of the ZeroShift IAM drift remediation Step Functions state machine.",
        )

        self.drift_detector_fn = drift_detector_fn
        self.remediator_fn = remediator_fn
        self.starter_fn = starter_fn
        self.status_fn = status_fn
        self.onboarding_fn = onboarding_fn
        self.state_machine = state_machine
        self.demo_role = demo_role
        self.demo_policy = demo_policy


def _grant_common_lambda_permissions(
    role: iam.Role, shared: SharedStack, region: str, account: str
) -> None:
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "dynamodb:PutItem",
                "dynamodb:GetItem",
                "dynamodb:Query",
                "dynamodb:UpdateItem",
                "dynamodb:BatchGetItem",
                "dynamodb:BatchWriteItem",
            ],
            resources=[shared.intent_table.table_arn, shared.scp_audit_table.table_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["cloudtrail:StartQuery", "cloudtrail:DescribeQuery", "cloudtrail:GetQueryResults"],
            resources=["*"],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["bedrock:InvokeModel", "bedrock:Converse"],
            resources=[
                f"arn:aws:bedrock:{region}::foundation-model/moonshotai.kimi-k2.5*",
                f"arn:aws:bedrock:{region}:{account}:inference-profile/moonshotai.kimi-k2.5*",
            ],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            resources=[shared.llm_credentials_secret.secret_arn],
        )
    )
    role.add_to_policy(
        iam.PolicyStatement(
            actions=[
                "kms:Decrypt",
                "kms:DescribeKey",
                "kms:GenerateDataKey",
                "kms:GenerateDataKeyWithoutPlaintext",
            ],
            resources=[shared.shared_kms_key.key_arn],
        )
    )
