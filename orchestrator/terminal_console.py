"""Instance-wide terminal presentation policy.

This module controls stdout presentation only.  It must never gate Telegram,
external clients/TUI, transcripts, audit records, or file logging.  The default is
deliberately quiet; operators can opt back into the historical plaintext
console with ``/terminal raw``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.activity_digest import ActivityDigest
from orchestrator.pathing import instance_runtime_dir

LEVEL_QUIET = "quiet"
LEVEL_ACTIVITY = "activity"
LEVEL_DEBUG = "debug"
LEVEL_RAW = "raw"
LEVELS = (LEVEL_QUIET, LEVEL_ACTIVITY, LEVEL_DEBUG, LEVEL_RAW)
DEFAULT_LEVEL = LEVEL_QUIET

_STATE_VERSION = 1
_STATE_FILENAME = "terminal.json"
_OUTPUT_DIAGNOSTIC_FILENAME = "terminal_output_errors.jsonl"
_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9_.:@/+\-]+")
_ERROR_CODE_RE = re.compile(r"^\s*\[([A-Z][A-Z0-9_]{2,63})\]")
_SAFE_ERROR_PREFIXES = (
    "BACKGROUND_",
    "BACKEND_",
    "CONTEXT_",
    "PROVIDER_",
    "REMOTE_",
    "REQUEST_",
    "RUNTIME_",
    "SIDE_EFFECT_",
    "TELEGRAM_",
    "WIP_",
)
_CONTENT_EVENT_KINDS = {
    "acknowledgement",
    "commentary",
    "initial_resolution",
    "text_delta",
    "thinking",
}
_lock = threading.RLock()
_configured_home: Path | None = None
_level = DEFAULT_LEVEL


@dataclass(frozen=True)
class ConsoleWriteFailure:
    """Sanitised metadata for one failed terminal-output attempt."""

    sink: str
    error_type: str
    errno: int | None = None
    winerror: int | None = None


@dataclass(frozen=True)
class ConsoleWriteResult:
    """Outcome of one best-effort terminal write."""

    success: bool
    sink: str | None
    failures: tuple[ConsoleWriteFailure, ...] = ()


@dataclass(frozen=True)
class TerminalAnimationCapability:
    """Whether the current terminal can safely render cursor animation."""

    supported: bool
    reason: str
    sink: str | None = None


def normalize_level(value: object) -> str:
    return str(value or "").strip().casefold()


def _state_path(bridge_home: Path) -> Path:
    return instance_runtime_dir(bridge_home) / _STATE_FILENAME


def _read_level(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        return DEFAULT_LEVEL
    if not isinstance(payload, dict):
        return DEFAULT_LEVEL
    candidate = normalize_level(payload.get("level"))
    return candidate if candidate in LEVELS else DEFAULT_LEVEL


def configure(bridge_home: str | Path) -> str:
    """Load the persisted instance policy once for the supplied bridge home."""

    global _configured_home, _level
    resolved = Path(bridge_home).expanduser().resolve()
    with _lock:
        if _configured_home == resolved:
            return _level
        _configured_home = resolved
        _level = _read_level(_state_path(resolved))
        return _level


def get_level() -> str:
    with _lock:
        return _level


def setting_path(bridge_home: str | Path | None = None) -> Path | None:
    with _lock:
        home = (
            Path(bridge_home).expanduser().resolve()
            if bridge_home is not None
            else _configured_home
        )
    return _state_path(home) if home is not None else None


def output_diagnostic_path(bridge_home: str | Path | None = None) -> Path | None:
    """Return the instance-scoped terminal delivery diagnostic path."""

    with _lock:
        home = (
            Path(bridge_home).expanduser().resolve()
            if bridge_home is not None
            else _configured_home
        )
    return home / "logs" / _OUTPUT_DIAGNOSTIC_FILENAME if home is not None else None


def set_level(level: object, *, bridge_home: str | Path | None = None) -> str:
    """Persist and immediately activate one terminal level.

    The in-memory setting changes only after the durable replacement succeeds.
    """

    global _configured_home, _level
    candidate = normalize_level(level)
    if candidate not in LEVELS:
        raise ValueError(f"Unknown terminal level: {candidate or '<empty>'}")
    if bridge_home is not None:
        configure(bridge_home)

    with _lock:
        if _configured_home is None:
            raise RuntimeError("Terminal console policy has not been configured")
        path = _state_path(_configured_home)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        payload = {
            "version": _STATE_VERSION,
            "level": candidate,
        }
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        _level = candidate
        return _level


def is_raw() -> bool:
    return get_level() == LEVEL_RAW


def shows_activity() -> bool:
    return get_level() in {LEVEL_ACTIVITY, LEVEL_DEBUG}


def shows_debug() -> bool:
    return get_level() == LEVEL_DEBUG


def safe_identifier(value: object, *, fallback: str = "-") -> str:
    compact = str(value or "").strip().replace("\n", " ").replace("\r", " ")
    compact = _IDENTIFIER_RE.sub("?", compact)[:160]
    return compact or fallback


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _write_failure(sink: str, exc: BaseException) -> ConsoleWriteFailure:
    """Reduce an exception to non-content-bearing diagnostic fields."""

    return ConsoleWriteFailure(
        sink=sink,
        error_type=type(exc).__name__,
        errno=getattr(exc, "errno", None),
        winerror=getattr(exc, "winerror", None),
    )


def _persist_output_diagnostic(
    *,
    purpose: str,
    result: ConsoleWriteResult,
) -> None:
    if not result.failures:
        return
    path = output_diagnostic_path()
    if path is None:
        return
    record = {
        "version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "event": "terminal_output_failure",
        "pid": os.getpid(),
        "platform": sys.platform,
        "purpose": safe_identifier(purpose, fallback="terminal_output"),
        "delivered": result.success,
        "final_sink": result.sink,
        "attempts": [
            {
                "sink": failure.sink,
                "error_type": failure.error_type,
                "errno": failure.errno,
                "winerror": failure.winerror,
            }
            for failure in result.failures
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
            handle.flush()
    except Exception:
        # A disk failure cannot be made durable.  Leave a content-free last
        # resort clue on the original process stderr without recursing through
        # this module's delivery path.
        fallback = getattr(sys, "__stderr__", None)
        if fallback is not None:
            try:
                fallback.write(
                    "[HASHI] terminal output diagnostic could not be persisted.\n"
                )
                fallback.flush()
            except Exception:
                pass


def record_output_exception(
    *,
    purpose: str,
    sink: str,
    error: BaseException,
) -> None:
    """Persist a sanitised failure from a related presentation sink."""

    _persist_output_diagnostic(
        purpose=purpose,
        result=ConsoleWriteResult(
            success=False,
            sink=None,
            failures=(_write_failure(sink, error),),
        ),
    )


def _stream_is_tty(stream: object) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:
        return False


def _should_use_windows_console() -> bool:
    """Use Win32 console I/O only for an attached interactive stdout."""

    return os.name == "nt" and _stream_is_tty(sys.stdout)


def _write_windows_console(payload: str) -> None:
    """Write Unicode directly through the attached Win32 console handle."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.WriteConsoleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPDWORD,
        wintypes.LPVOID,
    ]
    kernel32.WriteConsoleW.restype = wintypes.BOOL

    stdout_handle = kernel32.GetStdHandle(wintypes.DWORD(-11).value)
    invalid_handle = ctypes.c_void_p(-1).value
    if not stdout_handle or stdout_handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), "GetStdHandle failed")
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(stdout_handle, ctypes.byref(mode)):
        raise OSError(ctypes.get_last_error(), "stdout is not a console handle")

    # WriteConsoleW documents a practical upper bound below 64 KiB.  Startup
    # blocks are small, but chunking keeps the helper safe for other callers.
    for offset in range(0, len(payload), 16_000):
        chunk = payload[offset : offset + 16_000]
        written = wintypes.DWORD()
        if not kernel32.WriteConsoleW(
            stdout_handle,
            chunk,
            len(chunk),
            ctypes.byref(written),
            None,
        ):
            raise OSError(ctypes.get_last_error(), "WriteConsoleW failed")
        if written.value != len(chunk):
            raise OSError(0, "WriteConsoleW performed a partial write")


