"""Minimal in-process Amazon States Language interpreter.

Executes the repo's real state-machine definitions
(``infrastructure/step_functions/*.json``) against the real Lambda handlers,
so the demo API follows exactly the same workflow as the AWS deployment.
Supports the subset those definitions use: Task, Choice, Pass, Wait, Parameters
with ``.$`` JSONPath, ``ResultPath``, ``$$.Execution.Name``, ``IsPresent``,
``BooleanEquals`` and ``StringEquals``.
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import copy
import os
import re
import time
from typing import Any, Callable

# Time limits so a stuck LLM call ends the run as FAILED (with a message the UI shows) instead of RUNNING forever.
# A worker thread cannot be killed, so these bound what the *user* waits for; the LLM client has its own budget.
STEP_TIMEOUT_S = float(os.environ.get("SFN_STEP_TIMEOUT_S", "120"))
AGENT_STEP_TIMEOUT_S = float(os.environ.get("SFN_AGENT_STEP_TIMEOUT_S", "240"))
EXECUTION_TIMEOUT_S = float(os.environ.get("SFN_EXECUTION_TIMEOUT_S", "270"))

_TOKEN = re.compile(r"\.([A-Za-z0-9_\-]+)|\[(\d+)\]")


def _get(path: str, data: Any, ctx: dict[str, Any]) -> Any:
    if path.startswith("$$."):
        data, path = ctx, "$" + path[2:]
    if path == "$":
        return data
    cur = data
    for name, idx in _TOKEN.findall(path[1:]):
        if name:
            if not isinstance(cur, dict) or name not in cur:
                raise KeyError(path)
            cur = cur[name]
        else:
            cur = cur[int(idx)]
    return cur


def _resolve(params: Any, data: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(params, dict):
        out: dict[str, Any] = {}
        for k, v in params.items():
            if k.endswith(".$"):
                out[k[:-2]] = _get(v, data, ctx)
            else:
                out[k] = _resolve(v, data, ctx)
        return out
    if isinstance(params, list):
        return [_resolve(v, data, ctx) for v in params]
    return params


def _set_path(data: dict[str, Any], result_path: str | None, result: Any) -> Any:
    if result_path is None:
        return result
    if result_path == "$":
        return result
    parts = [p for p in result_path[2:].split(".") if p]
    base = copy.deepcopy(data) if isinstance(data, dict) else {}
    cur = base
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = result
    return base


def _choice_matches(rule: dict[str, Any], data: Any, ctx: dict[str, Any]) -> bool:
    var = rule["Variable"]
    if "IsPresent" in rule:
        try:
            _get(var, data, ctx)
            present = True
        except (KeyError, IndexError, TypeError):
            present = False
        return present == rule["IsPresent"]
    try:
        value = _get(var, data, ctx)
    except (KeyError, IndexError, TypeError):
        return False
    if "BooleanEquals" in rule:
        return value is rule["BooleanEquals"] or value == rule["BooleanEquals"]
    if "StringEquals" in rule:
        return value == rule["StringEquals"]
    return False


class ExecutionTimeout(TimeoutError):
    """A step or the whole execution used up its time budget."""


class _TimedOut(Exception):
    pass


def _call_with_timeout(fn: Callable[[dict[str, Any]], dict[str, Any]], params: dict[str, Any], timeout: float) -> Any:
    """Run ``fn`` in a worker thread (keeping contextvars) and give up after ``timeout`` seconds."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="sfn-task")
    ctx = contextvars.copy_context()
    fut = pool.submit(ctx.run, fn, params)
    try:
        done, _ = concurrent.futures.wait([fut], timeout=timeout)
        if not done:
            raise _TimedOut
        return fut.result()
    finally:
        pool.shutdown(wait=False)


