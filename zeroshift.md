ZeroShift — Complete Project Context for Claude Code
What Is This?
ZeroShift is a three-part autonomous IAM lifecycle platform built on AWS. It enforces Zero Trust IAM across the complete lifecycle of AWS workloads — from the moment code is written, through continuous operation, to org-wide governance. It is being built as a hackathon submission proposing a solution to real, documented gaps in AWS's IAM tooling.

The Core Problem Being Solved
AWS provides excellent detection tools (IAM Access Analyzer, Config, CloudTrail) but leaves remediation manual. Specifically:

Developers over-permission IAM roles at deployment and never clean them up
IAM Access Analyzer cannot process data-plane events (S3 GetObject, DynamoDB PutItem) — only management events — leaving a major blind spot in policy analysis
Access denied errors don't always tell you which policy layer caused the denial — especially across accounts or when multiple SCPs exist
SCPs approaching the 10,240 character limit must be manually refactored — a complex, error-prone process
No native AWS tool autonomously closes the loop from detection → reasoning → remediation
The Three Parts
PART 1 — "Born Right"
Goal: User provides a GitHub repo URL. The app scans the code, understands what it does, generates least-privilege IAM policies, deploys the app on AWS, and keeps policies updated as code evolves.

User flow:

User pastes GitHub repo URL
App clones repo, scans all code files
Bedrock analyzes code → produces human-readable summary: "This is a news summarizer app that reads RSS feeds, stores articles in S3, and sends email digests via SES"
App generates: required AWS resources (S3 bucket, Lambda, SES), exact least-privilege IAM policy JSON, estimated monthly cost
User sees approval card → clicks Approve
App deploys via CloudFormation, creates IAM role with Permissions Boundary, attaches generated policy
On every future git push: CodePipeline triggers, CodeBuild runs git diff, Bedrock analyzes what changed, proposes IAM delta (additions require approval, removals are scheduled for auto-removal via Part 2)
Key design rule: Every IAM policy created by the app is tagged:

{
  "ManagedBy": "ZeroShift",
  "GitRepo": "github.com/user/repo",
  "GitCommit": "abc123",
  "GeneratedAt": "2026-08-01",
  "PolicyVersion": "v4",
  "Part": "1"
}

PART 2 — "Stay Right"
Goal: Continuously monitor all IAM policies assigned to ZeroShift-managed projects. Over time, detect unused/over-permissioned policies and autonomously remediate them using a risk-tiered approach.

How it works:

3 trigger sources: AWS Config rules (real-time on policy change), EventBridge (CloudTrail IAM API events), scheduled weekly deep scans
Analysis layer: IAM Access Analyzer (unused permissions) + CloudTrail Lake queries (actual API usage including data-plane events Access Analyzer misses) + Bedrock (generates minimal replacement policy + assigns risk score)
Risk-tiered decision:
LOW: Auto-apply (e.g., remove unused permission from non-prod role)
MEDIUM: Notify + 48hr grace period → auto-apply (e.g., scope wildcard to specific ARN)
HIGH: Require explicit human approval (e.g., any production role change)
Remediation via Step Functions: (1) generate new policy, (2) simulate via IAM Policy Simulator, (3) attach new version, (4) verify, (5) detach old version, (6) update DynamoDB registry, (7) log audit trail
Critical feedback loop with Part 1:

When Part 1 adds a new permission (code update), it writes to DynamoDB with status ACTIVE and the commit hash
Part 2 reads this before flagging drift — suppresses alerts for intentional additions
When Part 1 detects a removed SDK call, it writes PENDING_REMOVAL to DynamoDB with a grace period expiry
Part 2 executes the removal after the grace period
DynamoDB intent registry schema:

{
  "roleArn": "arn:aws:iam::123456789:role/ZeroShift-MyAppRole",
  "permission": "ses:SendEmail",
  "resource": "arn:aws:ses:us-east-1:123456789:identity/*",
  "status": "PENDING_REMOVAL | ACTIVE | REMOVED",
  "addedAtCommit": "abc123",
  "removedAtCommit": "def456",
  "gracePeriodExpiry": "2026-08-08T00:00:00Z",
  "riskScore": "LOW | MEDIUM | HIGH",
  "source": "PART1_CODE_ANALYSIS | PART2_DRIFT_DETECTION",
  "lastUpdated": "2026-08-01T17:00:00Z"
}

PART 3 — "Govern Right"
Goal: Extend IAM intelligence to the AWS Organization level. Solve three SCP problems: (1) identify which policy layer caused an access denial, (2) refactor/split SCPs approaching the 10,240 character limit, (3) flag stale SCP statements.

