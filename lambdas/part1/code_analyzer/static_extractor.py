"""Static AST extraction of AWS SDK calls from Python source.

Walks the abstract syntax tree, identifies bindings of the form
``boto3.client("<service>")`` and ``boto3.resource("<service>")``, then finds
method calls on those bindings and translates them into IAM action strings.

Deterministic and fast — the trade-off is that dynamic dispatch (e.g.
``getattr(client, method_name)()`` or clients returned from factory functions)
is invisible to the extractor. ``llm_augmenter`` picks up that slack.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class ExtractedCall:
    service: str  # e.g. "s3"
    method: str  # snake_case as it appears in code
    iam_action: str  # e.g. "s3:PutObject"
    line: int
    via_resource: bool = False


def extract(source: str) -> list[ExtractedCall]:
    """Return the deduplicated set of IAM actions inferred from the source."""
    tree = ast.parse(source)
    resolver = _Resolver()
    resolver.visit(tree)
    calls: dict[tuple[str, str, int], ExtractedCall] = {}
    for call in resolver.calls:
        key = (call.service, call.method, call.line)
        calls[key] = call
    return sorted(calls.values(), key=lambda c: (c.iam_action, c.line))


def _to_camel(snake: str) -> str:
    """Convert boto3 snake_case method names to IAM CamelCase action names.

    Preserves numeric suffixes verbatim (list_objects_v2 -> ListObjectsV2).
    """
    parts = snake.split("_")
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.isdigit():
            out.append(part)
        elif re.fullmatch(r"v\d+", part, re.IGNORECASE):
            out.append("V" + part[1:])
        else:
            out.append(part[0].upper() + part[1:])
    return "".join(out)


class _Resolver(ast.NodeVisitor):
    """Two-pass resolver: bindings first, then calls on those bindings."""

    # boto3 resource sub-methods that produce a sub-resource of the same
    # service (not an IAM action). Extracted from boto3 resource specs.
    SUB_RESOURCE_METHODS = {
        "Table", "Object", "Bucket", "Item", "Queue", "Topic", "Message",
        "MultipartUpload", "MultipartUploadPart", "ObjectSummary", "ObjectVersion",
    }

    def __init__(self) -> None:
        # var_name -> ("client"|"resource", service)
        self.bindings: dict[str, tuple[str, str]] = {}
        # attr chain -> service (for self.X and cls.X within a class)
        self.attr_bindings: dict[str, str] = {}
        self.calls: list[ExtractedCall] = []

    # ---- binding discovery ----

    def visit_Assign(self, node: ast.Assign) -> None:
        service_info = _extract_boto3_service(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name) and service_info:
                self.bindings[target.id] = service_info
            elif isinstance(target, ast.Attribute) and service_info:
                chain = _attr_chain(target)
                if chain:
                    self.attr_bindings[chain] = service_info[1]
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is None:
            return self.generic_visit(node)
        service_info = _extract_boto3_service(node.value)
        if isinstance(node.target, ast.Name) and service_info:
            self.bindings[node.target.id] = service_info
        elif isinstance(node.target, ast.Attribute) and service_info:
            chain = _attr_chain(node.target)
            if chain:
                self.attr_bindings[chain] = service_info[1]
        self.generic_visit(node)

    # ---- call discovery ----

    def visit_Call(self, node: ast.Call) -> None:
        service, method, via_resource = self._resolve_call(node)
        if service and method and method not in self.SUB_RESOURCE_METHODS:
            iam_action = f"{service}:{_to_camel(method)}"
            self.calls.append(
                ExtractedCall(
                    service=service,
                    method=method,
                    iam_action=iam_action,
                    line=node.lineno,
                    via_resource=via_resource,
                )
            )
        self.generic_visit(node)

    def _resolve_call(self, node: ast.Call) -> tuple[str | None, str | None, bool]:
        """Return (service, method, via_resource) or (None, None, False)."""
        if not isinstance(node.func, ast.Attribute):
            return None, None, False
        method_name = node.func.attr

        # 1. Direct call on a bound Name: e.g. s3.put_object(...) where s3 = boto3.client("s3")
        if isinstance(node.func.value, ast.Name):
            binding = self.bindings.get(node.func.value.id)
            if binding:
                kind, service = binding
                return service, method_name, kind == "resource"

        # 2. Direct call on an Attribute we tracked: e.g. self.table.put_item(...)
        chain = _attr_chain(node.func.value) if isinstance(node.func.value, ast.Attribute) else None
        if chain and chain in self.attr_bindings:
            return self.attr_bindings[chain], method_name, True

        # 3. Chained call like RESULTS_TABLE.put_item(...) where
        #    RESULTS_TABLE = ddb.Table("x") and ddb = boto3.resource("dynamodb")
        #    (Handled via the visit_Assign hook that binds RESULTS_TABLE to
        #    the dynamodb resource — see below.)

        return None, None, False


def _attr_chain(node: ast.AST) -> str | None:
    """Convert ``self.foo.bar`` (an Attribute node) to the string ``self.foo.bar``."""
    parts: list[str] = []
    current: Any = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    else:
        return None
    return ".".join(reversed(parts))


def _extract_boto3_service(node: ast.AST) -> tuple[str, str] | None:
    """If ``node`` is ``boto3.client("X")``, ``boto3.resource("X")``, or a chained
    call on such an expression (e.g. ``boto3.resource("dynamodb").Table("t")``),
    return ``(kind, service)``. Otherwise None."""
    if not isinstance(node, ast.Call):
        return None
    # Chained: boto3.resource("X").Table("t") -> unwrap to the inner Call.
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Call):
        inner = _extract_boto3_service(node.func.value)
        if inner:
            return inner
    if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
        return None
    if node.func.value.id != "boto3":
        return None
    kind = node.func.attr
    if kind not in ("client", "resource"):
        return None
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return None
    return kind, node.args[0].value


# ---- Additional helper: bind sub-resource variables to the same service ----

def _resolve_module_level_subresources(tree: ast.AST, resolver: _Resolver) -> None:
    """Second pass: track top-level bindings like ``RESULTS_TABLE = ddb.Table("x")``
    where ``ddb`` is a known resource binding — bind the sub-resource to the same
    service so subsequent calls (RESULTS_TABLE.put_item) are attributed."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        # sub-resource pattern: <known_var>.Method(...)
        if isinstance(node.value.func, ast.Attribute) and isinstance(node.value.func.value, ast.Name):
            parent_name = node.value.func.value.id
            binding = resolver.bindings.get(parent_name)
            if binding and binding[0] == "resource":
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        resolver.bindings[target.id] = ("resource", binding[1])
                    elif isinstance(target, ast.Attribute):
                        chain = _attr_chain(target)
                        if chain:
                            resolver.attr_bindings[chain] = binding[1]


def extract_actions(source: str) -> list[str]:
    """Convenience wrapper: return sorted unique IAM action strings."""
    tree = ast.parse(source)
    resolver = _Resolver()
    resolver.visit(tree)
    _resolve_module_level_subresources(tree, resolver)
    # Re-visit calls now that sub-resource bindings are populated.
    resolver.calls = []
    resolver.visit(tree)
    return sorted({c.iam_action for c in resolver.calls if c.method not in _Resolver.SUB_RESOURCE_METHODS})


def extract_full(source: str) -> list[ExtractedCall]:
    """Return full ExtractedCall records after the two-pass resolution."""
    tree = ast.parse(source)
    resolver = _Resolver()
    resolver.visit(tree)
    _resolve_module_level_subresources(tree, resolver)
    resolver.calls = []
    resolver.visit(tree)
    seen: set[str] = set()
    out: list[ExtractedCall] = []
    for call in sorted(resolver.calls, key=lambda c: (c.iam_action, c.line)):
        if call.iam_action in seen:
            continue
        seen.add(call.iam_action)
        out.append(call)
    return out
