"""Shared foundation for all ZeroShift parts.

Provisions the DynamoDB intent registry (used by Parts 1 and 2), the SCP audit
table (used by Part 3), the customer-managed KMS key, the S3 artifacts bucket,
the SNS topics, a placeholder Secrets Manager entry for LLM fallback credentials,
and the ZeroShift Permissions Boundary policy.
"""
from __future__ import annotations

import json
from pathlib import Path

from aws_cdk import (
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from constructs import Construct


class SharedStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.shared_kms_key = kms.Key(
            self,
            "SharedKmsKey",
            alias="alias/zeroshift-shared",
            description="ZeroShift shared encryption key (DynamoDB, S3, Secrets Manager)",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.intent_table = dynamodb.Table(
            self,
            "IntentRegistryTable",
            table_name="zeroshift-intent-registry",
            partition_key=dynamodb.Attribute(
                name="roleArn", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="permission#version", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.shared_kms_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.scp_audit_table = dynamodb.Table(
            self,
            "ScpAuditTable",
            table_name="zeroshift-scp-audit",
            partition_key=dynamodb.Attribute(
                name="scpId", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="changeId#timestamp", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.shared_kms_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Persists LangGraph agent decision-log rows for the two autonomous
        # agents (IAM Remediation, SCP Governance). Frontend polls this table
        # during a remediation to render the live reasoning trace.
        self.agent_reasoning_table = dynamodb.Table(
            self,
            "AgentReasoningTable",
            table_name="zeroshift-agent-reasoning",
            partition_key=dynamodb.Attribute(
                name="agentExecutionId", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="stepId#timestamp", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=self.shared_kms_key,
            point_in_time_recovery=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.artifacts_bucket = s3.Bucket(
            self,
            "ArtifactsBucket",
            bucket_name=f"zeroshift-artifacts-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=self.shared_kms_key,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        self.denial_alerts_topic = sns.Topic(
            self,
            "DenialAlertsTopic",
            topic_name="zeroshift-denial-alerts",
            display_name="ZeroShift SCP denial alerts",
            master_key=self.shared_kms_key,
        )

        self.approval_requests_topic = sns.Topic(
            self,
            "ApprovalRequestsTopic",
            topic_name="zeroshift-approval-requests",
            display_name="ZeroShift approval requests (Change Manager)",
            master_key=self.shared_kms_key,
        )

        self.platform_alarms_topic = sns.Topic(
            self,
            "PlatformAlarmsTopic",
            topic_name="zeroshift-platform-alarms",
            display_name="ZeroShift platform CloudWatch alarms",
            master_key=self.shared_kms_key,
        )

        self.llm_credentials_secret = secretsmanager.Secret(
            self,
            "LlmCredentialsSecret",
            secret_name="zeroshift/llm-credentials",
            description="Placeholder for direct-provider LLM credentials (Moonshot, OpenAI, Azure OpenAI). Unused when LLM_PROVIDER=bedrock, which authenticates via IAM.",
            encryption_key=self.shared_kms_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template=json.dumps(
                    {
                        "moonshotApiKey": "",
                        "openaiApiKey": "",
                        "openaiOrgId": "",
                        "azureEndpoint": "",
                        "azureDeploymentName": "",
                        "azureApiVersion": "",
                    }
                ),
                generate_string_key="_unused",
                exclude_punctuation=True,
                password_length=16,
            ),
        )

        # ZeroShift Permissions Boundary. Not attached to any Lambda in this
        # stack (Part 3 Lambdas do not create IAM roles), but authored now
        # because Part 1 will attach it to every generated role.
        boundary_doc_path = Path(__file__).resolve().parent.parent / "policies" / "zeroshift_platform_boundary.json"
        with boundary_doc_path.open("r", encoding="utf-8") as f:
            boundary_doc = json.load(f)

        self.platform_boundary = iam.ManagedPolicy(
            self,
            "PlatformBoundary",
            managed_policy_name="ZeroShiftPlatformBoundary",
            description="Permissions Boundary attached to every ZeroShift-managed IAM role. Caps the blast radius of any LLM-generated policy.",
            document=iam.PolicyDocument.from_json(boundary_doc),
        )

        # Lambda Layer hosting LangGraph + langchain-aws for the agentic apply
        # Lambdas in Part 2 and Part 3. Assembled by `make build-agent-layer`
        # into build/agent-layer/. Attached to agent Lambdas only; deterministic
        # Lambdas don't need these dependencies.
        repo_root = Path(__file__).resolve().parent.parent.parent
        agent_layer_dir = repo_root / "build" / "agent-layer"

        self.agent_layer = lambda_.LayerVersion(
            self,
            "AgentDepsLayer",
            layer_version_name="zeroshift-agent-deps",
            code=lambda_.Code.from_asset(str(agent_layer_dir)),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="LangGraph + langchain-aws + langchain-core for ZeroShift agentic apply Lambdas.",
        )
