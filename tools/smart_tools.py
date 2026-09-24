"""Minimal deterministic intelligence for HASHI tool execution.

The module deliberately contains no service, model, semantic evidence scoring,
or task-success attribution.  It gives :class:`tools.registry.ToolRegistry`
five bounded capabilities when enabled by configuration:

* reject a mechanically unsafe command shape and name a safer Tool;
* bind every tool to one shared behaviour profile;
* adapt legacy string results into one five-field result contract;
* add soft, task-local repeat warnings without blocking execution; and
* append one compact ledger row after each completed call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.schemas import ALL_TOOL_NAMES, TOOL_SCHEMA_MAP


_LOGGER = logging.getLogger("Tools.SmartRegistry")


SMART_TOOL_STATUSES = frozenset(
    {"success", "failed", "unavailable", "partial", "needs_replan"}
)
SMART_TOOL_EFFECTS = frozenset({"observed", "changed", "no_change", "unknown"})
SMART_TOOL_PROFILES = frozenset(
    {
        "query",
        "poll",
        "verify",
        "idempotent_action",
        "side_effect_action",
        "generic",
    }
)


_QUERY_TOOLS = frozenset(
    {
        "file_read",
        "log_query",
        "media_read",
        "vision_inspect",
        "web_search",
        "web_fetch",
        "file_list",
        "process_list",
        "request_diagnostics",
        "browser_active_tab",
        "browser_get_media_state",
        "browser_screenshot",
        "browser_get_text",
        "browser_get_html",
        "browser_get_attribute",
        "windows_screenshot",
        "windows_info",
        "windows_window_list",
        "desktop_screenshot",
        "desktop_info",
        "desktop_window_list",
        "hashi_scheduler_list",
        "hashi_scheduler_run_history",
        "hashi_superloop_list",
        "hashi_superloop_get",
        "obsidian_read_note",
        "obsidian_list_folder",
        "obsidian_search",
        "obsidian_get_active",
        "memory_search",
    }
)

_POLL_TOOLS = frozenset(
    {
        "background_job_status",
        "background_job_tail",
        "background_job_list",
        "managed_process_status",
        "hashi_scheduler_status",
        "browser_wait_for",
    }
)

_VERIFY_TOOLS = frozenset({"workspace_inspect", "verification_run"})

_IDEMPOTENT_ACTION_TOOLS = frozenset(
    {
        "file_write",
        "browser_play",
        "browser_open_play_verify",
        "browser_react",
        "background_job_cancel",
        "managed_process_stop",
        "windows_mouse_move",
        "windows_window_focus",
        "windows_reset_input_state",
        "desktop_mouse_move",
        "desktop_window_focus",
        "obsidian_write_note",
        "obsidian_open_note",
    }
)

_SIDE_EFFECT_ACTION_TOOLS = frozenset(
    {
        "xai_imagine",
        "apply_patch",
        "process_kill",
        "telegram_send",
        "telegram_send_file",
        "browser_scroll",
        "browser_hover",
        "browser_key",
        "browser_select",
        "browser_drag",
        "browser_upload",
        "browser_click",
        "browser_fill",
        "browser_type_text",
        "background_job_start",
        "managed_process_start",
        "managed_process_stop",
        "hashi_scheduler_rerun",
        "hashi_scheduler_create",
        "hashi_scheduler_update",
        "hashi_scheduler_delete",
        "hashi_superloop_create",
        "hashi_superloop_update",
        "hashi_superloop_delete",
        "windows_click",
        "windows_drag",
        "windows_type",
        "windows_key",
        "windows_scroll",
        "windows_helper_warmup",
        "windows_window_close",
        "desktop_click",
        "desktop_type",
        "desktop_key",
        "desktop_scroll",
        "obsidian_append_note",
    }
)

_TOOL_ADAPTERS = {
    "bash": "bash",
    "shell": "bash",
    "apply_patch": "apply_patch",
    "hashi_scheduler_list": "scheduler",
    "hashi_scheduler_status": "scheduler",
    "hashi_scheduler_run_history": "scheduler",
    "hashi_scheduler_rerun": "scheduler",
    "hashi_scheduler_create": "scheduler",
    "hashi_scheduler_update": "scheduler",
    "hashi_scheduler_delete": "scheduler",
    "hashi_superloop_list": "superloop",
    "hashi_superloop_get": "superloop",
    "hashi_superloop_create": "superloop",
    "hashi_superloop_update": "superloop",
    "hashi_superloop_delete": "superloop",
    "managed_process_start": "managed_process",
    "managed_process_status": "managed_process",
    "managed_process_stop": "managed_process",
}


@dataclass(frozen=True)
class SmartToolSpec:
    """The complete per-tool declaration used by the smart registry."""

    name: str
    version: str
    profile: str
    description: str
    adapter: str | None = None


@dataclass(frozen=True)
class SmartToolError:
    code: str
    message: str
    retryable: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class SmartToolWarning:
    code: str
    message: str
    suggested_action: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True)
class SmartToolOutcome:
    """The only five fields exposed to an Executor."""

    status: str
    effect: str
    data: Any = None
    error: SmartToolError | None = None
    warning: SmartToolWarning | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "effect": self.effect,
            "data": self.data,
            "error": self.error.as_dict() if self.error is not None else None,
            "warning": self.warning.as_dict() if self.warning is not None else None,
        }

    def model_output(self) -> str:
        return json.dumps(
            self.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


@dataclass(frozen=True)
class SmartToolAdmission:
    """Deterministic pre-execution result that asks HER to choose a safer tool."""

    code: str
    message: str
    suggested_tool: str
    data: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "suggested_tool": self.suggested_tool,
            **dict(self.data),
        }


@dataclass
class _TaskRepeatState:
    last_fingerprint: str | None = None
    repeat_count: int = 0
    successful_side_effect_args: set[tuple[str, str]] | None = None
    successful_side_effect_order: deque[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.successful_side_effect_args is None:
            self.successful_side_effect_args = set()
        if self.successful_side_effect_order is None:
            self.successful_side_effect_order = deque()


_TEXT_SEARCH_PROGRAM_RE = re.compile(
    r"(?i)(?:^|[\s|;&()])(?:[^\s|;&()\\/]+[\\/])?"
    r"(?:grep|egrep|fgrep|pcregrep|rg|ripgrep)(?:\.exe)?(?=\s|$)"
)
_ONLY_MATCHING_RE = re.compile(
    r"(?i)(?<!\S)(?:-[A-Za-z]*o[A-Za-z]*|--only-matching)(?=\s|$)"
)
_BOUNDED_SEARCH_OUTPUT_RE = re.compile(
    r"(?i)(?<!\S)(?:-[A-Za-z]*[qlc][A-Za-z]*|"
    r"--(?:quiet|count|files-with-matches))(?=\s|$)"
)
_BRE_CONTEXT_RE = re.compile(r"\.\\\{\d*,\d+\\\}")
_ERE_CONTEXT_RE = re.compile(r"\.\{\d*,\d+\}")
_LOG_PATH_RE = re.compile(
    r'''(?ix)
    (?:
        "(?P<double>[^"\r\n]+\.(?:jsonl|ndjson|log|txt))"
      | '(?P<single>[^'\r\n]+\.(?:jsonl|ndjson|log|txt))'
      | (?P<bare>[^\s|;&<>]+\.(?:jsonl|ndjson|log|txt))
    )
    '''
)
_DEFAULT_LARGE_RECORD_BYTES = 1_000_000
_DEFAULT_FILE_PROBE_BYTES = 32 * 1024 * 1024


def _positive_int_setting(
    value: object, *, default: int, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _positive_float_setting(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _candidate_log_paths(
    command: str,
    *,
    workspace_dir: Path,
    access_roots: tuple[Path, ...],
) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for match in _LOG_PATH_RE.finditer(command):
        raw = next((value for value in match.groupdict().values() if value), "")
        if not raw:
            continue
        candidate = Path(raw)
        try:
            resolved = (
                candidate.resolve()
                if candidate.is_absolute()
                else (workspace_dir / candidate).resolve()
            )
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        if not any(
            resolved == root or resolved.is_relative_to(root) for root in access_roots
        ):
            continue
        seen.add(resolved)
        candidates.append((raw, resolved))
    return candidates


def _probe_large_record(
    path: Path, *, record_limit: int, probe_limit: int
) -> dict[str, Any]:
    scanned = 0
    current_record = 0
    line = 1
    file_size = path.stat().st_size
    with path.open("rb") as stream:
        while scanned < probe_limit:
            chunk = stream.read(min(64 * 1024, probe_limit - scanned))
            if not chunk:
                break
            scanned += len(chunk)
            parts = chunk.split(b"\n")
            current_record += len(parts[0])
            if current_record > record_limit:
                return {
                    "record_over_limit": True,
                    "record_bytes_lower_bound": current_record,
                    "record_line": line,
                    "file_probe_bytes": scanned,
                    "file_probe_complete": scanned >= file_size,
                }
            for part in parts[1:]:
                line += 1
                current_record = len(part)
                if current_record > record_limit:
                    return {
                        "record_over_limit": True,
                        "record_bytes_lower_bound": current_record,
                        "record_line": line,
                        "file_probe_bytes": scanned,
                        "file_probe_complete": scanned >= file_size,
                    }
    return {
        "record_over_limit": False,
        "record_bytes_lower_bound": current_record,
        "record_line": None,
        "file_probe_bytes": scanned,
        "file_probe_complete": scanned >= file_size,
    }


def _shell_text_search_admission(
    arguments: Mapping[str, Any],
    *,
    workspace_dir: Path,
    access_roots: tuple[Path, ...],
    record_limit: int,
    probe_limit: int,
) -> SmartToolAdmission | None:
    command = str((arguments or {}).get("command") or "")
    if not command or _TEXT_SEARCH_PROGRAM_RE.search(command) is None:
        return None
    only_matching = _ONLY_MATCHING_RE.search(command) is not None
    context_expansions = len(_BRE_CONTEXT_RE.findall(command)) + len(
        _ERE_CONTEXT_RE.findall(command)
    )
    dangerous_context = bool(only_matching and context_expansions)
    bounded_output = _BOUNDED_SEARCH_OUTPUT_RE.search(command) is not None
    candidate_paths = _candidate_log_paths(
        command,
        workspace_dir=workspace_dir,
        access_roots=access_roots,
    )
    selected_raw = ""
    selected_probe: dict[str, Any] = {
        "record_over_limit": False,
        "record_bytes_lower_bound": None,
        "record_line": None,
        "file_probe_bytes": 0,
        "file_probe_complete": False,
    }
    for raw, path in candidate_paths:
        try:
            probe = _probe_large_record(
                path,
                record_limit=record_limit,
                probe_limit=probe_limit,
            )
        except OSError:
            continue
        if not selected_raw:
            selected_raw = raw
            selected_probe = probe
        if probe["record_over_limit"]:
            selected_raw = raw
            selected_probe = probe
            break

    large_unbounded_record = bool(
        selected_probe["record_over_limit"] and not bounded_output
    )
    if not dangerous_context and not large_unbounded_record:
        return None
    if selected_probe["record_over_limit"]:
        message = (
            "The requested line-oriented search targets a record larger than "
            f"{record_limit} bytes. Running it in foreground shell can consume "
            "unbounded CPU or output before HER regains control."
        )
    else:
        message = (
            "Only-matching wildcard context expansion is unsafe for unknown or "
            "long records and must not run in foreground shell."
        )
    return SmartToolAdmission(
        code="unsafe_text_search",
        message=message,
        suggested_tool="log_query",
        data={
            "path": selected_raw or None,
            "record_limit_bytes": record_limit,
            "only_matching": only_matching,
            "context_expansions": context_expansions,
            **selected_probe,
        },
    )


def _profile_for(tool_name: str) -> str:
    if tool_name in _QUERY_TOOLS:
        return "query"
    if tool_name in _POLL_TOOLS:
        return "poll"
    if tool_name in _VERIFY_TOOLS:
        return "verify"
    if tool_name in _IDEMPOTENT_ACTION_TOOLS:
        return "idempotent_action"
    if tool_name in _SIDE_EFFECT_ACTION_TOOLS:
        return "side_effect_action"
    return "generic"


def smart_tool_spec(tool_name: str) -> SmartToolSpec:
    """Resolve one tool's effective, versioned behaviour declaration."""

    name = str(tool_name or "").strip()
    schema = TOOL_SCHEMA_MAP.get(name, {})
    function = schema.get("function") if isinstance(schema, Mapping) else {}
    description = str((function or {}).get("description") or "").strip()
    return SmartToolSpec(
        name=name,
        version="2.0.0" if name in {"bash", "shell", "log_query"} else "1.0.0",
        profile=_profile_for(name),
        description=description,
        adapter=_TOOL_ADAPTERS.get(name),
    )


