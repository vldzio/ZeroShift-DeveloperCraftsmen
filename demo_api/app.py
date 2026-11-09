"""FastAPI adapter that serves the real ZeroShift Lambda handlers over HTTP.

Runs entirely in fixture mode (no AWS account needed). LLM calls go to an
OpenAI-compatible gateway (FreeLLMAPI) and are traced to LangSmith.
Routes and payloads mirror the API Gateway routes the frontend already uses.
"""
from __future__ import annotations

import contextvars
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambdas"))

os.environ.setdefault("ZEROSHIFT_FIXTURE_MODE", "true")
os.environ.setdefault("ZEROSHIFT_FIXTURES_DIR", str(ROOT / "fixtures"))
os.environ.setdefault("DEMO_PROJECT", "zeroshift")
os.environ.setdefault("LANGSMITH_PROJECT", "zeroshift-demo")
if os.environ.get("LANGSMITH_API_KEY"):
    os.environ.setdefault("LANGSMITH_TRACING", "true")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from agents.iam_remediator import handler as iam_agent  # noqa: E402
from agents.scp_governance import handler as scp_agent  # noqa: E402
from part1.code_analyzer import handler as code_handler  # noqa: E402
from part2.drift_detector import handler as drift_handler  # noqa: E402
from part2.onboarding import handler as onboarding_handler  # noqa: E402
from part2.remediator import handler as remediator  # noqa: E402
from part3.denial_analyzer import handler as denial_handler  # noqa: E402
from part3.refactorer import handler as refactorer  # noqa: E402
from part3.stale_detector import handler as stale_handler  # noqa: E402
from shared import llm_context  # noqa: E402

from . import guard  # noqa: E402
from .sfn_emulator import Execution  # noqa: E402

SFN_DIR = ROOT / "infrastructure" / "step_functions"
REFACTOR_DEF = json.loads((SFN_DIR / "scp_refactor.json").read_text(encoding="utf-8"))
DRIFT_DEF = json.loads((SFN_DIR / "iam_drift_remediation.json").read_text(encoding="utf-8"))

REFACTOR_TASKS = ["LoadScp", "CheckControlTower", "CheckSize", "ProposeRefactor", "VerifyEquivalence", "CreateChangeRequest"]
REFACTOR_TERMINALS = {
    "Accepted": "SUCCEEDED",
    "Skipped_ControlTower": "SKIPPED_CONTROL_TOWER",
    "Skipped_UnderThreshold": "SKIPPED_UNDER_THRESHOLD",
    "Rejected_NotEquivalent": "REJECTED_NOT_EQUIVALENT",
}
DRIFT_TASKS = [
    "LoadRole", "CheckManaged", "ScanUsage", "ReadIntentRegistry", "ProposeReplacement",
    "SimulateEquivalence", "ScoreRisk", "WaitGracePeriod", "ApplyPolicy", "UpdateIntentRegistry",
]

# credit cost per route (roughly the number of LLM calls it triggers)
COSTS = {"/analyze-denial": 1, "/refactor-scp": 2, "/detect-drift": 4, "/remediate-role": 2, "/analyze-code": 2, "/analyze-github": 2}

