"""Closed offline Python script contract for managed execution.

Real files are read-only input snapshots. Outputs are named temporary artifacts;
saving business files remains an independently checked file_write/apply_patch.
"""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re

from orchestrator.permissions.models import normalize_scope

TOOL_NAME = "run_script"
CAPABILITY = "process.python.isolated"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


def leaf(value):
    if (not isinstance(value, str) or _NAME.fullmatch(value) is None or value.endswith(".")
            or value.casefold().split(".")[0] in {"con", "prn", "aux", "nul",
                                                 *("com" + str(n) for n in range(10)),
                                                 *("lpt" + str(n) for n in range(10))}):
        raise ValueError("use a safe leaf name for each input/output")
    return value


def validate(arguments):
    if (not isinstance(arguments, dict) or set(arguments) - {"script", "inputs", "outputs"}
            or not isinstance(arguments.get("script"), str)
            or not arguments["script"].strip() or len(arguments["script"].encode("utf-8")) > 128 * 1024):
        raise ValueError("a bounded offline Python script is required")
    inputs, outputs = arguments.get("inputs", []), arguments.get("outputs", [])
    if not isinstance(inputs, list) or not isinstance(outputs, list) or len(inputs) > 16 or len(outputs) > 16:
        raise ValueError("declare at most 16 input snapshots and 16 temporary outputs")
    names = set()
    for item in inputs:
        if (not isinstance(item, dict) or set(item) != {"path", "name"}
                or not isinstance(item["path"], str) or not item["path"]):
            raise ValueError("script inputs need an exact path and a leaf name")
        name = leaf(item["name"]).casefold()
        if name in names:
            raise ValueError("script input names must be distinct")
        names.add(name)
    names = set()
    for item in outputs:
        name = leaf(item).casefold()
        if name in names:
            raise ValueError("script output names must be distinct")
        names.add(name)
    # Reject syntax errors before an approval or native resource is created.
    try:
        compile(arguments["script"], "<managed-script>", "exec")
    except (SyntaxError, ValueError) as error:
        raise ValueError("the script is not valid Python") from error
    return {"script": arguments["script"], "inputs": [dict(item) for item in inputs], "outputs": list(outputs)}


def scopes(arguments, *, cwd):
    plan = validate(arguments)
    return (
        normalize_scope("capability", CAPABILITY, ["execute"], cwd=cwd),
        *(normalize_scope("file", item["path"], ["read"], cwd=cwd) for item in plan["inputs"]),
    )


def digest(plan):
    return sha256(json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def definition():
    return {"type": "function", "function": {
        "name": TOOL_NAME,
        "description": (
            "Run offline Python in an isolated process with declared read-only input snapshots. "
            "Read inputs/<name> and write outputs/<name>; named outputs and bounded stdout are returned. "
            "Declare at most 4 MiB of input snapshots; returned temporary artifacts total at most 512 KiB. "
            "No network, credentials, subprocesses, interactive input or direct business filesystem writes. "
            "Use approved HTTP tools for network actions, then file_write/apply_patch to save final results."
        ),
        "parameters": {"type": "object", "additionalProperties": False, "required": ["script"], "properties": {
            "script": {"type": "string", "maxLength": 131072},
            "inputs": {"type": "array", "maxItems": 16, "items": {
                "type": "object", "additionalProperties": False, "required": ["path", "name"],
                "properties": {"path": {"type": "string"}, "name": {"type": "string"}},
            }},
            "outputs": {"type": "array", "maxItems": 16, "items": {"type": "string"}},
        }},
    }}


def paths(root):
    root = Path(root)
    return root / "task.py", root / "inputs", root / "outputs", root / "stdout.txt", root / "stderr.txt"