SMART_TOOL_SPECS = {
    name: smart_tool_spec(name)
    for name in ALL_TOOL_NAMES
}


def _sha256(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _result_data(output: str) -> Any:
    stripped = str(output or "").strip()
    if not stripped:
        return None
    if stripped[:1] in {"{", "["}:
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return stripped


def _effect_from_data(data: Any, *, fallback: str) -> str:
    if not isinstance(data, Mapping):
        return fallback
    for key in ("state_changed", "changed"):
        value = data.get(key)
        if value is True:
            return "changed"
        if value is False:
            return "no_change"
    if data.get("no_change") is True or data.get("already_satisfied") is True:
        return "no_change"
    return fallback


def _legacy_error_message(output: str) -> str:
    message = str(output or "").strip()
    if message.casefold().startswith("error:"):
        message = message.split(":", 1)[1].strip()
    return message or "The tool failed without an error message."


def _generic_outcome(
    spec: SmartToolSpec,
    output: str,
    raw_is_error: bool,
    details: Mapping[str, Any],
) -> SmartToolOutcome:
    if raw_is_error:
        if details.get("unavailable") is True:
            return SmartToolOutcome(
                status="unavailable",
                effect="no_change",
                error=SmartToolError(
                    "tool_unavailable", _legacy_error_message(output), False
                ),
                warning=SmartToolWarning(
                    "unavailable_environment",
                    "The tool is unavailable in the current environment.",
                    "Use an available alternative or continue without this tool.",
                ),
            )
        disposition = str(details.get("control_disposition") or "").casefold()
        if disposition in {"denied", "blocked"}:
            return SmartToolOutcome(
                status="failed",
                effect="no_change",
                error=SmartToolError(
                    "permission_denied", _legacy_error_message(output), False
                ),
            )
        return SmartToolOutcome(
            status="failed",
            effect="unknown",
            error=SmartToolError(
                "tool_error", _legacy_error_message(output), False
            ),
        )

    data = _result_data(output)
    default_effect = (
        "observed" if spec.profile in {"query", "poll", "verify"} else "unknown"
    )
    return SmartToolOutcome(
        status="success",
        effect=_effect_from_data(data, fallback=default_effect),
        data=data,
    )


def _bash_outcome(
    spec: SmartToolSpec,
    output: str,
    raw_is_error: bool,
    details: Mapping[str, Any],
) -> SmartToolOutcome:
    del spec
    text = str(output or "")
    lowered = text.casefold()
    disposition = str(details.get("control_disposition") or "").casefold()
    if disposition == "needs_replan":
        admission = details.get("smart_admission")
        data = dict(admission) if isinstance(admission, Mapping) else {}
        code = str(data.get("code") or "needs_replan")
        message = str(data.get("message") or _legacy_error_message(text))
        suggested_tool = str(data.get("suggested_tool") or "log_query")
        return SmartToolOutcome(
            status="needs_replan",
            effect="no_change",
            data=data,
            error=SmartToolError(code, message, False),
            warning=SmartToolWarning(
                "safer_tool_required",
                "The command was not started because a safer bounded tool is available.",
                f"Re-plan with {suggested_tool}; do not bypass the admission guard.",
            ),
        )
    raw_exit_code = details.get("exit_code")
    exit_code: int | None = None
    if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool):
        exit_code = raw_exit_code
    if exit_code is None:
        matched = re.search(r"(?m)^\[exit code (-?\d+)\]$", text.strip())
        if matched:
            exit_code = int(matched.group(1))

    if "timed out" in lowered:
        return SmartToolOutcome(
            status="failed",
            effect="unknown",
            error=SmartToolError("timeout", _legacy_error_message(text), True),
        )
    if "blocked by policy" in lowered:
        return SmartToolOutcome(
            status="failed",
            effect="no_change",
            error=SmartToolError(
                "permission_denied", _legacy_error_message(text), False
            ),
        )
    if "no command provided" in lowered:
        return SmartToolOutcome(
            status="failed",
            effect="no_change",
            error=SmartToolError(
                "invalid_arguments", _legacy_error_message(text), False
            ),
        )
    if exit_code not in {None, 0}:
        code = "command_not_found" if exit_code == 127 else "nonzero_exit"
        return SmartToolOutcome(
            status="failed",
            effect="unknown",
            error=SmartToolError(
                code,
                f"Command exited with code {exit_code}."
                + (f"\n{text}" if text.strip() else ""),
                code != "command_not_found",
            ),
        )
    if raw_is_error:
        return SmartToolOutcome(
            status="failed",
            effect="unknown",
            error=SmartToolError(
                "command_error", _legacy_error_message(text), False
            ),
        )
    return SmartToolOutcome(status="success", effect="unknown", data=_result_data(text))


