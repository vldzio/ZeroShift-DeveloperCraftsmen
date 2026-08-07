"""Part 1 — Born Right — code analyzer (Reframe B: intent-registry sync).

Analyzes Python source, extracts AWS SDK calls, and writes intent-registry
deltas so Part 2's drift detection can distinguish intentional additions from
drift and can act on Part-1-flagged pending removals after the grace period.
Does NOT deploy the analyzed app or generate CloudFormation — that ground is
already covered by CDK / Terraform / SAM.
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as apigw_integrations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from stacks.part3_stack import Part3Stack
from stacks.shared_stack import SharedStack


class Part1Stack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        shared: SharedStack,
        part3: Part3Stack | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self._shared = shared

        repo_root = Path(__file__).resolve().parent.parent.parent
        lambda_package_dir = repo_root / "build" / "lambda-package"

        analyzer_role = iam.Role(
            self,
            "CodeAnalyzerRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            role_name="ZeroShiftPart1CodeAnalyzerRole",
            description="Execution role for the ZeroShift Part 1 code analyzer.",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                ),
            ],
        )
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:PutItem",
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:UpdateItem",
                ],
                resources=[shared.intent_table.table_arn],
            )
        )
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:Converse"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/moonshotai.kimi-k2.5*",
                    f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/moonshotai.kimi-k2.5*",
                ],
            )
        )
        analyzer_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                resources=[shared.llm_credentials_secret.secret_arn],
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
                resources=[shared.shared_kms_key.key_arn],
            )
        )

        analyzer_fn = lambda_.Function(
            self,
            "CodeAnalyzerFunction",
            function_name="zeroshift-part1-code-analyzer",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="part1.code_analyzer.handler.handler",
            code=lambda_.Code.from_asset(str(lambda_package_dir)),
            role=analyzer_role,
            timeout=Duration.seconds(60),
            memory_size=512,
            environment={
                "LOG_LEVEL": "INFO",
                "LLM_PROVIDER": "bedrock",
                "LLM_MODEL_ID": "moonshotai.kimi-k2.5",
                "LLM_CREDENTIALS_SECRET_ID": shared.llm_credentials_secret.secret_name,
                "ZEROSHIFT_INTENT_TABLE": shared.intent_table.table_name,
                "ZEROSHIFT_FIXTURE_MODE": "true",
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )

        if part3 is not None:
            part3.http_api.add_routes(
                path="/analyze-code",
                methods=[apigw.HttpMethod.POST],
                integration=apigw_integrations.HttpLambdaIntegration(
                    "AnalyzeCodeIntegration", handler=analyzer_fn
                ),
            )
            part3.http_api.add_routes(
                path="/analyze-github",
                methods=[apigw.HttpMethod.POST],
                integration=apigw_integrations.HttpLambdaIntegration(
                    "AnalyzeGithubIntegration", handler=analyzer_fn
                ),
            )

        CfnOutput(
            self,
            "CodeAnalyzerFunctionName",
            value=analyzer_fn.function_name,
            description="Name of the ZeroShift Part 1 code analyzer Lambda.",
        )

        self.analyzer_fn = analyzer_fn
