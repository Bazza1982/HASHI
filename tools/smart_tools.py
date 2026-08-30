"""Minimal deterministic intelligence for HASHI tool execution.

The module deliberately contains no service, model, semantic evidence scoring,
or task-success attribution.  It gives :class:`tools.registry.ToolRegistry`
four small capabilities when enabled by configuration:

* bind every tool to one shared behaviour profile;
* adapt legacy string results into one five-field result contract;
* add soft, task-local repeat warnings without blocking execution; and
* append one compact ledger row after each completed call.
"""

from __future__ import annotations

import hashlib
import json
import logging
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


SMART_TOOL_STATUSES = frozenset({"success", "failed", "unavailable", "partial"})
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
        "media_read",
        "vision_inspect",
        "web_search",
        "web_fetch",
        "file_list",
        "process_list",
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
        "hashi_scheduler_rerun",
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
    "apply_patch": "apply_patch",
    "hashi_scheduler_list": "scheduler",
    "hashi_scheduler_status": "scheduler",
    "hashi_scheduler_run_history": "scheduler",
    "hashi_scheduler_rerun": "scheduler",
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
        version="1.0.0",
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
    raw_exit_code = details.get("exit_code")
    exit_code: int | None = None
    if isinstance(raw_exit_code, int) and not isinstance(raw_exit_code, bool):
        exit_code = raw_exit_code
    if exit_code is None:
        matched = re.match(r"\[exit code (-?\d+)\]", text.strip())
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
    if outcome.status == "success" and spec.name == "hashi_scheduler_rerun":
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
    """Request-local repeat intelligence plus one-row-per-call ledger output."""

    def __init__(self, workspace_dir: Path, options: Mapping[str, Any] | None):
        configured = dict(options or {})
        self.enabled = configured.get("enabled") is True
        raw_threshold = configured.get("repeat_threshold", 3)
        try:
            threshold = int(raw_threshold)
        except (TypeError, ValueError):
            threshold = 3
        self.repeat_threshold = max(2, threshold)
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
