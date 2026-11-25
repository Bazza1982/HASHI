"""DeepSeek Harness (DSH) backend adapter driven over ACP v1 JSON-RPC stdio."""

from __future__ import annotations

import asyncio
import base64
from collections import deque
import json
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import time
from typing import Any, Callable, Mapping

from adapters.base import (
    BackendCapabilities,
    BackendResponse,
    BaseBackend,
    TokenUsage,
)
from adapters.hashi_mcp import HashiMcpInvocation, prepare_hashi_mcp
from adapters.stream_events import (
    DELIVERY_REASONING,
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_COMMENTARY,
    KIND_ERROR,
    KIND_FILE_EDIT,
    KIND_FILE_READ,
    KIND_PROVIDER_ACTIVITY,
    KIND_SHELL_EXEC,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
)
from orchestrator.pathing import resolve_dsh_invocation
from orchestrator.process_execution import process_group_kwargs

_ACP_PROTOCOL_VERSION = 1
_ACP_STDOUT_LIMIT = 16 * 1024 * 1024  # 16 MiB per JSON-RPC line
_ACP_RPC_TIMEOUT_SEC = 30.0
_ACP_CANCEL_GRACE_SEC = 5.0
# ``dsh --help`` boots the whole CLI application (5-13s warm on Windows, and
# routinely longer once an idle Agent's page cache goes cold). Probing it from
# ``initialize()`` used to block the first message after idle past the old 20s
# hard limit, so the launch is validated by entry-file existence instead. The
# persistent ACP ``initialize`` handshake in ``_ensure_worker`` remains the
# authoritative runtime health check.
_SCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".ts", ".mts"})
_SUPPORTED_IMAGE_MIMES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)
_SUPPORTED_REASONING_EFFORTS = frozenset({"off", "low", "high", "max"})


def _launch_problem(argv: list[str]) -> str:
    """Return why ``argv`` cannot start, or ``""`` when its entries exist.

    Cheap, non-spawning preflight: it catches a missing executable or launch
    script without paying the multi-second CLI cold start. Anything deeper
    (broken install, bad Node modules) is reported by the real ACP worker
    handshake with its stderr attached.
    """

    if not argv or not str(argv[0] or "").strip():
        return "empty launch command"
    head = str(argv[0])
    if not Path(head).is_file() and shutil.which(head) is None:
        return f"executable not found: {head}"
    for token in argv[1:]:
        text = str(token)
        if text.startswith("-"):
            continue
        candidate = Path(text)
        if candidate.suffix.lower() in _SCRIPT_SUFFIXES:
            if not candidate.is_file():
                return f"launch script not found: {text}"
            break
    return ""


def _canonical_image_parts(
    request_content: Mapping[str, Any] | None,
) -> tuple[Mapping[str, Any], ...]:
    """Return the canonical image media parts of one contract v1 request.

    Reuses the shared multimodal contract so the production ``parts`` envelope
    (``type: media``, ``modality: image``, ``local_ref``) is recognised instead
    of only the legacy ``content_blocks``/``attachments`` shapes.  Invalid or
    absent canonical content yields no parts rather than failing the turn.
    """

    if not isinstance(request_content, Mapping):
        return ()
    if not isinstance(request_content.get("parts"), list):
        return ()
    try:
        from orchestrator.multimodal_contract import attachment_manifest

        manifest = attachment_manifest(request_content)
    except Exception:
        return ()
    return tuple(
        part
        for part in manifest
        if str(part.get("modality") or "").strip().casefold() == "image"
    )


class _AcpRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"ACP RPC error {code}: {message}")
        self.code = code
        self.rpc_message = message
        self.data = data


class _DshSessionResumeError(RuntimeError):
    def __init__(self, session_id: str, cause: Exception):
        super().__init__(
            f"DSH failed to resume session {session_id}: {cause}. "
            "Cleared stale session binding so the next request re-initializes full PCM context."
        )
        self.session_id = session_id
        self.cause = cause


class _DshConfigError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        effective_model: str | None = None,
        effective_effort: str | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.effective_model = effective_model
        self.effective_effort = effective_effort


def _normalize_dsh_effort(raw: str | None) -> str:
    """Normalize explicit HASHI effort aliases without silently coercing unknown values."""

    value = str(raw or "").strip().lower()
    if value in {"zero", "none", "disabled"}:
        return "off"
    return value


def _classify_tool_event(tool_name: str, raw_input: Any) -> tuple[str, str, str]:
    """Map a DSH tool call into a HASHI StreamEvent kind, file_path, and summary."""

    name = str(tool_name or "tool").strip() or "tool"
    lower = name.lower()
    file_path = ""
    command = ""
    if isinstance(raw_input, Mapping):
        for key in ("path", "file_path", "filePath", "target_path", "filename"):
            val = raw_input.get(key)
            if isinstance(val, str) and val.strip():
                file_path = val.strip()
                break
        for key in ("command", "cmd", "script"):
            val = raw_input.get(key)
            if isinstance(val, str) and val.strip():
                command = val.strip()
                break

    if any(tok in lower for tok in ("bash", "shell", "exec", "terminal", "command", "run")):
        preview = command[:160] + ("…" if len(command) > 160 else "") if command else name
        return KIND_SHELL_EXEC, file_path, f"{name}: {preview}" if command else name
    if any(tok in lower for tok in ("edit", "write", "patch", "replace", "create", "delete", "mv", "rm")):
        return KIND_FILE_EDIT, file_path, f"{name} {file_path}".strip()
    if any(tok in lower for tok in ("read", "view", "glob", "grep", "search", "find", "list", "ls")):
        return KIND_FILE_READ, file_path, f"{name} {file_path}".strip()
    return KIND_TOOL_START, file_path, f"{name} {file_path}".strip() if file_path else name


def _format_tool_result_preview(content: Any, limit: int = 400) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        inner = item.get("content") if item.get("type") == "content" else item
        if isinstance(inner, Mapping) and inner.get("type") == "text":
            text = str(inner.get("text") or "").strip()
            if text:
                parts.append(text)
    joined = "\n".join(parts).strip()
    if len(joined) > limit:
        return joined[: limit - 1] + "…"
    return joined


_DSH_USAGE_COUNTERS = (
    "uncachedInputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "outputTokens",
)