def _scheduler_outcome(
    spec: SmartToolSpec,
    output: str,
    raw_is_error: bool,
    details: Mapping[str, Any],
) -> SmartToolOutcome:
    text = str(output or "")
    lowered = text.casefold()
    if "workbench api is unavailable" in lowered or "gateway context" in lowered:
        return SmartToolOutcome(
            status="unavailable",
            effect="no_change",
            error=SmartToolError(
                "gateway_context_missing",
                "Scheduler is unavailable in the current environment.",
                False,
            ),
            warning=SmartToolWarning(
                "unavailable_environment",
                "Repeating this call in the current environment will not help.",
                "Continue without scheduler access.",
            ),
        )
    if "scheduler api is unavailable" in lowered or "cannot connect" in lowered:
        return SmartToolOutcome(
            status="unavailable",
            effect=(
                "unknown"
                if spec.profile == "side_effect_action"
                else "no_change"
            ),
            error=SmartToolError(
                "scheduler_unreachable", _legacy_error_message(text), True
            ),
            warning=SmartToolWarning(
                "temporary_unavailability",
                "The Scheduler endpoint is currently unreachable.",
                "Retry later after the Scheduler endpoint is available.",
            ),
        )
    outcome = _generic_outcome(spec, text, raw_is_error, details)
    if outcome.status == "success" and spec.name in {
        "hashi_scheduler_rerun",
        "hashi_scheduler_create",
        "hashi_scheduler_update",
        "hashi_scheduler_delete",
    }:
        return replace(outcome, effect="changed")
    return outcome


