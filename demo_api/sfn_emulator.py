"""Minimal in-process Amazon States Language interpreter.

Executes the repo's real state-machine definitions
(``infrastructure/step_functions/*.json``) against the real Lambda handlers,
so the demo API follows exactly the same workflow as the AWS deployment.
Supports the subset those definitions use: Task, Choice, Pass, Wait, Parameters
with ``.$`` JSONPath, ``ResultPath``, ``$$.Execution.Name``, ``IsPresent``,
``BooleanEquals`` and ``StringEquals``.
"""
from __future__ import annotations

import copy
import re
import time
from typing import Any, Callable

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


class Execution:
    """Runs one state machine, recording per-state history for status polling."""

    def __init__(
        self,
        definition: dict[str, Any],
        input_: dict[str, Any],
        resources: dict[str, Callable[[dict[str, Any]], dict[str, Any]]],
        name: str,
        wait_cap_seconds: float = 2.0,
    ) -> None:
        self.definition = definition
        self.input = input_
        self.resources = resources
        self.name = name
        self.wait_cap = wait_cap_seconds
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
                    result = fn(params)
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
