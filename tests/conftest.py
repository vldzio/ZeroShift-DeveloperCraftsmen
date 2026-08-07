"""Pytest configuration.

Adds ``lambdas/`` to sys.path so tests can import the Lambda code using the
same paths the Lambda runtime uses (e.g. ``from shared.llm_client import ...``
rather than ``from lambdas.shared.llm_client import ...``). Also points the
fixture loader at the repo-level ``fixtures/`` directory.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LAMBDAS_DIR = _REPO_ROOT / "lambdas"

if str(_LAMBDAS_DIR) not in sys.path:
    sys.path.insert(0, str(_LAMBDAS_DIR))

os.environ.setdefault("ZEROSHIFT_FIXTURES_DIR", str(_REPO_ROOT / "fixtures"))
os.environ.setdefault("ZEROSHIFT_FIXTURE_MODE", "true")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
