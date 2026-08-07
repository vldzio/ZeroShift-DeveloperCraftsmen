"""Assemble the Lambda deployment package.

Copies ``lambdas/*`` and ``fixtures/`` into ``build/lambda-package/`` and strips
``__pycache__`` / ``*.pyc`` artifacts. Cross-platform (Windows, Linux, macOS)
because it uses ``shutil`` instead of Unix ``rm``/``cp``/``find``.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD = REPO_ROOT / "build" / "lambda-package"
LAMBDAS = REPO_ROOT / "lambdas"
FIXTURES = REPO_ROOT / "fixtures"


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [n for n in names if n == "__pycache__" or n.endswith(".pyc")]


def main() -> int:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True, exist_ok=True)

    for item in LAMBDAS.iterdir():
        dst = BUILD / item.name
        if item.is_dir():
            shutil.copytree(item, dst, ignore=_ignore)
        else:
            shutil.copy2(item, dst)

    shutil.copytree(FIXTURES, BUILD / "fixtures", ignore=_ignore)

    files = sum(1 for p in BUILD.rglob("*") if p.is_file())
    rel = BUILD.relative_to(REPO_ROOT).as_posix()
    print(f"Package ready: {files} files in {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
