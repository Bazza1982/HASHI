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
    BoundWindowsCreate, BoundWindowsRead, BoundWindowsWrite, BoundWindowsDirectory, _Windows,
)


class FilePlanRejected(ValueError):
    """A fixed helper rejected input before any file mutation."""


def _save(path, value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    with open(path, "xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def run(plan):
    fields = {"format", "action", "handles", "directory", "name", "data", "result"}
    if (not isinstance(plan, dict) or set(plan) != fields or plan["format"] != "hashi-file-helper-v1"
            or plan["action"] not in {"read", "write", "create", "list", "search", "patch_create", "patch_write", "http", "script"}
            or not isinstance(plan["handles"], list) or not 0 <= len(plan["handles"]) <= 32
            or any(type(handle) is not int or handle <= 0 for handle in plan["handles"])):
        raise ValueError("invalid trusted file plan")
    if plan["action"] == "script":
        from orchestrator.permissions.script_helper import run as run_script

        return run_script(json.loads(base64.b64decode(plan["data"], validate=True)), plan["handles"])
    if plan["action"] == "http":
        if plan["handles"]:
            raise ValueError("a network helper must not receive filesystem handles")
        import asyncio
        from orchestrator.permissions.network_worker import run as run_network

        result = asyncio.run(run_network(json.loads(base64.b64decode(plan["data"], validate=True))))
        return {"outcome": "succeeded", "data": base64.b64encode(result.encode("utf-8")).decode("ascii")}
    api = _Windows()
    from orchestrator.permissions.file_operations import list_held, search_held, strict_patch

    if plan["action"] in {"list", "search"}:
        options = json.loads(base64.b64decode(plan["data"], validate=True))
        bindings = [BoundWindowsDirectory(api, [handle]) for handle in plan["handles"]]
        try:
            text = (list_held(bindings[0], options["arguments"], options["protected"])
                    if plan["action"] == "list" else search_held(
                        bindings, options["roots"], options["arguments"], options["protected"]))
            return {"outcome": "succeeded", "data": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        finally:
            for bound in bindings:
                bound.close()
    if len(plan["handles"]) != 1:
        raise ValueError("a file action requires one held object")
    if plan["action"] in {"patch_write", "patch_create"}:
        bound = (BoundWindowsWrite(api, plan["handles"]) if plan["action"] == "patch_write"
                 else BoundWindowsCreate(api, plan["handles"], plan["directory"], plan["name"]))
        with bound:
            original = (BoundWindowsRead.read(bound, max_bytes=1024 * 1024).decode("utf-8")
                        if plan["action"] == "patch_write" else "")
            patch = base64.b64decode(plan["data"], validate=True).decode("utf-8")
            try:
                result = strict_patch(original, patch)
            except ValueError as error:
                raise FilePlanRejected("the patch does not match the held file") from error
            bound.commit(result)
            return {"outcome": "succeeded", "data": base64.b64encode(b"OK: patch applied").decode("ascii")}
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
        from orchestrator.permissions.network_worker import RequestNotSent
        from orchestrator.permissions.script_helper import ScriptNotStarted

        if isinstance(error, (FilePlanRejected, RequestNotSent, ScriptNotStarted)) or plan.get("action") in {"read", "list", "search"}:
            result = {"outcome": "failed", "data": base64.b64encode(
                b"Error: the fixed operation was rejected before its business mutation; choose an allowed alternative.").decode("ascii")}
        else:
            result = {"outcome": "unknown", "error_type": type(error).__name__}
    _save(plan["result"], result)


if __name__ == "__main__":
    main()