SCP Problem 1 — Denial Root Cause:

User reports an access denied error
App parses the CloudTrail event for the denial
Traverses Root → OU → Account SCP hierarchy via AWS Organizations API
Simulates each SCP against the denied action using IAM Policy Simulator
Identifies exact SCP name + Statement ID that caused the denial
Produces human-readable explanation: "Denied by SCP 'RestrictRegions' attached to OU 'Production', Statement 'DenyNonApprovedRegions'. Suggested fix: add ap-south-1 to the allowed regions list."
Sends via SNS to the developer immediately
SCP Problem 2 — SCP Refactoring:

Triggered when an SCP exceeds 80% of the 10,240 character limit
Bedrock reads the SCP and understands its semantic intent
Identifies redundant/overlapping statements
Proposes compressed version (same effect, fewer characters)
If still too large: intelligently splits into 2–3 SCPs (max 10 per node)
IAM Policy Simulator runs equivalence check: tests both original and proposed against hundreds of actions
Shows diff to human → requires TWO approvals (security admin + second reviewer) → applies via AWS Organizations API
SCP Problem 3 — Stale SCP Detection:

Weekly scheduled scan via EventBridge
Queries CloudTrail Lake: are any SCP-denied actions ever being attempted? (dead deny statements)
Queries CloudTrail Lake: are any SCP-allowed services ever used? (stale allow statements)
Flags: "SCP 'DenyGlacier' has not had a single attempted Glacier API call in 180 days — consider removing to reduce SCP complexity"
Surfaces as recommendations only — never auto-applies SCP changes
Critical safety rule for Part 3:

Part 3 is read-propose-only for SCPs by default
ALL SCP mutations require explicit human approval (minimum two approvers)
App never touches SCPs managed by AWS Control Tower
Rollback of SCP changes also requires human approval
AWS Services Used
Part 1 Services
Service	Role
Amazon Bedrock	Code analysis, intent understanding, IAM policy generation, Git diff semantic analysis
AWS CodePipeline	GitHub webhook integration, pipeline orchestration on every git push
AWS CodeBuild	Repo cloning, git diff execution, code packaging
AWS CloudFormation	Infrastructure deployment via Change Sets (preview before apply)
AWS IAM	Role creation, policy attachment, Permissions Boundary enforcement
Amazon DynamoDB	Policy intent registry (shared with Parts 2 and 3)
AWS Systems Manager Change Manager	Single human approval gate before any deployment
Amazon SNS	Approval notifications, code update alerts
Amazon S3	CloudFormation templates, policy documents, deployment artifacts
Part 2 Services
Service	Role
AWS Config	Real-time IAM policy change detection, compliance rule evaluation
Amazon EventBridge	Routes CloudTrail IAM events to analysis pipeline, triggers scheduled scans
AWS CloudTrail Lake	Queryable API usage history including data-plane events (fills Access Analyzer blind spot)
IAM Access Analyzer	Unused permission detection, overly broad policy identification
Amazon Bedrock	Generates minimal replacement policies, assigns risk scores
AWS Step Functions	Orchestrates multi-step remediation workflow with rollback
AWS Lambda	Execution glue between all services
IAM Policy Simulator	Verifies new scoped-down policy won't break the application before applying
Amazon DynamoDB	Reads Part 1 intent registry, suppresses false drift alerts
Amazon CloudWatch	Platform health metrics, anomaly alarms on the agent itself
AWS Security Hub	Centralizes all IAM findings across accounts
Part 3 Services
Service	Role
AWS Organizations	Traverses Root → OU → Account hierarchy, reads all SCP content and attachments
AWS CloudTrail Lake	Denial forensics, SCP relevance analysis (dead/stale statements)
Amazon Bedrock	SCP semantic understanding, redundancy detection, refactoring proposals
IAM Policy Simulator	SCP equivalence verification (original vs proposed must produce identical outcomes)
AWS Lambda	SCP traversal, denial analysis, Bedrock orchestration
AWS Step Functions	Two-approver SCP change workflow
AWS Systems Manager Change Manager	Enforces mandatory two-approver gate for all SCP mutations
Amazon SNS	Denial root cause alerts to developers, SCP proposal notifications to approvers
Amazon EventBridge	Weekly scheduled org scans, unauthorized SCP change detection
Amazon DynamoDB	SCP change audit trail, proposal history, approval records
AWS Security Hub	Org-wide security posture dashboard
AWS Control Tower	Read-only: identifies Control Tower-managed SCPs that must never be touched
Shared Infrastructure
Service	Shared Role
Amazon DynamoDB	Single source of truth across all three parts
AWS CloudTrail Lake	Unified telemetry layer for all three parts
Amazon Bedrock	AI reasoning engine across all three parts
Amazon S3	Long-term storage: policy versions, templates, compliance reports
AWS Lambda	Execution glue across all three parts
Amazon CloudWatch	Monitors the ZeroShift platform itself
Key Architectural Decisions
1. Permissions Boundary on Every Created Role
Every IAM role ZeroShift creates has a Permissions Boundary attached. This caps the maximum permissions the agent can ever grant — even if Bedrock makes a mistake, the blast radius is bounded.