def _enable_windows_virtual_terminal_processing() -> None:
    """Enable ANSI cursor and colour handling on the attached stdout console."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
    kernel32.GetConsoleMode.restype = wintypes.BOOL
    kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetConsoleMode.restype = wintypes.BOOL

    stdout_handle = kernel32.GetStdHandle(wintypes.DWORD(-11).value)
    invalid_handle = ctypes.c_void_p(-1).value
    if not stdout_handle or stdout_handle == invalid_handle:
        raise OSError(ctypes.get_last_error(), "GetStdHandle failed")

    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(stdout_handle, ctypes.byref(mode)):
        raise OSError(ctypes.get_last_error(), "stdout is not a console handle")

    enable_virtual_terminal_processing = 0x0004
    desired_mode = mode.value | enable_virtual_terminal_processing
    if desired_mode != mode.value and not kernel32.SetConsoleMode(
        stdout_handle,
        desired_mode,
    ):
        raise OSError(
            ctypes.get_last_error(),
            "SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING) failed",
        )


def prepare_terminal_animation() -> TerminalAnimationCapability:
    """Qualify animation support without allowing presentation to stop Core.

    Animation is the normal interactive startup path.  Static output is used
    only when stdout is not interactive, the terminal explicitly reports no
    cursor-control support, or Windows cannot enable virtual-terminal mode.
    """

    if not _stream_is_tty(sys.stdout):
        return TerminalAnimationCapability(False, "stdout_not_interactive")

    if os.name == "nt":
        try:
            _enable_windows_virtual_terminal_processing()
        except Exception as exc:
            record_output_exception(
                purpose="startup_animation_capability",
                sink="windows_console_mode",
                error=exc,
            )
            return TerminalAnimationCapability(
                False,
                "windows_virtual_terminal_unavailable",
            )
        return TerminalAnimationCapability(
            True,
            "windows_console_virtual_terminal",
            "windows_console",
        )

    if str(os.environ.get("TERM") or "").strip().casefold() == "dumb":
        return TerminalAnimationCapability(False, "terminal_reports_dumb")

    return TerminalAnimationCapability(True, "interactive_ansi_terminal", "stdout")


def _write_stream(
    stream: object,
    payload: str,
    *,
    sink: str,
    failures: list[ConsoleWriteFailure],
) -> str | None:
    try:
        stream.write(payload)
        stream.flush()
        return sink
    except (UnicodeEncodeError, OSError, ValueError, AttributeError) as exc:
        failures.append(_write_failure(sink, exc))

    encoding = str(getattr(stream, "encoding", None) or "ascii")
    try:
        safe = payload.encode(encoding, errors="backslashreplace").decode(
            encoding,
            errors="strict",
        )
    except (LookupError, UnicodeError):
        safe = payload.encode("ascii", errors="backslashreplace").decode("ascii")

    try:
        stream.write(safe)
        stream.flush()
        return f"{sink}_escaped"
    except (UnicodeEncodeError, OSError, ValueError, AttributeError) as exc:
        failures.append(_write_failure(f"{sink}_escaped", exc))
        return None


def _safe_print(
    text: str,
    *,
    end: str = "\n",
    purpose: str = "terminal_output",
) -> ConsoleWriteResult:
    payload = f"{text}{end}"
    failures: list[ConsoleWriteFailure] = []

    if _should_use_windows_console():
        try:
            _write_windows_console(payload)
            result = ConsoleWriteResult(True, "windows_console")
            return result
        except Exception as exc:
            failures.append(_write_failure("windows_console", exc))
        sink = _write_stream(
            sys.stderr,
            payload,
            sink="stderr",
            failures=failures,
        )
    else:
        sink = _write_stream(
            sys.stdout,
            payload,
            sink="stdout",
            failures=failures,
        )
        if sink is None:
            sink = _write_stream(
                sys.stderr,
                payload,
                sink="stderr",
                failures=failures,
            )

    result = ConsoleWriteResult(
        success=sink is not None,
        sink=sink,
        failures=tuple(failures),
    )
    _persist_output_diagnostic(purpose=purpose, result=result)
    return result


def safe_print(
    text: str = "",
    *,
    end: str = "\n",
    purpose: str = "terminal_output",
) -> ConsoleWriteResult:
    """Best-effort stdout output that cannot interrupt HASHI runtime work."""

    return _safe_print(str(text), end=end, purpose=purpose)


def print_raw(text: str, *, end: str = "\n") -> None:
    """Write historical plaintext console output only in raw mode."""

    if is_raw():
        _safe_print(text, end=end)


def print_activity(text: str) -> None:
    if shows_activity():
        _safe_print(text)


def print_debug(text: str) -> None:
    if shows_debug():
        _safe_print(text)


@dataclass
class _RequestTerminalState:
    agent: str
    request_id: str
    source: str
    backend: str
    started_at: float = field(default_factory=time.monotonic)
    digest: ActivityDigest = field(default_factory=ActivityDigest)
    last_phase: str = "preparing"
    input_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    token_source: str = ""
    response_tool_count: int = 0
    error_code: str = ""
    http_status: int | None = None
    exception_type: str = ""
    thinking_chars: int = 0
    raw_reasoning: str = ""
    reasoning_presented: bool = False


_requests: dict[tuple[str, str], _RequestTerminalState] = {}


def _request_key(agent: object, request_id: object) -> tuple[str, str]:
    return str(agent or ""), str(request_id or "")


def start_request(
    agent: object,
    request_id: object,
    *,
    source: object = "",
    backend: object = "",
) -> None:
    key = _request_key(agent, request_id)
    now = time.monotonic()
    state = _RequestTerminalState(
        agent=safe_identifier(agent, fallback="agent"),
        request_id=safe_identifier(request_id, fallback="request"),
        source=safe_identifier(source),
        backend=safe_identifier(backend),
        started_at=now,
        digest=ActivityDigest(started_at=now, last_activity_at=now),
    )
    with _lock:
        _requests[key] = state
        while len(_requests) > 512:
            _requests.pop(next(iter(_requests)), None)
    if shows_activity():
        suffix = f" · {state.backend}" if state.backend != "-" else ""
        _safe_print(f"▶ [{state.agent}] {state.request_id} · started{suffix}")


def record_stream_event(agent: object, request_id: object, event: object) -> None:
    key = _request_key(agent, request_id)
    with _lock:
        state = _requests.get(key)
        if state is None:
            now = time.monotonic()
            state = _RequestTerminalState(
                agent=safe_identifier(agent, fallback="agent"),
                request_id=safe_identifier(request_id, fallback="request"),
                source="-",
                backend="-",
                started_at=now,
                digest=ActivityDigest(started_at=now, last_activity_at=now),
            )
            _requests[key] = state
        mode = get_level()
        previous_phase = state.digest.phase
        if mode in {LEVEL_ACTIVITY, LEVEL_DEBUG}:
            try:
                state.digest.record(event)
            except Exception:
                # Terminal presentation must never interfere with provider events.
                return
        current_phase = state.digest.phase
        phase_changed = current_phase != previous_phase
        event_kind = str(getattr(event, "kind", "") or "")
        if event_kind == "thinking":
            raw_delta = str(getattr(event, "raw_delta", "") or "")
            summary = str(getattr(event, "summary", "") or "").strip()
            state.thinking_chars += len(raw_delta or summary)
        if mode == LEVEL_RAW and event_kind == "thinking":
            if raw_delta:
                state.raw_reasoning += raw_delta
            else:
                if summary and summary != "Thinking...":
                    separator = " " if state.raw_reasoning else ""
                    state.raw_reasoning += separator + summary
        elapsed = max(0.0, time.monotonic() - state.started_at)
        phase_icon = state.digest.phase_icon
        phase_label = state.digest.phase_label
        if phase_changed:
            state.last_phase = current_phase

    if phase_changed and shows_activity():
        _safe_print(
            f"{phase_icon} [{state.agent}] {state.request_id} · "
            f"{phase_label} · {elapsed:.1f}s"
        )
    if shows_debug():
        kind = safe_identifier(getattr(event, "kind", "event"), fallback="event")
        if kind in _CONTENT_EVENT_KINDS:
            return
        origin = safe_identifier(getattr(event, "origin", ""))
        tool = safe_identifier(getattr(event, "tool_name", ""))
        fields = [f"event={kind}"]
        if origin != "-":
            fields.append(f"origin={origin}")
        if tool != "-":
            fields.append(f"tool={tool}")
        _safe_print(
            f"· [{state.agent}] {state.request_id} · " + " · ".join(fields)
        )


def observe_response(agent: object, request_id: object, response: object) -> None:
    key = _request_key(agent, request_id)
    with _lock:
        state = _requests.get(key)
        if state is None:
            return
        usage = getattr(response, "usage", None)
        if usage is not None:
            state.input_tokens = _nonnegative_int(
                getattr(usage, "input_tokens", 0)
            )
            state.output_tokens = _nonnegative_int(
                getattr(usage, "output_tokens", 0)
            )
            state.thinking_tokens = _nonnegative_int(
                getattr(usage, "thinking_tokens", 0)
            )
            state.token_source = "provider"
        state.response_tool_count = max(
            state.response_tool_count,
            _nonnegative_int(getattr(response, "tool_call_count", 0)),
        )
        state.error_code = safe_identifier(
            getattr(response, "error_code", ""), fallback=""
        )
        status = getattr(response, "http_status", None)
        try:
            state.http_status = int(status) if status is not None else None
        except (TypeError, ValueError):
            state.http_status = None


def observe_estimated_usage(
    agent: object,
    request_id: object,
    *,
    input_tokens: object,
    output_tokens: object,
    thinking_tokens: object = 0,
) -> None:
    key = _request_key(agent, request_id)
    with _lock:
        state = _requests.get(key)
        if state is None or state.token_source == "provider":
            return
        if input_tokens is not None:
            state.input_tokens = _nonnegative_int(input_tokens)
        state.output_tokens = _nonnegative_int(output_tokens)
        if thinking_tokens is not None:
            state.thinking_tokens = _nonnegative_int(thinking_tokens)
        state.token_source = "estimated"


def observe_exception(agent: object, request_id: object, exc: BaseException) -> None:
    key = _request_key(agent, request_id)
    with _lock:
        state = _requests.get(key)
        if state is not None:
            state.exception_type = safe_identifier(type(exc).__name__, fallback="")


def mark_reasoning_presented(agent: object, text: object) -> None:
    agent_key = str(agent or "")
    presented = str(text or "")
    if not presented:
        return
    with _lock:
        for (candidate, _request_id), state in _requests.items():
            if candidate == agent_key and (
                presented in state.raw_reasoning
                or (state.raw_reasoning and state.raw_reasoning in presented)
            ):
                state.reasoning_presented = True


def _error_code(error: object, state: _RequestTerminalState) -> str:
    if state.error_code:
        candidate = state.error_code.upper()
        if candidate.startswith(_SAFE_ERROR_PREFIXES):
            return candidate
    match = _ERROR_CODE_RE.search(str(error or ""))
    if match and match.group(1).startswith(_SAFE_ERROR_PREFIXES):
        return match.group(1)
    lowered = str(error or "").casefold()
    if "timeout" in lowered or "timed out" in lowered:
        return "TIMEOUT"
    if "cancel" in lowered:
        return "CANCELLED"
    return "REQUEST_FAILED"


def finish_request(
    agent: object,
    request_id: object,
    *,
    success: bool,
    error: object = "",
    interrupted: bool = False,
) -> None:
    key = _request_key(agent, request_id)
    with _lock:
        state = _requests.pop(key, None)
    if state is None:
        return
    elapsed = max(0.0, time.monotonic() - state.started_at)

    # Raw deliberately preserves the historical console without new duplicate
    # lifecycle lines.  Existing logging and plaintext helpers own that mode.
    mode = get_level()
    if mode == LEVEL_RAW:
        if state.raw_reasoning and not state.reasoning_presented:
            _safe_print(
                f"\033[38;5;240m[{state.agent}] 💭 "
                f"{state.raw_reasoning}\033[0m"
            )
        return

    if not success and mode == LEVEL_QUIET:
        icon = "⛔" if interrupted else "❌"
        code = "INTERRUPTED" if interrupted else _error_code(error, state)
        _safe_print(f"{icon} [{state.agent}] {state.request_id} · {code}")
        return
    if mode not in {LEVEL_ACTIVITY, LEVEL_DEBUG}:
        return

    operation_count = sum(int(value or 0) for value in state.digest.operations.values())
    tool_count = state.response_tool_count or operation_count
    fields = [f"{elapsed:.1f}s", f"tools={tool_count}"]
    if state.input_tokens is not None:
        reported_thinking_tokens = state.thinking_tokens or 0
        if state.token_source == "estimated":
            reported_thinking_tokens = max(
                reported_thinking_tokens, state.thinking_chars // 4
            )
        token_marker = (
            "tokens(i/o/t)≈"
            if state.token_source == "estimated"
            else "tokens(i/o/t)="
        )
        fields.append(
            f"{token_marker}{state.input_tokens}/"
            f"{state.output_tokens or 0}/"
            f"{reported_thinking_tokens}"
        )
    if success:
        icon = "✅"
        outcome = "completed"
    elif interrupted:
        icon = "⛔"
        outcome = "interrupted"
    else:
        icon = "❌"
        outcome = _error_code(error, state)
    if mode == LEVEL_DEBUG:
        if state.http_status is not None:
            fields.append(f"http={state.http_status}")
        if state.exception_type:
            fields.append(f"exception={state.exception_type}")
    _safe_print(
        f"{icon} [{state.agent}] {state.request_id} · {outcome} · "
        + " · ".join(fields)
    )


def reset_for_tests() -> None:
    """Reset process-local state without touching persisted files."""

    global _configured_home, _level
    with _lock:
        _configured_home = None
        _level = DEFAULT_LEVEL
        _requests.clear()
