"""Write frontend/config.js from the ZEROSHIFT_API_URL environment variable (used by the Vercel build)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "frontend" / "config.js"


def main() -> int:
    url = os.environ.get("ZEROSHIFT_API_URL", "").strip().rstrip("/")
    if not url:
        print("ZEROSHIFT_API_URL not set; leaving frontend/config.js unchanged.")
        return 0
    CONFIG.write_text(
        "// Generated at build time from ZEROSHIFT_API_URL.\n"
        f"window.ZEROSHIFT_CONFIG = {{ API_URL: {json.dumps(url)} }};\n",
        encoding="utf-8",
    )
    print(f"Wrote {CONFIG} -> {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