def _superloop_outcome(
    spec: SmartToolSpec,
    output: str,
    raw_is_error: bool,
    details: Mapping[str, Any],
) -> SmartToolOutcome:
    text = str(output or "")
    lowered = text.casefold()
    if "workbench api is unavailable" in lowered or "gateway context" in lowered:
        return SmartToolOutcome(
            status="unavailable",
            effect="no_change",
            error=SmartToolError(
                "gateway_context_missing",
                "Superloop is unavailable in the current environment.",
                False,
            ),
            warning=SmartToolWarning(
                "unavailable_environment",
                "Repeating this call in the current environment will not help.",
                "Continue without Superloop access.",
            ),
        )
    if "superloop api is unavailable" in lowered or "cannot connect" in lowered:
        return SmartToolOutcome(
            status="unavailable",
            effect="unknown" if spec.profile == "side_effect_action" else "no_change",
            error=SmartToolError(
                "superloop_unreachable", _legacy_error_message(text), True
            ),
            warning=SmartToolWarning(
                "temporary_unavailability",
                "The Superloop endpoint is currently unreachable.",
                "Retry later after the endpoint is available.",
            ),
        )
    outcome = _generic_outcome(spec, text, raw_is_error, details)
    if outcome.status == "success" and spec.name in {
        "hashi_superloop_create",
        "hashi_superloop_update",
        "hashi_superloop_delete",
    }:
        return replace(outcome, effect="changed")
    return outcome


