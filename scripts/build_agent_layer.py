"""Build the Lambda Layer that hosts LangGraph + langchain-aws for the agentic layer.

Runs ``pip install --target build/agent-layer/python/`` for the pinned set of
packages. Layer must have its Python content under ``python/`` at the root —
that's the Lambda Layer convention for Python runtimes.

Usage:
    python scripts/build_agent_layer.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAYER_DIR = REPO_ROOT / "build" / "agent-layer"
PYTHON_DIR = LAYER_DIR / "python"

REQUIREMENTS = [
    "langgraph>=0.2.0",
    "langchain-aws>=0.2.0",
    "langchain-core>=0.3.0",
]


def main() -> int:
    if LAYER_DIR.exists():
        shutil.rmtree(LAYER_DIR)
    PYTHON_DIR.mkdir(parents=True)

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--target",
        str(PYTHON_DIR),
        *REQUIREMENTS,
    ]
    print("Installing agent-layer dependencies into", PYTHON_DIR)
    print(" ", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        return result.returncode

    # Strip caches to keep the layer under the Lambda size limit.
    for pattern in ("__pycache__", "*.dist-info", "*.egg-info"):
        for path in PYTHON_DIR.rglob(pattern):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file():
                path.unlink(missing_ok=True)

    file_count = sum(1 for _ in PYTHON_DIR.rglob("*") if _.is_file())
    print(f"Layer ready: {file_count} files at {LAYER_DIR.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
