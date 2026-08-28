"""
Built-in tool executor implementations for HASHI V2.2.

Each function is a standalone async executor. They are called by ToolRegistry.
All file operations are sandboxed to access_root.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode

import aiohttp

from tools.workbench_client import request_workbench_json, workbench_endpoint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _access_roots(value: Path | Sequence[Path]) -> tuple[Path, ...]:
    raw_roots = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Path)) else (value,)
    roots: list[Path] = []
    for raw in raw_roots:
        root = Path(raw).expanduser().resolve()
        if root not in roots:
            roots.append(root)
    if not roots:
        raise ValueError("no allowed access roots are configured")
    return tuple(roots)


def _resolve_path(
    raw_path: str,
    access_root: Path | Sequence[Path],
    workspace_dir: Path,
) -> Path:
    """
    Resolve a user-supplied path.
    - Absolute paths are kept as-is but verified against access_root.
    - Relative paths are resolved from workspace_dir.
    Raises ValueError if the resolved path escapes access_root.
    """
    p = Path(raw_path)
    if not p.is_absolute():
        p = (workspace_dir / p).resolve()
    else:
        p = p.resolve()

    access_roots = _access_roots(access_root)
    if not any(p == root or p.is_relative_to(root) for root in access_roots):
        rendered = ", ".join(str(root) for root in access_roots)
        raise ValueError(
            f"Path '{p}' is outside the allowed access scopes [{rendered}]"
        )
    return p


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

_BASH_CLEANUP_GRACE_SECONDS = 1.0


@dataclass(frozen=True)
class BuiltinExecutionResult:
    """Builtin output plus deterministic, machine-readable execution facts."""

    output: str
    details: Mapping[str, Any] = field(default_factory=dict)


def _positive_seconds(value: object, *, label: str) -> tuple[float | None, str | None]:
    if isinstance(value, bool):
        return None, f"Error: {label} must be a positive number of seconds"
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None, f"Error: {label} must be a positive number of seconds"
    if not math.isfinite(seconds) or seconds <= 0:
        return None, f"Error: {label} must be a positive number of seconds"
    return seconds, None


def _bash_timeout(
    args: Mapping[str, Any], timeout_max: float | None
) -> tuple[float | None, dict[str, Any], str | None]:
    """Resolve only an explicitly requested timeout and optional operator cap."""

    if "timeout" not in args or args.get("timeout") is None:
        return None, {"timeout_explicit": False}, None
    requested, error = _positive_seconds(args.get("timeout"), label="timeout")
    if error is not None:
        return None, {"timeout_explicit": True}, error
    assert requested is not None
    effective = requested
    capped = False
    configured_max = None
    if timeout_max is not None:
        configured_max, error = _positive_seconds(
            timeout_max, label="configured bash timeout_max"
        )
        if error is not None:
            return None, {"timeout_explicit": True}, error
        assert configured_max is not None
        effective = min(requested, configured_max)
        capped = effective < requested
    return (
        effective,
        {
            "timeout_explicit": True,
            "timeout_requested_s": requested,
            "timeout_effective_s": effective,
            "timeout_capped": capped,
            "timeout_max_s": configured_max,
        },
        None,
    )


def _bash_process_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return {"creationflags": creation_flag} if creation_flag else {}


def _bash_process_group_id(proc: asyncio.subprocess.Process) -> int | None:
    if os.name != "posix" or not proc.pid:
        return None
    # start_new_session=True makes the spawned shell both session and process
    # group leader.  Refuse any unexpected group identity rather than risk
    # signalling HASHI's own group.
    try:
        pgid = os.getpgid(proc.pid)
    except (OSError, ProcessLookupError):
        return None
    if pgid != proc.pid or pgid == os.getpgrp():
        return None
    return pgid


def _bash_group_alive(pgid: int | None) -> bool:
    if os.name != "posix" or pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_for_bash_cleanup(
    communicate_task: asyncio.Task,
    *,
    pgid: int | None,
    timeout: float,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(timeout))
    while True:
        communication_done = communicate_task.done()
        group_gone = not _bash_group_alive(pgid)
        if communication_done and group_gone:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.02, remaining))


async def _cleanup_bash_process(
    proc: asyncio.subprocess.Process,
    communicate_task: asyncio.Task,
    *,
    pgid: int | None,
    grace_seconds: float = _BASH_CLEANUP_GRACE_SECONDS,
) -> dict[str, Any]:
    """Terminate and reap only the exact foreground process group."""

    scope = "process_group" if pgid is not None else "process_only_fallback"
    forced = False
    errors: list[str] = []

    def signal_process(*, force: bool) -> None:
        nonlocal forced
        forced = forced or force
        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
            elif proc.returncode is None:
                proc.kill() if force else proc.terminate()
        except ProcessLookupError:
            return
        except Exception as exc:  # cleanup truth is reported, never hidden
            errors.append(f"{type(exc).__name__}: {exc}")

    signal_process(force=False)
    complete = await _wait_for_bash_cleanup(
        communicate_task,
        pgid=pgid,
        timeout=grace_seconds,
    )
    if not complete:
        signal_process(force=True)
        complete = await _wait_for_bash_cleanup(
            communicate_task,
            pgid=pgid,
            timeout=grace_seconds,
        )

    if communicate_task.done():
        await asyncio.gather(communicate_task, return_exceptions=True)
    else:
        communicate_task.cancel()
        await asyncio.gather(communicate_task, return_exceptions=True)
    if proc.returncode is None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    group_alive = _bash_group_alive(pgid)
    reaped = proc.returncode is not None
    success = bool(reaped and not group_alive and not errors)
    return {
        "status": (
            "force_killed" if success and forced else
            "terminated" if success else
            "cleanup_failed"
        ),
        "scope": scope,
        "pgid": pgid,
        "forced": forced,
        "process_reaped": reaped,
        "group_alive": group_alive,
        "errors": errors,
    }


async def _shield_bash_cleanup(cleanup) -> dict[str, Any]:
    """Finish the short cleanup barrier even if cancellation is repeated."""

    task = asyncio.create_task(cleanup)
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return await task


def _seconds_label(value: float) -> str:
    return f"{value:g}"

async def execute_bash(
    args: dict,
    workspace_dir: Path,
    timeout_max: float | None = None,
    blocked_patterns: Optional[list[str]] = None,
) -> str | BuiltinExecutionResult:
    command = str(args.get("command", "")).strip()
    if not command:
        return "Error: no command provided"

    timeout, timeout_details, timeout_error = _bash_timeout(args, timeout_max)
    if timeout_error is not None:
        return BuiltinExecutionResult(timeout_error, timeout_details)

    # Check blocked patterns
    if blocked_patterns:
        for pattern in blocked_patterns:
            if re.search(pattern, command):
                return f"Error: command blocked by policy (matched: {pattern!r})"

    proc: asyncio.subprocess.Process | None = None
    communicate_task: asyncio.Task | None = None
    pgid: int | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace_dir),
            **_bash_process_kwargs(),
        )
        pgid = _bash_process_group_id(proc)
        communicate_task = asyncio.create_task(proc.communicate())
        if timeout is None:
            stdout, stderr = await asyncio.shield(communicate_task)
        else:
            done, _pending = await asyncio.wait(
                {communicate_task}, timeout=timeout
            )
            if communicate_task not in done:
                cleanup = await _shield_bash_cleanup(
                    _cleanup_bash_process(
                        proc,
                        communicate_task,
                        pgid=pgid,
                    )
                )
                return BuiltinExecutionResult(
                    f"Error: command timed out after {_seconds_label(timeout)}s",
                    {
                        **timeout_details,
                        "foreground_cleanup": cleanup,
                    },
                )
            stdout, stderr = await communicate_task

        output_parts = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            output_parts.append(f"[stderr]\n{stderr.decode('utf-8', errors='replace')}")

        result = "\n".join(output_parts).strip()
        if proc.returncode != 0:
            result = f"[exit code {proc.returncode}]\n{result}" if result else f"[exit code {proc.returncode}]"

        # Truncate very long output
        if len(result) > 20000:
            result = result[:20000] + "\n...[output truncated]"

        if _bash_group_alive(pgid):
            completion_cleanup = await _shield_bash_cleanup(
                _cleanup_bash_process(proc, communicate_task, pgid=pgid)
            )
        else:
            completion_cleanup = {
                "status": "normal_completion",
                "scope": "process_group" if pgid is not None else "process_only_fallback",
                "pgid": pgid,
                "forced": False,
                "process_reaped": proc.returncode is not None,
                "group_alive": False,
                "errors": [],
            }
        return BuiltinExecutionResult(
            result or "(no output)",
            {
                **timeout_details,
                "foreground_cleanup": completion_cleanup,
            },
        )

    except asyncio.CancelledError as exc:
        cleanup = None
        if proc is not None and communicate_task is not None:
            cleanup = await _shield_bash_cleanup(
                _cleanup_bash_process(proc, communicate_task, pgid=pgid)
            )
        details = {
            **timeout_details,
            "foreground_cleanup": cleanup or {
                "status": "not_started",
                "process_reaped": True,
                "errors": [],
            },
        }
        setattr(exc, "hashi_tool_details", details)
        raise
    except Exception as exc:
        cleanup = None
        if proc is not None and communicate_task is not None:
            cleanup = await _shield_bash_cleanup(
                _cleanup_bash_process(proc, communicate_task, pgid=pgid)
            )
        return BuiltinExecutionResult(
            f"Error executing command: {exc}",
            {
                **timeout_details,
                "foreground_cleanup": cleanup or {
                    "status": "not_started",
                    "process_reaped": True,
                    "errors": [],
                },
            },
        )


# ---------------------------------------------------------------------------
# file_read
# ---------------------------------------------------------------------------

async def execute_file_read(
    args: dict,
    access_root: Path | Sequence[Path],
    workspace_dir: Path,
) -> str:
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: no path provided"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as e:
        return f"Error: {e}"

    if not path.exists():
        return f"Error: file not found: {path}"
    if not path.is_file():
        return f"Error: path is not a file: {path}"

    offset = max(1, int(args.get("offset", 1)))
    limit = int(args.get("limit", 500))

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        selected = lines[offset - 1 : offset - 1 + limit]
        content = "".join(selected)

        header = f"[{path}]"
        if offset > 1 or len(lines) > limit:
            header += f" lines {offset}-{offset + len(selected) - 1} of {len(lines)}"

        return f"{header}\n{content}"
    except Exception as e:
        return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# file_write
# ---------------------------------------------------------------------------

async def execute_file_write(
    args: dict,
    access_root: Path | Sequence[Path],
    workspace_dir: Path,
    max_file_size_kb: int = 1024,
) -> str:
    raw_path = args.get("path", "")
    content = args.get("content", "")

    if not raw_path:
        return "Error: no path provided"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as e:
        return f"Error: {e}"

    if len(content.encode("utf-8")) > max_file_size_kb * 1024:
        return f"Error: content exceeds max file size of {max_file_size_kb}KB"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} characters to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# web_search (Brave Search API)
# ---------------------------------------------------------------------------

async def execute_web_search(
    args: dict,
    brave_api_key: Optional[str],
) -> str:
    if not brave_api_key:
        return "Error: brave_api_key not configured in secrets.json"

    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: no query provided"

    count = min(int(args.get("count", 5)), 20)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": brave_api_key,
                },
                params={"q": query, "count": count},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("web", {}).get("results", [])
        if not results:
            return f"No results found for: {query}"

        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("description", "")
            lines.append(f"{i}. {title}\n   {url}\n   {snippet}\n")

        return "\n".join(lines)

    except Exception as e:
        return f"Error during web search: {e}"


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------

async def execute_file_list(
    args: dict,
    access_root: Path | Sequence[Path],
    workspace_dir: Path,
) -> str:
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: no path provided"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as e:
        return f"Error: {e}"

    if not path.exists():
        return f"Error: path not found: {path}"
    if not path.is_dir():
        return f"Error: path is not a directory: {path}"

    pattern = args.get("pattern", "*")
    recursive = bool(args.get("recursive", False))

    try:
        import fnmatch
        entries = []
        if recursive:
            all_paths = sorted(path.rglob(pattern))
        else:
            all_paths = sorted(path.glob(pattern))

        for p in all_paths:
            rel = p.relative_to(path)
            kind = "dir" if p.is_dir() else "file"
            try:
                size = p.stat().st_size if p.is_file() else 0
                size_str = f"{size:,}B" if size < 1024 else f"{size//1024:,}KB"
            except Exception:
                size_str = "?"
            entries.append(f"{'[dir] ' if kind=='dir' else '      '}{rel}  {size_str if kind=='file' else ''}")

        if not entries:
            return f"No entries found in {path} (pattern: {pattern})"

        header = f"[{path}]  {len(entries)} items"
        return header + "\n" + "\n".join(entries)
    except Exception as e:
        return f"Error listing directory: {e}"


async def execute_apply_patch(
    args: dict,
    access_root: Path | Sequence[Path],
    workspace_dir: Path,
) -> str:
    raw_path = args.get("path", "")
    patch_str = args.get("patch", "")

    if not raw_path:
        return "Error: no path provided"
    if not patch_str:
        return "Error: no patch provided"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as e:
        return f"Error: {e}"

    if not path.exists():
        return f"Error: file not found: {path}"

    try:
        import subprocess
        result = subprocess.run(
            ["patch", "--dry-run", "-u", str(path)],
            input=patch_str.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            return f"Error: patch rejected (dry-run):\n{result.stderr.decode()}"

        result = subprocess.run(
            ["patch", "-u", str(path)],
            input=patch_str.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            return f"Error: patch failed:\n{result.stderr.decode()}"

        out = result.stdout.decode().strip()
        return f"OK: patch applied to {path}" + (f"\n{out}" if out else "")
    except FileNotFoundError:
        return "Error: 'patch' command not found on this system"
    except Exception as e:
        return f"Error applying patch: {e}"


async def execute_process_list(args: dict) -> str:
    filter_str = args.get("filter", "").lower()
    limit = int(args.get("limit", 30))

    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "cmdline"]):
            try:
                info = p.info
                name = info.get("name") or ""
                if filter_str and filter_str not in name.lower():
                    continue
                cmd = " ".join(info.get("cmdline") or [])[:80]
                cpu = info.get("cpu_percent") or 0.0
                mem = info.get("memory_percent") or 0.0
                procs.append((info["pid"], name, cpu, mem, cmd))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs = procs[:limit]
        if not procs:
            return "No matching processes found."

        lines = ["PID      NAME                     CPU%   MEM%   COMMAND"]
        lines.append("-" * 70)
        for pid, name, cpu, mem, cmd in procs:
            lines.append(f"{pid:<8} {name:<25} {cpu:>5.1f}  {mem:>5.1f}  {cmd}")
        return "\n".join(lines)
    except ImportError:
        return "Error: psutil not installed. Run: pip install psutil"
    except Exception as e:
        return f"Error listing processes: {e}"


async def execute_process_kill(args: dict) -> str:
    pid = args.get("pid")
    if pid is None:
        return "Error: pid is required"

    signal_num = int(args.get("signal", 15))
    pid = int(pid)

    try:
        import psutil, signal as _signal
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except psutil.NoSuchProcess:
            return f"Error: process {pid} not found"
        except psutil.AccessDenied:
            name = "?"

        import os
        os.kill(pid, signal_num)
        sig_name = {15: "SIGTERM", 9: "SIGKILL", 2: "SIGINT"}.get(signal_num, f"signal {signal_num}")
        return f"OK: sent {sig_name} to PID {pid} ({name})"
    except PermissionError:
        return f"Error: permission denied to signal PID {pid}"
    except ProcessLookupError:
        return f"Error: process {pid} not found"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# background jobs
# ---------------------------------------------------------------------------

def _background_manager_from_context(audit_context: dict | None):
    context = audit_context or {}
    runtime = context.get("_runtime")
    kernel = (
        getattr(runtime, "orchestrator", None)
        or getattr(runtime, "kernel", None)
        or context.get("_kernel")
    )
    manager = getattr(kernel, "background_job_manager", None) if kernel is not None else None
    return manager or getattr(runtime, "background_job_manager", None)


async def _background_job_api_request(
    audit_context: dict | None,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        base_url, _agent = workbench_endpoint(audit_context)
        status, body = await request_workbench_json(
            method,
            f"{base_url}{path}",
            payload=payload,
        )
    except ValueError as exc:
        return None, f"Error: {exc}"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return (
            None,
            (
                "Error: HASHI Workbench API is unavailable: "
                f"{type(exc).__name__}: {exc}"
            ),
        )
    if status >= 400 or body.get("ok") is False:
        detail = str(body.get("error") or body.get("message") or "request failed")
        return (
            None,
            f"Error: HASHI BackgroundJob API request failed ({status}): {detail}",
        )
    return body, None


def _coerce_chat_id(value: Any) -> Any:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return value


def _job_summary(record: Any) -> dict[str, Any]:
    def value(key: str, default: Any = None) -> Any:
        if isinstance(record, Mapping):
            return record.get(key, default)
        return getattr(record, key, default)

    command = value("command", {}) or {}
    logs = value("logs", {}) or {}
    return {
        "job_id": value("job_id"),
        "state": value("state"),
        "returncode": value("returncode"),
        "created_at": value("created_at"),
        "updated_at": value("updated_at"),
        "ended_at": value("ended_at"),
        "error": value("error"),
        "command": command.get("display") if isinstance(command, Mapping) else command,
        "stdout_path": logs.get("stdout_path") if isinstance(logs, Mapping) else None,
        "stderr_path": logs.get("stderr_path") if isinstance(logs, Mapping) else None,
        "notification": value("notification"),
    }


async def execute_background_job_start(
    args: dict,
    access_root: Path | Sequence[Path],
    workspace_dir: Path,
    audit_context: dict | None = None,
) -> str:
    command = str(args.get("command") or "").strip()
    argv = args.get("argv")
    if argv is not None:
        if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
            return "Error: argv must be a non-empty list of strings"
    if not command and not argv:
        return "Error: command or argv is required"
    if command and argv:
        return "Error: provide command or argv, not both"

    raw_cwd = str(args.get("cwd") or ".").strip() or "."
    try:
        cwd = _resolve_path(raw_cwd, access_root, workspace_dir)
    except ValueError as exc:
        return f"Error: {exc}"
    if not cwd.exists() or not cwd.is_dir():
        return f"Error: cwd is not a directory: {cwd}"

    context = audit_context or {}
    origin = {
        "chat_id": _coerce_chat_id(context.get("chat_id")),
        "request_id": context.get("request_id"),
        "source": context.get("request_source") or "tool:background_job_start",
        "summary": context.get("request_summary"),
        "tool": "background_job_start",
    }
    agent = str(args.get("agent") or context.get("agent_name") or "unknown")
    manager = _background_manager_from_context(audit_context)
    if manager is None:
        payload, error = await _background_job_api_request(
            audit_context,
            "POST",
            "/api/background-jobs",
            payload={
                "agent": agent,
                "cwd": str(cwd),
                "argv": argv,
                "command": command or None,
                "origin": origin,
                "notify_on_complete": bool(args.get("notify_on_complete", True)),
                "notify_on_failure": bool(args.get("notify_on_failure", True)),
                "trigger_agent_on_complete": bool(args.get("trigger_agent_on_complete", True)),
                "trigger_agent_on_failure": bool(args.get("trigger_agent_on_failure", True)),
            },
        )
        if error:
            return error
        record = (payload or {}).get("job")
        if not isinstance(record, dict):
            return "Error: HASHI BackgroundJob API returned an invalid job record"
        summary = _job_summary(record)
        job_id = str(summary.get("job_id") or "")
        summary["follow_up"] = {
            "status": f"/bg status {job_id}",
            "tail": f"/bg tail {job_id}",
            "cancel": f"/bg cancel {job_id}",
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)

    record = await manager.start_job(
        agent=agent,
        cwd=cwd,
        argv=argv,
        command=command or None,
        origin=origin,
        notify_on_complete=bool(args.get("notify_on_complete", True)),
        notify_on_failure=bool(args.get("notify_on_failure", True)),
        trigger_agent_on_complete=bool(args.get("trigger_agent_on_complete", True)),
        trigger_agent_on_failure=bool(args.get("trigger_agent_on_failure", True)),
    )
    payload = _job_summary(record)
    payload["follow_up"] = {
        "status": f"/bg status {record.job_id}",
        "tail": f"/bg tail {record.job_id}",
        "cancel": f"/bg cancel {record.job_id}",
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


async def execute_background_job_status(args: dict, audit_context: dict | None = None) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return await execute_background_job_list(args, audit_context=audit_context)
    manager = _background_manager_from_context(audit_context)
    if manager is None:
        payload, error = await _background_job_api_request(
            audit_context,
            "GET",
            f"/api/background-jobs/{quote(job_id, safe='')}",
        )
        if error:
            return error
        record = (payload or {}).get("job")
        if not isinstance(record, dict):
            return "Error: HASHI BackgroundJob API returned an invalid job record"
        return json.dumps(_job_summary(record), ensure_ascii=False, indent=2)
    record = manager.get(job_id)
    if record is None:
        return f"Error: background job not found: {job_id}"
    return json.dumps(_job_summary(record), ensure_ascii=False, indent=2)


async def execute_background_job_tail(args: dict, audit_context: dict | None = None) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return "Error: job_id is required"
    stream = str(args.get("stream") or "stdout").strip().lower()
    if stream not in {"stdout", "stderr"}:
        return "Error: stream must be stdout or stderr"
    lines = int(args.get("lines") or 80)
    manager = _background_manager_from_context(audit_context)
    if manager is None:
        query = urlencode({"stream": stream, "lines": str(max(1, min(lines, 1000)))})
        payload, error = await _background_job_api_request(
            audit_context,
            "GET",
            f"/api/background-jobs/{quote(job_id, safe='')}/tail?{query}",
        )
        if error:
            return error
        text = (payload or {}).get("tail")
        if not isinstance(text, str):
            return "Error: HASHI BackgroundJob API returned an invalid tail response"
        return text or "(no output yet)"
    try:
        text = manager.tail(job_id, stream=stream, lines=max(1, lines))
    except KeyError:
        return f"Error: background job not found: {job_id}"
    return text or "(no output yet)"


async def execute_background_job_cancel(args: dict, audit_context: dict | None = None) -> str:
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return "Error: job_id is required"
    manager = _background_manager_from_context(audit_context)
    if manager is None:
        payload, error = await _background_job_api_request(
            audit_context,
            "POST",
            f"/api/background-jobs/{quote(job_id, safe='')}/cancel",
        )
        if error:
            return error
        record = (payload or {}).get("job")
        if not isinstance(record, dict):
            return "Error: HASHI BackgroundJob API returned an invalid job record"
        return json.dumps(_job_summary(record), ensure_ascii=False, indent=2)
    try:
        record = await manager.cancel(job_id)
    except KeyError:
        return f"Error: background job not found: {job_id}"
    return json.dumps(_job_summary(record), ensure_ascii=False, indent=2)


async def execute_background_job_list(args: dict, audit_context: dict | None = None) -> str:
    agent = args.get("agent")
    limit = int(args.get("limit") or 20)
    bounded_limit = max(1, min(limit, 100))
    manager = _background_manager_from_context(audit_context)
    if manager is None:
        query_values = {"limit": str(bounded_limit)}
        if agent:
            query_values["agent"] = str(agent)
        payload, error = await _background_job_api_request(
            audit_context,
            "GET",
            f"/api/background-jobs?{urlencode(query_values)}",
        )
        if error:
            return error
        records = (payload or {}).get("jobs")
        if not isinstance(records, list) or not all(
            isinstance(record, dict) for record in records
        ):
            return "Error: HASHI BackgroundJob API returned an invalid job list"
        return json.dumps(
            [_job_summary(record) for record in records],
            ensure_ascii=False,
            indent=2,
        )
    records = manager.list(agent=str(agent) if agent else None, limit=bounded_limit)
    return json.dumps([_job_summary(record) for record in records], ensure_ascii=False, indent=2)


async def execute_telegram_send(
    args: dict,
    secrets: dict,
    agents_config: Optional[list] = None,
) -> str:
    text = args.get("text", "").strip()
    if not text:
        return "Error: text is required"

    chat_id = args.get("chat_id")
    agent_id = args.get("agent_id")

    # Resolve agent_id -> chat_id via agents config
    if not chat_id and agent_id and agents_config:
        for ag in agents_config:
            if ag.get("id") == agent_id:
                chat_id = ag.get("telegram_chat_id") or ag.get("chat_id")
                token = ag.get("token") or secrets.get(f"{agent_id}_telegram_token")
                break
        if not chat_id:
            return f"Error: could not resolve chat_id for agent '{agent_id}'"
    elif not chat_id:
        return "Error: either chat_id or agent_id must be provided"

    token = args.get("token") or secrets.get("telegram_bot_token")
    if not token:
        return "Error: no telegram token available"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
            data = resp.json()
            if data.get("ok"):
                return f"OK: message sent to {chat_id}"
            else:
                return f"Error: Telegram API error: {data.get('description', 'unknown')}"
    except Exception as e:
        return f"Error sending Telegram message: {e}"


async def execute_telegram_send_file(
    args: dict,
    secrets: dict,
) -> str:
    """Send a file (photo, document, video, or audio) to a Telegram chat."""
    import mimetypes

    path = args.get("path", "").strip()
    if not path:
        return "Error: path is required"

    from pathlib import Path as _Path
    file_path = _Path(path)
    if not file_path.exists():
        return f"Error: file not found: {path}"
    if not file_path.is_file():
        return f"Error: not a file: {path}"

    caption = args.get("caption", "").strip() or None
    chat_id = args.get("chat_id") or secrets.get("_authorized_telegram_id")
    if not chat_id:
        return "Error: chat_id not provided and authorized_telegram_id not available"

    token = secrets.get("_agent_telegram_token") or secrets.get("telegram_bot_token")
    if not token:
        return "Error: no telegram token available"

    # Determine send method
    file_type = args.get("file_type", "auto").lower()
    if file_type == "auto":
        suffix = file_path.suffix.lower()
        if suffix in (".jpg", ".jpeg", ".png", ".webp"):
            file_type = "photo"
        elif suffix in (".mp4", ".mov", ".avi", ".mkv"):
            file_type = "video"
        elif suffix in (".mp3", ".ogg", ".flac", ".wav", ".m4a"):
            file_type = "audio"
        else:
            file_type = "document"

    method_map = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "audio": "sendAudio",
        "document": "sendDocument",
    }
    field_map = {
        "photo": "photo",
        "video": "video",
        "audio": "audio",
        "document": "document",
    }
    api_method = method_map.get(file_type, "sendDocument")
    field_name = field_map.get(file_type, "document")

    try:
        import httpx
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "application/octet-stream"

        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption

        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                files = {field_name: (file_path.name, f, mime_type)}
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/{api_method}",
                    data=data,
                    files=files,
                )
            result = resp.json()
            if result.get("ok"):
                return f"OK: {file_type} sent to {chat_id} ({file_path.name})"
            else:
                return f"Error: Telegram API error: {result.get('description', 'unknown')}"
    except Exception as e:
        return f"Error sending Telegram file: {e}"


async def execute_http_request(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required"

    method = str(args.get("method", "GET")).upper()
    headers = args.get("headers") or {}
    body = args.get("body")
    timeout = min(int(args.get("timeout", 30)), 60)

    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "HASHI/2.2"},
        ) as client:
            req_kwargs: dict = {"headers": headers}
            if body:
                req_kwargs["content"] = body.encode() if isinstance(body, str) else body

            response = await client.request(method, url, **req_kwargs)

        content_type = response.headers.get("content-type", "")
        body_text = response.text
        if len(body_text) > 10000:
            body_text = body_text[:10000] + "\n...[truncated]"

        return (
            f"Status: {response.status_code}\n"
            f"Content-Type: {content_type}\n\n"
            f"{body_text}"
        )
    except Exception as e:
        return f"Error making HTTP request: {e}"


async def execute_web_fetch(
    args: dict,
    max_length: int = 10000,
) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: no URL provided"

    max_len = int(args.get("max_length", max_length))

    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HASHI/2.2)"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            html = response.text

        # Convert HTML to Markdown if html2text is available
        try:
            import html2text
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            text = h.handle(html)
        except ImportError:
            # Fallback: basic tag stripping
            import re as _re
            text = _re.sub(r"<[^>]+>", "", html)
            text = _re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) > max_len:
            text = text[:max_len] + "\n...[content truncated]"

        return f"[Fetched: {url}]\n\n{text}"

    except Exception as e:
        return f"Error fetching URL: {e}"


# ---------------------------------------------------------------------------
# xai_imagine
# ---------------------------------------------------------------------------

async def execute_xai_imagine(args: dict, secrets: dict, global_config: Any = None) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "Error: no prompt provided"

    from adapters.xai_imagine import DEFAULT_IMAGINE_MODEL, generate_xai_image

    hermes_home = None
    base_url = "https://api.x.ai/v1"
    if global_config is not None:
        hermes_home = str(getattr(global_config, "hermes_home", "") or "").strip() or None
        base_url = str(getattr(global_config, "xai_api_base_url", "") or "").strip() or base_url

    bearer_token = str(secrets.get("xai_api_key") or secrets.get("XAI_API_KEY") or "").strip() or None
    oauth_refresh = str(secrets.get("xai_oauth_refresh_token") or "").strip() or None
    model = str(args.get("model") or DEFAULT_IMAGINE_MODEL).strip() or DEFAULT_IMAGINE_MODEL

    try:
        result = await generate_xai_image(
            prompt=prompt,
            model=model,
            bearer_token=bearer_token,
            oauth_refresh_token=oauth_refresh,
            hermes_home=hermes_home,
            base_url=base_url,
            aspect_ratio=str(args.get("aspect_ratio") or "").strip() or None,
            resolution=str(args.get("resolution") or "").strip() or None,
            n=int(args.get("n") or 1),
        )
    except Exception as exc:
        return f"Error: xAI Imagine failed: {exc}"

    lines = [f"Generated {len(result.urls)} image(s) with {result.model}:"]
    lines.extend(result.urls)
    return "\n".join(lines)
