"""
Built-in tool executor implementations for HASHI V2.2.

Each function is a standalone async executor. They are called by ToolRegistry.
All file operations are sandboxed to access_root.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import re
import shutil
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode

import aiohttp

from orchestrator.process_execution import (
    decode_process_output,
    process_group_kwargs,
    resolve_shell_invocation,
    terminate_windows_process_tree,
)
from tools.workbench_client import request_workbench_json, workbench_endpoint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _access_roots(value: Path | Sequence[Path]) -> tuple[Path, ...]:
    raw_roots = (
        value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, Path))
        else (value,)
    )
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
    args: Mapping[str, Any],
    timeout_max: float | None,
    timeout_default: float | None = None,
) -> tuple[float | None, dict[str, Any], str | None]:
    """Resolve a caller timeout or an explicitly configured instance fuse."""

    if "timeout" not in args or args.get("timeout") is None:
        if timeout_default is None:
            return None, {"timeout_explicit": False}, None
        default, error = _positive_seconds(
            timeout_default, label="configured shell timeout_default"
        )
        if error is not None:
            return None, {"timeout_explicit": False}, error
        assert default is not None
        configured_max = None
        if timeout_max is not None:
            configured_max, error = _positive_seconds(
                timeout_max, label="configured bash timeout_max"
            )
            if error is not None:
                return None, {"timeout_explicit": False}, error
            assert configured_max is not None
            default = min(default, configured_max)
        return (
            default,
            {
                "timeout_explicit": False,
                "timeout_effective_s": default,
                "timeout_source": "instance_safety_default",
                "timeout_max_s": configured_max,
            },
            None,
        )
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
    return process_group_kwargs()


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

    scope = (
        "process_tree"
        if os.name == "nt"
        else "process_group" if pgid is not None else "process_only_fallback"
    )
    forced = False
    errors: list[str] = []

    async def signal_process(*, force: bool) -> None:
        nonlocal forced
        forced = forced or force
        try:
            if os.name == "nt" and proc.pid:
                # Windows has no safe tree-wide graceful signal for arbitrary
                # console descendants. A non-forced taskkill can broadcast a
                # console interrupt beyond the child tree, including HASHI.
                # Use the OS-targeted forced tree termination from the outset.
                outcome = await terminate_windows_process_tree(proc.pid, force=True)
                forced = True
                if outcome.get("returncode") not in {0, 128} and proc.returncode is None:
                    errors.append(
                        "taskkill: " + str(outcome.get("output") or outcome.get("returncode"))
                    )
            elif pgid is not None:
                os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
            elif proc.returncode is None:
                proc.kill() if force else proc.terminate()
        except ProcessLookupError:
            return
        except Exception as exc:  # cleanup truth is reported, never hidden
            errors.append(f"{type(exc).__name__}: {exc}")

    await signal_process(force=False)
    complete = await _wait_for_bash_cleanup(
        communicate_task,
        pgid=pgid,
        timeout=grace_seconds,
    )
    if not complete:
        await signal_process(force=True)
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

_PARTIAL_OUTPUT_TAIL_CHARS = 4000


def _bounded_partial_tail(stdout: bytes | None, stderr: bytes | None, encoding) -> str:
    parts = []
    if stdout:
        parts.append(decode_process_output(stdout, encoding=encoding))
    if stderr:
        parts.append("[stderr]\n" + decode_process_output(stderr, encoding=encoding))
    text = "\n".join(parts).strip()
    if not text:
        return ""
    if len(text) > _PARTIAL_OUTPUT_TAIL_CHARS:
        return text[-_PARTIAL_OUTPUT_TAIL_CHARS:] + "\n...[partial output truncated]"
    return text


def _communicate_partial_tail(communicate_task, encoding) -> str:
    if communicate_task is None or not communicate_task.done():
        return ""
    if communicate_task.cancelled() or communicate_task.exception() is not None:
        return ""
    stdout, stderr = communicate_task.result()
    return _bounded_partial_tail(stdout, stderr, encoding)


async def execute_shell(
    args: dict,
    workspace_dir: Path,
    timeout_max: float | None = None,
    timeout_default: float | None = None,
    blocked_patterns: Optional[list[str]] = None,
) -> str | BuiltinExecutionResult:
    command = str(args.get("command", "")).strip()
    if not command:
        return "Error: no command provided"

    timeout, timeout_details, timeout_error = _bash_timeout(
        args, timeout_max, timeout_default
    )
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
    invocation = None
    try:
        invocation = resolve_shell_invocation(command, args.get("shell"))
        proc = await asyncio.create_subprocess_exec(
            *invocation.argv,
            stdin=asyncio.subprocess.DEVNULL,
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
                partial_tail = _communicate_partial_tail(
                    communicate_task, invocation.encoding
                )
                message = f"Error: command timed out after {_seconds_label(timeout)}s"
                if partial_tail:
                    message += f"\n[partial output]\n{partial_tail}"
                return BuiltinExecutionResult(
                    message,
                    {
                        **timeout_details,
                        "shell": invocation.shell,
                        "shell_executable": invocation.executable,
                        "launcher": invocation.launcher,
                        "launcher_executable": invocation.launcher_executable,
                        "cwd": str(Path(workspace_dir).resolve()),
                        "encoding": invocation.encoding,
                        "exit_code": proc.returncode,
                        "partial_output": partial_tail or None,
                        "foreground_cleanup": cleanup,
                    },
                )
            stdout, stderr = await communicate_task

        output_parts = []
        if stdout:
            output_parts.append(
                decode_process_output(stdout, encoding=invocation.encoding)
            )
        if stderr:
            output_parts.append(
                "[stderr]\n"
                + decode_process_output(stderr, encoding=invocation.encoding)
            )

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
                "scope": (
                    "process_tree"
                    if os.name == "nt"
                    else "process_group" if pgid is not None else "process_only_fallback"
                ),
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
                "shell": invocation.shell,
                "shell_executable": invocation.executable,
                "launcher": invocation.launcher,
                "launcher_executable": invocation.launcher_executable,
                "cwd": str(Path(workspace_dir).resolve()),
                "encoding": invocation.encoding,
                "exit_code": proc.returncode,
                "foreground_cleanup": completion_cleanup,
            },
        )

    except asyncio.CancelledError as exc:
        cleanup = None
        if proc is not None and communicate_task is not None:
            cleanup = await _shield_bash_cleanup(
                _cleanup_bash_process(proc, communicate_task, pgid=pgid)
            )
        partial_tail = _communicate_partial_tail(
            communicate_task,
            invocation.encoding if invocation is not None else None,
        )
        details = {
            **timeout_details,
            "shell": invocation.shell if invocation is not None else None,
            "shell_executable": (
                invocation.executable if invocation is not None else None
            ),
            "launcher": invocation.launcher if invocation is not None else None,
            "launcher_executable": (
                invocation.launcher_executable if invocation is not None else None
            ),
            "cwd": str(Path(workspace_dir).resolve()),
            "encoding": invocation.encoding if invocation is not None else None,
            "exit_code": proc.returncode if proc is not None else None,
            "partial_output": partial_tail or None,
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
                "shell": invocation.shell if invocation is not None else None,
                "shell_executable": (
                    invocation.executable if invocation is not None else None
                ),
                "launcher": invocation.launcher if invocation is not None else None,
                "launcher_executable": (
                    invocation.launcher_executable
                    if invocation is not None
                    else None
                ),
                "cwd": str(Path(workspace_dir).resolve()),
                "encoding": invocation.encoding if invocation is not None else None,
                "exit_code": proc.returncode if proc is not None else None,
                "foreground_cleanup": cleanup or {
                    "status": "not_started",
                    "process_reaped": True,
                    "errors": [],
                },
            },
        )


async def execute_bash(
    args: dict,
    workspace_dir: Path,
    timeout_max: float | None = None,
    timeout_default: float | None = None,
    blocked_patterns: Optional[list[str]] = None,
) -> str | BuiltinExecutionResult:
    """Compatibility alias that always invokes a real Bash executable."""

    compatibility_args = dict(args)
    compatibility_args["shell"] = "bash"
    return await execute_shell(
        compatibility_args,
        workspace_dir=workspace_dir,
        timeout_max=timeout_max,
        timeout_default=timeout_default,
        blocked_patterns=blocked_patterns,
    )


# ---------------------------------------------------------------------------
# log_query / file_read
# ---------------------------------------------------------------------------

_LOG_QUERY_CHUNK_CHARS = 64 * 1024
_LOG_QUERY_DEFAULT_MAX_RESULTS = 30
_LOG_QUERY_MAX_RESULTS = 200
_LOG_QUERY_DEFAULT_CONTEXT_CHARS = 300
_LOG_QUERY_MAX_CONTEXT_CHARS = 2000
_LOG_QUERY_MAX_TERMS = 32
_LOG_QUERY_MAX_TERM_CHARS = 1024


def _bounded_integer_argument(
    value: object,
    *,
    label: str,
    default: int,
    minimum: int,
    maximum: int,
) -> tuple[int | None, str | None]:
    if value is None:
        return default, None
    if isinstance(value, bool):
        return None, f"Error: {label} must be an integer from {minimum} to {maximum}"
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, f"Error: {label} must be an integer from {minimum} to {maximum}"
    if parsed < minimum or parsed > maximum:
        return None, f"Error: {label} must be an integer from {minimum} to {maximum}"
    return parsed, None


def _literal_log_query(
    path: Path,
    *,
    terms: tuple[str, ...],
    case_sensitive: bool,
    max_results: int,
    context_chars: int,
) -> dict[str, Any]:
    flags = 0 if case_sensitive else re.IGNORECASE
    patterns = tuple((term, re.compile(re.escape(term), flags)) for term in terms)
    longest_term = max(len(term) for term in terms)
    buffer = ""
    buffer_offset = 0
    newlines_before_buffer = 0
    next_scan_offset = 0
    characters_read = 0
    matches: list[dict[str, Any]] = []
    result_limit_reached = False

    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        while True:
            chunk = stream.read(_LOG_QUERY_CHUNK_CHARS)
            eof = chunk == ""
            if chunk:
                buffer += chunk
                characters_read += len(chunk)

            safe_start_limit = buffer_offset + len(buffer)
            if not eof:
                safe_start_limit = max(
                    next_scan_offset,
                    safe_start_limit - context_chars - longest_term,
                )
            local_cursor = max(0, next_scan_offset - buffer_offset)
            local_limit = max(local_cursor, safe_start_limit - buffer_offset)

            while local_cursor < local_limit and len(matches) < max_results:
                candidates: list[tuple[int, int, str, re.Match[str]]] = []
                for index, (term, pattern) in enumerate(patterns):
                    match = pattern.search(buffer, local_cursor)
                    if match is not None and match.start() < local_limit:
                        candidates.append((match.start(), index, term, match))
                if not candidates:
                    break
                _start, _index, term, match = min(
                    candidates, key=lambda candidate: (candidate[0], candidate[1])
                )
                start = match.start()
                end = match.end()
                excerpt_start = max(0, start - context_chars)
                excerpt_end = min(len(buffer), end + context_chars)
                matches.append(
                    {
                        "term": term,
                        "line": newlines_before_buffer
                        + buffer.count("\n", 0, start)
                        + 1,
                        "character_offset": buffer_offset + start,
                        "excerpt": buffer[excerpt_start:excerpt_end],
                    }
                )
                local_cursor = max(end, start + 1)

            if len(matches) >= max_results:
                result_limit_reached = True
                break
            next_scan_offset = safe_start_limit
            if eof:
                break

            keep_from = max(buffer_offset, next_scan_offset - context_chars)
            drop_count = keep_from - buffer_offset
            if drop_count:
                newlines_before_buffer += buffer.count("\n", 0, drop_count)
                buffer = buffer[drop_count:]
                buffer_offset = keep_from

    return {
        "path": str(path),
        "file_size_bytes": path.stat().st_size,
        "literal_only": True,
        "case_sensitive": case_sensitive,
        "terms": list(terms),
        "matches": matches,
        "result_limit_reached": result_limit_reached,
        "characters_read": characters_read,
    }


async def execute_log_query(
    args: dict,
    access_root: Path | Sequence[Path],
    workspace_dir: Path,
) -> str | BuiltinExecutionResult:
    """Search bounded literal excerpts without materialising whole records."""

    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        return "Error: no path provided"
    raw_terms = args.get("terms")
    if not isinstance(raw_terms, list) or not raw_terms:
        return "Error: terms must be a non-empty list of literal strings"
    if len(raw_terms) > _LOG_QUERY_MAX_TERMS:
        return f"Error: terms may contain at most {_LOG_QUERY_MAX_TERMS} values"
    terms: list[str] = []
    seen: set[str] = set()
    for raw_term in raw_terms:
        if not isinstance(raw_term, str) or not raw_term:
            return "Error: every term must be a non-empty string"
        if len(raw_term) > _LOG_QUERY_MAX_TERM_CHARS:
            return (
                f"Error: every term must contain at most "
                f"{_LOG_QUERY_MAX_TERM_CHARS} characters"
            )
        if raw_term not in seen:
            seen.add(raw_term)
            terms.append(raw_term)
    max_results, error = _bounded_integer_argument(
        args.get("max_results"),
        label="max_results",
        default=_LOG_QUERY_DEFAULT_MAX_RESULTS,
        minimum=1,
        maximum=_LOG_QUERY_MAX_RESULTS,
    )
    if error is not None:
        return error
    context_chars, error = _bounded_integer_argument(
        args.get("context_chars"),
        label="context_chars",
        default=_LOG_QUERY_DEFAULT_CONTEXT_CHARS,
        minimum=0,
        maximum=_LOG_QUERY_MAX_CONTEXT_CHARS,
    )
    if error is not None:
        return error
    case_sensitive = args.get("case_sensitive", False)
    if not isinstance(case_sensitive, bool):
        return "Error: case_sensitive must be a boolean"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as exc:
        return f"Error: {exc}"
    if not path.is_file():
        return f"Error: file not found: {path}"
    assert max_results is not None and context_chars is not None
    try:
        payload = await asyncio.to_thread(
            _literal_log_query,
            path,
            terms=tuple(terms),
            case_sensitive=case_sensitive,
            max_results=max_results,
            context_chars=context_chars,
        )
    except OSError as exc:
        return f"Error reading file: {exc}"
    return BuiltinExecutionResult(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        {
            "query_mode": "literal",
            "file_size_bytes": payload["file_size_bytes"],
            "match_count": len(payload["matches"]),
            "result_limit_reached": payload["result_limit_reached"],
        },
    )

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


# ---------------------------------------------------------------------------
# apply_patch: cross-platform ``patch`` discovery + git / pure-Python fallbacks
# ---------------------------------------------------------------------------


def _decode_patch_output(data: object) -> str:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8", "replace")
    return str(data or "")


def _windows_patch_candidates(
    git_exe: str | None, env: Mapping[str, str]
) -> list[str]:
    """Absolute ``patch.exe`` candidates shipped with Git for Windows.

    Derives the layout from the ``git`` executable (``<root>\\cmd\\git.exe``
    implies ``<root>\\usr\\bin\\patch.exe``) and probes the usual install
    roots, so a fresh Windows install never needs a hand-copied ``patch.exe``.
    """

    roots: list[Path] = []
    if git_exe:
        try:
            roots.append(Path(git_exe).resolve().parent.parent)
        except OSError:
            pass
    for env_key in ("ProgramFiles", "ProgramFiles(x86)"):
        base = env.get(env_key)
        if base:
            roots.append(Path(base) / "Git")
    local_appdata = env.get("LOCALAPPDATA")
    if local_appdata:
        roots.append(Path(local_appdata) / "Programs" / "Git")

    candidates: list[str] = []
    for root in roots:
        for relative in (
            "usr/bin/patch.exe",
            "mingw64/bin/patch.exe",
            "bin/patch.exe",
        ):
            candidates.append(str(root / relative))
    return candidates


def _candidate_patch_commands() -> list[str]:
    """Ordered candidate paths to a ``patch`` executable.

    ``PATH`` is consulted first. On Windows the well-known Git-for-Windows
    locations are probed next. WSL, Linux and macOS need nothing beyond
    ``PATH``.
    """

    candidates: list[str] = []

    on_path = shutil.which("patch")
    if on_path:
        candidates.append(on_path)

    if os.name == "nt":
        candidates.extend(_windows_patch_candidates(shutil.which("git"), os.environ))

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        key = candidate.replace("\\", "/").casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _find_patch_command() -> str | None:
    """Return the first usable ``patch`` executable, or ``None``."""

    for candidate in _candidate_patch_commands():
        try:
            if Path(candidate).is_file():
                return candidate
        except OSError:
            continue
    return None


def _format_patch_outcome(outcome: tuple[str, str], path: Path) -> str:
    status, detail = outcome
    if status == "ok":
        return f"OK: patch applied to {path}" + (f"\n{detail}" if detail else "")
    if status == "failed":
        return f"Error: patch failed:\n{detail}"
    return f"Error: patch rejected (dry-run):\n{detail}"


def _apply_patch_with_patch_command(
    patch_cmd: str, path: Path, patch_str: str
) -> tuple[str, str] | None:
    """Run ``patch`` with the existing dry-run-then-apply contract.

    Returns ``None`` only when the resolved binary cannot be executed, so the
    caller can fall back; a real rejection is returned verbatim.
    """

    import subprocess

    payload = patch_str.encode("utf-8", "surrogateescape")
    try:
        dry_run = subprocess.run(
            [patch_cmd, "--dry-run", "-u", str(path)],
            input=payload,
            capture_output=True,
        )
    except OSError:
        return None
    if dry_run.returncode != 0:
        return ("rejected", _decode_patch_output(dry_run.stderr))

    try:
        applied = subprocess.run(
            [patch_cmd, "-u", str(path)],
            input=payload,
            capture_output=True,
        )
    except OSError:
        return None
    if applied.returncode != 0:
        return ("failed", _decode_patch_output(applied.stderr))
    return ("ok", _decode_patch_output(applied.stdout).strip())


def _retarget_patch_headers(patch_str: str, filename: str) -> str:
    """Rewrite the first ``---`` / ``+++`` pair to target ``filename``.

    ``patch -u <file>`` ignores the header and patches the named file; the
    ``git apply`` fallback keys off the header, so normalise it to the single
    file the caller asked for (``git apply -p1`` from the file's directory).
    """

    old_rewritten = False
    new_rewritten = False
    rewritten: list[str] = []
    for line in patch_str.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        if not old_rewritten and body.startswith("---"):
            rewritten.append(f"--- a/{filename}{ending or chr(10)}")
            old_rewritten = True
            continue
        if old_rewritten and not new_rewritten and body.startswith("+++"):
            rewritten.append(f"+++ b/{filename}{ending or chr(10)}")
            new_rewritten = True
            continue
        rewritten.append(line)
    return "".join(rewritten)


def _apply_patch_with_git(
    git_path: str, path: Path, patch_str: str
) -> tuple[str, str] | None:
    """Apply through ``git apply``; ``None`` when git is unusable here.

    Line-ending conversion is disabled (``core.autocrlf=false``,
    ``core.eol=lf``) so applying a diff never rewrites an LF file as CRLF, the
    way a global ``core.autocrlf=true`` otherwise would.
    """

    import subprocess

    payload = _retarget_patch_headers(patch_str, path.name).encode(
        "utf-8", "surrogateescape"
    )
    cwd = str(path.parent)
    base_argv = [git_path, "-c", "core.autocrlf=false", "-c", "core.eol=lf", "apply"]
    try:
        check = subprocess.run(
            [*base_argv, "--check", "-"],
            input=payload,
            capture_output=True,
            cwd=cwd,
        )
    except OSError:
        return None
    if check.returncode != 0:
        return ("rejected", _decode_patch_output(check.stderr).strip())

    try:
        applied = subprocess.run(
            [*base_argv, "-"],
            input=payload,
            capture_output=True,
            cwd=cwd,
        )
    except OSError:
        return None
    if applied.returncode != 0:
        return ("failed", _decode_patch_output(applied.stderr).strip())
    return ("ok", _decode_patch_output(applied.stdout).strip())


def _parse_hunk_header(header: str) -> tuple[int, int, int] | None:
    """Parse ``@@ -old[,len] +new[,len] @@``; ``None`` when not a hunk."""

    if not header.startswith("@@"):
        return None
    pieces = header.split("@@")
    if len(pieces) < 2:
        return None
    spec = pieces[1].strip().split()
    if len(spec) != 2:
        return None
    old_spec, new_spec = spec
    if not old_spec.startswith("-") or not new_spec.startswith("+"):
        return None
    try:
        old_start = int(old_spec[1:].split(",")[0])
        old_len = int(old_spec.split(",")[1]) if "," in old_spec else 1
        new_len = int(new_spec.split(",")[1]) if "," in new_spec else 1
    except (ValueError, IndexError):
        return None
    return old_start, old_len, new_len


def _apply_unified_diff(text: str, patch_str: str) -> tuple[bool, str]:
    """Apply a standard unified diff without any external tool.

    Line endings are honoured exactly: a CRLF file stays CRLF, a LF file stays
    LF. All matching is done on normalised (LF) lines.
    """

    newline = "\r\n" if "\r\n" in text else "\n"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    source_lines = normalized.split("\n")
    if source_lines and source_lines[-1] == "":
        source_lines.pop()
    trailing_newline = normalized.endswith("\n")

    patch_lines = patch_str.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    hunks: list[tuple[int, int, list[str], list[str]]] = []
    index = 0
    total = len(patch_lines)
    while index < total:
        parsed = _parse_hunk_header(patch_lines[index])
        if parsed is None:
            index += 1
            continue
        old_start, old_len, new_len = parsed
        index += 1
        old_block: list[str] = []
        new_block: list[str] = []
        old_seen = 0
        new_seen = 0
        while index < total and (old_seen < old_len or new_seen < new_len):
            entry = patch_lines[index]
            if entry.startswith("\\"):
                index += 1
                continue
            if entry.startswith("+"):
                new_block.append(entry[1:])
                new_seen += 1
            elif entry.startswith("-"):
                old_block.append(entry[1:])
                old_seen += 1
            elif entry.startswith(" "):
                old_block.append(entry[1:])
                new_block.append(entry[1:])
                old_seen += 1
                new_seen += 1
            elif entry == "":
                old_block.append("")
                new_block.append("")
                old_seen += 1
                new_seen += 1
            else:
                break
            index += 1
        hunks.append((old_start, old_len, old_block, new_block))

    if not hunks:
        return (False, "no unified-diff hunks found in patch")

    result = list(source_lines)
    offset = 0
    for old_start, old_len, old_block, new_block in hunks:
        base = old_start if old_len == 0 else old_start - 1
        floor = max(base + offset, 0)
        position = floor
        if result[position:position + len(old_block)] != old_block:
            position = -1
            for candidate in range(floor, len(result) - len(old_block) + 1):
                if result[candidate:candidate + len(old_block)] == old_block:
                    position = candidate
                    break
            if position < 0:
                return (
                    False,
                    f"hunk at line {old_start} does not match file content",
                )
        result[position:position + len(old_block)] = new_block
        offset += len(new_block) - len(old_block)

    output = "\n".join(result)
    if trailing_newline:
        output += "\n"
    if newline == "\r\n":
        output = output.replace("\n", "\r\n")
    return (True, output)


def _apply_patch_with_python(path: Path, patch_str: str) -> tuple[str, str]:
    """Final fallback: apply the diff in-process, no external binary needed.

    The file is read and written with newline translation disabled so the
    result keeps the file's original line endings even on Windows.
    """

    try:
        with path.open("r", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            original = handle.read()
    except (OSError, UnicodeError) as exc:
        return ("rejected", f"cannot read {path}: {exc}")

    ok, result = _apply_unified_diff(original, patch_str)
    if not ok:
        return ("rejected", result)

    try:
        with path.open("w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            handle.write(result)
    except (OSError, UnicodeError) as exc:
        return ("failed", f"cannot write {path}: {exc}")
    return ("ok", "")


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
        patch_cmd = _find_patch_command()
    except Exception:
        patch_cmd = None

    if patch_cmd is not None:
        outcome = _apply_patch_with_patch_command(patch_cmd, path, patch_str)
        if outcome is not None:
            # A resolved ``patch`` binary is authoritative: honour its verdict,
            # including a dry-run rejection, and never silently fall through.
            return _format_patch_outcome(outcome, path)

    try:
        git_path = shutil.which("git")
        git_detail = ""
        if git_path:
            outcome = _apply_patch_with_git(git_path, path, patch_str)
            if outcome is not None:
                if outcome[0] == "ok":
                    return _format_patch_outcome(outcome, path)
                git_detail = outcome[1]

        outcome = _apply_patch_with_python(path, patch_str)
        if outcome[0] == "ok":
            return _format_patch_outcome(outcome, path)

        detail = outcome[1]
        if git_detail:
            detail = f"{git_detail}\n{detail}"
        return f"Error: patch rejected (dry-run):\n{detail}"
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
        import psutil

        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except psutil.NoSuchProcess:
            return f"Error: process {pid} not found"
        except psutil.AccessDenied:
            name = "?"

        if signal_num == 9:
            proc.kill()
        elif signal_num == 15:
            proc.terminate()
        elif os.name == "nt":
            return (
                "Error: native Windows process_kill supports only "
                "signal 15 (terminate) or 9 (force kill)"
            )
        else:
            os.kill(pid, signal_num)
        sig_name = {15: "SIGTERM", 9: "SIGKILL", 2: "SIGINT"}.get(
            signal_num, f"signal {signal_num}"
        )
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
    shell = str(args.get("shell") or "").strip() or None
    if argv is not None:
        if not isinstance(argv, list) or not all(isinstance(item, str) and item for item in argv):
            return "Error: argv must be a non-empty list of strings"
    if not command and not argv:
        return "Error: command or argv is required"
    if command and argv:
        return "Error: provide command or argv, not both"
    if argv and shell:
        return "Error: shell is valid only with command mode"

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
                "shell": shell,
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
        shell=shell,
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
    if inspect.isawaitable(record):
        record = await record
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
        if inspect.isawaitable(text):
            text = await text
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
    if inspect.isawaitable(records):
        records = await records
    return json.dumps([_job_summary(record) for record in records], ensure_ascii=False, indent=2)


async def execute_request_diagnostics(
    args: dict,
    workspace_dir: Path,
    audit_context: dict | None = None,
) -> str:
    """Join existing sanitised evidence for one request without changing it."""

    request_id = str(args.get("request_id") or "").strip()
    if not request_id:
        return "Error: request_id is required"
    manager = _background_manager_from_context(audit_context)
    records: list[Any] = []
    history: dict[str, list[dict[str, Any]]] = {}
    if manager is not None:
        try:
            records = manager.list(limit=100)
            if inspect.isawaitable(records):
                records = await records
            history_reader = getattr(manager, "history", None)
            if callable(history_reader):
                for record in records:
                    origin = getattr(record, "origin", {})
                    if not isinstance(origin, Mapping) or str(
                        origin.get("request_id") or ""
                    ) != request_id:
                        continue
                    job_id = str(getattr(record, "job_id", "") or "")
                    events = history_reader(job_id)
                    if inspect.isawaitable(events):
                        events = await events
                    history[job_id] = [dict(item) for item in events or ()]
        except Exception:
            # Background evidence is optional to the read-only projection.
            records = []
            history = {}
    from orchestrator.request_diagnostics import build_request_diagnostics

    report = await asyncio.to_thread(
        build_request_diagnostics,
        workspace_dir=workspace_dir,
        request_id=request_id,
        background_jobs=records,
        background_history=history,
    )
    return json.dumps(report, ensure_ascii=False, indent=2)


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


async def execute_frontend_send_attachments(
    args: dict,
    *,
    access_root: Path | Sequence[Path],
    workspace_dir: Path,
    audit_context: dict | None,
    tool_call_id: str = "",
) -> str:
    """Bind ordered local files to the current canonical assistant Message.

    Local source paths remain inside the Agent's configured workspace/workzone
    roots.  The returned receipt proves durable Session binding only; a
    Frontend Connector records transport delivery separately.
    """

    import hashlib
    import mimetypes

    from orchestrator.session_store import (
        MAX_SESSION_ATTACHMENT_BYTES,
        MAX_SESSION_ATTACHMENTS_PER_MESSAGE,
        MAX_SESSION_ATTACHMENT_TOTAL_BYTES,
        IdempotencyConflict,
        SessionConflict,
        SessionNotFound,
        SessionStore,
    )

    context = dict(audit_context or {})
    surface = str(context.get("session_surface") or "").strip().casefold()
    if surface == "tui":
        return "Error: the built-in TUI uses its dedicated HASHI attachment path"
    # Unified attachment delivery contract: the surface is a rendering hint,
    # never an admission gate.  Telegram turns may bind to the canonical
    # Session too; pushing to Telegram remains the caller's concern.
    request_id = str(context.get("request_id") or "").strip()
    session_id = str(context.get("hashi_session_id") or "").strip()
    owner_id = str(context.get("owner_id") or "").strip()
    agent_id = str(context.get("agent_name") or "").strip().casefold()
    if not all((session_id, owner_id, agent_id, surface)):
        return "Error: frontend attachments require an active HASHI Session context"
    bind_only = False

    runtime = context.get("_runtime")
    store = getattr(runtime, "session_store", None)
    if not isinstance(store, SessionStore):
        descriptor = context.get("session_store_descriptor")
        if not isinstance(descriptor, Mapping):
            return "Error: canonical Session attachment storage is unavailable"
        try:
            store = SessionStore(
                str(descriptor["db_path"]),
                instance_id=str(descriptor["instance_id"]),
                attachment_root=str(descriptor["attachment_root"]),
            )
        except (KeyError, OSError, TypeError, ValueError) as exc:
            return f"Error: canonical Session attachment storage is unavailable: {exc}"

    if not request_id:
        # Bind-only path: resolve the primary Session and its most recent run
        # instead of rejecting non-frontend turns outright.
        try:
            resolved = store.resolve_primary_session(owner_id=owner_id, agent_id=agent_id)
            resolved_session = str(resolved.get("session_id") or "").strip()
            if resolved_session:
                session_id = resolved_session
            recent = store.recent_session_runs(
                session_id=session_id, owner_id=owner_id, limit=1
            )
            candidate = str((recent[0] or {}).get("request_id") or "").strip() if recent else ""
            if not candidate:
                return "Error: no active HASHI run to bind attachments to"
            request_id = candidate
            bind_only = True
        except Exception as exc:
            return f"Error: unable to resolve an active HASHI run for attachment binding: {exc}"

    try:
        run = store.get_run_by_request(
            request_id,
            owner_id=owner_id,
            agent_id=agent_id,
        )
        if str(run.get("session_id") or "") != session_id:
            raise SessionConflict("frontend attachment Session binding changed")
        if str(run.get("state") or "") != "running":
            raise SessionConflict(
                "frontend attachments require the current running Session Run"
            )
    except (SessionConflict, SessionNotFound) as exc:
        return f"Error: {exc}"

    raw_attachments = args.get("attachments")
    if not isinstance(raw_attachments, list) or not raw_attachments:
        return "Error: attachments must be a non-empty array"
    if len(raw_attachments) > MAX_SESSION_ATTACHMENTS_PER_MESSAGE:
        return "Error: attachment count exceeds the current Session limit"

    prepared: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        for raw in raw_attachments:
            if not isinstance(raw, Mapping):
                raise ValueError("each attachment must be an object")
            raw_path = str(raw.get("path") or "").strip()
            if not raw_path:
                raise ValueError("each attachment requires path")
            path = _resolve_path(raw_path, access_root, workspace_dir)
            if not path.exists():
                raise ValueError(f"file not found: {path}")
            if not path.is_file():
                raise ValueError(f"path is not a file: {path}")
            size_bytes = int(path.stat().st_size)
            if size_bytes > MAX_SESSION_ATTACHMENT_BYTES:
                raise ValueError(f"attachment exceeds the Session size limit: {path.name}")
            total_bytes += size_bytes
            if total_bytes > MAX_SESSION_ATTACHMENT_TOTAL_BYTES:
                raise ValueError("attachments exceed the Session total size limit")
            payload = path.read_bytes()
            if len(payload) != size_bytes:
                raise SessionConflict(f"attachment changed while being read: {path.name}")
            caption = str(raw.get("caption") or "").strip()
            if len(caption) > 1024:
                raise ValueError("attachment caption exceeds 1024 characters")
            media_type = str(raw.get("media_type") or "").strip().casefold()
            if not media_type:
                media_type = (
                    mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                ).casefold()
            media_type = media_type.split(";", 1)[0].strip()
            if "/" not in media_type or len(media_type) > 255:
                raise ValueError(f"invalid attachment MIME type: {media_type!r}")
            prepared.append(
                {
                    "filename": path.name,
                    "caption": caption,
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "payload": payload,
                }
            )

        request_digest = hashlib.sha256(
            json.dumps(
                [
                    {
                        key: item[key]
                        for key in (
                            "filename",
                            "caption",
                            "media_type",
                            "size_bytes",
                            "sha256",
                        )
                    }
                    for item in prepared
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        idempotency_key = "frontend-attachments:" + str(
            tool_call_id or request_digest
        ).strip()
        existing = store.run_output_attachment_group(
            request_id=request_id,
            session_id=session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            if str(existing["request_digest"]) != request_digest:
                raise IdempotencyConflict(
                    "frontend attachment idempotency key is bound to different files"
                )
            return json.dumps(
                {
                    "ok": True,
                    "request_id": request_id,
                    "attachment_count": len(existing["attachments"]),
                    "attachments": existing["attachments"],
                    "bind_only": bool(bind_only),
                    "replayed": True,
                    "receipt": {
                        "type": "hashi.managed-attachment-binding",
                        "version": 1,
                        "state": "bound_to_run",
                        "durable": True,
                        "transport_delivery_state": "not_observed",
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )

        bindings = []
        for item in prepared:
            staged = store.stage_attachment(
                session_id=session_id,
                owner_id=owner_id,
                filename=item["filename"],
                media_type=item["media_type"],
                size_bytes=item["size_bytes"],
                sha256=item["sha256"],
                semantic_role=(
                    "audio_attachment"
                    if item["media_type"].startswith("audio/")
                    else ""
                ),
            )
            store.upload_attachment_bytes(
                session_id=session_id,
                owner_id=owner_id,
                attachment_id=staged["attachment_id"],
                payload=item["payload"],
                audio_direction="output",
            )
            store.commit_attachment(
                session_id=session_id,
                owner_id=owner_id,
                attachment_id=staged["attachment_id"],
            )
            bindings.append(
                {
                    "attachment_id": staged["attachment_id"],
                    "caption": item["caption"],
                }
            )
        bound = store.bind_run_output_attachments(
            request_id=request_id,
            session_id=session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            attachments=bindings,
        )
        return json.dumps(
            {
                "ok": True,
                "request_id": request_id,
                "attachment_count": len(bound["attachments"]),
                "attachments": bound["attachments"],
                "bind_only": bool(bind_only),
                "replayed": bool(bound["replayed"]),
                "receipt": {
                    "type": "hashi.managed-attachment-binding",
                    "version": 1,
                    "state": "bound_to_run",
                    "durable": True,
                    "transport_delivery_state": "not_observed",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except (IdempotencyConflict, OSError, SessionConflict, SessionNotFound, ValueError) as exc:
        return f"Error: {exc}"


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
