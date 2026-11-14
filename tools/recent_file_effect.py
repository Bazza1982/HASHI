"""Bounded receipt adapter for managed writes; no watcher or inferred authorship."""
from __future__ import annotations

import hashlib
import re


def _version(path):
    try:
        stat = path.stat()
        if not path.is_file():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.st_size <= 2 * 1024 * 1024 else None
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, digest)
    except FileNotFoundError:
        return ("absent",)
    except OSError:
        return None


def _targets(registry, name, args):
    from tools.builtins import _resolve_path
    if name in {"file_write", "apply_patch"}:
        raw = [args.get("path")]
    elif name in {"shell", "bash"}:
        operand = r"""(?:"([^"\r\n]+)"|'([^'\r\n]+)'|([^\s;|&<>]+))"""
        command = str(args.get("command") or "")
        raw = []
        for pattern in (r'(?<![<\d])>{1,2}\s*' + operand,
                        r'(?i)-(?:LiteralPath|FilePath)\s+' + operand):
            raw.extend(next((p for p in match.groups() if p), "") for match in re.finditer(pattern, command))
    else:
        return []
    result = []
    for value in raw[:8]:
        if not isinstance(value, str) or not value or any(c in value for c in (chr(0), "$", chr(96))):
            continue
        try:
            path = _resolve_path(value, registry.access_roots, registry.workspace_dir)
            result.append((value, path, _version(path)))
        except (OSError, ValueError):
            continue
    return result


async def dispatch_with_file_receipt(registry, name, args, *, tool_call_id):
    from tools.builtins import BuiltinExecutionResult
    import asyncio
    observed = await asyncio.to_thread(_targets, registry, name, args)
    result = await registry._dispatch(name, args, tool_call_id=tool_call_id)
    if not observed:
        return result
    output = result.output if isinstance(result, BuiltinExecutionResult) else str(result)
    details = dict(result.details) if isinstance(result, BuiltinExecutionResult) else {}
    if output.startswith("Error") or details.get("exit_code", 0) not in {0, None}:
        return result
    observations = []
    for raw, path, before in observed:
        after = await asyncio.to_thread(_version, path)
        if after is None or len(after) != 5:
            continue
        if name in {"file_write", "apply_patch"}:
            verified = output.startswith("OK") and after[4] is not None and before is not None
            if name == "file_write":
                import os
                expected = str(args.get("content") or "").replace("\n", os.linesep).encode("utf-8")
                verified = verified and hashlib.sha256(expected).hexdigest() == after[4]
            action = "created" if before == ("absent",) else "modified" if before and before[-1] != after[4] else "touched"
            details["file_effect"] = {"path": raw, "verified": verified, "action": action if verified else "touched"}
        elif before != after:
            observations.append({"path": str(path), "action": "touched", "evidence": "interval_observation"})
    if observations:
        details["file_observations"] = observations
    return BuiltinExecutionResult(output, details)