def _dsh_home_dir() -> Path:
    """Return the DSH data root, honouring DSH_HOME when the worker has one."""

    override = str(os.environ.get("DSH_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".dsh"


def _read_dsh_session_snapshot(
    session_id: str,
) -> tuple[int, dict[str, int]] | None:
    """Read DSH's projection revision and cumulative token totals for a session.

    DSH keeps an uncompressed per-session projection in
    ``<dsh_home>/storages/session_projcache/sessions/<id>.json``; its
    ``record.rows.tokenUsage.val`` carries the real provider counters
    (including prompt-cache reads) plus a monotonically increasing ``seq``.
    Anything unreadable or malformed returns ``None`` so the caller keeps the
    existing local estimate instead of inventing usage numbers.
    """

    sid = str(session_id or "").strip()
    if not sid:
        return None
    path = (
        _dsh_home_dir()
        / "storages"
        / "session_projcache"
        / "sessions"
        / f"{sid}.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    record = payload.get("record") if isinstance(payload, Mapping) else None
    rows = record.get("rows") if isinstance(record, Mapping) else None
    token_usage = rows.get("tokenUsage") if isinstance(rows, Mapping) else None
    if not isinstance(token_usage, Mapping):
        return None
    value = token_usage.get("val")
    totals = value.get("totals") if isinstance(value, Mapping) else None
    if not isinstance(totals, Mapping):
        return None
    counters: dict[str, int] = {}
    for name in _DSH_USAGE_COUNTERS:
        counter = totals.get(name, 0)
        if isinstance(counter, bool) or not isinstance(counter, (int, float)):
            return None
        counters[name] = max(0, int(counter))
    seq = token_usage.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, (int, float)):
        seq = 0
    return max(0, int(seq)), counters


def _read_dsh_session_totals(session_id: str) -> dict[str, int] | None:
    """Return only the cumulative counters from the session projection."""

    snapshot = _read_dsh_session_snapshot(session_id)
    return snapshot[1] if snapshot is not None else None


_DSH_USAGE_FLUSH_BUDGET_SEC = 0.75
_DSH_USAGE_FLUSH_POLL_SEC = 0.025


async def _await_dsh_usage_snapshot(
    session_id: str,
    baseline: tuple[int, dict[str, int]] | None,
) -> tuple[int, dict[str, int]] | None:
    """Wait briefly for DSH to persist the counters for the turn just finished.

    DSH writes the session projection a moment after the ACP prompt response,
    so the first read can still show the previous turn.  The wait is keyed on
    the projection revision and tightly bounded, so a missing or stalled store
    falls back to the local estimate instead of delaying the turn.
    """

    baseline_seq = baseline[0] if baseline is not None else None
    baseline_totals = baseline[1] if baseline is not None else None
    deadline = time.monotonic() + _DSH_USAGE_FLUSH_BUDGET_SEC
    snapshot = _read_dsh_session_snapshot(session_id)
    while True:
        if snapshot is not None:
            seq, totals = snapshot
            if baseline_seq is None:
                if any(totals.values()):
                    return snapshot
            elif seq > baseline_seq and totals != baseline_totals:
                return snapshot
        if time.monotonic() >= deadline:
            return snapshot
        await asyncio.sleep(_DSH_USAGE_FLUSH_POLL_SEC)
        snapshot = _read_dsh_session_snapshot(session_id)


def _dsh_turn_usage(
    before: Mapping[str, int] | None,
    after: Mapping[str, int] | None,
) -> dict[str, int] | None:
    """Return one turn's counter delta, or ``None`` when it is not usable.

    A fresh session legitimately starts with an empty snapshot, so a missing
    ``before`` counts as all zeros.  Negative deltas mean the store was reset
    or compacted mid-turn; keeping the estimate is safer than reporting a
    fabricated negative turn.
    """

    if after is None:
        return None
    baseline = before or {}
    delta: dict[str, int] = {}
    for name in _DSH_USAGE_COUNTERS:
        previous = int(baseline.get(name, 0) or 0)
        delta[name] = int(after.get(name, 0) or 0) - previous
    if any(value < 0 for value in delta.values()):
        return None
    if not delta["uncachedInputTokens"] and not delta["cacheReadTokens"] and not delta["outputTokens"]:
        return None
    return delta


def _select_permission_option(options: Any) -> str:
    """Pick an allow option for full-permission autonomous execution."""

    if isinstance(options, list):
        for opt in options:
            if not isinstance(opt, Mapping):
                continue
            kind = str(opt.get("kind") or "").strip().lower()
            opt_id = str(opt.get("optionId") or "").strip()
            if kind in {"allow_always", "allow_once"} and opt_id:
                return opt_id
        for opt in options:
            if not isinstance(opt, Mapping):
                continue
            opt_id = str(opt.get("optionId") or "").strip()
            if "allow" in opt_id.lower():
                return opt_id
    return "allow-once"


def _match_model_option(
    config_options: list[dict[str, Any]],
    desired_model: str | None,
) -> tuple[str | None, str | None]:
    """Match a HASHI model name against ACP configOptions.

    Returns ``(matched_opaque_value, current_opaque_value)``.
    """

    model_opt = next(
        (
            item
            for item in config_options
            if isinstance(item, Mapping) and item.get("id") == "model"
        ),
        None,
    )
    if not isinstance(model_opt, Mapping):
        return None, None

    current_val = str(model_opt.get("currentValue") or "").strip() or None
    target = str(desired_model or "").strip()
    if not target:
        return current_val, current_val

    flat_options: list[tuple[str, str, str]] = []  # (value, group, name)
    for entry in model_opt.get("options") or []:
        if not isinstance(entry, Mapping):
            continue
        if isinstance(entry.get("options"), list):
            group = str(entry.get("group") or "").strip()
            for sub in entry["options"]:
                if isinstance(sub, Mapping) and sub.get("value") is not None:
                    flat_options.append(
                        (
                            str(sub["value"]),
                            group,
                            str(sub.get("name") or "").strip(),
                        )
                    )
        elif entry.get("value") is not None:
            flat_options.append(
                (str(entry["value"]), "", str(entry.get("name") or "").strip())
            )

    target_lower = target.lower()
    # 1. Exact opaque value match (e.g. '["deepseek-official","deepseek-v4-pro"]')
    for val, _group, _name in flat_options:
        if val == target:
            return val, current_val

    # 2. Match decoded [provider, model] tuple or "provider/model"
    for val, group, _name in flat_options:
        provider = group
        model_id = val
        try:
            decoded = json.loads(val)
            if isinstance(decoded, list) and len(decoded) == 2:
                provider = str(decoded[0] or "").strip()
                model_id = str(decoded[1] or "").strip()
        except Exception:
            pass
        if model_id.lower() == target_lower:
            return val, current_val
        if provider and f"{provider}/{model_id}".lower() == target_lower:
            return val, current_val

    # 3. Display name fallback
    for val, _group, name in flat_options:
        if name and name.lower() == target_lower:
            return val, current_val

    return None, current_val


def _decode_opaque_model_value(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
        if isinstance(decoded, list) and len(decoded) == 2:
            provider = str(decoded[0] or "").strip()
            model_id = str(decoded[1] or "").strip()
            return f"{provider}/{model_id}" if provider and model_id else (model_id or raw)
    except Exception:
        pass
    return raw


def _extract_effective_config(
    config_options: list[dict[str, Any]],
) -> tuple[str | None, str | None, list[str], list[str]]:
    """Return ``(effective_model, effective_effort, available_models, available_efforts)``."""

    effective_model: str | None = None
    effective_effort: str | None = None
    available_models: list[str] = []
    available_efforts: list[str] = []

    for item in config_options:
        if not isinstance(item, Mapping):
            continue
        cid = item.get("id")
        if cid == "model":
            effective_model = _decode_opaque_model_value(item.get("currentValue"))
            for entry in item.get("options") or []:
                if not isinstance(entry, Mapping):
                    continue
                sub_opts = entry.get("options")
                if isinstance(sub_opts, list):
                    for sub in sub_opts:
                        if isinstance(sub, Mapping) and sub.get("value") is not None:
                            label = _decode_opaque_model_value(str(sub["value"]))
                            if label:
                                available_models.append(label)
                elif entry.get("value") is not None:
                    label = _decode_opaque_model_value(str(entry["value"]))
                    if label:
                        available_models.append(label)
        elif cid == "reasoning_effort":
            val = str(item.get("currentValue") or "").strip()
            effective_effort = val or None
            for opt in item.get("options") or []:
                if isinstance(opt, Mapping) and opt.get("value") is not None:
                    opt_val = str(opt["value"]).strip()
                    if opt_val:
                        available_efforts.append(opt_val)

    return effective_model, effective_effort, available_models, available_efforts


def _match_effort_option(
    config_options: list[dict[str, Any]],
    desired_effort: str | None,
) -> tuple[str | None, str | None, list[str]]:
    effort_opt = next(
        (
            item
            for item in config_options
            if isinstance(item, Mapping) and item.get("id") == "reasoning_effort"
        ),
        None,
    )
    if not isinstance(effort_opt, Mapping):
        return None, None, []
    current_val = str(effort_opt.get("currentValue") or "").strip()
    normalized = _normalize_dsh_effort(desired_effort)
    valid_values = [
        str(opt.get("value") or "").strip()
        for opt in (effort_opt.get("options") or [])
        if isinstance(opt, Mapping) and str(opt.get("value") or "").strip()
    ]
    if normalized and normalized in valid_values:
        return normalized, current_val, valid_values
    return None, current_val, valid_values


class DshCLIAdapter(BaseBackend):
    """HASHI adapter for DeepSeek Harness (DSH) via persistent ACP stdio."""

    DEFAULT_IDLE_TIMEOUT_SEC = 60 * 60
    SUPPORTED_REASONING_EFFORTS = _SUPPORTED_REASONING_EFFORTS
    DEFAULT_REASONING_EFFORT = "high"

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=True,
            supports_commentary_stream=True,
            supports_headless_mode=True,
            supports_progress_stream=True,
            supports_tool_stream=True,
            supports_answer_stream=True,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.DSH.{self.config.name}")
        self.cmd_base = getattr(self.global_config, "dsh_cmd", "dsh")
        self._profile = self._extra_str("dsh_profile", "acp") or "acp"
        raw_patches = (self.config.extra or {}).get("dsh_patches") or ()
        self._patches: tuple[str, ...] = (
            tuple(str(p).strip() for p in raw_patches if str(p).strip())
            if isinstance(raw_patches, (list, tuple))
            else ()
        )
        raw_configured_effort = self._extra_str("effort", "")
        self._effort: str = _normalize_dsh_effort(
            raw_configured_effort or self.DEFAULT_REASONING_EFFORT
        )
        self._effort_explicit: bool = bool(raw_configured_effort)
        self._session_mode: bool = bool(
            (self.config.extra or {}).get("session_mode", False)
        )
        self._session_id: str | None = None
        self._session_in_current_worker: bool = False
        self._config_options: list[dict[str, Any]] = []
        self._effective_model: str | None = None
        self._effective_effort: str | None = None
        self._image_prompts_supported: bool = False
        self._hashi_mcp_enabled: bool = False
        self._active_run_mcp_invocation: HashiMcpInvocation | None = None
        self._last_initialize_error: str = ""

        # Persistent ACP stdio worker state
        self._request_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=60)
        self._next_rpc_id: int = 0
        self._pending_rpcs: dict[int, asyncio.Future[Any]] = {}
        self._initialized: bool = False

        # Active turn state for streaming and graceful /stop cancellation
        self._active_turn_session_id: str | None = None
        self._active_prompt_rpc_id: int | None = None
        self._update_handler: Callable[[dict[str, Any]], None] | None = None

    @property
    def effort(self) -> str:
        return self._effort

    @effort.setter
    def effort(self, value: str | None) -> None:
        self._effort = _normalize_dsh_effort(value)
        self._effort_explicit = bool(self._effort)

    def _extra_str(self, key: str, default: str = "") -> str:
        extra = getattr(self.config, "extra", {}) or {}
        value = extra.get(key, default)
        return str(value or "").strip()

    def _resolve_launch(self) -> tuple[list[str], Path | None, str]:
        return resolve_dsh_invocation(
            self.cmd_base,
            profile=self._profile,
            patches=self._patches,
        )

    async def initialize(self) -> bool:
        self.logger.info("Initializing DSH (DeepSeek Harness ACP) backend...")
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        argv, _launch_cwd, label = self._resolve_launch()
        problem = _launch_problem(argv)
        if problem:
            self._last_initialize_error = f"{problem} (launch: {label})"
            self.logger.error(
                "DSH ACP backend not accessible (%s): %s", label, problem
            )
            return False
        self._last_initialize_error = ""
        self.logger.info(
            "DSH ACP backend command verified (%s): %s", label, argv[0]
        )
        self._probe_hashi_mcp()
        return True

    @property
    def last_initialize_error(self) -> str:
        return self._last_initialize_error

    def _probe_hashi_mcp(self) -> None:
        try:
            descriptor = prepare_hashi_mcp(self, backend="dsh-cli")
            if descriptor is not None:
                descriptor.close()
        except Exception as exc:
            self._hashi_mcp_enabled = False
            self.logger.debug("DSH HASHI MCP probe skipped during initialize: %s", exc)

    def set_session_mode(self, enabled: bool) -> None:
        self._session_mode = bool(enabled)
        if not self._session_mode:
            self._session_id = None
            self._session_in_current_worker = False
        self.logger.info("DSH session mode set to %s", "ON" if enabled else "OFF")

    async def handle_new_session(self) -> bool:
        old_sid = self._session_id
        self._session_id = None
        self._session_in_current_worker = False
        if old_sid and self._is_worker_alive():
            try:
                await self._rpc_call(
                    "session/close",
                    {"sessionId": old_sid},
                    timeout=5.0,
                )
            except Exception as exc:
                self.logger.debug("DSH session/close on reset failed: %s", exc)
        self._close_run_mcp()
        self.logger.info("DSH session reset (previous session_id=%s).", old_sid)
        return True

    def interrupt_nowait(self, reason: str = "USER_STOP") -> bool:
        """Gracefully cancel an active DSH ACP turn without killing the warm worker."""

        session_id = self._active_turn_session_id
        prompt_rpc_id = self._active_prompt_rpc_id
        loop = self._loop
        if (
            session_id
            and loop is not None
            and not loop.is_closed()
            and self._is_worker_alive()
        ):
            try:
                loop.call_soon_threadsafe(
                    self._schedule_graceful_cancel,
                    session_id,
                    prompt_rpc_id,
                    reason,
                )
                return True
            except Exception as exc:
                self.logger.warning(
                    "Failed to schedule graceful DSH cancel (reason=%r): %s",
                    reason,
                    exc,
                )
        return False

    def _schedule_graceful_cancel(
        self,
        session_id: str,
        prompt_rpc_id: int | None,
        reason: str,
    ) -> None:
        asyncio.create_task(
            self._send_cancel_notifications(session_id, prompt_rpc_id, reason)
        )

    async def _send_cancel_notifications(
        self,
        session_id: str,
        prompt_rpc_id: int | None,
        reason: str,
    ) -> None:
        self.logger.info(
            "Sending ACP session/cancel for session=%s reason=%r",
            session_id,
            reason,
        )
        try:
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": session_id},
                }
            )
            if prompt_rpc_id is not None:
                await self._send_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "$/cancel_request",
                        "params": {"requestId": prompt_rpc_id},
                    }
                )
        except Exception as exc:
            self.logger.debug("Failed to send ACP cancel frame: %s", exc)

    def _is_worker_alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self._proc.stdin is not None
            and not self._proc.stdin.is_closing()
        )

    def _close_run_mcp(self, invocation: HashiMcpInvocation | None = None) -> None:
        inv = invocation if invocation is not None else self._active_run_mcp_invocation
        if inv is self._active_run_mcp_invocation:
            self._active_run_mcp_invocation = None
        if inv is not None:
            try:
                inv.close()
            except Exception as exc:
                self.logger.debug("Error closing DSH HASHI MCP invocation: %s", exc)

    async def _stop_worker(self, reason: str = "shutdown") -> None:
        proc = self._proc
        self._proc = None
        self._initialized = False
        self._session_in_current_worker = False
        self._active_turn_session_id = None
        self._active_prompt_rpc_id = None
        self._update_handler = None

        # Reject any pending RPC futures immediately
        pending = list(self._pending_rpcs.values())
        self._pending_rpcs.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(RuntimeError(f"DSH ACP worker stopped ({reason})"))

        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None and not proc.stdin.is_closing():
                    proc.stdin.close()
                    try:
                        await asyncio.wait_for(proc.stdin.wait_closed(), timeout=2.0)
                    except Exception:
                        pass
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except Exception:
                await self.force_kill_process_tree(proc, self.logger, reason=f"dsh {reason}")

        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = [t for t in (self._reader_task, self._stderr_task) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None

    async def shutdown(self):
        if self._session_id and self._is_worker_alive():
            try:
                await self._rpc_call(
                    "session/close",
                    {"sessionId": self._session_id},
                    timeout=3.0,
                )
            except Exception:
                pass
        await self._stop_worker(reason="shutdown")
        self._close_run_mcp()

    async def _ensure_worker(self) -> None:
        if self._is_worker_alive() and self._initialized:
            return
        await self._stop_worker(reason="restart")

        argv, launch_cwd, label = self._resolve_launch()
        self.logger.info("Starting persistent DSH ACP worker (%s)...", label)
        self._stderr_tail.clear()
        self._loop = asyncio.get_running_loop()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(launch_cwd) if launch_cwd else None,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_ACP_STDOUT_LIMIT,
            **process_group_kwargs(),
        )
        self._proc = proc
        self._reader_task = asyncio.create_task(self._reader_loop(proc))
        self._stderr_task = asyncio.create_task(self._stderr_loop(proc))

        init_result = await self._rpc_call(
            "initialize",
            {
                "protocolVersion": _ACP_PROTOCOL_VERSION,
                "clientInfo": {"name": "hashi-dsh-adapter", "version": "1.0.0"},
            },
            timeout=_ACP_RPC_TIMEOUT_SEC,
        )
        caps = (
            init_result.get("agentCapabilities", {})
            if isinstance(init_result, Mapping)
            else {}
        )
        prompt_caps = (
            caps.get("promptCapabilities", {}) if isinstance(caps, Mapping) else {}
        )
        self._image_prompts_supported = bool(prompt_caps.get("image", False))
        self._initialized = True
        self.logger.info(
            "DSH ACP worker initialized (pid=%s, image_prompts=%s)",
            proc.pid,
            self._image_prompts_supported,
        )

    async def _send_frame(self, payload: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdin.is_closing():
            raise RuntimeError("DSH ACP worker stdin is closed")
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            proc.stdin.write(raw)
            await proc.stdin.drain()

    async def _rpc_call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = _ACP_RPC_TIMEOUT_SEC,
        on_rpc_id: Callable[[int], None] | None = None,
    ) -> Any:
        self._next_rpc_id += 1
        rpc_id = self._next_rpc_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending_rpcs[rpc_id] = fut
        if on_rpc_id is not None:
            on_rpc_id(rpc_id)
        try:
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "method": method,
                    "params": dict(params or {}),
                }
            )
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_rpcs.pop(rpc_id, None)

    async def _reader_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                text = raw_line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    self.logger.debug("Ignoring non-JSON line from DSH stdout: %s", text[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                await self._dispatch_incoming(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning("DSH ACP reader loop error: %s", exc)
        finally:
            stderr_preview = " | ".join(list(self._stderr_tail)[-5:])
            err_msg = "DSH ACP worker exited unexpectedly"
            if stderr_preview:
                err_msg += f" (stderr: {stderr_preview})"
            for fut in list(self._pending_rpcs.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(err_msg))
            self._pending_rpcs.clear()
            self._initialized = False
            self._session_in_current_worker = False

    async def _stderr_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        try:
            while True:
                raw_line = await proc.stderr.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_tail.append(line)
                    self.logger.info("[dsh stderr] %s", line)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _dispatch_incoming(self, msg: dict[str, Any]) -> None:
        has_id = "id" in msg and msg["id"] is not None
        has_method = isinstance(msg.get("method"), str)

        # 1. Response to our outgoing request
        if has_id and not has_method:
            rpc_id = msg["id"]
            fut = self._pending_rpcs.get(rpc_id)
            if fut is not None and not fut.done():
                if "error" in msg and msg["error"] is not None:
                    err = msg["error"] if isinstance(msg["error"], Mapping) else {}
                    fut.set_exception(
                        _AcpRpcError(
                            int(err.get("code", -32000)),
                            str(err.get("message", "unknown ACP error")),
                            err.get("data"),
                        )
                    )
                else:
                    fut.set_result(msg.get("result"))
            return

        # 2. Server-to-client request (e.g. session/request_permission)
        if has_id and has_method:
            method = msg["method"]
            req_id = msg["id"]
            params = msg.get("params") if isinstance(msg.get("params"), Mapping) else {}
            if method == "session/request_permission":
                option_id = _select_permission_option(params.get("options"))
                self.logger.debug(
                    "Auto-approving DSH permission request (session=%s, option=%s)",
                    params.get("sessionId"),
                    option_id,
                )
                await self._send_frame(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "outcome": {
                                "outcome": "selected",
                                "optionId": option_id,
                            }
                        },
                    }
                )
                return
            await self._send_frame(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found: {method}",
                    },
                }
            )
            return

        # 3. Server notification (e.g. session/update)
        if has_method and msg["method"] == "session/update":
            params = msg.get("params")
            if isinstance(params, dict):
                update = params.get("update")
                if (
                    isinstance(update, Mapping)
                    and update.get("sessionUpdate") == "config_option_update"
                    and isinstance(update.get("configOptions"), list)
                ):
                    self._config_options = list(update["configOptions"])
                handler = self._update_handler
                if handler is not None:
                    handler(params)

    def _build_run_mcp_servers(self) -> tuple[HashiMcpInvocation | None, list[dict[str, Any]]]:
        """Prepare a strictly Run-scoped HASHI MCP invocation for the current turn."""

        if getattr(self, "tool_registry", None) is None:
            self._hashi_mcp_enabled = False
            return None, []
        descriptor = prepare_hashi_mcp(self, backend="dsh-cli")
        if not descriptor:
            return None, []
        invocation = descriptor if isinstance(descriptor, HashiMcpInvocation) else None
        self._active_run_mcp_invocation = invocation
        command_path = str(Path(descriptor["command"]).resolve())
        project_root = str(Path(descriptor["cwd"]).resolve())
        existing_pythonpath = os.environ.get("PYTHONPATH", "").strip()
        pythonpath = (
            f"{project_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else project_root
        )
        server_decl = {
            "name": str(descriptor.get("name") or "hashi_tools"),
            "command": command_path,
            "args": [str(a) for a in (descriptor.get("args") or [])],
            "env": [{"name": "PYTHONPATH", "value": pythonpath}],
        }
        return invocation, [server_decl]

    async def _close_in_worker_session(self, session_id: str, *, clear_id: bool) -> None:
        if clear_id and self._session_id == session_id:
            self._session_id = None
        self._session_in_current_worker = False
        if session_id and self._is_worker_alive():
            try:
                await asyncio.shield(
                    self._rpc_call("session/close", {"sessionId": session_id}, timeout=5.0)
                )
            except Exception as exc:
                self.logger.debug("DSH session/close(%s) failed: %s", session_id, exc)

    async def _ensure_session(self, *, mcp_servers: list[dict[str, Any]]) -> tuple[str, bool]:
        workdir = str(self.effective_workdir.resolve())

        # Stateless / flex mode: always close prior ephemeral session before opening a fresh one.
        if not self._session_mode and self._session_id and self._session_in_current_worker:
            await self._close_in_worker_session(self._session_id, clear_id=True)

        # If this Run requires fresh Run-scoped HASHI MCP servers and the session is still open
        # in the warm worker from an earlier Run, close only the in-worker session scope so
        # session/resume below mounts the current Run's fresh MCP authority while keeping the
        # warm ACP worker and persisted DSH session intact.
        if (
            self._session_mode
            and self._session_id
            and self._session_in_current_worker
            and mcp_servers
        ):
            await self._close_in_worker_session(self._session_id, clear_id=False)

        if self._session_mode and self._session_id and self._session_in_current_worker:
            return self._session_id, False

        # Persistent fixed mode with an existing session_id: MUST resume that exact session.
        # Never silently fall through to session/new because HASHI's runtime_pipeline has
        # already prepared an incremental prompt assuming this session's PCM context exists.
        if self._session_mode and self._session_id and not self._session_in_current_worker:
            old_sid = self._session_id
            try:
                resumed = await self._rpc_call(
                    "session/resume",
                    {
                        "sessionId": old_sid,
                        "cwd": workdir,
                        "mcpServers": mcp_servers,
                    },
                    timeout=_ACP_RPC_TIMEOUT_SEC,
                )
            except Exception as exc:
                self._session_id = None
                self._session_in_current_worker = False
                self.logger.error(
                    "DSH session/resume failed for %s (%s); refusing to send incremental prompt to a fresh session.",
                    old_sid,
                    exc,
                )
                raise _DshSessionResumeError(old_sid, exc) from exc

            if isinstance(resumed, Mapping) and isinstance(resumed.get("configOptions"), list):
                self._config_options = list(resumed["configOptions"])
            self._session_in_current_worker = True
            self.logger.info("Resumed DSH ACP session %s", old_sid)
            return old_sid, False

        # Fresh session (first turn or after explicit /new / reset).
        created = await self._rpc_call(
            "session/new",
            {"cwd": workdir, "mcpServers": mcp_servers},
            timeout=_ACP_RPC_TIMEOUT_SEC,
        )
        sid = str((created or {}).get("sessionId") or "").strip()
        if not sid:
            raise RuntimeError(f"DSH session/new returned no sessionId: {created!r}")
        if isinstance(created, Mapping) and isinstance(created.get("configOptions"), list):
            self._config_options = list(created["configOptions"])
        self._session_id = sid
        self._session_in_current_worker = True
        self.logger.info("Created DSH ACP session %s (cwd=%s)", sid, workdir)
        return sid, True

    async def _sync_session_config(
        self,
        session_id: str,
        *,
        desired_effort: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Apply requested model and reasoning_effort truthfully against live DSH configOptions."""

        # 1. Model selection
        desired_model = str(getattr(self.config, "model", "") or "").strip()
        if desired_model:
            matched_model, current_model = _match_model_option(
                self._config_options, desired_model
            )
            eff_model, eff_effort, avail_models, _avail_efforts = _extract_effective_config(
                self._config_options
            )
            self._effective_model = eff_model
            self._effective_effort = eff_effort
            if matched_model is None:
                raise _DshConfigError(
                    "dsh_model_unavailable",
                    (
                        f"Requested DSH model {desired_model!r} is not available in DSH catalog; "
                        f"effective model remains {eff_model!r} "
                        f"(available: {', '.join(avail_models) or 'none'})."
                    ),
                    effective_model=eff_model,
                    effective_effort=eff_effort,
                )
            if matched_model != current_model:
                try:
                    resp = await self._rpc_call(
                        "session/set_config_option",
                        {
                            "sessionId": session_id,
                            "configId": "model",
                            "value": matched_model,
                        },
                        timeout=_ACP_RPC_TIMEOUT_SEC,
                    )
                except Exception as exc:
                    raise _DshConfigError(
                        "dsh_model_set_failed",
                        (
                            f"DSH rejected requested model {desired_model!r}: {exc}; "
                            f"effective model remains {eff_model!r}."
                        ),
                        effective_model=eff_model,
                        effective_effort=eff_effort,
                    ) from exc
                if isinstance(resp, Mapping) and isinstance(resp.get("configOptions"), list):
                    self._config_options = list(resp["configOptions"])
                _, updated_model = _match_model_option(self._config_options, desired_model)
                if updated_model != matched_model:
                    actual_model, actual_effort, _, _ = _extract_effective_config(
                        self._config_options
                    )
                    self._effective_model = actual_model
                    self._effective_effort = actual_effort
                    raise _DshConfigError(
                        "dsh_model_mismatch",
                        (
                            f"DSH did not apply requested model {desired_model!r}; "
                            f"effective model is {actual_model!r}."
                        ),
                        effective_model=actual_model,
                        effective_effort=actual_effort,
                    )
                self.logger.info("DSH model switched to %s", matched_model)

        # 2. Reasoning effort selection
        effort_explicit = desired_effort is not None or self._effort_explicit
        raw_effort = desired_effort if desired_effort is not None else self._effort
        normalized_effort = _normalize_dsh_effort(raw_effort)
        if normalized_effort:
            matched_effort, current_effort, avail_efforts = _match_effort_option(
                self._config_options, normalized_effort
            )
            eff_model, eff_effort, _, _ = _extract_effective_config(self._config_options)
            self._effective_model = eff_model
            self._effective_effort = eff_effort
            if matched_effort is None:
                if effort_explicit:
                    raise _DshConfigError(
                        "dsh_effort_unavailable",
                        (
                            f"Requested DSH reasoning_effort {raw_effort!r} is not supported "
                            f"for effective model {eff_model!r}; effective effort remains "
                            f"{eff_effort!r} (available: {', '.join(avail_efforts) or 'none'})."
                        ),
                        effective_model=eff_model,
                        effective_effort=eff_effort,
                    )
            elif matched_effort != current_effort:
                try:
                    resp = await self._rpc_call(
                        "session/set_config_option",
                        {
                            "sessionId": session_id,
                            "configId": "reasoning_effort",
                            "value": matched_effort,
                        },
                        timeout=_ACP_RPC_TIMEOUT_SEC,
                    )
                except Exception as exc:
                    raise _DshConfigError(
                        "dsh_effort_set_failed",
                        (
                            f"DSH rejected requested reasoning_effort {raw_effort!r}: {exc}; "
                            f"effective effort remains {eff_effort!r}."
                        ),
                        effective_model=eff_model,
                        effective_effort=eff_effort,
                    ) from exc
                if isinstance(resp, Mapping) and isinstance(resp.get("configOptions"), list):
                    self._config_options = list(resp["configOptions"])
                _, updated_effort, _ = _match_effort_option(
                    self._config_options, normalized_effort
                )
                if updated_effort != matched_effort:
                    actual_model, actual_effort, _, _ = _extract_effective_config(
                        self._config_options
                    )
                    self._effective_model = actual_model
                    self._effective_effort = actual_effort
                    raise _DshConfigError(
                        "dsh_effort_mismatch",
                        (
                            f"DSH did not apply requested reasoning_effort {raw_effort!r}; "
                            f"effective effort is {actual_effort!r}."
                        ),
                        effective_model=actual_model,
                        effective_effort=actual_effort,
                    )
                self.logger.info("DSH reasoning_effort switched to %s", matched_effort)

        eff_model, eff_effort, _, _ = _extract_effective_config(self._config_options)
        self._effective_model = eff_model
        self._effective_effort = eff_effort
        return eff_model, eff_effort

    def _build_prompt_blocks(
        self,
        prompt: str,
        request_content: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if prompt:
            blocks.append({"type": "text", "text": prompt})

        # Legacy compatibility shape: ``content_blocks``/``attachments`` carrying
        # local paths.  Canonical production requests instead use the contract v1
        # ``parts`` envelope (``type: media``, ``modality: image``, ``local_ref``).
        raw_blocks: list[Any] = []
        if isinstance(request_content, Mapping):
            candidate_blocks = request_content.get("content_blocks") or request_content.get("attachments") or []
            if isinstance(candidate_blocks, list):
                raw_blocks = candidate_blocks
        legacy_images: list[tuple[Mapping[str, Any], Path, str]] = []
        for item in raw_blocks:
            if not isinstance(item, Mapping):
                continue
            path_str = str(item.get("path") or item.get("file_path") or "").strip()
            if not path_str:
                continue
            mime = str(item.get("mime_type") or item.get("mimeType") or "").strip().lower()
            if not mime:
                mime = (mimetypes.guess_type(path_str)[0] or "").lower()
            if mime not in _SUPPORTED_IMAGE_MIMES:
                continue
            legacy_images.append((item, Path(path_str), mime))

        attached_legacy = 0
        if self._image_prompts_supported:
            for _item, img_path, mime in legacy_images:
                if not img_path.is_file():
                    continue
                try:
                    raw_bytes = img_path.read_bytes()
                    blocks.append(
                        {
                            "type": "image",
                            "mimeType": mime,
                            "data": base64.b64encode(raw_bytes).decode("ascii"),
                        }
                    )
                    attached_legacy += 1
                except Exception as exc:
                    self.logger.debug("Skipping unreadable image %s: %s", img_path, exc)

        # Canonical image attachments have no DSH transport here, so they are
        # always among the images that were not transmitted.
        canonical_images = _canonical_image_parts(request_content)
        unattached_legacy = len(legacy_images) - attached_legacy
        unattached = unattached_legacy + len(canonical_images)
        if unattached:
            self.logger.warning(
                "DSH ACP did not transmit %d image attachment(s) "
                "(canonical=%d, legacy=%d, image_prompts=%s).",
                unattached,
                len(canonical_images),
                unattached_legacy,
                self._image_prompts_supported,
            )

        if not blocks:
            blocks.append({"type": "text", "text": " "})
        return blocks

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
        request_content: Mapping[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> BackendResponse:
        async with self._request_lock:
            return await self._generate_response_locked(
                prompt,
                request_id,
                is_retry=is_retry,
                silent=silent,
                on_stream_event=on_stream_event,
                request_content=request_content,
                reasoning_effort=reasoning_effort,
            )

    async def _generate_response_locked(
        self,
        prompt: str,
        request_id: str,
        *,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
        request_content: Mapping[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> BackendResponse:
        started = time.monotonic()
        self._touch_activity()
        run_mcp_invocation: HashiMcpInvocation | None = None
        session_id: str | None = None
        created_fresh_session = False
        session_committed = False
        effective_model: str | None = None
        effective_effort: str | None = None

        try:
            await self._ensure_worker()
            run_mcp_invocation, mcp_servers = self._build_run_mcp_servers()
            session_id, created_fresh_session = await self._ensure_session(
                mcp_servers=mcp_servers
            )
            effective_model, effective_effort = await self._sync_session_config(
                session_id,
                desired_effort=reasoning_effort,
            )
            # ACP ``usage_update`` only carries context size, so snapshot DSH's
            # own cumulative counters here and price this turn's delta below.
            usage_before = _read_dsh_session_snapshot(session_id)

            # Turn-level streaming state
            held_message_id: str | None = None
            held_text_parts: list[str] = []
            tool_titles: dict[str, str] = {}
            tool_call_count = 0
            latest_usage: dict[str, int] | None = None
            thinking_chars = 0
            thinking_event_count = 0

            def _flush_held_as_commentary() -> None:
                nonlocal held_message_id, held_text_parts
                if not held_text_parts:
                    return
                commentary = "".join(held_text_parts).strip()
                held_text_parts = []
                held_message_id = None
                if commentary and on_stream_event is not None:
                    self._schedule_stream_event(
                        on_stream_event,
                        StreamEvent(
                            kind=KIND_COMMENTARY,
                            summary=commentary,
                            delivery_class=DELIVERY_USER_COMMENTARY,
                            origin="dsh-cli:commentary",
                        ),
                    )

            def _handle_update(params: dict[str, Any]) -> None:
                nonlocal held_message_id, latest_usage, tool_call_count
                nonlocal thinking_chars, thinking_event_count
                if params.get("sessionId") != session_id:
                    return
                update = params.get("update")
                if not isinstance(update, Mapping):
                    return
                self._touch_activity()
                utype = str(update.get("sessionUpdate") or "")

                if utype == "agent_thought_chunk":
                    content = update.get("content")
                    if isinstance(content, Mapping) and content.get("type") == "text":
                        thought = str(content.get("text") or "")
                        if thought:
                            thinking_chars += len(thought)
                            thinking_event_count += 1
                        if thought and on_stream_event is not None:
                            self._schedule_stream_event(
                                on_stream_event,
                                StreamEvent(
                                    kind=KIND_THINKING,
                                    summary=thought,
                                    raw_delta=thought,
                                    delivery_class=DELIVERY_REASONING,
                                    origin="dsh-cli:thought",
                                ),
                            )
                    return

                if utype == "agent_message_chunk":
                    content = update.get("content")
                    if isinstance(content, Mapping) and content.get("type") == "text":
                        chunk = str(content.get("text") or "")
                        if not chunk:
                            return
                        msg_id = str(update.get("messageId") or "")
                        if held_message_id and msg_id and msg_id != held_message_id and held_text_parts:
                            _flush_held_as_commentary()
                        if msg_id:
                            held_message_id = msg_id
                        held_text_parts.append(chunk)
                        if on_stream_event is not None:
                            self._schedule_stream_event(
                                on_stream_event,
                                StreamEvent(
                                    kind=KIND_PROVIDER_ACTIVITY,
                                    summary="DSH generating response",
                                    origin="dsh-cli:activity",
                                ),
                            )
                    return

                if utype == "tool_call":
                    # Any assistant text emitted right before a tool call is commentary!
                    _flush_held_as_commentary()
                    tool_call_count += 1
                    call_id = str(update.get("toolCallId") or "")
                    title = str(update.get("title") or "tool")
                    if call_id:
                        tool_titles[call_id] = title
                    raw_input = update.get("rawInput")
                    kind, file_path, summary = _classify_tool_event(title, raw_input)
                    detail = ""
                    if raw_input is not None:
                        try:
                            detail = json.dumps(raw_input, ensure_ascii=False)[:600]
                        except Exception:
                            detail = str(raw_input)[:600]
                    if on_stream_event is not None:
                        self._schedule_stream_event(
                            on_stream_event,
                            StreamEvent(
                                kind=kind,
                                summary=summary,
                                detail=detail,
                                tool_name=title,
                                file_path=file_path,
                                delivery_class=DELIVERY_TECHNICAL,
                                origin="dsh-cli:tool",
                            ),
                        )
                    return

                if utype == "tool_call_update":
                    call_id = str(update.get("toolCallId") or "")
                    title = tool_titles.get(call_id) or str(update.get("title") or "tool")
                    status = str(update.get("status") or "completed")
                    preview = _format_tool_result_preview(update.get("content"))
                    if on_stream_event is not None:
                        self._schedule_stream_event(
                            on_stream_event,
                            StreamEvent(
                                kind=KIND_ERROR if status == "failed" else KIND_TOOL_END,
                                summary=f"{title} ({status})",
                                detail=preview,
                                tool_name=title,
                                delivery_class=DELIVERY_TECHNICAL,
                                origin="dsh-cli:tool",
                            ),
                        )
                    return

                if utype == "usage_update":
                    used = update.get("used")
                    size = update.get("size")
                    if isinstance(used, int) and isinstance(size, int):
                        latest_usage = {"used": used, "size": size}
                    return

            self._active_turn_session_id = session_id
            self._active_prompt_rpc_id = None
            self._update_handler = _handle_update

            prompt_blocks = self._build_prompt_blocks(prompt, request_content)
            prompt_task = asyncio.create_task(
                self._rpc_call(
                    "session/prompt",
                    {"sessionId": session_id, "prompt": prompt_blocks},
                    timeout=None,
                    on_rpc_id=lambda rid: setattr(self, "_active_prompt_rpc_id", rid),
                )
            )

            try:
                timeout_kind = await self._wait_for_task_with_timeouts(
                    prompt_task,
                    started_monotonic=started,
                )
                if timeout_kind is not None:
                    await self._settle_cancelled_prompt(
                        session_id,
                        prompt_task,
                        reason=f"timeout:{timeout_kind}:{request_id}",
                    )
                    duration_ms = (time.monotonic() - started) * 1000.0
                    return BackendResponse(
                        text="",
                        duration_ms=duration_ms,
                        is_success=False,
                        error=self._timeout_diagnostic(timeout_kind, started_monotonic=started),
                    )
                result = await prompt_task
            except asyncio.CancelledError:
                await self._settle_cancelled_prompt(session_id, prompt_task, reason=f"cancel:{request_id}")
                raise
            finally:
                self._active_turn_session_id = None
                self._active_prompt_rpc_id = None
                self._update_handler = None
                await self._drain_stream_event_tasks()

            usage_after = await _await_dsh_usage_snapshot(session_id, usage_before)
            turn_usage_delta = _dsh_turn_usage(
                usage_before[1] if usage_before is not None else None,
                usage_after[1] if usage_after is not None else None,
            )
            token_usage = (
                TokenUsage(
                    input_tokens=(
                        turn_usage_delta["uncachedInputTokens"]
                        + turn_usage_delta["cacheReadTokens"]
                    ),
                    output_tokens=turn_usage_delta["outputTokens"],
                    # DSH output counters already include reasoning tokens.
                    thinking_tokens=0,
                    prompt_cache_hit_tokens=turn_usage_delta["cacheReadTokens"],
                    prompt_cache_miss_tokens=turn_usage_delta["uncachedInputTokens"],
                )
                if turn_usage_delta is not None
                else None
            )

            final_text = "".join(held_text_parts).strip()
            stop_reason = (
                str(result.get("stopReason") or "").strip()
                if isinstance(result, Mapping)
                else ""
            )
            duration_ms = (time.monotonic() - started) * 1000.0

            if stop_reason == "cancelled":
                return BackendResponse(
                    text=final_text,
                    duration_ms=duration_ms,
                    is_success=False,
                    usage=token_usage,
                    error="Request cancelled.",
                )

            if final_text and on_stream_event is not None:
                self._schedule_stream_event(
                    on_stream_event,
                    StreamEvent(
                        kind=KIND_TEXT_DELTA,
                        summary=final_text,
                        raw_delta=final_text,
                        origin="dsh-cli:final",
                    ),
                )
                await self._drain_stream_event_tasks()

            if not final_text:
                return BackendResponse(
                    text="",
                    duration_ms=duration_ms,
                    is_success=False,
                    usage=token_usage,
                    error=f"DSH returned empty response (stopReason={stop_reason or 'unknown'})",
                )

            stream_metadata: dict[str, Any] = {
                "session_id": session_id,
                "stop_reason": stop_reason or "end_turn",
                "effective_model": effective_model,
                "effective_effort": effective_effort,
            }
            if latest_usage is not None:
                stream_metadata["context_usage"] = latest_usage
            # Reasoning volume is provider-reported by DSH through the ACP
            # thought chunks; surface it so token accounting does not lose it
            # when the request has no Telegram delivery or active audit.
            stream_metadata["thinking"] = {
                "thinking_chars": thinking_chars,
                "thinking_event_count": thinking_event_count,
                "thinking_redacted_count": 0,
                "thinking_sources": ["dsh-cli"],
            }
            stream_metadata["token_usage_source"] = (
                "dsh_session_projcache" if token_usage is not None else "estimated"
            )

            session_committed = True
            return BackendResponse(
                text=final_text,
                duration_ms=duration_ms,
                is_success=True,
                stop_reason=stop_reason or "end_turn",
                usage=token_usage,
                tool_call_count=tool_call_count,
                stream_metadata=stream_metadata,
            )
        except _DshSessionResumeError as exc:
            duration_ms = (time.monotonic() - started) * 1000.0
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                is_success=False,
                error=str(exc),
                error_code="dsh_session_resume_failed",
                error_retryable=True,
            )
        except _DshConfigError as exc:
            duration_ms = (time.monotonic() - started) * 1000.0
            self.logger.error("DSH configuration mismatch for %s: %s", request_id, exc)
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                is_success=False,
                error=str(exc),
                error_code=exc.error_code,
                error_retryable=False,
                stream_metadata={
                    "effective_model": exc.effective_model,
                    "effective_effort": exc.effective_effort,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            duration_ms = (time.monotonic() - started) * 1000.0
            self.logger.error("DSH ACP error (%s): %s", request_id, exc)
            return BackendResponse(
                text="",
                duration_ms=duration_ms,
                is_success=False,
                error=f"DSH ACP error: {exc}",
            )
        finally:
            # Never leave an unused fresh session_id bound after a failed first turn
            # (otherwise runtime_pipeline would emit an incremental prompt without PCM context).
            # P1-01: When Run-scoped HASHI MCP is active, close only the in-worker ACP session
            # so this Run's MCP child is disposed while keeping the warm DSH worker alive and
            # preserving committed self._session_id on disk for seamless next-Run session/resume.
            discard_uncommitted_fresh = created_fresh_session and not session_committed
            should_close_in_worker = (
                bool(session_id)
                and self._session_in_current_worker
                and (discard_uncommitted_fresh or run_mcp_invocation is not None)
            )
            if should_close_in_worker and session_id is not None:
                try:
                    await self._close_in_worker_session(
                        session_id,
                        clear_id=discard_uncommitted_fresh,
                    )
                except BaseException:
                    self._session_in_current_worker = False
                    if discard_uncommitted_fresh and self._session_id == session_id:
                        self._session_id = None
            elif discard_uncommitted_fresh and session_id and self._session_id == session_id:
                self._session_id = None
            if run_mcp_invocation is not None:
                self._close_run_mcp(run_mcp_invocation)

    async def _settle_cancelled_prompt(
        self,
        session_id: str,
        prompt_task: asyncio.Task[Any],
        *,
        reason: str,
    ) -> None:
        """Send ACP session/cancel and wait briefly so the session remains reusable."""

        if not prompt_task.done():
            await self._send_cancel_notifications(
                session_id,
                self._active_prompt_rpc_id,
                reason,
            )
            try:
                await asyncio.wait_for(asyncio.shield(prompt_task), timeout=_ACP_CANCEL_GRACE_SEC)
                return
            except Exception:
                pass
            prompt_task.cancel()
            try:
                await prompt_task
            except BaseException:
                pass
            # Worker did not settle cancel cleanly; recycle it so the next turn resumes cleanly
            self.logger.warning(
                "DSH ACP prompt did not settle within %.1fs after cancel; recycling worker",
                _ACP_CANCEL_GRACE_SEC,
            )
            await self._stop_worker(reason=reason)
# HASHI-Fingerprint: hfp_904901c620f942819ec7462a55edb36b
