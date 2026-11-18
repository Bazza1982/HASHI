"""Managed subprocess for cancellable workspace regex fallback."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.her_verification import _python_workspace_search


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    try:
        request = json.load(sys.stdin)
        output, matches, coverage = _python_workspace_search(
            Path(request["path"]), query=request["query"], regex=True,
            collect_all=request.get("collect_all") is True,
        )
        response = {"output": output, "matches": matches, "coverage": coverage}
    except re.error as exc:
        response = {"error": "invalid_regex", "message": str(exc)}
    except Exception as exc:
        response = {"error": "worker_failure", "message": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(response, ensure_ascii=True, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
