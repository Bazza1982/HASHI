"""Fixed trusted file helper; it never accepts Python, commands or a tool name.

The supervisor owns the qualified helper image, immutable plan and inherited
held handles. The helper has no receipt-signing key, approval method, model
credential, generic RPC endpoint or shell dispatch.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sys

# -I deliberately removes the current directory and user PYTHONPATH.
# The installed entrypoint may import only its own qualified package root.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchestrator.permissions.windows_file_binding import (
    BoundWindowsCreate, BoundWindowsRead, BoundWindowsWrite, _Windows,
)


def _save(path, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    with open(path, "xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def run(plan):
    fields = {"format", "action", "handles", "directory", "name", "data", "result"}
    if (not isinstance(plan, dict) or set(plan) != fields or plan["format"] != "hashi-file-helper-v1"
            or plan["action"] not in {"read", "write", "create"}
            or not isinstance(plan["handles"], list) or len(plan["handles"]) != 1
            or type(plan["handles"][0]) is not int or plan["handles"][0] <= 0):
        raise ValueError("invalid trusted file plan")
    api = _Windows()
    constructors = {"read": BoundWindowsRead, "write": BoundWindowsWrite}
    if plan["action"] == "create":
        bound = BoundWindowsCreate(api, plan["handles"], plan["directory"], plan["name"])
    else:
        bound = constructors[plan["action"]](api, plan["handles"])
    with bound:
        if plan["action"] == "read":
            data = bound.read(max_bytes=16 * 1024 * 1024)
            return {"outcome": "succeeded", "data": base64.b64encode(data).decode("ascii")}
        data = base64.b64decode(plan["data"], validate=True)
        if len(data) > 1024 * 1024:
            raise ValueError("file helper payload is too large")
        bound.commit(data)
        return {"outcome": "succeeded", "data": ""}


def main():
    if len(sys.argv) != 3 or len(sys.argv[2]) != 64:
        raise SystemExit("a fixed trusted plan and digest are required")
    path = Path(sys.argv[1])
    payload = path.read_bytes()
    if len(payload) > 2 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != sys.argv[2]:
        raise SystemExit("trusted file plan integrity failure")
    plan = json.loads(payload)
    try:
        result = run(plan)
    except BaseException as error:
        result = {"outcome": "unknown", "error_type": type(error).__name__}
    _save(plan["result"], result)


if __name__ == "__main__":
    main()