class Execution:
    """Runs one state machine, recording per-state history for status polling."""

    def __init__(
        self,
        definition: dict[str, Any],
        input_: dict[str, Any],
        resources: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
        name: str,
        wait_cap_seconds: float = 2.0,
        step_timeout_s: float | None = None,
        execution_timeout_s: float | None = None,
    ) -> None:
        self.definition = definition
        self.input = input_
        self.resources = resources
        self.name = name
        self.wait_cap = wait_cap_seconds
        self.step_timeout = step_timeout_s
        self.execution_timeout = EXECUTION_TIMEOUT_S if execution_timeout_s is None else execution_timeout_s
        self.status = "RUNNING"
        self.output: Any = None
        self.error: str | None = None
        self.history: list[dict[str, Any]] = []  # {name,type,status,output}
        self.terminal: str | None = None
        self.started = time.time()
        self.stopped: float | None = None

    def _record(self, name: str, type_: str, status: str, output: Any = None) -> None:
        for h in self.history:
            if h["name"] == name:
                h.update(status=status, output=output if output is not None else h.get("output"))
                return
        self.history.append({"name": name, "type": type_, "status": status, "output": output})

    def _run_task(self, state_name: str, resource: str, fn: Callable[[dict[str, Any]], dict[str, Any]], params: dict[str, Any]) -> Any:
        left = self.execution_timeout - (time.time() - self.started)
        if left <= 0:
            raise ExecutionTimeout(
                f"The workflow exceeded its {self.execution_timeout:.0f}s time limit before step '{state_name}'. "
                "The free LLM pool may be slow or rate-limited; try again, or add your own key via 'LLM key'."
            )
        step_cap = self.step_timeout or (AGENT_STEP_TIMEOUT_S if "Agent" in resource else STEP_TIMEOUT_S)
        limit = min(step_cap, left)
        try:
            return _call_with_timeout(fn, params, limit)
        except _TimedOut:
            which = "the workflow time limit" if limit < step_cap else f"the {step_cap:.0f}s step limit"
            raise ExecutionTimeout(
                f"Step '{state_name}' did not finish within {which} ({limit:.0f}s). "
                "The free LLM pool may be slow or rate-limited; try again, or add your own key via 'LLM key'."
            ) from None

    def run(self) -> None:
        ctx = {"Execution": {"Name": self.name}}
        data: Any = copy.deepcopy(self.input)
        state_name = self.definition["StartAt"]
        try:
            while True:
                state = self.definition["States"][state_name]
                type_ = state["Type"]
                if type_ == "Task":
                    self._record(state_name, type_, "RUNNING")
                    params = _resolve(state.get("Parameters", {}), data, ctx)
                    fn = self.resources[state["Resource"]]
                    result = self._run_task(state_name, state["Resource"], fn, params)
                    self._record(state_name, type_, "SUCCEEDED", result)
                    data = _set_path(data, state.get("ResultPath", "$"), result)
                elif type_ == "Choice":
                    nxt = state.get("Default")
                    for rule in state.get("Choices", []):
                        if _choice_matches(rule, data, ctx):
                            nxt = rule["Next"]
                            break
                    state_name = nxt
                    continue
                elif type_ == "Pass":
                    self._record(state_name, type_, "RUNNING")
                    if "Parameters" in state:
                        result = _resolve(state["Parameters"], data, ctx)
                    else:
                        result = state.get("Result", data)
                    self._record(state_name, type_, "SUCCEEDED", result)
                    if state.get("End") and "ResultPath" not in state:
                        self.terminal = state_name
                    data = _set_path(data, state.get("ResultPath", "$"), result)
                elif type_ == "Wait":
                    self._record(state_name, type_, "RUNNING")
                    secs = 0.0
                    if "SecondsPath" in state:
                        try:
                            secs = float(_get(state["SecondsPath"], data, ctx))
                        except (KeyError, TypeError, ValueError):
                            secs = 0.0
                    elif "Seconds" in state:
                        secs = float(state["Seconds"])
                    time.sleep(min(secs, self.wait_cap))  # demo: grace period is shortened
                    self._record(state_name, type_, "SUCCEEDED", {"waitedSeconds": min(secs, self.wait_cap)})
                else:
                    raise ValueError(f"Unsupported state type {type_}")
                if state.get("End"):
                    break
                state_name = state["Next"]
            self.output = data
            self.status = "SUCCEEDED"
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI like a FAILED execution
            self.status = "FAILED"
            self.error = f"{type(exc).__name__}: {exc}"
            self.output = {"error": type(exc).__name__, "cause": str(exc)}
            for h in self.history:
                if h["status"] == "RUNNING":
                    h["status"] = "FAILED"
        finally:
            self.stopped = time.time()