app = FastAPI(title="ZeroShift demo API", docs_url="/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.on_event("startup")
def _connect_gateway() -> None:
    """Start the AWS sandbox, then find the private FreeLLMAPI gateway (non-blocking)."""
    from . import gateway, sandbox

    sandbox.start()

    threading.Thread(target=gateway.connect, daemon=True, name="gateway-connect").start()


_executions: dict[str, Execution] = {}


def _client_ip(req: Request) -> str:
    # The platform proxy appends the real client IP as the LAST entry; earlier entries are client-supplied
    # and spoofable, so never use them for rate limiting.
    fwd = req.headers.get("x-forwarded-for")
    return fwd.split(",")[-1].strip() if fwd else (req.client.host if req.client else "unknown")


def _admit(req: Request) -> JSONResponse | None:
    """Apply limits / BYO key for LLM-spending routes. Returns an error response or None."""
    cost = COSTS.get(req.url.path, 0)
    try:
        byo = guard.validate_byo(req.headers.get("x-llm-api-key"), req.headers.get("x-llm-base-url"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if byo:
        llm_context.api_key_override.set(byo[0])
        llm_context.base_url_override.set(byo[1])
        llm_context.trace_metadata.set({"byo_key": True})
    decision = guard.check(_client_ip(req), cost, byo=bool(byo))
    if not decision.allowed:
        return JSONResponse({"error": decision.message, "rateLimited": True}, status_code=429)
    return None


def _lambda(module: Any, path: str, body: dict[str, Any]) -> JSONResponse:
    resp = module.handler({"body": json.dumps(body), "rawPath": path, "path": path}, None)
    payload = json.loads(resp["body"]) if isinstance(resp.get("body"), str) else resp
    return JSONResponse(payload, status_code=resp.get("statusCode", 200))


async def _json(req: Request) -> dict[str, Any]:
    try:
        data = await req.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@app.get("/")
def root() -> dict[str, Any]:
    return {"service": "zeroshift-demo-api", "mode": "fixture", "llmConfigured": bool(os.environ.get("LLM_BASE_URL"))}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


# ---- synchronous Lambdas ----------------------------------------------------
# These are plain `def` routes (threadpool) because the handlers block on LLM calls.

@app.post("/analyze-denial")
async def analyze_denial(req: Request):
    body = await _json(req)
    if (err := _admit(req)):
        return err
    return await _run(lambda: _lambda(denial_handler, "/analyze-denial", body))


@app.post("/detect-stale-scps")
async def detect_stale(req: Request):
    body = await _json(req)
    return await _run(lambda: _lambda(stale_handler, "/detect-stale-scps", body))


@app.post("/detect-drift")
async def detect_drift(req: Request):
    body = await _json(req)
    if (err := _admit(req)):
        return err
    return await _run(lambda: _lambda(drift_handler, "/detect-drift", body))


@app.post("/analyze-code")
async def analyze_code(req: Request):
    body = await _json(req)
    if (err := _admit(req)):
        return err
    return await _run(lambda: _lambda(code_handler, "/analyze-code", body))


@app.post("/analyze-github")
async def analyze_github(req: Request):
    body = await _json(req)
    if (err := _admit(req)):
        return err
    return await _run(lambda: _lambda(code_handler, "/analyze-github", body))


@app.post("/discover-roles")
async def discover_roles(req: Request):
    return await _run(lambda: _lambda(onboarding_handler, "/discover-roles", {}))


@app.post("/onboard-roles")
async def onboard_roles(req: Request):
    body = await _json(req)
    return await _run(lambda: _lambda(onboarding_handler, "/onboard-roles", body))


def _is_llm_error(exc: Exception) -> bool:
    return type(exc).__module__.startswith("openai") or "LLM did not return" in str(exc)


def _friendly_error(exc: Exception) -> str:
    if _is_llm_error(exc):
        return (
            "The free LLM pool could not answer right now (it may be rate-limited or busy). "
            "Try again in a minute, or add your own API key via 'LLM key' in the header."
        )
    return f"{type(exc).__name__}: {exc}"


async def _run(fn):
    """Run a blocking handler in a worker thread, preserving the request's contextvars."""
    import anyio

    ctx = contextvars.copy_context()
    try:
        return await anyio.to_thread.run_sync(lambda: ctx.run(fn))
    except Exception as exc:  # noqa: BLE001 - the UI shows error.message
        return JSONResponse({"error": _friendly_error(exc)}, status_code=503 if _is_llm_error(exc) else 502)


# ---- state-machine workflows ------------------------------------------------

def _start(defn: dict[str, Any], input_: dict[str, Any], resources: dict[str, Any], prefix: str) -> str:
    name = f"{prefix}-{uuid.uuid4().hex[:12]}"
    arn = f"arn:aws:states:demo:000000000000:execution:{prefix}:{name}"
    ex = Execution(defn, input_, resources, name)
    if len(_executions) > 200:  # bound memory
        for k in sorted(_executions, key=lambda k: _executions[k].started)[:50]:
            _executions.pop(k, None)
    _executions[arn] = ex
    ctx = contextvars.copy_context()  # carries BYO key / trace metadata into the worker thread
    threading.Thread(target=ctx.run, args=(ex.run,), daemon=True).start()
    return arn


def _lam(module: Any):
    return lambda params: module.handler(params, None)


def _status_body(arn: str, tasks: list[str], terminals: dict[str, str] | None) -> JSONResponse:
    ex = _executions.get(arn)
    if not ex:
        return JSONResponse(
            {"error": "execution not found (demo state is kept in memory and expires)", "executionArn": arn}, status_code=404
        )
    by_name = {h["name"]: h for h in ex.history}
    steps = []
    for n in tasks:
        h = by_name.get(n)
        step: dict[str, Any] = {"name": n, "status": h["status"] if h else "PENDING"}
        if h and h.get("output") is not None:
            step["output"] = h["output"]
        if ex.status != "RUNNING" and step["status"] == "PENDING":
            step["status"] = "SKIPPED"
        steps.append(step)
    terminal = None
    for n, label in (terminals or {}).items():
        if n in by_name:
            terminal = label
    return JSONResponse(
        {
            "executionArn": arn,
            "status": ex.status,
            "terminal": terminal,
            "startDate": ex.started,
            "stopDate": ex.stopped,
            "steps": steps,
            "finalOutput": ex.output,
        }
    )


@app.post("/refactor-scp")
async def refactor_scp(req: Request):
    body = await _json(req)
    if (err := _admit(req)):
        return err
    if not body.get("scpId"):
        return JSONResponse({"error": "scpId is required"}, status_code=400)
    arn = _start(
        REFACTOR_DEF,
        {"scpId": body["scpId"], "useAgent": bool(body.get("useAgent"))},
        {"${RefactorerFunctionArn}": _lam(refactorer), "${ScpAgentFunctionArn}": _lam(scp_agent)},
        "scp-refactor",
    )
    return {"executionArn": arn}


@app.get("/refactor-status")
def refactor_status(executionArn: str):
    return _status_body(executionArn, REFACTOR_TASKS, REFACTOR_TERMINALS)


@app.post("/remediate-role")
async def remediate_role(req: Request):
    body = await _json(req)
    if (err := _admit(req)):
        return err
    if not body.get("roleArn"):
        return JSONResponse({"error": "roleArn is required"}, status_code=400)
    arn = _start(
        DRIFT_DEF,
        {"roleArn": body["roleArn"], "useAgent": bool(body.get("useAgent")), "gracePeriodSecondsOverride": 2},
        {"${RemediatorFunctionArn}": _lam(remediator), "${IamAgentFunctionArn}": _lam(iam_agent)},
        "iam-drift-remediation",
    )
    return {"executionArn": arn}


@app.get("/remediation-status")
def remediation_status(executionArn: str):
    return _status_body(executionArn, DRIFT_TASKS, None)


@app.get("/agent-reasoning")
def agent_reasoning(agentExecutionId: str):
    import boto3

    resp = boto3.resource("dynamodb").Table(os.environ["ZEROSHIFT_AGENT_REASONING_TABLE"]).query(
        KeyConditionExpression="agentExecutionId = :e",
        ExpressionAttributeValues={":e": agentExecutionId},
        ScanIndexForward=True,
        Limit=200,
    )
    return JSONResponse({"agentExecutionId": agentExecutionId, "steps": json.loads(json.dumps(resp.get("Items", []), default=str))})