def _patch_outcome(
    spec: SmartToolSpec,
    output: str,
    raw_is_error: bool,
    details: Mapping[str, Any],
) -> SmartToolOutcome:
    del spec, details
    text = str(output or "")
    lowered = text.casefold()
    if "patch rejected (dry-run)" in lowered:
        return SmartToolOutcome(
            status="failed",
            effect="no_change",
            error=SmartToolError("patch_rejected", _legacy_error_message(text), True),
        )
    if "file not found" in lowered or "no path provided" in lowered:
        return SmartToolOutcome(
            status="failed",
            effect="no_change",
            error=SmartToolError("path_not_found", _legacy_error_message(text), False),
        )
    if raw_is_error:
        return SmartToolOutcome(
            status="failed",
            effect="unknown",
            error=SmartToolError("patch_error", _legacy_error_message(text), True),
        )
    return SmartToolOutcome(status="success", effect="changed", data=_result_data(text))


_ADAPTERS = {
    "bash": _bash_outcome,
    "scheduler": _scheduler_outcome,
    "superloop": _superloop_outcome,
    "apply_patch": _patch_outcome,
}


def adapt_legacy_result(
    tool_name: str,
    *,
    output: str,
    raw_is_error: bool,
    details: Mapping[str, Any] | None = None,
) -> tuple[SmartToolSpec, SmartToolOutcome]:
    """Translate one legacy tool result without changing the underlying tool."""

    spec = SMART_TOOL_SPECS.get(tool_name) or smart_tool_spec(tool_name)
    adapter = _ADAPTERS.get(spec.adapter or "", _generic_outcome)
    outcome = adapter(spec, str(output or ""), bool(raw_is_error), dict(details or {}))
    if outcome.status not in SMART_TOOL_STATUSES:
        raise ValueError(f"invalid smart tool status: {outcome.status}")
    if outcome.effect not in SMART_TOOL_EFFECTS:
        raise ValueError(f"invalid smart tool effect: {outcome.effect}")
    return spec, outcome


