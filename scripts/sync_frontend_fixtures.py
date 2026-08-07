"""Sync denial-event fixtures into ``frontend/fixtures/`` for the browser UI.

Cross-platform replacement for a ``cp`` shell command.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "fixtures" / "denial_events"
DST = REPO_ROOT / "frontend" / "fixtures"


def main() -> int:
    DST.mkdir(parents=True, exist_ok=True)
    for src_file in sorted(SRC.glob("*.json")):
        shutil.copy2(src_file, DST / src_file.name)

    code_src = REPO_ROOT / "fixtures" / "code_samples"
    code_dst = REPO_ROOT / "frontend" / "fixtures" / "code_samples"
    if code_src.exists():
        code_dst.mkdir(parents=True, exist_ok=True)
        for src_file in sorted(code_src.glob("*.py")):
            shutil.copy2(src_file, code_dst / src_file.name)

    denial = [p.name for p in sorted(DST.glob("*.json"))]
    code = [p.name for p in sorted((code_dst if code_src.exists() else DST).glob("*.py"))] if code_src.exists() else []
    print(f"Synced {len(denial)} denial fixture(s) and {len(code)} code sample(s) to frontend/fixtures/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