2. CloudTrail Lake Over Standard CloudTrail
Standard CloudTrail logs are used by IAM Access Analyzer but miss data-plane events. CloudTrail Lake enables SQL-like queries across all event history including S3 GetObject, DynamoDB PutItem, Kinesis PutRecord — filling the documented Access Analyzer blind spot.

3. IAM Policy Simulator as Safety Gate
Before any policy change is applied (Parts 2 and 3), the IAM Policy Simulator runs the proposed policy against the application's known API calls. This is the automated regression test for IAM — if the simulation fails, the change is blocked.

4. DynamoDB as Shared Intent Registry
The DynamoDB table is the communication channel between all three parts. Part 1 writes intent (what was added/removed and why). Part 2 reads intent before flagging drift. Part 3 reads it for audit history. This prevents false positives and creates a complete audit trail.

5. Risk-Tiered Automation (Part 2)
Not all IAM changes are equal. The risk tier determines automation level:

LOW risk → auto-apply immediately
MEDIUM risk → notify + 48hr grace period → auto-apply
HIGH risk → block until explicit human approval
Risk is determined by: environment (prod vs non-prod), role blast radius (how many services/resources it accesses), permission sensitivity (admin-adjacent actions), and whether the role is ZeroShift-managed or human-created.

6. Read-Propose-Only for SCPs (Part 3)
SCPs affect entire AWS accounts and OUs. A mistake can lock out an entire organization. ZeroShift never autonomously applies SCP changes. It only reads, analyzes, and proposes. All SCP mutations require two human approvals via Systems Manager Change Manager.

7. Control Tower Awareness
Part 3 reads the list of Control Tower-managed SCPs before proposing any changes. ZeroShift never proposes modifications to SCPs that Control Tower owns — doing so would cause Control Tower drift and potentially break the landing zone.

What ZeroShift Does NOT Do
Does not modify SCPs autonomously (always human-approved)
Does not touch IAM roles it did not create (only manages ManagedBy: ZeroShift tagged resources)
Does not analyze service-linked roles (AWS explicitly excludes these from IAM Access Analyzer)
Does not replace AWS Control Tower or AWS Organizations — it reads from them
Does not store or log actual code content — only SDK call patterns extracted by Bedrock
The Innovation Gap (Why This Doesn't Already Exist)
Capability	AWS Today	ZeroShift
Detect unused permissions	✅ IAM Access Analyzer	✅ Included
Generate replacement policy	⚠️ Partial (management events only)	✅ Full (includes data-plane via CloudTrail Lake)
Auto-apply remediation	❌ Manual	✅ Risk-tiered autonomous
Pre-deployment policy generation from code	❌ Does not exist	✅ Part 1 core feature
SCP denial root cause identification	⚠️ Partial (same-account only, not universal)	✅ Full org hierarchy traversal
SCP semantic refactoring	❌ Does not exist	✅ Part 3 core feature
Code update → IAM delta	❌ Does not exist	✅ Part 1 git diff pipeline
Cross-part intent registry	❌ Does not exist	✅ DynamoDB shared layer
Suggested Build Order for Claude Code
Start with the DynamoDB schema — it's the foundation all three parts depend on
Build Part 1 pipeline — CodePipeline → CodeBuild → Bedrock analysis → CloudFormation deploy → IAM attach
Build the approval UI — the summary card the user sees before approving deployment
Build Part 2 monitoring — Config rule → EventBridge → Lambda → CloudTrail Lake query → Bedrock → Step Functions remediation
Build Part 3 SCP denial analyzer — the highest-value, most demo-able feature
Build Part 3 SCP refactoring — Bedrock semantic analysis + equivalence check
Wire the shared DynamoDB intent registry across all three parts
Build the Security Hub dashboard — unified view for the demo