class SmartToolRuntime:
    """Deterministic admission, result shaping, repeat guidance, and Ledger output."""

    def __init__(self, workspace_dir: Path, options: Mapping[str, Any] | None):
        configured = dict(options or {})
        self.workspace_dir = Path(workspace_dir).resolve()
        self.enabled = configured.get("enabled") is True
        raw_threshold = configured.get("repeat_threshold", 3)
        try:
            threshold = int(raw_threshold)
        except (TypeError, ValueError):
            threshold = 3
        self.repeat_threshold = max(2, threshold)
        self.shell_text_search_guard = configured.get("shell_text_search_guard", True) is True
        self.large_record_bytes = _positive_int_setting(
            configured.get("large_record_bytes"),
            default=_DEFAULT_LARGE_RECORD_BYTES,
            minimum=64,
            maximum=64 * 1024 * 1024,
        )
        self.file_probe_bytes = _positive_int_setting(
            configured.get("file_probe_bytes"),
            default=max(_DEFAULT_FILE_PROBE_BYTES, self.large_record_bytes + 1),
            minimum=self.large_record_bytes + 1,
            maximum=512 * 1024 * 1024,
        )
        self.foreground_timeout_seconds = _positive_float_setting(
            configured.get("foreground_timeout_seconds")
        )
        raw_path = str(configured.get("ledger_path") or "tool_ledger.jsonl").strip()
        ledger_path = Path(raw_path)
        self.ledger_path = (
            ledger_path
            if ledger_path.is_absolute()
            else Path(workspace_dir).resolve() / ledger_path
        )
        self._lock = threading.Lock()
        self._tasks: OrderedDict[str, _TaskRepeatState] = OrderedDict()
        self._max_tasks = 256
        self._max_side_effect_fingerprints = 512

    @staticmethod
    def new_call_id() -> str:
        return f"call-{uuid.uuid4().hex}"

    def evaluate_admission(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        access_roots: tuple[Path, ...],
    ) -> SmartToolAdmission | None:
        """Return a deterministic safer-tool decision before process creation."""

        if (
            not self.enabled
            or not self.shell_text_search_guard
            or str(tool_name or "") not in {"bash", "shell"}
        ):
            return None
        return _shell_text_search_admission(
            arguments,
            workspace_dir=self.workspace_dir,
            access_roots=access_roots,
            record_limit=self.large_record_bytes,
            probe_limit=self.file_probe_bytes,
        )

    def complete(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        output: str,
        raw_is_error: bool,
        details: Mapping[str, Any] | None,
        duration_ms: int,
        call_id: str,
        audit_context: Mapping[str, Any] | None,
    ) -> tuple[SmartToolOutcome, SmartToolSpec, dict[str, Any]]:
        """Classify, softly warn, record, and return one completed call."""

        spec, outcome = adapt_legacy_result(
            tool_name,
            output=output,
            raw_is_error=raw_is_error,
            details=details,
        )
        context = dict(audit_context or {})
        task_id = str(
            context.get("task_id") or context.get("turn_id") or "unscoped"
        ).strip() or "unscoped"
        stage = str(context.get("stage") or context.get("her_stage") or "tool")
        model = str(context.get("model") or "")
        request_id = str(context.get("request_id") or "").strip()
        args_hash = _sha256(dict(arguments or {}))
        result_hash = _sha256(
            {
                "status": outcome.status,
                "effect": outcome.effect,
                "data": outcome.data,
                "error_code": outcome.error.code if outcome.error else None,
            }
        )
        fingerprint = _sha256([tool_name, args_hash, result_hash])
        state_key = task_id if task_id != "unscoped" else f"unscoped:{call_id}"

        with self._lock:
            state = self._tasks.get(state_key)
            if state is None:
                state = _TaskRepeatState()
                self._tasks[state_key] = state
            self._tasks.move_to_end(state_key)
            while len(self._tasks) > self._max_tasks:
                self._tasks.popitem(last=False)

            if state.last_fingerprint == fingerprint:
                state.repeat_count += 1
            else:
                state.last_fingerprint = fingerprint
                state.repeat_count = 0
            repeat_count = state.repeat_count

            side_effect_key = (tool_name, args_hash)
            repeated_side_effect = bool(
                spec.profile == "side_effect_action"
                and side_effect_key in (state.successful_side_effect_args or set())
            )
            if spec.profile == "side_effect_action" and outcome.status == "success":
                assert state.successful_side_effect_args is not None
                assert state.successful_side_effect_order is not None
                if side_effect_key not in state.successful_side_effect_args:
                    state.successful_side_effect_args.add(side_effect_key)
                    state.successful_side_effect_order.append(side_effect_key)
                    while (
                        len(state.successful_side_effect_order)
                        > self._max_side_effect_fingerprints
                    ):
                        expired = state.successful_side_effect_order.popleft()
                        state.successful_side_effect_args.discard(expired)

            if outcome.warning is None and repeated_side_effect:
                outcome = replace(
                    outcome,
                    warning=SmartToolWarning(
                        "repeated_side_effect",
                        "The same side-effect action previously succeeded in this task.",
                        "Continue only when repeating the side effect is intentional.",
                    ),
                )
            elif (
                outcome.warning is None
                and repeat_count >= self.repeat_threshold - 1
            ):
                if spec.profile == "poll":
                    warning = SmartToolWarning(
                        "poll_state_unchanged",
                        "The polled state is still unchanged.",
                        "Polling may continue; consider increasing the interval.",
                    )
                else:
                    warning = SmartToolWarning(
                        "same_result_repeated",
                        "This result is identical to the previous two calls.",
                        "Continue only if another identical observation is necessary.",
                    )
                outcome = replace(outcome, warning=warning)

            record = {
                "timestamp": datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "task_id": task_id,
                "request_id": request_id,
                "call_id": call_id,
                "stage": stage,
                "model": model,
                "tool": tool_name,
                "tool_version": spec.version,
                "args_hash": args_hash,
                "status": outcome.status,
                "effect": outcome.effect,
                "error_code": outcome.error.code if outcome.error else None,
                "duration_ms": max(0, int(duration_ms)),
                "result_hash": result_hash,
                "repeat_count": repeat_count,
            }
            if tool_name in {"file_write", "apply_patch"}:
                record["target"] = str(arguments.get("path") or "")[:4096]
            self._append_record(record)

        return outcome, spec, record

    def _append_record(self, record: Mapping[str, Any]) -> None:
        """Best-effort append; tool execution must not fail because logging did."""

        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                dict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception as exc:
            _LOGGER.error("Failed to append Smart Tool ledger row: %s", exc)


__all__ = [
    "SMART_TOOL_EFFECTS",
    "SMART_TOOL_PROFILES",
    "SMART_TOOL_SPECS",
    "SMART_TOOL_STATUSES",
    "SmartToolError",
    "SmartToolOutcome",
    "SmartToolRuntime",
    "SmartToolSpec",
    "SmartToolWarning",
    "adapt_legacy_result",
    "smart_tool_spec",
]
