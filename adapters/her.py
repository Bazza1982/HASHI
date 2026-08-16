from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import platform as py_platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from adapters import her_habits as _her_habits
from adapters import her_persona as _her_persona
from adapters import her_ultra as _her_ultra
from adapters import stream_events as _stream_events
from adapters.base import BackendCapabilities, BackendResponse, BaseBackend, TokenUsage
from adapters.stream_events import (
    KIND_COMMENTARY,
    KIND_ERROR,
    KIND_PROGRESS,
    KIND_TEXT_DELTA,
    KIND_THINKING,
    KIND_TOOL_END,
    KIND_TOOL_START,
    StreamCallback,
    StreamEvent,
)
from adapters.stream_io import iter_stream_lines

# Bootstrap compatibility for the first hot reload from a runtime whose
# stream_events module predates acknowledgement events.  Subsequent /reboot
# calls load stream_events first, as enforced by orchestrator.hot_reload.
KIND_ACKNOWLEDGEMENT = getattr(
    _stream_events,
    "KIND_ACKNOWLEDGEMENT",
    "acknowledgement",
)
KIND_REVIEW = getattr(_stream_events, "KIND_REVIEW", "review")
KIND_VALIDATION = getattr(_stream_events, "KIND_VALIDATION", "validation")
KIND_TESTING = getattr(_stream_events, "KIND_TESTING", "testing")
DELIVERY_TECHNICAL = getattr(_stream_events, "DELIVERY_TECHNICAL", "technical")
DELIVERY_USER_COMMENTARY = getattr(
    _stream_events,
    "DELIVERY_USER_COMMENTARY",
    "user_commentary",
)
DELIVERY_REASONING = getattr(_stream_events, "DELIVERY_REASONING", "reasoning")
DELIVERY_FINAL = getattr(_stream_events, "DELIVERY_FINAL", "final")
DELIVERY_CONTROL = getattr(_stream_events, "DELIVERY_CONTROL", "control")
DELIVERY_INTERNAL = getattr(_stream_events, "DELIVERY_INTERNAL", "internal")

DEFAULT_CLAW_TIMEOUT_SEC = 30
DEFAULT_CLAW_TASK_TIMEOUT_SEC = 1800
VALID_PERMISSION_MODES = {"read-only", "workspace-write", "danger-full-access"}
PERMISSION_MODE_RANK = {"read-only": 0, "workspace-write": 1, "danger-full-access": 2}
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OLLAMA_DUMMY_API_KEY = "__ollama_dummy__"
HER_DISPLAY_NAME = "HASHI Engine Runtime (HER)"
HER_VERSION = "0.1.0-hashi.22"
PACKAGED_CLAW_RUNTIME = "hashi-her"
PACKAGED_CLAW_MANIFEST_VERSION = 1
CLAW_RUNTIME_POLICIES = {"prefer-packaged", "require-packaged", "system-only"}
HER_SESSION_SCOPE_PERSISTENT = "persistent"
HER_SESSION_SCOPE_ISOLATED = "isolated_per_run"
HER_SESSION_SCOPE_ISOLATED_RESUME = "isolated_resume"
HER_DIAGNOSTIC_MAX_CHARS = 8192
SECRET_ENV_KEYS = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "XAI_API_KEY",
    "DASHSCOPE_API_KEY",
}
OS_ENV_ALLOWLIST = ("HOME", "USER", "TMPDIR", "TEMP", "PATH")


@dataclass
class ClawThinkingStreamUsage:
    thinking_chars: int = 0
    thinking_tokens: int = 0
    thinking_event_count: int = 0
    thinking_redacted_count: int = 0
    thinking_sources: set[str] = field(default_factory=set)
    saw_actual_thinking_event: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "thinking_chars": self.thinking_chars,
            "thinking_tokens": self.thinking_tokens,
            "thinking_event_count": self.thinking_event_count,
            "thinking_redacted_count": self.thinking_redacted_count,
            "thinking_sources": sorted(self.thinking_sources),
        }


CLAW_ENV_ALLOWLIST = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "XAI_BASE_URL",
    "CLAW_CONFIG_HOME",
    "PYTHONPATH",
    "CLAW_MAX_TOOL_ITERATIONS",
    "CLAW_TASK_PLANNING",
    "CLAW_EXECUTION_EFFORT",
    *OS_ENV_ALLOWLIST,
)

CLAW_EXECUTION_EFFORT_ITERATIONS = {
    "low": 12,
    "medium": 32,
    "high": 96,
    "xhigh": 192,
    "max": 384,
    "max+": 512,
}
HER_EXECUTION_EFFORTS = frozenset(
    {*CLAW_EXECUTION_EFFORT_ITERATIONS, _her_ultra.HER_ULTRA_EFFORT}
)

HER_COMMENTARY_EFFORTS = frozenset({"high", "xhigh", "max", "max+", "ultra"})
HER_COMMENTARY_FIRST_UPDATE_S = 90.0
HER_COMMENTARY_TARGET_INTERVAL_S = 180.0
HER_COMMENTARY_HARD_INTERVAL_S = 300.0
HER_COMMENTARY_ACTIVITY_GRACE_S = 30.0

_CLAW_INCOMPLETE_STATUSES = {"incomplete"}
_CLAW_INCOMPLETE_STOP_REASONS = {"budget_exhausted", "max_iterations", "no_final_text"}
_CLAW_READ_ONLY_TOOL_MARKERS = (
    "get_",
    "read",
    "list",
    "find",
    "search",
    "grep",
    "glob",
    "status",
    "inspect",
    "screenshot",
    "query",
)


class ClawError(RuntimeError):
    """Base class for HER CLI diagnostic failures."""


class ClawBinaryNotFound(ClawError):
    """Raised when no executable HER binary can be resolved."""


class ClawPackagedRuntimeError(ClawBinaryNotFound):
    """Raised when a packaged HER runtime exists but is unsafe or unusable."""


class ClawProviderConfigError(ClawError):
    """Raised when a named HER provider is missing or disabled."""


class ClawProviderSecretMissing(ClawProviderConfigError):
    """Raised when a provider references a missing secret."""


class ClawCommandError(ClawError):
    """Raised when HER exits non-zero."""

    def __init__(
        self,
        message: str,
        *,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        parsed_error: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.parsed_error = parsed_error


class ClawJsonError(ClawError):
    """Raised when HER output is expected to be JSON but is not parseable."""

    def __init__(self, message: str, *, output: str):
        super().__init__(message)
        self.output = output


class ClawTimeoutError(ClawError):
    """Raised when HER does not finish before the configured timeout."""

    def __init__(self, message: str, *, timeout_s: float):
        super().__init__(message)
        self.timeout_s = timeout_s


@dataclass(frozen=True)
class ClawCommandResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float
    json_data: dict[str, Any]


@dataclass(frozen=True)
class ClawTaskResult:
    text: str
    model: str
    permission_mode: str
    cwd: str
    returncode: int
    duration_ms: float
    stdout: str
    stderr: str
    json_data: dict[str, Any]
    tool_uses: list[Any]
    tool_results: list[Any]
    session_id: str | None = None
    iterations: int | None = None
    completion_status: str | None = None
    stop_reason: str | None = None
    provider_stop_reason: str | None = None
    estimated_cost: str | None = None


def _claw_run_is_incomplete(result: ClawTaskResult) -> bool:
    completion_status = str(result.completion_status or "").strip().lower()
    stop_reason = str(result.stop_reason or "").strip().lower()
    return (
        completion_status in _CLAW_INCOMPLETE_STATUSES
        or stop_reason in _CLAW_INCOMPLETE_STOP_REASONS
    )


def _claw_tool_name(item: Any) -> str:
    if not isinstance(item, Mapping):
        return "unknown_tool"
    name = str(item.get("name") or item.get("tool_name") or "unknown_tool")
    name = " ".join(name.replace("`", "'").split())[:80]
    return name or "unknown_tool"


def _claw_result_is_error(item: Any) -> bool:
    return isinstance(item, Mapping) and bool(
        item.get("is_error") or item.get("isError")
    )


def _claw_result_output(item: Any) -> Any:
    if not isinstance(item, Mapping):
        return None
    return item.get("output")


def _claw_parse_structured_output(output: Any) -> Any:
    if isinstance(output, (dict, list)):
        return output
    if not isinstance(output, str):
        return None
    candidate = output.strip()
    if not candidate or candidate[0] not in "[{":
        return None
    try:
        return json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _claw_nested_truth(value: Any, keys: set[str]) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower() in keys and nested is True:
                return True
            if _claw_nested_truth(nested, keys):
                return True
    elif isinstance(value, list):
        return any(_claw_nested_truth(item, keys) for item in value)
    return False


def _claw_has_explicit_verification(result: Any) -> bool:
    structured = _claw_parse_structured_output(_claw_result_output(result))
    if structured is not None and _claw_nested_truth(
        structured,
        {"state_changed", "verified", "verification_succeeded"},
    ):
        return True
    output = _claw_result_output(result)
    if not isinstance(output, str):
        return False
    compact = "".join(output.lower().split())
    return any(
        marker in compact
        for marker in (
            '"state_changed":true',
            "state_changed=true",
            '"verified":true',
            "verified=true",
        )
    )


def _claw_tool_is_read_only(name: str) -> bool:
    normalized = name.strip().lower()
    # MCP tools are emitted as ``mcp__server__tool``.  Classify the callable
    # leaf rather than the transport namespace so a successful Tool Gateway
    # ``file_read`` is not reported as unverified progress.
    normalized = normalized.rsplit("__", 1)[-1]
    leaf = normalized.rsplit(".", 1)[-1]
    leaf = leaf.removeprefix("browser_")
    return leaf == "file_read" or leaf.startswith(_CLAW_READ_ONLY_TOOL_MARKERS)


def _claw_count_names(names: list[str], *, empty: str = "无") -> str:
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return (
        ", ".join(f"`{name}` ×{count}" for name, count in sorted(counts.items()))
        or empty
    )


def _claw_pair_tool_ledger(
    tool_uses: list[Any],
    tool_results: list[Any],
) -> list[tuple[str, Any | None]]:
    results_by_id: dict[str, Any] = {}
    unkeyed_results: list[Any] = []
    for result in tool_results:
        result_id = ""
        if isinstance(result, Mapping):
            result_id = str(
                result.get("tool_use_id") or result.get("toolUseId") or ""
            ).strip()
        if result_id:
            results_by_id[result_id] = result
        else:
            unkeyed_results.append(result)

    ledger: list[tuple[str, Any | None]] = []
    unkeyed_index = 0
    matched_result_ids: set[str] = set()
    for tool_use in tool_uses:
        tool_id = ""
        if isinstance(tool_use, Mapping):
            tool_id = str(
                tool_use.get("id") or tool_use.get("tool_use_id") or ""
            ).strip()
        result = results_by_id.get(tool_id) if tool_id else None
        if result is not None and tool_id:
            matched_result_ids.add(tool_id)
        if result is None and unkeyed_index < len(unkeyed_results):
            result = unkeyed_results[unkeyed_index]
            unkeyed_index += 1
        ledger.append((_claw_tool_name(tool_use), result))

    for result in unkeyed_results[unkeyed_index:]:
        ledger.append((_claw_tool_name(result), result))
    for result_id, result in results_by_id.items():
        if result_id not in matched_result_ids:
            ledger.append((_claw_tool_name(result), result))
    return ledger


def _claw_uses_chinese(*values: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for value in values for char in value)


def _build_claw_technical_lease(prompt: str) -> str:
    """Return a neutral runtime lease without inventing task progress or Persona."""
    if _claw_uses_chinese(prompt):
        return (
            "HER 仍在处理这项任务。目前没有新的、可确认结果；一有可靠进展就会继续更新。"
        )[:500]
    return (
        "HER is still processing this task. There is no new confirmed result yet; "
        "another update will follow when reliable progress is available."
    ).strip()[:500]


class _HERStreamCadenceController:
    """Apply effort-level generation cadence before the presentation router."""

    def __init__(
        self,
        callback,
        *,
        request_id: str = "",
        prompt: str,
        progress_enabled: bool,
        first_update_s: float = HER_COMMENTARY_FIRST_UPDATE_S,
        target_interval_s: float = HER_COMMENTARY_TARGET_INTERVAL_S,
        hard_interval_s: float = HER_COMMENTARY_HARD_INTERVAL_S,
        activity_grace_s: float = HER_COMMENTARY_ACTIVITY_GRACE_S,
    ) -> None:
        self._callback = callback
        self._request_id = str(request_id or "her-request")
        self._prompt = prompt
        self.progress_enabled = bool(progress_enabled)
        self._first_update_s = max(0.01, float(first_update_s))
        self._target_interval_s = max(0.01, float(target_interval_s))
        self._hard_interval_s = max(self._target_interval_s, float(hard_interval_s))
        self._activity_grace_s = max(0.0, float(activity_grace_s))
        now = time.monotonic()
        self._last_visible_at = now
        self._last_activity_at = now
        self._has_progress_update = False
        self._lease_revision = 0
        self._pending_commentary: StreamEvent | None = None
        self._last_material_fingerprint = ""
        self._closed = False
        self._changed = asyncio.Event()
        self._emit_lock = asyncio.Lock()

    async def forward(self, event: StreamEvent) -> None:
        now = time.monotonic()
        self._last_activity_at = now
        self._changed.set()
        if event.kind == KIND_ACKNOWLEDGEMENT:
            async with self._emit_lock:
                self._last_visible_at = time.monotonic()
                await self._callback(event)
            return
        if event.kind != KIND_COMMENTARY:
            await self._callback(event)
            return
        if not (event.summary or "").strip():
            return
        if not self.progress_enabled:
            await self._callback(
                replace(
                    event,
                    delivery_class=DELIVERY_INTERNAL,
                    required=False,
                    detail=(
                        f"{event.detail};" if event.detail else ""
                    )
                    + "suppressed_reason=effort_progress_disabled",
                )
            )
            return
        fingerprint = self._material_fingerprint(event)
        if fingerprint and fingerprint == self._last_material_fingerprint:
            await self._callback(
                replace(
                    event,
                    delivery_class=DELIVERY_INTERNAL,
                    required=False,
                    detail=(
                        f"{event.detail};" if event.detail else ""
                    )
                    + "suppressed_reason=unchanged_material_progress",
                )
            )
            return
        self._pending_commentary = event
        self._changed.set()

    @staticmethod
    def _material_fingerprint(event: StreamEvent) -> str:
        payload = {
            "summary": " ".join((event.summary or "").split()),
            "detail": str(event.detail or "").strip(),
            "phase": str(event.phase or "").strip(),
            "current": event.current,
            "total": event.total,
            "unit": str(event.unit or "").strip(),
            "tool_name": str(event.tool_name or "").strip(),
            "file_path": str(event.file_path or "").strip(),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    async def _emit(self, event: StreamEvent) -> None:
        async with self._emit_lock:
            if self._closed:
                return
            summary = (event.summary or "").strip()
            if not summary:
                return
            self._last_visible_at = time.monotonic()
            self._has_progress_update = True
            if event.kind == KIND_COMMENTARY:
                self._last_material_fingerprint = self._material_fingerprint(event)
            self._changed.set()
            await self._callback(event)

    async def run(self) -> None:
        if not self.progress_enabled:
            return
        while not self._closed:
            self._changed.clear()
            now = time.monotonic()
            if not self._has_progress_update:
                target_deadline = self._last_visible_at + self._first_update_s
                hard_deadline = target_deadline
            else:
                target_deadline = self._last_visible_at + self._target_interval_s
                hard_deadline = self._last_visible_at + self._hard_interval_s

            deadline = target_deadline
            if now >= target_deadline and now < hard_deadline:
                recent_activity_until = self._last_activity_at + self._activity_grace_s
                deadline = min(max(now, recent_activity_until), hard_deadline)

            if now < deadline:
                try:
                    await asyncio.wait_for(self._changed.wait(), timeout=deadline - now)
                except asyncio.TimeoutError:
                    pass
                continue
            if self._closed:
                break
            if self._pending_commentary is not None:
                commentary = self._pending_commentary
                self._pending_commentary = None
                await self._emit(commentary)
                continue
            self._lease_revision += 1
            await self._emit(
                StreamEvent(
                    kind=KIND_PROGRESS,
                    summary=_build_claw_technical_lease(self._prompt),
                    detail="HER neutral runtime technical lease",
                    event_id=(
                        f"{self._request_id}:technical:lease:{self._lease_revision}"
                    ),
                    delivery_class=DELIVERY_TECHNICAL,
                    origin="her_runtime",
                    phase="execution",
                    revision=self._lease_revision,
                )
            )

    def close(self) -> None:
        self._closed = True
        self._changed.set()


def _build_claw_incomplete_report(
    result: ClawTaskResult, *, prompt: str
) -> tuple[str, dict[str, Any]]:
    ledger = _claw_pair_tool_ledger(result.tool_uses, result.tool_results)
    successful: list[str] = []
    failed: list[str] = []
    verified: list[str] = []
    uncertain: list[str] = []
    missing: list[str] = []

    for name, tool_result in ledger:
        if tool_result is None:
            missing.append(name)
            uncertain.append(name)
        elif _claw_result_is_error(tool_result):
            failed.append(name)
        else:
            successful.append(name)
            if _claw_tool_is_read_only(name) or _claw_has_explicit_verification(
                tool_result
            ):
                verified.append(name)
            else:
                uncertain.append(name)

    failed_counts: dict[str, int] = {}
    for name in failed:
        failed_counts[name] = failed_counts.get(name, 0) + 1
    repeated_failure = any(count >= 2 for count in failed_counts.values())

    stop_reason = str(result.stop_reason or "incomplete").strip().lower()
    execution_limit = stop_reason in {"max_iterations", "budget_exhausted"}
    if execution_limit:
        recommendation = "CONTINUE"
        recommendation_zh = "从已保存的 session 继续；执行轮数耗尽不代表计划需要改变。"
        recommendation_en = (
            "Resume the saved session; exhausting execution turns does not require a plan change."
        )
    elif missing or repeated_failure or failed:
        recommendation = "PIVOT"
        recommendation_zh = "改变策略后再继续；不要重复执行未经核验的副作用操作。"
        recommendation_en = (
            "Change strategy before continuing; do not repeat unverified side effects."
        )
    elif successful:
        recommendation = "CONTINUE"
        recommendation_zh = "从已保存的 session 继续，并先核验当前页面或外部状态。"
        recommendation_en = (
            "Resume the saved session and verify current external state first."
        )
    else:
        recommendation = "STOP"
        recommendation_zh = "当前账本没有可确认进展；停止并重新评估任务或请求人工决定。"
        recommendation_en = "No confirmed progress is present; stop and reassess or request a human decision."

    iterations = result.iterations if result.iterations is not None else "未知"
    stop_reason = result.stop_reason or "incomplete"
    checkpoint_zh = (
        "- 已保存本次 session，可在后续回合继续。"
        if result.session_id
        else "- 本次结果没有 session checkpoint；后续继续前应先核验外部状态。"
    )
    checkpoint_en = (
        "- The session checkpoint was preserved for a later turn."
        if result.session_id
        else "- No session checkpoint was returned; verify external state before continuing."
    )
    use_chinese = any(
        "\u4e00" <= char <= "\u9fff" for char in f"{prompt}\n{result.text}"
    )

    if use_chinese:
        report = "\n".join(
            (
                "## ⚠️ 执行未完成",
                "",
                f"任务在 **{iterations}** 轮后停止；原始模型收尾未作为最终答复发送。",
                "",
                "### 完成了什么",
                f"- 成功返回的工具结果：{_claw_count_names(successful)}",
                f"- 失败的工具结果：{_claw_count_names(failed)}",
                "- 以上只代表工具执行记录，不代表整体业务目标已经完成。",
                "",
                "### 已验证",
                f"- 有明确读取结果或状态变化证据：{_claw_count_names(verified)}",
                "",
                "### 状态不确定",
                f"- 缺少明确状态变化证据或执行回执：{_claw_count_names(uncertain)}",
                f"- 工具执行失败：{_claw_count_names(failed)}",
                "",
                "### 为什么停止",
                f"- `completion_status=incomplete`；`stop_reason={stop_reason}`。",
                checkpoint_zh,
                "",
                "### 建议下一步",
                f"- **{recommendation}** — {recommendation_zh}",
            )
        )
    else:
        report = "\n".join(
            (
                "## ⚠️ Execution incomplete",
                "",
                f"The task stopped after **{iterations}** iterations. The model's raw closing text was not delivered as the final answer.",
                "",
                "### What completed",
                f"- Successful tool results: {_claw_count_names(successful, empty='none')}",
                f"- Failed tool results: {_claw_count_names(failed, empty='none')}",
                "- Tool success does not by itself prove that the overall task completed.",
                "",
                "### Verified",
                f"- Explicit read results or state-change evidence: {_claw_count_names(verified, empty='none')}",
                "",
                "### Uncertain",
                f"- Missing explicit state-change evidence or execution receipt: {_claw_count_names(uncertain, empty='none')}",
                f"- Failed tool executions: {_claw_count_names(failed, empty='none')}",
                "",
                "### Why it stopped",
                f"- `completion_status=incomplete`; `stop_reason={stop_reason}`.",
                checkpoint_en,
                "",
                "### Recommended next step",
                f"- **{recommendation}** — {recommendation_en}",
            )
        )

    metadata = {
        "fallback_report_generated": True,
        "successful_tool_results": len(successful),
        "failed_tool_results": len(failed),
        "verified_tool_results": len(verified),
        "uncertain_tool_results": len(uncertain),
        "missing_tool_results": len(missing),
        "recommended_action": recommendation.lower(),
    }
    return report, metadata


def _claw_incomplete_persona_facts(
    result: ClawTaskResult,
    *,
    metadata: Mapping[str, Any],
) -> list[str]:
    """Build the immutable evidence lines for an incomplete Persona report."""

    ledger = _claw_pair_tool_ledger(result.tool_uses, result.tool_results)
    successful: list[str] = []
    failed: list[str] = []
    verified: list[str] = []
    uncertain: list[str] = []
    for name, tool_result in ledger:
        if tool_result is None:
            uncertain.append(name)
        elif _claw_result_is_error(tool_result):
            failed.append(name)
        else:
            successful.append(name)
            if _claw_tool_is_read_only(name) or _claw_has_explicit_verification(
                tool_result
            ):
                verified.append(name)
            else:
                uncertain.append(name)

    recommendation = str(metadata.get("recommended_action") or "stop").upper()
    iterations = result.iterations if result.iterations is not None else "unknown"
    return [
        "Overall task status: incomplete.",
        f"Stop reason: {result.stop_reason or 'incomplete'}.",
        f"Iterations used: {iterations}.",
        f"Successful tool results: {_claw_count_names(successful, empty='none')}.",
        f"Failed tool results: {_claw_count_names(failed, empty='none')}.",
        f"Verified results: {_claw_count_names(verified, empty='none')}.",
        f"Uncertain results: {_claw_count_names(uncertain, empty='none')}.",
        "Session checkpoint preserved: " + ("yes." if result.session_id else "no."),
        f"Recommended action: {recommendation}.",
    ]


def _claw_incomplete_response(
    result: ClawTaskResult,
    *,
    prompt: str,
) -> tuple[str, dict[str, Any]]:
    """Preserve a safe model closing; otherwise return the neutral evidence report."""
    report, metadata = _build_claw_incomplete_report(result, prompt=prompt)
    model_text = str(result.text or "").strip()
    dangling_tool_markup = (
        "DSML" in model_text and ("tool_calls" in model_text or "invoke name=" in model_text)
    ) or any(
        marker in model_text
        for marker in (
            "<｜｜DSML｜｜tool_calls>",
            "<tool_call>",
            '"tool_calls":',
        )
    )
    if model_text and not dangling_tool_markup:
        recommendation = str(metadata.get("recommended_action") or "stop").upper()
        use_chinese = any(
            "\u4e00" <= char <= "\u9fff" for char in f"{prompt}\n{model_text}"
        )
        actions = (
            {
                "CONTINUE": "从已保存的 session 继续；不要重复已经完成的工作。",
                "PIVOT": "存在明确失败或缺失回执；改变策略后再继续。",
                "STOP": "停止并重新评估任务或请求人工决定。",
            }
            if use_chinese
            else {
                "CONTINUE": "Resume the saved session without repeating completed work.",
                "PIVOT": "A concrete failure or missing receipt exists; change strategy before continuing.",
                "STOP": "Stop and reassess the task or request a human decision.",
            }
        )
        return (
            f"{model_text}\n\n**{recommendation}** — {actions.get(recommendation, actions['STOP'])}",
            {
                **metadata,
                "fallback_report_generated": False,
                "persona_final_response_preserved": True,
                "persona_interpretation_generated": False,
            },
        )
    return report, {
        **metadata,
        "persona_final_response_preserved": False,
        "persona_interpretation_generated": False,
        "persona_render_required": True,
    }


@dataclass(frozen=True)
class ClawPlatform:
    key: str
    rust_target_triple: str
    system: str
    machine: str
    is_wsl: bool = False
    candidate_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackagedClawBinarySpec:
    platform_key: str
    relative_path: Path
    sha256: str
    rust_target_triple: str
    binary_name: str


@dataclass(frozen=True)
class PackagedClawManifest:
    manifest_path: Path
    version: str
    binaries: dict[str, PackagedClawBinarySpec]


@dataclass(frozen=True)
class ClawBinaryResolution:
    path: Path
    source: str
    warnings: tuple[str, ...] = ()
    platform: ClawPlatform | None = None
    manifest_path: Path | None = None
    packaged_version: str | None = None


def redact_secret_text(text: str | None, extra_values: list[str] | None = None) -> str:
    if not text:
        return ""
    redacted = str(text)
    for key in SECRET_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            redacted = redacted.replace(value, "<redacted>")
    for value in extra_values or []:
        if value and value != OLLAMA_DUMMY_API_KEY:
            redacted = redacted.replace(value, "<redacted>")
    redacted = re.sub(
        r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+",
        r"\1<redacted>",
        redacted,
    )
    return redacted


def build_claw_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a minimal environment for HER subprocesses."""
    source = source or os.environ
    env: dict[str, str] = {}
    for key in CLAW_ENV_ALLOWLIST:
        value = source.get(key)
        if value:
            env[key] = str(value)
    if "PATH" not in env and os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    if "HOME" not in env and os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


def detect_hashi_claw_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
    release: str | None = None,
) -> ClawPlatform:
    raw_system = (system or py_platform.system() or "").strip().lower()
    raw_machine = (machine or py_platform.machine() or "").strip().lower()
    raw_release = (release or py_platform.release() or "").strip().lower()
    normalized_machine = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(raw_machine, raw_machine)
    normalized_system = {
        "linux": "linux",
        "windows": "windows",
        "darwin": "macos",
    }.get(raw_system, raw_system)
    is_wsl = normalized_system == "linux" and "microsoft" in raw_release
    mapping = {
        ("linux", "x86_64"): ("linux-x86_64", "x86_64-unknown-linux-gnu"),
        ("linux", "arm64"): ("linux-arm64", "aarch64-unknown-linux-gnu"),
        ("windows", "x86_64"): ("windows-x86_64", "x86_64-pc-windows-msvc"),
        ("windows", "arm64"): ("windows-arm64", "aarch64-pc-windows-msvc"),
        ("macos", "x86_64"): ("macos-x86_64", "x86_64-apple-darwin"),
        ("macos", "arm64"): ("macos-arm64", "aarch64-apple-darwin"),
    }
    resolved = mapping.get((normalized_system, normalized_machine))
    if not resolved:
        raise ClawPackagedRuntimeError(
            "Unsupported packaged HER platform "
            f"(system={raw_system or '<unknown>'}, machine={raw_machine or '<unknown>'})"
        )
    key, triple = resolved
    candidate_keys = (f"{key}-wsl", key) if is_wsl else (key,)
    return ClawPlatform(
        key=key,
        rust_target_triple=triple,
        system=normalized_system,
        machine=normalized_machine,
        is_wsl=is_wsl,
        candidate_keys=candidate_keys,
    )


def _packaged_claw_roots(global_config: Any | None = None) -> list[Path]:
    project_root = getattr(global_config, "project_root", None)
    if project_root:
        roots = [Path(project_root).expanduser() / "hashi_assets" / "her"]
    else:
        roots = [Path(__file__).resolve().parent.parent / "hashi_assets" / "her"]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _resolve_development_claw_binary(
    global_config: Any | None = None,
) -> ClawBinaryResolution | None:
    """Resolve only an explicitly selected, validated `/rebuild` candidate."""
    bridge_home = getattr(global_config, "bridge_home", None)
    if not bridge_home:
        return None
    state_root = Path(bridge_home).expanduser().resolve() / "state" / "her_rebuild"
    selection_path = state_root / "development-selection.json"
    if not selection_path.exists():
        return None
    platform = detect_hashi_claw_platform()
    try:
        from orchestrator.her_rebuild import DevelopmentSelectionStore

        candidate = DevelopmentSelectionStore(
            selection_path,
            candidates_root=state_root / "candidates",
        ).active_candidate(target=platform.rust_target_triple)
    except Exception as exc:  # noqa: BLE001 - selected development runtime must fail closed
        raise ClawPackagedRuntimeError(
            "Selected HER development runtime is invalid; refusing silent fallback "
            f"({type(exc).__name__}: {exc})"
        ) from exc
    if candidate is None:
        return None
    return ClawBinaryResolution(
        path=Path(candidate.binary_path).resolve(),
        source="development-source-build",
        platform=platform,
        manifest_path=selection_path,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_packaged_claw_manifest(manifest_path: Path) -> PackagedClawManifest:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClawPackagedRuntimeError(
            f"Packaged HER manifest not found: {manifest_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ClawPackagedRuntimeError(
            f"Packaged HER manifest is invalid JSON: {manifest_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ClawPackagedRuntimeError(
            f"Packaged HER manifest must be an object: {manifest_path}"
        )
    manifest_version = payload.get("manifest_version")
    if manifest_version != PACKAGED_CLAW_MANIFEST_VERSION:
        raise ClawPackagedRuntimeError(
            f"Packaged HER manifest_version must be {PACKAGED_CLAW_MANIFEST_VERSION}; got {manifest_version!r}"
        )
    runtime = str(payload.get("runtime") or "").strip()
    if runtime != PACKAGED_CLAW_RUNTIME:
        raise ClawPackagedRuntimeError(
            f"Packaged HER manifest runtime must be {PACKAGED_CLAW_RUNTIME!r}; got {runtime!r}"
        )
    version = str(payload.get("version") or "").strip()
    if not version:
        raise ClawPackagedRuntimeError(
            f"Packaged HER manifest missing version: {manifest_path}"
        )
    raw_binaries = payload.get("binaries")
    if not isinstance(raw_binaries, Mapping):
        raise ClawPackagedRuntimeError(
            f"Packaged HER manifest binaries must be an object: {manifest_path}"
        )
    binaries: dict[str, PackagedClawBinarySpec] = {}
    for platform_key, raw_spec in raw_binaries.items():
        if not isinstance(raw_spec, Mapping):
            raise ClawPackagedRuntimeError(
                f"Packaged HER binary entry must be an object: {platform_key}"
            )
        rel_path = Path(str(raw_spec.get("path") or "").strip())
        sha256 = str(raw_spec.get("sha256") or "").strip().lower()
        rust_target_triple = str(
            raw_spec.get("rust_target_triple") or raw_spec.get("triple") or ""
        ).strip()
        binary_name = str(raw_spec.get("binary_name") or rel_path.name).strip()
        if not rel_path.as_posix() or rel_path.is_absolute() or ".." in rel_path.parts:
            raise ClawPackagedRuntimeError(
                f"Packaged HER path must be relative and stay under root: {platform_key}"
            )
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ClawPackagedRuntimeError(
                f"Packaged HER sha256 must be a 64-character hex digest: {platform_key}"
            )
        if not rust_target_triple:
            raise ClawPackagedRuntimeError(
                f"Packaged HER rust_target_triple missing: {platform_key}"
            )
        binaries[str(platform_key)] = PackagedClawBinarySpec(
            platform_key=str(platform_key),
            relative_path=rel_path,
            sha256=sha256,
            rust_target_triple=rust_target_triple,
            binary_name=binary_name,
        )
    return PackagedClawManifest(
        manifest_path=manifest_path, version=version, binaries=binaries
    )


def resolve_packaged_claw_binary(
    packaged_root: Path,
    *,
    platform: ClawPlatform | None = None,
) -> ClawBinaryResolution:
    platform = platform or detect_hashi_claw_platform()
    manifest = load_packaged_claw_manifest(packaged_root / "manifest.json")
    spec = next(
        (
            manifest.binaries.get(key)
            for key in platform.candidate_keys
            if manifest.binaries.get(key)
        ),
        None,
    )
    if spec is None:
        supported = ", ".join(sorted(manifest.binaries)) or "<none>"
        raise ClawPackagedRuntimeError(
            f"Packaged HER manifest has no binary for {platform.key}; supported={supported}"
        )
    if spec.rust_target_triple != platform.rust_target_triple:
        raise ClawPackagedRuntimeError(
            f"Packaged HER target mismatch for {spec.platform_key}: "
            f"expected {platform.rust_target_triple}, got {spec.rust_target_triple}"
        )
    binary_path = (packaged_root / spec.relative_path).resolve()
    try:
        binary_path.relative_to(packaged_root.resolve())
    except ValueError as exc:
        raise ClawPackagedRuntimeError(
            f"Packaged HER binary escapes packaged root: {spec.relative_path}"
        ) from exc
    if not binary_path.is_file():
        raise ClawPackagedRuntimeError(f"Packaged HER binary missing: {binary_path}")
    if not os.access(binary_path, os.X_OK):
        raise ClawPackagedRuntimeError(
            f"Packaged HER binary is not executable: {binary_path}"
        )
    actual_sha256 = _sha256_file(binary_path)
    if actual_sha256 != spec.sha256:
        raise ClawPackagedRuntimeError(
            f"Packaged HER checksum mismatch for {binary_path}: "
            f"expected {spec.sha256[:12]}..., got {actual_sha256[:12]}..."
        )
    return ClawBinaryResolution(
        path=binary_path,
        source="packaged",
        platform=platform,
        manifest_path=manifest.manifest_path,
        packaged_version=manifest.version,
    )


def _claw_runtime_policy(
    global_config: Any | None = None, agent_config: Any | None = None
) -> str:
    extra = getattr(agent_config, "extra", None) or {}
    if isinstance(extra, Mapping) and (
        extra.get("her_runtime_policy") or extra.get("claw_runtime_policy")
    ):
        policy = (
            str(extra.get("her_runtime_policy") or extra.get("claw_runtime_policy"))
            .strip()
            .lower()
        )
    else:
        global_her = (
            getattr(global_config, "her_providers", None)
            or getattr(global_config, "claw_providers", None)
            or {}
        )
        policy = (
            str(global_her.get("runtime_policy") or "prefer-packaged").strip().lower()
            if isinstance(global_her, Mapping)
            else "prefer-packaged"
        )
    if policy not in CLAW_RUNTIME_POLICIES:
        raise ClawBinaryNotFound(
            f"Invalid HER runtime_policy={policy!r}; expected one of {sorted(CLAW_RUNTIME_POLICIES)}"
        )
    return policy


def _resolve_executable_candidate(
    candidate: str | os.PathLike[str],
) -> tuple[Path | None, str | None]:
    raw = str(candidate).strip()
    if not raw:
        return None, None
    resolved = shutil.which(raw) if os.path.basename(raw) == raw else raw
    if not resolved:
        return None, f"{raw}: not found on PATH"
    path = Path(resolved).expanduser()
    if not path.exists():
        return None, f"{path}: does not exist"
    if not path.is_file():
        return None, f"{path}: not a file"
    if not os.access(path, os.X_OK):
        return None, f"{path}: not executable"
    return path.resolve(), None


def discover_claw_binary(
    configured_path: str | os.PathLike[str] | None = None,
    *,
    global_config: Any | None = None,
    agent_config: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> ClawBinaryResolution:
    """Resolve an executable HER binary without requiring Cargo."""
    policy = _claw_runtime_policy(global_config, agent_config)
    development = _resolve_development_claw_binary(global_config)
    if development is not None:
        return development
    early_candidates: list[tuple[str, str | os.PathLike[str]]] = []
    if configured_path:
        early_candidates.append(("configured", configured_path))

    extra = getattr(agent_config, "extra", None) or {}
    if isinstance(extra, Mapping):
        for key in ("her_binary_path", "her_cmd", "claw_binary_path", "claw_cmd"):
            value = extra.get(key)
            if value:
                early_candidates.append((f"agent:{key}", value))

    for key in ("her_binary_path", "her_cmd", "claw_binary_path", "claw_cmd"):
        value = getattr(global_config, key, None)
        if value:
            early_candidates.append((f"global:{key}", value))

    global_her = (
        getattr(global_config, "her_providers", None)
        or getattr(global_config, "claw_providers", None)
        or {}
    )
    if isinstance(global_her, Mapping):
        for key in (
            "binary_path",
            "her_binary_path",
            "her_cmd",
            "claw_binary_path",
            "claw_cmd",
        ):
            value = global_her.get(key)
            if value:
                early_candidates.append((f"global.her_providers:{key}", value))

    failures: list[str] = []
    if policy != "require-packaged":
        for source, candidate in early_candidates:
            path, failure = _resolve_executable_candidate(candidate)
            if path is not None:
                return ClawBinaryResolution(path=path, source=source)
            if failure:
                failures.append(failure)
        if early_candidates:
            detail = (
                "; ".join(failures)
                if failures
                else "configured HER runtime is unavailable"
            )
            raise ClawBinaryNotFound(f"Configured HER binary not found ({detail})")

    packaged_errors: list[str] = []
    if policy != "system-only":
        for root in _packaged_claw_roots(global_config):
            manifest_path = root / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                return resolve_packaged_claw_binary(root)
            except ClawPackagedRuntimeError as exc:
                packaged_errors.append(str(exc))
        failures.extend(packaged_errors)
        if policy == "require-packaged":
            detail = (
                "; ".join(failures) if failures else "no packaged HER manifest found"
            )
            raise ClawBinaryNotFound(
                f"Packaged HER runtime required but unavailable ({detail})"
            )

    candidates: list[tuple[str, str | os.PathLike[str]]] = []
    env = env or os.environ
    for key in ("CLAW_BINARY", "CLAW_BIN"):
        value = env.get(key)
        if value:
            candidates.append((f"env:{key}", value))

    candidates.append(("PATH", "claw"))

    for source, candidate in candidates:
        path, failure = _resolve_executable_candidate(candidate)
        if path is not None:
            warnings = (
                tuple(packaged_errors)
                if packaged_errors and source.startswith(("env:", "PATH"))
                else ()
            )
            return ClawBinaryResolution(path=path, source=source, warnings=warnings)
        if failure:
            failures.append(failure)

    detail = "; ".join(failures) if failures else "no candidate configured"
    raise ClawBinaryNotFound(f"HER binary not found ({detail})")


def find_claw_binary(
    configured_path: str | os.PathLike[str] | None = None,
    *,
    global_config: Any | None = None,
    agent_config: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> Path:
    return discover_claw_binary(
        configured_path,
        global_config=global_config,
        agent_config=agent_config,
        env=env,
    ).path


def _parse_json_output(text: str, *, command: list[str]) -> dict[str, Any]:
    command_name = Path(command[0]).name if command else PACKAGED_CLAW_RUNTIME
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClawJsonError(
            f"HER command {command_name} produced non-JSON output ({len(text)} chars)",
            output=text,
        ) from exc
    if not isinstance(loaded, dict):
        raise ClawJsonError(
            f"HER command {command_name} JSON output was not an object",
            output=text,
        )
    return loaded


def _parse_stream_json_output(text: str, *, command: list[str]) -> dict[str, Any]:
    final: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None
    non_json_line_count = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A complete run_finished event is authoritative. Preserve forward
            # compatibility with older HER builds that leaked an interactive
            # permission prompt into stdout, but retain a count for diagnostics.
            non_json_line_count += 1
            continue
        if isinstance(event, dict):
            if event.get("kind") == "run_finished":
                final = event
            elif (
                event.get("kind") == "error"
                or event.get("type") == "error"
                or event.get("error")
            ):
                last_error = event
    if final is None:
        command_name = Path(command[0]).name if command else PACKAGED_CLAW_RUNTIME
        last_kind = str(
            (last_error or {}).get("kind") or (last_error or {}).get("type") or "none"
        )
        diagnostic = f"; last_error_kind={last_kind}" if last_error is not None else ""
        if non_json_line_count:
            diagnostic += f"; non_json_lines={non_json_line_count}"
        raise ClawJsonError(
            f"HER stream-json from {command_name} did not include run_finished{diagnostic}",
            output=text,
        )
    if non_json_line_count:
        final = dict(final)
        final["_protocol_non_json_line_count"] = non_json_line_count
    return final


def _last_stream_json_error(*streams: str) -> dict[str, Any] | None:
    """Return the last structured terminal error from HER stdout or stderr."""

    last_error: dict[str, Any] | None = None
    for text in streams:
        for raw_line in str(text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and (
                event.get("kind") == "error"
                or event.get("type") == "error"
                or event.get("error")
            ):
                last_error = event
    return last_error


def _stream_json_usage(text: str) -> dict[str, Any]:
    usage = ClawThinkingStreamUsage()
    legacy_summary_chars = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        kind = event.get("kind")
        if kind in {"thinking_delta", "thinking_redacted"}:
            thinking_chars = int(event.get("thinking_chars") or 0)
            usage.thinking_chars += thinking_chars
            usage.thinking_event_count += 1
            usage.saw_actual_thinking_event = True
            source = str(event.get("reasoning_source") or "").strip()
            if source:
                usage.thinking_sources.add(source)
            if kind == "thinking_redacted":
                usage.thinking_redacted_count += 1
        elif kind == "thinking_summary":
            legacy_summary_chars += int(event.get("thinking_chars") or 0)
        if event.get("kind") == "usage":
            thinking_tokens = int(
                event.get("thinking_tokens")
                or (event.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                )
                or 0
            )
            usage.thinking_tokens = max(usage.thinking_tokens, thinking_tokens)
    if not usage.saw_actual_thinking_event and legacy_summary_chars > 0:
        usage.thinking_chars += legacy_summary_chars
        usage.thinking_event_count += 1
    if usage.thinking_tokens == 0 and usage.thinking_chars > 0:
        usage.thinking_tokens = max(1, usage.thinking_chars // 4)
    return usage.to_dict()


def _her_stream_revision(event: Mapping[str, Any]) -> int | None:
    frame = event.get("frame") if isinstance(event.get("frame"), Mapping) else {}
    for value in (
        event.get("revision"),
        event.get("revision_round"),
        frame.get("revision"),
    ):
        if value is None or isinstance(value, bool):
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


def _her_task_frame_is_direct_response(event: Mapping[str, Any]) -> bool:
    frame = event.get("frame") if isinstance(event.get("frame"), Mapping) else {}
    value = frame.get("direct_response", event.get("direct_response"))
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _her_stream_phase(event: Mapping[str, Any]) -> str:
    explicit = str(event.get("phase") or "").strip().lower()
    if explicit:
        return explicit
    kind = str(event.get("kind") or "")
    if kind == "run_started":
        return "initial"
    if kind in {"independent_review", "control_invocation"}:
        return "verification"
    if kind in {"run_finished", "provider_stop_reason", "terminal_diagnostic"}:
        return "finalization"
    return "execution"


def _her_stream_origin(event: Mapping[str, Any]) -> str:
    kind = str(event.get("kind") or "")
    if kind in {"task_acknowledgement", "task_plan", "user_commentary", "task_commentary"}:
        return "her_planner"
    if kind in {"thinking_delta", "thinking_redacted", "thinking_summary"}:
        return "provider"
    if kind == "assistant_delta":
        return "primary_model"
    if kind in {"tool_call", "tool_start", "tool_end"}:
        return "tool_gateway"
    if kind in {"independent_review", "control_invocation"}:
        return "her_reviewer"
    return "her_runtime"


def _her_stream_delivery(
    event: Mapping[str, Any],
    mapped: StreamEvent,
) -> tuple[str, bool, str]:
    kind = str(event.get("kind") or "")
    if mapped.kind == KIND_THINKING:
        provenance = str(event.get("visibility") or "").strip()
        if not provenance:
            provenance = {
                "thinking_delta": "provider_returned",
                "thinking_redacted": "provider_redacted",
                "thinking_summary": "provider_summary",
            }.get(kind, "provider_returned")
        return DELIVERY_REASONING, False, provenance
    if mapped.kind in {KIND_ACKNOWLEDGEMENT, KIND_COMMENTARY}:
        return DELIVERY_USER_COMMENTARY, False, "model_authored"
    if mapped.kind == KIND_TEXT_DELTA:
        return DELIVERY_INTERNAL, False, "provider_returned"
    if kind == "permission_required":
        return DELIVERY_CONTROL, True, "runtime_control"
    actionability = str(event.get("actionability") or "").strip().lower()
    explicit_user_action = event.get("user_action_required")
    user_action_required = (
        explicit_user_action is True
        or str(explicit_user_action or "").strip().lower() in {"1", "true", "yes", "on"}
        or actionability
        in {
            "user_action_required",
            "requires_user",
            "permission_required",
            "missing_user_decision",
            "terminal_actionable_blocker",
        }
    )
    if user_action_required:
        return DELIVERY_CONTROL, True, "runtime_control"
    if kind == "error" or event.get("type") == "error" or event.get("error"):
        return DELIVERY_TECHNICAL, False, "runtime_error"
    if mapped.kind == KIND_ERROR:
        return DELIVERY_TECHNICAL, False, "runtime_error"
    return DELIVERY_TECHNICAL, False, "runtime_observed"


def _her_stream_event_id(
    event: Mapping[str, Any],
    mapped: StreamEvent,
    *,
    request_id: str,
    source_index: int | None,
    mapped_index: int,
) -> str:
    explicit = str(event.get("event_id") or "").strip()
    if explicit:
        return explicit if mapped_index == 0 else f"{explicit}:{mapped_index}"
    request = str(request_id or "her-request")
    kind = str(event.get("kind") or "unknown")
    phase = _her_stream_phase(event)
    revision = _her_stream_revision(event)
    if kind == "task_acknowledgement":
        return f"{request}:ack:initial"
    if kind == "task_plan":
        if phase == "initial":
            suffix = "initial"
        elif revision is not None:
            suffix = f"{phase}:{revision}"
        else:
            frame = event.get("frame") if isinstance(event.get("frame"), Mapping) else {}
            digest = hashlib.sha256(
                json.dumps(frame, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            suffix = f"{phase}:{digest}"
        if mapped.delivery_class == DELIVERY_USER_COMMENTARY:
            return f"{request}:commentary:{suffix}"
        if mapped_index == 0:
            return f"{request}:plan:{suffix}"
        return f"{request}:technical:task_plan:{suffix}:{mapped_index}"
    if kind in {"user_commentary", "task_commentary"}:
        identity = revision if revision is not None else source_index
        return f"{request}:commentary:{phase}:{identity or 0}:{mapped_index}"
    ordinal = source_index if source_index is not None else 0
    owner = mapped.delivery_class or DELIVERY_INTERNAL
    return f"{request}:{owner}:{kind}:{ordinal}:{mapped_index}"


def _with_her_stream_metadata(
    mapped: StreamEvent,
    event: Mapping[str, Any],
    *,
    request_id: str,
    source_index: int | None,
    mapped_index: int,
) -> StreamEvent:
    delivery_class, required, provenance = _her_stream_delivery(event, mapped)
    enriched = replace(
        mapped,
        delivery_class=delivery_class,
        origin=_her_stream_origin(event),
        phase=_her_stream_phase(event),
        revision=_her_stream_revision(event),
        required=required,
        provenance=provenance,
    )
    return replace(
        enriched,
        event_id=_her_stream_event_id(
            event,
            enriched,
            request_id=request_id,
            source_index=source_index,
            mapped_index=mapped_index,
        ),
    )


def _claw_jsonl_to_stream_event(event: Mapping[str, Any]) -> StreamEvent | None:
    kind = str(event.get("kind") or "")
    if kind == "run_started":
        model = event.get("model") or "model unknown"
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=f"HER stream started ({model})",
        )
    if kind == "thinking_delta":
        text = str(event.get("text") or "")
        thinking_chars = int(event.get("thinking_chars") or len(text))
        source = str(event.get("reasoning_source") or "").strip()
        detail_parts = (
            [f"thinking_chars={thinking_chars}"] if thinking_chars > 0 else []
        )
        if source:
            detail_parts.append(f"source={source}")
        return (
            StreamEvent(
                kind=KIND_THINKING,
                summary=text[:400],
                raw_delta=text,
                detail=";".join(detail_parts),
            )
            if text
            else None
        )
    if kind == "thinking_redacted":
        summary = str(
            event.get("summary") or "provider emitted redacted reasoning block"
        )
        thinking_chars = int(event.get("thinking_chars") or 0)
        source = str(event.get("reasoning_source") or "").strip()
        detail_parts = [f"thinking_chars={thinking_chars}", "redacted=true"]
        if source:
            detail_parts.append(f"source={source}")
        return StreamEvent(
            kind=KIND_THINKING, summary=summary[:400], detail=";".join(detail_parts)
        )
    if kind == "thinking_summary":
        summary = str(event.get("summary") or "HER thinking")
        thinking_chars = int(event.get("thinking_chars") or 0)
        detail = f"thinking_chars={thinking_chars}" if thinking_chars > 0 else ""
        return StreamEvent(kind=KIND_THINKING, summary=summary[:400], detail=detail)
    if kind == "assistant_delta":
        text = str(event.get("text") or "")
        return StreamEvent(kind=KIND_TEXT_DELTA, summary=text[:200]) if text else None
    if kind == "task_acknowledgement":
        text = str(event.get("text") or event.get("summary") or "").strip()
        return StreamEvent(kind=KIND_ACKNOWLEDGEMENT, summary=text) if text else None
    if kind in {"user_commentary", "task_commentary"}:
        text = str(event.get("text") or event.get("summary") or "").strip()
        return StreamEvent(kind=KIND_COMMENTARY, summary=text) if text else None
    if kind == "permission_required":
        tool_name = str(event.get("tool_name") or "tool")
        current_mode = str(event.get("current_mode") or "unknown")
        required_mode = str(event.get("required_mode") or "unknown")
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=f"HER permission required for {tool_name}"[:500],
            detail=f"current_mode={current_mode};required_mode={required_mode}"[:1000],
            tool_name=tool_name,
        )
    if kind == "permission_decision":
        tool_name = str(event.get("tool_name") or "tool")
        decision = str(event.get("decision") or "unknown")
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=f"HER permission {decision} for {tool_name}"[:500],
            detail=str(event.get("reason") or "")[:1000],
            tool_name=tool_name,
        )
    if kind == "provider_stop_reason":
        reason = str(event.get("reason") or "unknown").strip()
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=f"HER provider stop reason: {reason}"[:500],
            detail=reason[:1000],
        )
    if kind == "task_plan":
        phase = str(event.get("phase") or "update")
        frame = event.get("frame") if isinstance(event.get("frame"), Mapping) else {}
        goal = str(frame.get("active_goal") or "").strip()
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=f"HER task plan {phase}: {goal}"[:500],
            detail=json.dumps(frame, ensure_ascii=False)[:2000],
        )
    if kind == "independent_review":
        gate = str(event.get("gate") or "unknown")
        revision_round = int(event.get("revision_round") or 0)
        review = event.get("review") if isinstance(event.get("review"), Mapping) else {}
        decision = str(review.get("decision") or "unknown").upper()
        summary = str(
            review.get("summary") or event.get("summary") or "no summary"
        ).strip()
        return StreamEvent(
            kind=KIND_REVIEW,
            summary=f"Review {gate} r{revision_round}: {decision} — {summary}"[:500],
            detail=json.dumps(review, ensure_ascii=False)[:4000],
        )
    if kind == "max_plus_checkpoint":
        phase = str(event.get("phase") or "unknown")
        budget = event.get("budget") if isinstance(event.get("budget"), Mapping) else {}
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=f"MAX+ checkpoint: {phase}"[:500],
            detail=json.dumps(
                {"budget": budget, "stop_reason": event.get("stop_reason")},
                ensure_ascii=False,
            )[:4000],
        )
    if kind == "control_invocation":
        stage = str(event.get("stage") or "unknown")
        gate = str(event.get("gate") or "unknown")
        revision_round = int(event.get("revision_round") or 0)
        format_attempt = int(event.get("format_attempt") or 0)
        outcome = str(event.get("outcome") or "unknown")
        request = (
            event.get("request") if isinstance(event.get("request"), Mapping) else {}
        )
        usage = event.get("usage") if isinstance(event.get("usage"), Mapping) else {}
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=(
                f"HER control {stage}/{gate} r{revision_round} "
                f"attempt={format_attempt} outcome={outcome}"
            )[:500],
            detail=(
                f"allow_tools={bool(request.get('allow_tools'))};"
                f"input_tokens={int(usage.get('input_tokens') or 0)};"
                f"output_tokens={int(usage.get('output_tokens') or 0)};"
                f"cache_creation_input_tokens={int(usage.get('cache_creation_input_tokens') or 0)};"
                f"cache_read_input_tokens={int(usage.get('cache_read_input_tokens') or 0)}"
            ),
        )
    if kind == "plan_divergence":
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=str(event.get("summary") or "HER plan divergence")[:500],
            detail=str(event.get("reason") or "")[:1000],
            tool_name=str(event.get("tool_name") or ""),
        )
    if kind == "semantic_compaction":
        status = str(event.get("status") or "unknown")
        reason = redact_secret_text(str(event.get("reason") or ""))[:500]
        removed = int(event.get("removed_message_count") or 0)
        timeout_seconds = int(event.get("timeout_seconds") or 0)
        timeout_source = str(event.get("timeout_source") or "unknown")
        elapsed_ms = int(event.get("elapsed_ms") or 0)
        estimated_tokens = int(event.get("estimated_input_tokens") or 0)
        trigger_phase = str(event.get("trigger_phase") or "unknown")
        request_id = str(event.get("request_id") or "")
        session_id = str(event.get("session_id") or "")
        unchanged = bool(event.get("original_context_unchanged"))
        will_continue = bool(event.get("will_continue"))
        token_label = (
            f"~{estimated_tokens / 1000:.0f}K tokens"
            if estimated_tokens >= 1000
            else f"~{estimated_tokens} tokens"
        )
        if status == "started":
            summary = (
                "🧠 semantic_compaction started"
                f" · {token_label} · budget {timeout_seconds}s ({timeout_source})"
                f" · {trigger_phase}"
            )
        elif status == "completed":
            summary = (
                "✅ semantic_compaction completed"
                f" · removed {removed} · elapsed {elapsed_ms / 1000:.1f}s"
                " · raw history retained"
            )
        elif status == "failed":
            context_state = (
                "original context unchanged" if unchanged else "context state unknown"
            )
            continuation = "continuing" if will_continue else "cannot continue"
            summary = (
                "⚠️ semantic_compaction failed"
                f" · {context_state} · {continuation}"
                f" · {reason or 'unknown reason'}"
            )
        else:
            summary = f"HER semantic compaction {status}"
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=summary[:500],
            detail=(
                f"request_id={request_id};session_id={session_id};"
                f"trigger_phase={trigger_phase};"
                f"estimated_input_tokens={estimated_tokens};"
                f"removed={removed};timeout_seconds={timeout_seconds};"
                f"timeout_source={timeout_source};elapsed_ms={elapsed_ms};"
                f"original_context_unchanged={str(unchanged).lower()};"
                f"will_continue={str(will_continue).lower()};reason={reason}"
            )[:4000],
        )
    if kind == "terminal_diagnostic":
        classification = str(event.get("classification") or "unknown")
        action = str(event.get("action") or "unknown")
        provider_reason = str(event.get("provider_stop_reason") or "unknown")
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=f"HER terminal diagnostic: {classification}"[:500],
            detail=f"action={action};provider_stop_reason={provider_reason}"[:2000],
        )
    if kind in {"tool_call", "tool_start"}:
        name = str(event.get("name") or event.get("tool_name") or "tool")
        summary = str(event.get("summary") or f"HER tool started: {name}")
        return StreamEvent(kind=KIND_TOOL_START, summary=summary[:200], tool_name=name)
    if kind == "tool_end":
        name = str(event.get("name") or event.get("tool_name") or "tool")
        summary = str(event.get("summary") or f"HER tool finished: {name}")
        detail = str(event.get("output_preview") or "")
        return StreamEvent(
            kind=KIND_TOOL_END,
            summary=summary[:200],
            detail=detail[:500],
            tool_name=name,
        )
    if kind == "usage":
        thinking_tokens = int(
            event.get("thinking_tokens")
            or (event.get("completion_tokens_details") or {}).get("reasoning_tokens")
            or 0
        )
        return StreamEvent(
            kind=KIND_PROGRESS,
            summary=(
                f"HER usage input={int(event.get('input_tokens') or 0)} "
                f"output={int(event.get('output_tokens') or 0)} "
                f"thinking={thinking_tokens} "
                f"thinking_source={event.get('thinking_token_source') or 'unavailable'}"
            ),
        )
    if kind == "error":
        return StreamEvent(
            kind=KIND_ERROR, summary=str(event.get("error") or event)[:400]
        )
    if event.get("type") == "error" or event.get("error"):
        return StreamEvent(
            kind=KIND_ERROR, summary=str(event.get("error") or event)[:400]
        )
    if kind in {"message_stop", "run_finished", "prompt_cache"}:
        return None
    return StreamEvent(kind=KIND_PROGRESS, summary=f"HER event: {kind}"[:200])


def _claw_jsonl_to_stream_events(
    event: Mapping[str, Any],
    *,
    commentary_prompt: str = "",
    request_id: str = "",
    source_index: int | None = None,
) -> list[StreamEvent]:
    """Expand one HER JSONL record into explicitly owned stream activities."""
    _ = commentary_prompt  # retained for call compatibility; never used as Persona authority
    events: list[StreamEvent] = []
    primary = _claw_jsonl_to_stream_event(event)
    if primary is not None:
        events.append(primary)

    event_kind = str(event.get("kind") or "")
    if event_kind == "independent_review" and str(event.get("gate") or "") in {
        "completion",
        "execution_evidence",
    }:
        review = event.get("review") if isinstance(event.get("review"), Mapping) else {}
        decision = str(review.get("decision") or "unknown").upper()
        revision_round = int(event.get("revision_round") or 0)
        summary = str(
            review.get("summary") or event.get("summary") or "no summary"
        ).strip()
        findings = (
            review.get("findings") if isinstance(review.get("findings"), list) else []
        )
        validation_findings = [
            finding
            for finding in findings
            if isinstance(finding, Mapping)
            and str(finding.get("category") or "") == "verification"
        ]
        testing_findings = [
            finding
            for finding in findings
            if isinstance(finding, Mapping)
            and str(finding.get("category") or "") == "testing"
        ]
        events.append(
            StreamEvent(
                kind=KIND_VALIDATION,
                summary=f"Validation evidence review r{revision_round}: {decision} — {summary}"[
                    :500
                ],
                detail=json.dumps(
                    validation_findings or review.get("missing_evidence") or [],
                    ensure_ascii=False,
                )[:4000],
            )
        )
        events.append(
            StreamEvent(
                kind=KIND_TESTING,
                summary=f"Testing evidence review r{revision_round}: {decision} — {summary}"[
                    :500
                ],
                detail=json.dumps(testing_findings, ensure_ascii=False)[:4000],
            )
        )

    if event_kind != "task_plan":
        return [
            _with_her_stream_metadata(
                mapped,
                event,
                request_id=request_id,
                source_index=source_index,
                mapped_index=index,
            )
            for index, mapped in enumerate(events)
        ]
    frame = event.get("frame") if isinstance(event.get("frame"), Mapping) else {}
    assurance = (
        frame.get("assurance") if isinstance(frame.get("assurance"), Mapping) else {}
    )
    phase = str(event.get("phase") or "update")

    def _items(name: str) -> list[str]:
        value = assurance.get(name)
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _summary(label: str, status: str, items: list[str]) -> str:
        first = items[0] if items else "none recorded"
        suffix = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
        return f"{label} {status} [{phase}]: {first}{suffix}"[:500]

    validation_plan = _items("validation_strategy")
    validation_evidence = _items("validation_evidence")
    testing_plan = _items("test_strategy")
    testing_evidence = _items("testing_evidence")
    review_findings = _items("critical_review_findings")
    unverified = _items("unverified_items")

    if validation_plan:
        events.append(
            StreamEvent(
                kind=KIND_VALIDATION,
                summary=_summary("Validation", "plan", validation_plan),
                detail=json.dumps(validation_plan, ensure_ascii=False)[:4000],
            )
        )
    if validation_evidence:
        events.append(
            StreamEvent(
                kind=KIND_VALIDATION,
                summary=_summary("Validation", "evidence", validation_evidence),
                detail=json.dumps(validation_evidence, ensure_ascii=False)[:4000],
            )
        )
    if testing_plan:
        events.append(
            StreamEvent(
                kind=KIND_TESTING,
                summary=_summary("Testing", "plan", testing_plan),
                detail=json.dumps(testing_plan, ensure_ascii=False)[:4000],
            )
        )
    if testing_evidence:
        events.append(
            StreamEvent(
                kind=KIND_TESTING,
                summary=_summary("Testing", "evidence", testing_evidence),
                detail=json.dumps(testing_evidence, ensure_ascii=False)[:4000],
            )
        )
    if review_findings:
        events.append(
            StreamEvent(
                kind=KIND_REVIEW,
                summary=_summary("Critical review", "findings", review_findings),
                detail=json.dumps(review_findings, ensure_ascii=False)[:4000],
            )
        )
    elif phase in {"critical_review", "finalization_review"}:
        events.append(
            StreamEvent(
                kind=KIND_REVIEW,
                summary=f"Critical review checkpoint [{phase}]: no findings recorded"[
                    :500
                ],
            )
        )
    if unverified:
        events.append(
            StreamEvent(
                kind=KIND_VALIDATION,
                summary=_summary("Validation", "unverified", unverified),
                detail=json.dumps(unverified, ensure_ascii=False)[:4000],
            )
        )
    return [
        _with_her_stream_metadata(
            mapped,
            event,
            request_id=request_id,
            source_index=source_index,
            mapped_index=index,
        )
        for index, mapped in enumerate(events)
    ]


def claw_supports_stream_json(
    binary_path: str | os.PathLike[str],
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    timeout_s: float = 5,
) -> bool:
    process_env = build_claw_env(env)
    try:
        completed = subprocess.run(
            [str(binary_path), "--help"],
            cwd=cwd,
            env=process_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "stream-json" in f"{completed.stdout}\n{completed.stderr}"


def run_claw_json_command(
    args: list[str],
    *,
    cwd: str | os.PathLike[str],
    binary_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
    stdin_text: str | None = None,
) -> ClawCommandResult:
    binary = find_claw_binary(binary_path, env=env)
    command = [str(binary), *args]
    started = time.perf_counter()
    process_env = build_claw_env(env)
    secret_values = [process_env.get(key, "") for key in SECRET_ENV_KEYS]
    try:
        run_kwargs: dict[str, Any] = {
            "cwd": str(cwd),
            "env": process_env,
            "capture_output": True,
            "text": True,
            "timeout": timeout_s,
            "check": False,
        }
        if stdin_text is None:
            run_kwargs["stdin"] = subprocess.DEVNULL
        else:
            run_kwargs["input"] = stdin_text
        completed = subprocess.run(
            command,
            **run_kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClawTimeoutError(
            f"HER command timed out after {timeout_s}s: {' '.join(command)}",
            timeout_s=timeout_s,
        ) from exc

    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    stdout = redact_secret_text(completed.stdout, secret_values)
    stderr = redact_secret_text(completed.stderr, secret_values)
    if completed.returncode != 0:
        parsed = _last_stream_json_error(stdout, stderr)
        if parsed is None:
            output = stdout.strip() or stderr.strip()
            try:
                parsed = _parse_json_output(output, command=command) if output else {}
            except ClawJsonError:
                parsed = {}
        message = (
            parsed.get("error_message")
            or parsed.get("error")
            or parsed.get("message")
            if isinstance(parsed, dict)
            else None
        )
        raise ClawCommandError(
            message or f"HER command exited with code {completed.returncode}",
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            parsed_error=parsed if parsed else None,
        )

    output = stdout.strip() or stderr.strip()
    parsed = _parse_json_output(output, command=command) if output else {}

    return ClawCommandResult(
        command=command,
        cwd=str(cwd),
        returncode=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        json_data=parsed,
    )


def run_claw_task(
    workspace_dir: str | os.PathLike[str],
    prompt: str,
    model: str,
    *,
    permission_mode: str = "workspace-write",
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
    skip_permissions: bool = False,
    binary_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TASK_TIMEOUT_SEC,
) -> ClawTaskResult:
    """Run one HER prompt and return the machine-readable result."""
    if permission_mode not in VALID_PERMISSION_MODES:
        raise ValueError(
            f"invalid HER permission_mode {permission_mode!r}; "
            f"expected one of {sorted(VALID_PERMISSION_MODES)}"
        )
    if not str(prompt or "").strip():
        raise ValueError("prompt must not be empty")
    if not str(model or "").strip():
        raise ValueError("model must not be empty")

    args = build_claw_task_args(
        prompt,
        model,
        permission_mode=permission_mode,
        resume=resume,
        allowed_tools=allowed_tools,
        skip_permissions=skip_permissions,
    )

    result = run_claw_json_command(
        args,
        cwd=workspace_dir,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
        stdin_text=None if resume else prompt,
    )
    data = result.json_data
    return ClawTaskResult(
        text=str(data.get("message") or ""),
        model=str(data.get("model") or model),
        permission_mode=permission_mode,
        cwd=result.cwd,
        returncode=result.returncode,
        duration_ms=result.duration_ms,
        stdout=result.stdout,
        stderr=result.stderr,
        json_data=data,
        tool_uses=list(data.get("tool_uses") or []),
        tool_results=list(data.get("tool_results") or []),
        session_id=str(data.get("session_id") or "").strip() or None,
        iterations=data.get("iterations")
        if isinstance(data.get("iterations"), int)
        else None,
        completion_status=str(data.get("completion_status") or "").strip() or None,
        stop_reason=str(data.get("stop_reason") or "").strip() or None,
        provider_stop_reason=str(data.get("provider_stop_reason") or "").strip()
        or None,
        estimated_cost=data.get("estimated_cost")
        if isinstance(data.get("estimated_cost"), str)
        else None,
    )


def build_claw_task_args(
    prompt: str,
    model: str,
    *,
    permission_mode: str = "workspace-write",
    resume: str | None = None,
    allowed_tools: list[str] | None = None,
    skip_permissions: bool = False,
    output_format: str = "json",
) -> list[str]:
    if permission_mode not in VALID_PERMISSION_MODES:
        raise ValueError(
            f"invalid HER permission_mode {permission_mode!r}; "
            f"expected one of {sorted(VALID_PERMISSION_MODES)}"
        )
    if not str(prompt or "").strip():
        raise ValueError("prompt must not be empty")
    if not str(model or "").strip():
        raise ValueError("model must not be empty")
    if output_format not in {"json", "stream-json"}:
        raise ValueError("output_format must be json or stream-json")
    args = [
        "--model",
        model,
        "--permission-mode",
        permission_mode,
        "--output-format",
        output_format,
    ]
    if allowed_tools:
        args.extend(["--allowedTools", ",".join(allowed_tools)])
    if skip_permissions:
        args.append("--dangerously-skip-permissions")
    if resume:
        args.extend(["--resume", resume])
    # Fresh prompts are supplied as the process stdin with no positional
    # command. Current HER recognizes a non-TTY pipe as a one-shot prompt in
    # every permission mode; ``prompt --stdin`` is only a literal prompt in
    # read-only/workspace-write mode. Resumed turns use the CLI's distinct
    # ``--resume SESSION prompt TEXT`` grammar and cannot consume prompt stdin.
    if resume:
        args.extend(["prompt", prompt])
    return args


def run_claw_version(
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    binary_path: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> dict[str, Any]:
    return run_claw_json_command(
        ["version", "--output-format", "json"],
        cwd=cwd,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    ).json_data


def run_claw_doctor(
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    binary_path: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> dict[str, Any]:
    return run_claw_json_command(
        ["doctor", "--output-format", "json"],
        cwd=cwd,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    ).json_data


def run_claw_status(
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    binary_path: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> dict[str, Any]:
    return run_claw_json_command(
        ["status", "--output-format", "json"],
        cwd=cwd,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    ).json_data


def run_claw_state(
    cwd: str | os.PathLike[str],
    env: Mapping[str, str] | None = None,
    *,
    binary_path: str | os.PathLike[str] | None = None,
    timeout_s: float = DEFAULT_CLAW_TIMEOUT_SEC,
) -> dict[str, Any]:
    return run_claw_json_command(
        ["state", "--output-format", "json"],
        cwd=cwd,
        binary_path=binary_path,
        env=env,
        timeout_s=timeout_s,
    ).json_data


class HERAdapter(BaseBackend):
    """HASHI Engine Runtime (HER), derived from the MIT-licensed Claw runtime."""

    DEFAULT_IDLE_TIMEOUT_SEC = 60 * 60
    DEFAULT_HARD_TIMEOUT_SEC = 24 * 60 * 60
    habit_pipeline_owner = "adapter"

    def _define_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            supports_sessions=True,
            supports_files=True,
            supports_tool_use=True,
            supports_thinking_stream=False,
            supports_headless_mode=True,
            supports_progress_stream=True,
            supports_tool_stream=True,
            supports_answer_stream=False,
        )

    def __init__(self, agent_config, global_config, api_key: str = None):
        super().__init__(agent_config, global_config, api_key)
        self.logger = logging.getLogger(f"Backend.HER.{self.config.name}")
        requested_effort = str(self._extra.get("effort") or "high").strip().lower()
        self.effort = (
            requested_effort if requested_effort in HER_EXECUTION_EFFORTS else "high"
        )
        # ``current_proc`` remains a compatibility view for status surfaces.
        # The registry is authoritative because foreground, Dream, Meditation,
        # and other isolated HER executions can overlap within one Agent.
        self.current_proc = None
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._active_process_lock = asyncio.Lock()
        self._active_process_shutdown_lock = asyncio.Lock()
        self._stopping_active_processes = False
        self._binary: Path | None = None
        self._binary_resolution: ClawBinaryResolution | None = None
        self._supports_stream_json = False
        self._session_id: str | None = None
        self._session_mode = not bool(self._extra.get("ephemeral_session"))
        self._session_state_path = (
            self.config.workspace_dir / "backend_state" / "claw_session.json"
        )
        self._persistent_session_lock = asyncio.Lock()
        self._ultra_runs: dict[str, _her_ultra.HERUltraOrchestrator] = {}
        self._gateway_context_path: Path | None = None
        self._gateway_config_home: Path | None = None
        self._habit_store_instance: _her_habits.HERHabitStore | None = None
        self._habit_journal_instance: _her_habits.HERMeditationJournal | None = None
        self._habit_dream_journal_instance: Any | None = None
        # Primary execution never waits for Meditation.  The Meditation lock
        # serializes only the agent's independent snapshot-processing queue;
        # the short store lock protects atomic Habit mutations.
        self._habit_execution_lock = asyncio.Lock()
        self._habit_meditation_execution_lock = asyncio.Lock()
        self._habit_dream_execution_lock = asyncio.Lock()
        self._habit_dream_run_lock = asyncio.Lock()
        self._habit_dream_tasks: set[asyncio.Task] = set()
        self._habit_meditation_tasks: set[asyncio.Task] = set()
        self._habit_meditation_job_ids: set[str] = set()
        self._habit_notification_tasks: set[asyncio.Task] = set()
        self._habit_notification_job_ids: set[str] = set()

    @property
    def persistent_session_busy(self) -> bool:
        """Whether a turn currently owns the mutable persistent HER session."""
        return self._persistent_session_lock.locked()

    def _runtime_request_meta(self, request_id: str) -> dict[str, Any]:
        runtime = getattr(self.config, "_hashi_runtime", None)
        registry = getattr(runtime, "_request_meta_by_id", None)
        if isinstance(registry, Mapping):
            meta = registry.get(str(request_id or ""))
            if isinstance(meta, Mapping):
                return dict(meta)
        current = getattr(runtime, "current_request_meta", None)
        if isinstance(current, Mapping) and str(current.get("request_id") or "") == str(
            request_id or ""
        ):
            return dict(current)
        return {}

    def _request_session_scope(self, request_id: str) -> str:
        raw = (
            str(
                self._runtime_request_meta(request_id).get("session_scope")
                or HER_SESSION_SCOPE_PERSISTENT
            )
            .strip()
            .lower()
        )
        if raw in {HER_SESSION_SCOPE_ISOLATED, HER_SESSION_SCOPE_ISOLATED_RESUME}:
            return raw
        return HER_SESSION_SCOPE_PERSISTENT

    def _request_resume_session(self, request_id: str) -> str | None:
        value = self._runtime_request_meta(request_id).get("resume_session_id")
        session_id = str(value or "").strip()
        return session_id or None

    def _load_session_identity(self) -> None:
        if self._ephemeral_session() or not self._session_mode:
            return
        try:
            payload = json.loads(self._session_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            self.logger.warning("Ignoring invalid HER session checkpoint: %s", exc)
            return
        session_id = str(payload.get("session_id") or "").strip()
        model = str(payload.get("model") or "").strip()
        if session_id and (not model or model == self._claw_model()):
            self._session_id = session_id
            self.logger.info("Restored HER session checkpoint: session=%s", session_id)
        elif session_id:
            self.logger.info(
                "Ignoring HER session checkpoint for different model: checkpoint=%s current=%s",
                model or "unknown",
                self._claw_model(),
            )

    def _persist_session_identity(self) -> None:
        if self._ephemeral_session():
            return
        if not self._session_mode:
            self._session_id = None
            self._session_state_path.unlink(missing_ok=True)
            return
        if not self._session_id:
            self._session_state_path.unlink(missing_ok=True)
            return
        self._session_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": self._session_id,
            "model": self._claw_model(),
            "updated_at": time.time(),
        }
        temporary = self._session_state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, self._session_state_path)
        self._session_state_path.chmod(0o600)

    @staticmethod
    def _bounded_diagnostic_text(
        value: Any, limit: int = HER_DIAGNOSTIC_MAX_CHARS
    ) -> str:
        text = redact_secret_text(str(value or ""))
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "…"

    @classmethod
    def _sanitize_diagnostic_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                str(key): cls._sanitize_diagnostic_value(nested)
                for key, nested in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_diagnostic_value(item) for item in value]
        if isinstance(value, str):
            return cls._bounded_diagnostic_text(value)
        return value

    def _persist_diagnostic_record(self, record: Mapping[str, Any]) -> None:
        """Append one request-correlated, locally redacted HER diagnostic."""
        try:
            state_dir = self.config.workspace_dir / "backend_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            path = state_dir / "her_diagnostics.jsonl"
            payload = self._sanitize_diagnostic_value(
                {
                    "timestamp": time.time(),
                    "agent": self.config.name,
                    **dict(record),
                }
            )
            with path.open("a", encoding="utf-8") as diagnostics:
                diagnostics.write(
                    json.dumps(payload, ensure_ascii=False, default=str) + "\n"
                )
            path.chmod(0o600)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the backend failure
            self.logger.warning(
                "HER diagnostic persistence failed safely: error=%s",
                type(exc).__name__,
            )

    def _persist_command_failure(
        self,
        *,
        request_id: str,
        returncode: int,
        parsed_error: Mapping[str, Any] | None,
        stderr: str,
        resume: str | None,
    ) -> None:
        self._persist_diagnostic_record(
            {
                "kind": "command_failure",
                "request_id": request_id,
                "returncode": returncode,
                "resumed_session": bool(resume),
                "session_id": resume,
                "parsed_error": dict(parsed_error or {}),
                "stderr": self._bounded_diagnostic_text(stderr),
            }
        )

    def _quarantine_persistent_session(
        self, request_id: str, exc: BaseException
    ) -> None:
        previous_session = self._session_id
        self._session_id = None
        try:
            self._persist_session_identity()
        except Exception as persist_exc:  # noqa: BLE001 - preserve the original turn failure
            self.logger.warning(
                "HER session quarantine checkpoint cleanup failed safely: request=%s error=%s",
                request_id,
                type(persist_exc).__name__,
            )
        parsed_error = getattr(exc, "parsed_error", None)
        parsed_error = parsed_error if isinstance(parsed_error, Mapping) else {}
        self._persist_diagnostic_record(
            {
                "kind": "session_quarantined",
                "request_id": request_id,
                "session_id": previous_session,
                "error_kind": parsed_error.get("error_kind")
                or parsed_error.get("type")
                or type(exc).__name__,
                "reason": "persistent_turn_failed_before_safe_completion",
            }
        )
        self.logger.warning(
            "HER persistent session quarantined: request=%s session=%s error=%s",
            request_id,
            previous_session or "unavailable",
            type(exc).__name__,
        )

    @staticmethod
    def _command_error_metadata(exc: ClawCommandError) -> dict[str, Any]:
        parsed = exc.parsed_error if isinstance(exc.parsed_error, Mapping) else {}
        allowed = {
            "kind",
            "error_kind",
            "http_status",
            "error_type",
            "provider_request_id",
            "error_message",
            "body_snippet",
            "retryable",
            "last_safe_event",
            "checkpoint_preserved",
        }
        return {key: parsed.get(key) for key in allowed if key in parsed}

    @property
    def _extra(self) -> dict[str, Any]:
        extra = getattr(self.config, "extra", None) or {}
        return dict(extra) if isinstance(extra, Mapping) else {}

    def _habit_meditation_config(self) -> _her_habits.HabitMeditationConfig:
        return _her_habits.HabitMeditationConfig.resolve(
            self.global_config,
            self._extra,
        )

    def _habit_request_eligible(self, request_id: str) -> bool:
        """Honor request-scoped runtime eligibility without runtime coupling."""
        if (
            self._ephemeral_session()
            or self._extra.get("habit_learning_eligible") is False
        ):
            return False
        meta = self._runtime_request_meta(request_id)
        if not meta:
            return True
        if "habit_learning_eligible" not in meta:
            return True
        return bool(meta.get("habit_learning_eligible"))

    def _her_habit_store(self) -> _her_habits.HERHabitStore:
        if self._habit_store_instance is None:
            self._habit_store_instance = _her_habits.HERHabitStore(
                self.config.workspace_dir,
                logger=self.logger,
            )
        return self._habit_store_instance

    def _her_meditation_journal(self) -> _her_habits.HERMeditationJournal:
        if self._habit_journal_instance is None:
            self._habit_journal_instance = _her_habits.HERMeditationJournal(
                self.config.workspace_dir,
                logger=self.logger,
            )
        return self._habit_journal_instance

    def _her_dream_journal(self):
        if self._habit_dream_journal_instance is None:
            from adapters.her_dream import HERDreamJournal

            self._habit_dream_journal_instance = HERDreamJournal(
                self.config.workspace_dir,
                logger=self.logger,
            )
        return self._habit_dream_journal_instance

    def _her_persona_source(self) -> _her_persona.HERPersonaSource:
        """Load only this Agent's resolved configured system_md Persona."""

        return _her_persona.load_configured_persona(
            getattr(self.config, "system_md", None)
        )

    def _persist_persona_audit(self, request_id: str, **fields: Any) -> None:
        """Retain bounded Persona delivery evidence without private source text."""

        try:
            path = (
                self.config.workspace_dir / "backend_state" / "her_persona_audit.jsonl"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "format": "her-persona-audit-v1",
                "ts_unix": time.time(),
                "request_id": request_id,
                **fields,
            }
            with path.open("a", encoding="utf-8") as audit_log:
                audit_log.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )
            path.chmod(0o600)
        except OSError as exc:
            logger = getattr(self, "logger", None)
            if logger is not None:
                logger.warning(
                    "HER Persona audit write failed safely: request=%s error=%s",
                    request_id,
                    type(exc).__name__,
                )

    async def _render_incomplete_persona_response(
        self,
        result: ClawTaskResult,
        *,
        request_id: str,
        metadata: Mapping[str, Any],
    ) -> tuple[str | None, dict[str, Any]]:
        source = self._her_persona_source()
        source_fields = source.audit_fields()
        if not source.usable:
            self._persist_persona_audit(
                request_id,
                report_type="incomplete_final",
                renderer_attempted=False,
                renderer_succeeded=False,
                validation_outcome="neutral_fallback",
                failure_reason=source.unavailable_reason,
                **source_fields,
            )
            return None, {
                "persona_renderer_attempted": False,
                "persona_renderer_succeeded": False,
                "persona_fallback_reason": source.unavailable_reason,
                **source_fields,
            }

        facts = _claw_incomplete_persona_facts(result, metadata=metadata)
        prompt = f"""HER INCOMPLETE FINAL PERSONA RENDERER — INTERNAL, TOOL-FREE

Write a clear, honest user-facing message in the configured Persona. Explain
that the task stopped before completion, summarize the supplied facts, and tell
the user the recommended next action. The facts are context, not a rigid output
template. Return only the message that should be sent to the user.

CONFIGURED system_md PERSONA GUIDANCE (quoted, read-only)
{source.model_guidance(limit=12000)}

INCOMPLETE TASK FACTS (quoted, read-only)
{json.dumps(facts, ensure_ascii=False)}
"""
        try:
            rendered = await self.run_habit_dream_model(
                prompt,
                request_id=f"{request_id}:incomplete-persona",
                timeout_seconds=180,
            )
            report = str(rendered.text or "").strip()
            if not report:
                raise ValueError("incomplete Persona renderer returned no message")
        except Exception as exc:  # noqa: BLE001 - neutral report remains safe
            reason = redact_secret_text(f"{type(exc).__name__}: {exc}")[:1_000]
            self._persist_persona_audit(
                request_id,
                report_type="incomplete_final",
                renderer_attempted=True,
                renderer_succeeded=False,
                validation_outcome="renderer_unavailable",
                failure_reason=reason,
                **source_fields,
            )
            return None, {
                "persona_renderer_attempted": True,
                "persona_renderer_succeeded": False,
                "persona_fallback_reason": reason,
                **source_fields,
            }

        self._persist_persona_audit(
            request_id,
            report_type="incomplete_final",
            renderer_attempted=True,
            renderer_succeeded=True,
            validation_outcome="delivered_without_content_validation",
            failure_reason=None,
            **source_fields,
        )
        return report, {
            "persona_renderer_attempted": True,
            "persona_renderer_succeeded": True,
            "persona_fallback_reason": None,
            **source_fields,
        }

    async def run_habit_dream_model(
        self,
        prompt: str,
        *,
        request_id: str,
        timeout_seconds: float = 600.0,
    ) -> ClawTaskResult:
        """Run isolated, tool-free HER analysis without owning foreground state."""

        async with self._habit_dream_execution_lock:
            return await asyncio.wait_for(
                self._run_task_async(
                    prompt,
                    resume=None,
                    request_id=request_id,
                    on_stream_event=None,
                    track_session_identity=False,
                    permission_mode_override="read-only",
                    allowed_tools_override=[],
                    task_env_overrides={
                        "CLAW_TASK_PLANNING": "0",
                        "CLAW_MAX_TOOL_ITERATIONS": "8",
                        "CLAW_EXECUTION_EFFORT": "low",
                    },
                ),
                timeout=max(30.0, float(timeout_seconds)),
            )

    def _habit_notification_context(
        self,
        request_id: str,
        *,
        silent: bool,
    ) -> dict[str, Any]:
        meta = self._runtime_request_meta(request_id)
        return {
            "chat_id": meta.get("chat_id"),
            "verbose_at_start": bool(meta.get("verbose_at_start")),
            "silent": bool(meta.get("silent", silent)),
            "deliver_to_telegram": bool(meta.get("deliver_to_telegram")),
            "request_source": meta.get("source"),
            "request_summary": meta.get("summary"),
        }

    def _spawn_habit_meditation_job(
        self,
        job_id: str,
        *,
        config: _her_habits.HabitMeditationConfig,
    ) -> bool:
        if job_id in self._habit_meditation_job_ids:
            return False
        task = asyncio.create_task(
            self._run_habit_meditation(job_id=job_id, config=config),
            name=f"her-habit-meditation:{self.config.name}:{job_id}",
        )
        self._habit_meditation_job_ids.add(job_id)
        self._habit_meditation_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._habit_meditation_tasks.discard(done_task)
            self._habit_meditation_job_ids.discard(job_id)
            if done_task.cancelled():
                return
            try:
                error = done_task.exception()
            except Exception:  # noqa: BLE001 - callback must never escape
                error = None
            if error is not None:
                self.logger.warning(
                    "Unhandled HER Habit Meditation task error: job=%s error=%s",
                    job_id,
                    type(error).__name__,
                )

        task.add_done_callback(_done)
        return True

    def _resume_pending_habit_meditations(self) -> int:
        config = self._habit_meditation_config()
        if not config.enabled:
            return 0
        try:
            journal = self._her_meditation_journal()
            recovered = journal.recover_interrupted_jobs()
            spawned = sum(
                self._spawn_habit_meditation_job(job["job_id"], config=config)
                for job in journal.pending_jobs(limit=16)
            )
            if recovered or spawned:
                self.logger.info(
                    "HER Habit Meditation recovery: recovered=%d spawned=%d",
                    recovered,
                    spawned,
                )
            return spawned
        except Exception as exc:  # noqa: BLE001 - recovery must not disable HER
            self.logger.warning(
                "HER Habit Meditation recovery failed safely: error=%s",
                type(exc).__name__,
            )
            return 0

    def _spawn_habit_notification_job(self, job_id: str) -> bool:
        runtime = getattr(self.config, "_hashi_runtime", None)
        if runtime is None or not bool(getattr(runtime, "telegram_connected", False)):
            return False
        if job_id in self._habit_notification_job_ids:
            return False
        task = asyncio.create_task(
            self._run_habit_notification(job_id),
            name=f"her-habit-notification:{self.config.name}:{job_id}",
        )
        self._habit_notification_job_ids.add(job_id)
        self._habit_notification_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._habit_notification_tasks.discard(done_task)
            self._habit_notification_job_ids.discard(job_id)
            if done_task.cancelled():
                return
            try:
                error = done_task.exception()
            except Exception:  # noqa: BLE001 - callback must never escape
                error = None
            if error is not None:
                self.logger.warning(
                    "Unhandled HER Habit notification task error: job=%s error=%s",
                    job_id,
                    type(error).__name__,
                )

        task.add_done_callback(_done)
        return True

    def _resume_pending_habit_notifications(self) -> int:
        try:
            return sum(
                self._spawn_habit_notification_job(job["job_id"])
                for job in self._her_meditation_journal().pending_notifications(
                    limit=32
                )
            )
        except Exception as exc:  # noqa: BLE001 - notification recovery is fail-open
            self.logger.warning(
                "HER Habit notification recovery failed safely: error=%s",
                type(exc).__name__,
            )
            return 0

    async def _run_habit_notification(self, job_id: str) -> None:
        journal = self._her_meditation_journal()
        while True:
            job = journal.claim_notification(job_id)
            if job is None:
                return
            runtime = getattr(self.config, "_hashi_runtime", None)
            sender = getattr(runtime, "_deliver_her_habit_notification", None)
            try:
                if not callable(sender):
                    raise RuntimeError("runtime notification sender is unavailable")
                delivered = await sender(job)
                if delivered is None:
                    journal.mark_notification_deferred(
                        job_id,
                        reason="Telegram delivery is temporarily unavailable",
                    )
                    current = journal.get(job_id) or {}
                    _her_habits.append_habit_audit(
                        self.config.workspace_dir,
                        "habit_notification_deferred",
                        agent_id=self.config.name,
                        job_id=job_id,
                        request_id=job.get("request_id"),
                        changes=job.get("changes") or [],
                        notification=current.get("notification") or {},
                    )
                    self.logger.info("HER Habit notification deferred: job=%s", job_id)
                    return
                if delivered is not True:
                    raise RuntimeError("Telegram did not accept the Habit notification")
                journal.mark_notification_sent(job_id)
                current = journal.get(job_id) or {}
                _her_habits.append_habit_audit(
                    self.config.workspace_dir,
                    "habit_notification_sent",
                    agent_id=self.config.name,
                    job_id=job_id,
                    request_id=job.get("request_id"),
                    changes=job.get("changes") or [],
                    notification=current.get("notification") or {},
                )
                self.logger.info("HER Habit notification delivered: job=%s", job_id)
                return
            except asyncio.CancelledError:
                try:
                    journal.mark_notification_retry(job_id, reason="runtime_shutdown")
                except Exception:  # noqa: BLE001,S110 - cancellation must continue
                    pass
                raise
            except Exception as exc:  # noqa: BLE001 - notification failure cannot break HER
                try:
                    journal.mark_notification_retry(
                        job_id,
                        reason=f"{type(exc).__name__}: {exc}",
                    )
                    current = journal.get(job_id) or {}
                    notification = current.get("notification") or {}
                    _her_habits.append_habit_audit(
                        self.config.workspace_dir,
                        "habit_notification_failed",
                        agent_id=self.config.name,
                        job_id=job_id,
                        request_id=job.get("request_id"),
                        changes=job.get("changes") or [],
                        notification=notification,
                    )
                except Exception:  # noqa: BLE001,S110 - journal/audit stays fail-open
                    return
                self.logger.warning(
                    "HER Habit notification failed safely: job=%s attempt=%s error=%s",
                    job_id,
                    notification.get("attempts"),
                    type(exc).__name__,
                )
                if notification.get("status") != "pending":
                    return
                await asyncio.sleep(
                    min(10.0, 2.0 ** int(notification.get("attempts") or 1))
                )

    def _schedule_habit_meditation(
        self,
        *,
        job_id: str,
        request_id: str,
        task_prompt: str,
        task_result: ClawTaskResult,
        config: _her_habits.HabitMeditationConfig,
        notification_context: Mapping[str, Any] | None = None,
    ) -> bool:
        try:
            store = self._her_habit_store()
            meditation_prompt = _her_habits.build_meditation_prompt(
                agent_name=self.config.name,
                task_prompt=task_prompt,
                result=task_result,
                habits=store.load(),
                config=config,
            )
            job_id, queued = self._her_meditation_journal().enqueue(
                job_id=job_id,
                request_id=request_id,
                prompt=meditation_prompt,
                max_actions=config.max_actions,
                notification_context=notification_context,
            )
            journaled = self._her_meditation_journal().get(job_id)
            spawned = bool(
                journaled
                and journaled.get("status") in {"pending", "applying"}
                and self._spawn_habit_meditation_job(job_id, config=config)
            )
            self.logger.info(
                "HER Habit Meditation %s: request=%s job=%s",
                "queued" if queued else "already-journaled",
                request_id,
                job_id,
            )
            return queued or spawned
        except Exception as exc:  # noqa: BLE001 - queue failure must not break the turn
            self.logger.warning(
                "HER Habit Meditation scheduling failed safely: request=%s error=%s",
                request_id,
                type(exc).__name__,
            )
            return False

    async def _run_habit_meditation(
        self,
        *,
        job_id: str,
        config: _her_habits.HabitMeditationConfig,
    ) -> None:
        journal = self._her_meditation_journal()
        request_id = job_id
        completed = False
        try:
            async with self._habit_meditation_execution_lock:
                # Re-resolve the switch immediately before work so an operational
                # off override can drain already-queued Meditation tasks safely.
                if not self._habit_meditation_config().enabled:
                    self.logger.info(
                        "HER Habit Meditation skipped after disable: request=%s",
                        request_id,
                    )
                    return
                phase = journal.claim(job_id)
                if phase is None:
                    return
                job = journal.get(job_id)
                if job is None:
                    return
                if phase == "meditate":
                    meditation_prompt = str(job.get("prompt") or "")
                    actions = None
                    for validation_attempt in (1, 2):
                        meditation_result = await asyncio.wait_for(
                            self._run_task_async(
                                meditation_prompt,
                                resume=None,
                                request_id=f"{job_id}:habit-meditation",
                                on_stream_event=None,
                                track_session_identity=False,
                                permission_mode_override="read-only",
                                allowed_tools_override=[],
                                task_env_overrides={
                                    "CLAW_TASK_PLANNING": "0",
                                    "CLAW_MAX_TOOL_ITERATIONS": "8",
                                    "CLAW_EXECUTION_EFFORT": "low",
                                },
                            ),
                            timeout=config.meditation_timeout_seconds,
                        )
                        try:
                            actions = _her_habits.parse_meditation_actions(
                                meditation_result.text,
                                max_actions=int(
                                    job.get("max_actions") or config.max_actions
                                ),
                            )
                            break
                        except _her_habits.MeditationValidationError as exc:
                            if validation_attempt == 2:
                                raise
                            self.logger.info(
                                "HER Habit Meditation correcting invalid output once: "
                                "job=%s error=%s",
                                job_id,
                                exc,
                            )
                            meditation_prompt = (
                                _her_habits.build_meditation_correction_prompt(
                                    rejected_output=meditation_result.text,
                                    error=exc,
                                )
                            )
                    assert actions is not None
                    async with self._habit_execution_lock:
                        action_baseline = (
                            self._her_habit_store().capture_action_baseline(
                                actions,
                                max_actions=int(
                                    job.get("max_actions") or config.max_actions
                                ),
                                idempotency_key=job_id,
                            )
                        )
                        job = journal.store_actions(
                            job_id,
                            actions,
                            action_baseline=action_baseline,
                        )
                actions = job.get("actions")
                if not isinstance(actions, list):
                    raise _her_habits.MeditationValidationError(
                        "durable Meditation actions are missing"
                    )
                async with self._habit_execution_lock:
                    outcomes, changes = (
                        self._her_habit_store().apply_actions_with_changes(
                            actions,
                            max_actions=int(
                                job.get("max_actions") or config.max_actions
                            ),
                            idempotency_key=job_id,
                            audit_context={
                                "source": "meditation",
                                "job_id": job_id,
                                "request_id": job.get("request_id"),
                                "notification": job.get("notification"),
                            },
                            action_baseline=job.get("action_baseline"),
                        )
                    )
                    journal.mark_complete(
                        job_id,
                        outcomes,
                        changes=[change.to_payload() for change in changes],
                    )
                completed = True
                self.logger.info(
                    "HER Habit Meditation completed: job=%s actions=%d changes=%d outcomes=%s",
                    job_id,
                    len(actions),
                    len(changes),
                    ",".join(outcomes) or "no-change",
                )
            if completed:
                self._spawn_habit_notification_job(job_id)
        except asyncio.CancelledError:
            try:
                journal.mark_pending(job_id, reason="runtime_shutdown")
            except Exception:  # noqa: BLE001,S110 - cancellation must continue
                pass
            self.logger.info("HER Habit Meditation cancelled: job=%s", job_id)
            raise
        except _her_habits.MeditationValidationError as exc:
            try:
                journal.mark_failed(
                    job_id,
                    error_code="invalid_output",
                    error_summary=str(exc),
                )
            except Exception:  # noqa: BLE001,S110 - fail-open journal handling
                pass
            self.logger.warning(
                "HER Habit Meditation rejected invalid output: job=%s error=%s",
                job_id,
                exc,
            )
        except (ClawError, asyncio.TimeoutError) as exc:
            try:
                journal.mark_pending(job_id, reason=type(exc).__name__)
            except Exception:  # noqa: BLE001,S110 - fail-open journal handling
                pass
            self.logger.warning(
                "HER Habit Meditation deferred for retry: job=%s error=%s",
                job_id,
                type(exc).__name__,
            )
        except OSError as exc:
            # If actions were already made durable, leave the job in applying
            # so restart recovery replays those same actions without another
            # model call. Otherwise return it to the bounded retry queue.
            try:
                journal.mark_pending(job_id, reason=type(exc).__name__)
            except Exception:  # noqa: BLE001,S110 - fail-open journal handling
                pass
            self.logger.warning(
                "HER Habit Meditation write deferred safely: job=%s error=%s",
                job_id,
                type(exc).__name__,
            )
        except Exception as exc:  # noqa: BLE001 - Meditation never breaks the turn
            try:
                journal.mark_failed(
                    job_id,
                    error_code="runtime_error",
                    error_summary=type(exc).__name__,
                )
            except Exception:  # noqa: BLE001,S110 - fail-open journal handling
                pass
            self.logger.warning(
                "HER Habit Meditation failed safely: job=%s error=%s",
                job_id,
                type(exc).__name__,
            )

    def _record_failed_turn_meditation_skip(
        self,
        *,
        request_id: str,
        exc: BaseException,
    ) -> None:
        """Keep ungrounded foreground failures out of the Habit learner."""

        parsed_error = getattr(exc, "parsed_error", None)
        parsed_error = parsed_error if isinstance(parsed_error, Mapping) else {}
        raw_kind = (
            parsed_error.get("error_kind")
            or parsed_error.get("type")
            or parsed_error.get("kind")
        )
        error_kind = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(raw_kind or "unknown"))[:80]
        terminal_kind = re.sub(
            r"[^a-zA-Z0-9_.-]+",
            "_",
            str(parsed_error.get("kind") or "unknown"),
        )[:80]
        reason = "foreground_error_without_grounded_task_result"
        self.logger.info(
            "HER Habit Meditation skipped: request=%s reason=%s exception=%s error_kind=%s",
            request_id,
            reason,
            type(exc).__name__,
            error_kind,
        )
        try:
            _her_habits.append_habit_audit(
                self.config.workspace_dir,
                "habit_meditation_skipped",
                agent_id=self.config.name,
                request_id=request_id,
                reason=reason,
                exception_type=type(exc).__name__,
                error_kind=error_kind,
                terminal_kind=terminal_kind,
                returncode=getattr(exc, "returncode", None),
                grounded_task_result=False,
            )
        except Exception as audit_exc:  # noqa: BLE001 - audit must not mask failure
            self.logger.warning(
                "HER Habit Meditation skip audit failed safely: request=%s error=%s",
                request_id,
                type(audit_exc).__name__,
            )

    def _allowed_tools(self) -> list[str] | None:
        """Return an explicit Claw-native tool restriction, if configured.

        HASHI's default is unfiltered: omitting ``allowed_tools`` (or using
        ``*``) omits ``--allowedTools`` so HER exposes every native tool
        supported by the selected runtime. Filesystem and mutation authority
        remain controlled independently by ``permission_mode``.
        """
        raw = self._extra.get("allowed_tools")
        if raw is None:
            return None
        if isinstance(raw, str):
            parsed = [item.strip() for item in raw.split(",") if item.strip()]
            return None if "*" in parsed else parsed
        if isinstance(raw, list):
            parsed = [str(item).strip() for item in raw if str(item).strip()]
            return None if "*" in parsed else parsed
        return None

    def _ephemeral_session(self) -> bool:
        """Keep an internal one-shot run isolated from the user's HER state."""
        return bool(self._extra.get("ephemeral_session"))

    def _global_claw_config(self) -> dict[str, Any]:
        raw = (
            getattr(self.global_config, "her_providers", None)
            or getattr(self.global_config, "claw_providers", None)
            or {}
        )
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _provider_configs(self) -> dict[str, Any]:
        providers = self._global_claw_config().get("providers") or {}
        return dict(providers) if isinstance(providers, Mapping) else {}

    def _provider_and_model(self) -> tuple[str | None, str]:
        model = str(self.config.model or "").strip()
        provider = self._extra.get("provider")
        if provider:
            provider_name = str(provider).strip()
            prefix = f"{provider_name}:"
            if model.startswith(prefix) and len(model) > len(prefix):
                model = model[len(prefix) :]
            return provider_name, model
        if ":" in model:
            maybe_provider, maybe_model = model.split(":", 1)
            if maybe_provider in self._provider_configs() and maybe_model:
                return maybe_provider, maybe_model
        return None, model

    def _claw_model(self) -> str:
        provider_name, model = self._provider_and_model()
        if not provider_name or "/" in model or ":" in model:
            return model

        provider = self._provider_configs().get(provider_name)
        provider = provider if isinstance(provider, Mapping) else {}
        configured_prefix = provider.get("claw_model_prefix")
        if configured_prefix is not None:
            prefix = str(configured_prefix).strip().rstrip("/")
            return f"{prefix}/{model}" if prefix else model

        # The certified HER runtime uses the local/ route for named
        # OpenAI-compatible providers and strips that routing prefix before
        # forwarding the provider-native model ID upstream. OAuth-backed
        # routes retain their native model form.
        auth_mode = self._provider_auth_mode(provider)
        if auth_mode not in {"hashi_oauth", "hashi-xai-oauth", "xai_oauth"}:
            return f"local/{model}"
        return model

    def _permission_mode(self) -> str:
        requested = str(self._extra.get("permission_mode") or "workspace-write")
        if requested not in VALID_PERMISSION_MODES:
            return requested
        max_mode = str(
            self._global_claw_config().get("max_permission_mode") or ""
        ).strip()
        if (
            max_mode in VALID_PERMISSION_MODES
            and PERMISSION_MODE_RANK[requested] > PERMISSION_MODE_RANK[max_mode]
        ):
            self.logger.warning(
                "HER permission_mode %s exceeds global max_permission_mode %s; using %s.",
                requested,
                max_mode,
                max_mode,
            )
            return max_mode
        return requested

    def _skip_permissions(self) -> bool:
        return bool(
            self._extra.get("skip_permissions")
            or self._extra.get("dangerously_skip_permissions")
        )

    def _legacy_openai_base_url(self) -> str:
        return str(self._extra.get("openai_base_url") or DEFAULT_OPENROUTER_BASE_URL)

    def _hashi_secrets(self) -> dict[str, Any]:
        raw = getattr(self.config, "_hashi_secrets", None)
        if isinstance(raw, Mapping):
            return dict(raw)
        raw = getattr(self.global_config, "secrets", None)
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _env_from_agent_extra(self) -> dict[str, str]:
        env_source = dict(os.environ)
        if self.api_key:
            env_source["OPENAI_API_KEY"] = str(self.api_key)
        if self._legacy_openai_base_url():
            env_source["OPENAI_BASE_URL"] = self._legacy_openai_base_url()
        return build_claw_env(env_source)

    def _provider_auth_mode(self, provider: Mapping[str, Any]) -> str:
        return str(provider.get("auth_mode") or "").strip().lower()

    def _env_from_hashi_xai_oauth(
        self, provider_name: str, provider: Mapping[str, Any]
    ) -> dict[str, str]:
        """Inject HASHI-native xAI OAuth access token into HER (no Hermes, no grok-cli)."""
        from adapters.hashi_xai_oauth import (
            HashiXaiOAuthError,
            resolve_base_url,
            resolve_hashi_xai_credentials,
        )

        try:
            creds = resolve_hashi_xai_credentials(
                global_config=self.global_config,
                provider_cfg=provider,
                force_refresh=False,
            )
        except HashiXaiOAuthError as exc:
            raise ClawProviderConfigError(
                f"HER provider {provider_name} HASHI xAI OAuth unavailable: {exc}"
            ) from exc

        env_api_key = (
            str(provider.get("env_api_key") or "XAI_API_KEY").strip() or "XAI_API_KEY"
        )
        env_base_url = (
            str(provider.get("env_base_url") or "XAI_BASE_URL").strip()
            or "XAI_BASE_URL"
        )
        base_url = str(
            provider.get("base_url")
            or creds.base_url
            or resolve_base_url(self.global_config)
        ).strip()

        env_source = dict(os.environ)
        env_source[env_api_key] = creds.access_token
        if base_url:
            env_source[env_base_url] = base_url
            # Some OpenAI-compat paths also honor OPENAI_*; keep XAI_* primary for HER xAI routing.
            if env_api_key == "OPENAI_API_KEY":
                env_source["OPENAI_BASE_URL"] = base_url
        self.logger.info(
            "HER provider %s using HASHI xAI OAuth (source=%s).",
            provider_name,
            creds.source,
        )
        return build_claw_env(env_source)

    def _env_from_provider(self, provider_name: str) -> dict[str, str]:
        providers = self._provider_configs()
        provider = providers.get(provider_name)
        if not isinstance(provider, Mapping):
            raise ClawProviderConfigError(
                f"HER provider is not configured: {provider_name}"
            )

        status = str(provider.get("status") or "stable").strip().lower()
        if status == "disabled":
            raise ClawProviderConfigError(f"HER provider is disabled: {provider_name}")
        if status == "provisional":
            self.logger.warning(
                "HER provider %s is provisional; running with warning diagnostics.",
                provider_name,
            )

        auth_mode = self._provider_auth_mode(provider)
        if auth_mode in {"hashi_oauth", "hashi-xai-oauth", "xai_oauth"}:
            return self._env_from_hashi_xai_oauth(provider_name, provider)

        base_url = str(provider.get("base_url") or "").strip()
        if not base_url:
            raise ClawProviderConfigError(
                f"HER provider {provider_name} has no base_url"
            )

        secret_name = provider.get("secret")
        api_key = None
        if secret_name:
            secrets = self._hashi_secrets()
            api_key = secrets.get(str(secret_name))
            if not api_key:
                raise ClawProviderSecretMissing(
                    f"HER provider {provider_name} requires missing secret: {secret_name}"
                )
        else:
            api_key = provider.get("dummy_api_key")

        env_source = dict(os.environ)
        env_source["OPENAI_BASE_URL"] = base_url
        if api_key:
            env_source["OPENAI_API_KEY"] = str(api_key)
        return build_claw_env(env_source)

    def _resolve_task_env(self) -> dict[str, str]:
        provider_name, _ = self._provider_and_model()
        if provider_name:
            if self._extra.get("openai_base_url"):
                self.logger.warning(
                    "HER provider=%s overrides legacy openai_base_url for %s.",
                    provider_name,
                    self.config.name,
                )
            return self._env_from_provider(provider_name)
        return self._env_from_agent_extra()

    def _single_agent_effort(self) -> str:
        """Return the inner Claw effort; ``ultra`` never reaches the CLI."""

        if self.effort != _her_ultra.HER_ULTRA_EFFORT:
            return self.effort
        raw = self._extra.get("ultra")
        raw = raw if isinstance(raw, Mapping) else {}
        requested = str(raw.get("primary_inner_effort") or "high").strip().lower()
        if requested not in CLAW_EXECUTION_EFFORT_ITERATIONS:
            self.logger.warning(
                "Ignoring invalid HER Ultra primary_inner_effort=%r; using high.",
                requested,
            )
            return "high"
        return requested

    def _max_tool_iterations(self) -> int:
        inner_effort = self._single_agent_effort()
        raw_max_iterations = self._extra.get(
            "max_tool_iterations",
            CLAW_EXECUTION_EFFORT_ITERATIONS.get(inner_effort, 96),
        )
        try:
            max_iterations = int(raw_max_iterations)
        except (TypeError, ValueError):
            self.logger.warning(
                "Ignoring invalid HER max_tool_iterations=%r; using 96.",
                raw_max_iterations,
            )
            max_iterations = 96
        return min(512, max(8, max_iterations))

    def _task_env(self) -> dict[str, str]:
        inner_effort = self._single_agent_effort()
        env = self._resolve_task_env()
        env["CLAW_MAX_TOOL_ITERATIONS"] = str(self._max_tool_iterations())
        env["CLAW_TASK_PLANNING"] = "0" if inner_effort == "low" else "1"
        env["CLAW_EXECUTION_EFFORT"] = inner_effort
        if self._gateway_config_home is not None:
            env["CLAW_CONFIG_HOME"] = str(self._gateway_config_home)
            project_root = Path(__file__).resolve().parents[1]
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = os.pathsep.join(
                [str(project_root), existing] if existing else [str(project_root)]
            )
        return env

    def _prepare_tool_gateway(self) -> None:
        registry = getattr(self, "tool_registry", None)
        if registry is None:
            self.logger.warning(
                "HER initialized without HASHI ToolRegistry; only Claw-native tools will be available."
            )
            return
        from tools.gateway.context import (
            live_workbench_api_base_url,
            write_gateway_context,
        )

        state_dir = self.config.workspace_dir / "backend_state"
        context_path = state_dir / "her_gateway_context.json"
        config_home = state_dir / "her_config"
        config_home.mkdir(parents=True, exist_ok=True)
        media_roots = []
        base_media_dir = getattr(self.global_config, "base_media_dir", None)
        if base_media_dir is not None:
            media_roots.append(Path(base_media_dir) / self.config.name)
        context = write_gateway_context(
            registry,
            context_path,
            additional_allowed_tools={
                "media_read",
                "hashi_scheduler_list",
                "hashi_scheduler_status",
                "hashi_scheduler_run_history",
                "hashi_scheduler_rerun",
            },
            media_roots=media_roots,
            workbench_api_base_url=live_workbench_api_base_url(
                registry,
                self.global_config,
            ),
        )
        settings = {
            "mcpServers": {
                "hashi-tools": {
                    "command": sys.executable,
                    "args": [
                        "-m",
                        "tools.gateway.mcp_stdio",
                        "--context",
                        str(context_path),
                    ],
                    "env": {"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                    "toolCallTimeoutMs": 120_000,
                    "required": True,
                }
            },
            # Claw's legacy native WebSearch backend is not configured in
            # HASHI.  Keep it out of the model-visible tool surface so HER
            # agents consistently use the working HASHI/Brave MCP search.
            "permissions": {"deniedTools": ["WebSearch"]},
        }
        settings_path = config_home / "settings.json"
        fd, temporary = tempfile.mkstemp(
            prefix=".settings.", suffix=".json", dir=config_home
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    settings, handle, ensure_ascii=False, indent=2, sort_keys=True
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, settings_path)
            settings_path.chmod(0o600)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(temporary).unlink(missing_ok=True)
            raise
        self._gateway_context_path = context_path
        self._gateway_config_home = config_home
        self.logger.info(
            "HER HASHI Tool Gateway prepared: agent=%s tools=%d context=%s",
            context.agent,
            len(context.allowed_tools),
            context_path,
        )

    def _validate_tool_gateway(self) -> None:
        if self._gateway_context_path is None or self._gateway_config_home is None:
            return
        from tools.gateway.context import load_gateway_context

        context = load_gateway_context(self._gateway_context_path)
        definitions = context.build_registry().get_tool_definitions()
        if not definitions:
            raise ClawProviderConfigError(
                "HER HASHI Tool Gateway has no permitted tools"
            )
        status = run_claw_json_command(
            ["mcp", "list", "--output-format", "json"],
            cwd=self.effective_workdir,
            binary_path=self._binary,
            env=self._task_env(),
            timeout_s=30,
        ).json_data
        server = next(
            (
                item
                for item in status.get("servers", [])
                if item.get("name") == "hashi-tools"
            ),
            None,
        )
        # HER <= hashi.20 reported per-server validity, while hashi.21's
        # successful list contract reports only the top-level status and the
        # configured server entries.  Accept either success shape, but keep
        # malformed or explicitly invalid configurations fail-closed.
        list_valid = (
            status.get("status") == "ok"
            and status.get("config_load_error") is None
            and isinstance(status.get("configured_servers"), int)
            and status["configured_servers"] > 0
        )
        legacy_server_valid = list_valid and server and server.get("valid") is True
        current_server_valid = (
            list_valid
            and server
            and "valid" not in server
        )
        if not (legacy_server_valid or current_server_valid):
            raise ClawProviderConfigError(
                f"HER required HASHI Tool Gateway is invalid: {status.get('invalid_servers') or status}"
            )
        self.logger.info(
            "HER HASHI Tool Gateway validated: tools=%d mcp_server=hashi-tools",
            len(definitions),
        )

    async def initialize(self) -> bool:
        self.logger.info("Initializing %s backend...", HER_DISPLAY_NAME)
        self.config.workspace_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._binary_resolution = discover_claw_binary(
                global_config=self.global_config,
                agent_config=self.config,
            )
            self._binary = self._binary_resolution.path
            self._prepare_tool_gateway()
            for warning in self._binary_resolution.warnings:
                self.logger.warning("HER binary discovery warning: %s", warning)
            version = await asyncio.to_thread(
                run_claw_version,
                self.effective_workdir,
                binary_path=self._binary,
                env=self._task_env(),
                timeout_s=30,
            )
            self.logger.info(
                "HER version check passed "
                f"(binary={self._binary}, source={self._binary_resolution.source}, "
                f"packaged_version={self._binary_resolution.packaged_version}, "
                f"manifest={self._binary_resolution.manifest_path}, "
                f"version={version.get('version')}, git_sha={version.get('git_sha')})"
            )
            self._validate_tool_gateway()
            self._supports_stream_json = await asyncio.to_thread(
                claw_supports_stream_json,
                self._binary,
                self.effective_workdir,
                self._task_env(),
            )
            self.capabilities.supports_thinking_stream = self._supports_stream_json
            self.capabilities.supports_answer_stream = self._supports_stream_json
            self._load_session_identity()
            from adapters.her_dream import recover_interrupted_runs

            recovered_dreams = recover_interrupted_runs(
                store=self._her_habit_store(),
                journal=self._her_dream_journal(),
            )
            if recovered_dreams:
                self.logger.warning(
                    "Recovered %d interrupted HER Habit Dream commit(s).",
                    recovered_dreams,
                )
            self._resume_pending_habit_meditations()
            self._resume_pending_habit_notifications()
            if not self._supports_stream_json:
                self.logger.warning(
                    "HER binary does not advertise stream-json; verbose mode will use JSON fallback."
                )
            return True
        except ClawError as exc:
            self.logger.error(f"HER unavailable: {exc}")
            self._binary = None
            return False
        except Exception as exc:
            self.logger.error(f"HER initialization failed: {exc}")
            self._binary = None
            return False

    async def handle_new_session(self) -> bool:
        self._cancel_ultra_runs("new_session")
        self._session_id = None
        self._persist_session_identity()
        self.logger.info(
            "HER handle_new_session: cleared persisted HER session identity."
        )
        return True

    def set_session_mode(self, enabled: bool) -> None:
        """Apply HASHI's fixed-versus-full-context session ownership policy."""
        requested = bool(enabled) and not self._ephemeral_session()
        previous = self._session_mode
        self._session_mode = requested
        if requested:
            if not previous and self._session_id is None:
                self._load_session_identity()
        else:
            self._session_id = None
            self._session_state_path.unlink(missing_ok=True)
        self.logger.info(
            "HER session mode set to %s (previous=%s ephemeral=%s)",
            "ON" if requested else "OFF",
            "ON" if previous else "OFF",
            self._ephemeral_session(),
        )

    def _refresh_current_process(self) -> None:
        request_id = next(reversed(self._active_processes), None)
        self.current_proc = (
            self._active_processes[request_id] if request_id is not None else None
        )

    async def _unregister_active_process(
        self,
        request_id: str,
        proc: asyncio.subprocess.Process,
    ) -> None:
        async with self._active_process_lock:
            if self._active_processes.get(request_id) is not proc:
                return
            self._active_processes.pop(request_id, None)
            self._refresh_current_process()
            remaining = len(self._active_processes)
        self.logger.info(
            "HER subprocess exited: request=%s pid=%s returncode=%s active=%d",
            request_id,
            proc.pid,
            proc.returncode,
            remaining,
        )

    async def _stop_active_process(
        self,
        request_id: str,
        proc: asyncio.subprocess.Process,
        *,
        reason: str,
    ) -> bool:
        self.logger.warning(
            "Stopping HER execution: request=%s pid=%s reason=%s",
            request_id,
            proc.pid,
            reason,
        )
        await self.force_kill_process_tree(
            proc,
            logger=self.logger,
            reason=f"{reason}:{request_id}",
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.logger.error(
                "HER execution did not exit after forced stop: request=%s pid=%s",
                request_id,
                proc.pid,
            )
        return proc.returncode is not None

    async def _stop_all_active_processes(self, *, reason: str) -> int:
        """Stop every HER execution owned by this Agent adapter and await exit."""

        async with self._active_process_shutdown_lock:
            async with self._active_process_lock:
                self._stopping_active_processes = True
                active = list(self._active_processes.items())
            try:
                await asyncio.gather(
                    *(
                        self._stop_active_process(
                            request_id,
                            proc,
                            reason=reason,
                        )
                        for request_id, proc in active
                    ),
                    return_exceptions=True,
                )
            finally:
                async with self._active_process_lock:
                    for request_id, proc in active:
                        if (
                            self._active_processes.get(request_id) is proc
                            and proc.returncode is not None
                        ):
                            self._active_processes.pop(request_id, None)
                    self._refresh_current_process()
                    remaining = [
                        request_id
                        for request_id, proc in active
                        if self._active_processes.get(request_id) is proc
                    ]
                    self._stopping_active_processes = False

            stopped = len(active) - len(remaining)
            if remaining:
                self.logger.error(
                    "HER stop-all left executions registered: requests=%s",
                    ",".join(remaining),
                )
            if active:
                self.logger.warning(
                    "HER stop-all completed: stopped=%d total=%d reason=%s",
                    stopped,
                    len(active),
                    reason,
                )
            return stopped

    def _ultra_config(self) -> _her_ultra.HERUltraConfig:
        raw = self._extra.get("ultra")
        if raw is not None and not isinstance(raw, Mapping):
            raise _her_ultra.HERUltraContractError(
                "HER Ultra configuration must be an object"
            )
        config = _her_ultra.HERUltraConfig.from_mapping(
            raw if isinstance(raw, Mapping) else {},
            primary_model=self._claw_model(),
            allowed_models=(self._claw_model(),),
        )
        if config.primary_model != self._claw_model():
            raise _her_ultra.HERUltraContractError(
                "HER Ultra primary_model must match the active HER model"
            )
        return config

    def _ultra_authority(self) -> _her_ultra.HERUltraAuthorityEnvelope:
        configured_tools = self._allowed_tools()
        allowed_tools = tuple(configured_tools) if configured_tools else ("*",)
        permission_mode = self._permission_mode()
        return _her_ultra.HERUltraAuthorityEnvelope.build(
            permission_mode=permission_mode,
            access_root=str(self.config.resolve_access_root()),
            allowed_tools=allowed_tools,
            write_enabled=permission_mode != "read-only",
        )

    @staticmethod
    def _ultra_task_env(effort: str) -> dict[str, str]:
        normalized = str(effort or "high").strip().lower()
        iterations = CLAW_EXECUTION_EFFORT_ITERATIONS.get(normalized, 96)
        return {
            "CLAW_EXECUTION_EFFORT": normalized,
            "CLAW_MAX_TOOL_ITERATIONS": str(iterations),
            # Ultra already owns decomposition, dispatch, and assembly. Running
            # the native planning/review controller inside every Ultra call
            # creates nested orchestration and retry amplification.
            "CLAW_TASK_PLANNING": "0",
        }

    @staticmethod
    def _ultra_invocation_result(
        result: ClawTaskResult,
    ) -> _her_ultra.HERUltraInvocationResult:
        usage_data = result.json_data.get("usage") or {}
        usage_data = usage_data if isinstance(usage_data, Mapping) else {}
        stream_usage = _stream_json_usage(result.stdout)
        thinking_tokens = int(
            usage_data.get("thinking_tokens")
            or (usage_data.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            )
            or stream_usage.get("thinking_tokens")
            or 0
        )
        incomplete = _claw_run_is_incomplete(result)
        return _her_ultra.HERUltraInvocationResult(
            text=result.text,
            is_success=not incomplete,
            error=(
                "HER internal task stopped before completion: "
                f"{result.stop_reason or result.completion_status or 'incomplete'}"
                if incomplete
                else ""
            ),
            error_type="incomplete" if incomplete else "",
            retryable=False,
            session_id=result.session_id or "",
            model=result.model,
            input_tokens=int(usage_data.get("input_tokens") or 0),
            output_tokens=int(usage_data.get("output_tokens") or 0),
            thinking_tokens=thinking_tokens,
            tool_call_count=len(result.tool_uses),
            tool_loop_count=result.iterations or 0,
            duration_ms=result.duration_ms,
            cost_usd=None,
        )

    @staticmethod
    def _ultra_error_invocation(
        exc: BaseException,
    ) -> _her_ultra.HERUltraInvocationResult:
        parsed = getattr(exc, "parsed_error", None)
        parsed = parsed if isinstance(parsed, Mapping) else {}
        error_type = str(
            parsed.get("error_kind")
            or parsed.get("error_type")
            or ("timeout" if isinstance(exc, ClawTimeoutError) else type(exc).__name__)
        )
        # A timeout already consumed the configured HER idle/hard budget.
        # Replaying the identical isolated worker from scratch is not a useful
        # transient retry and can duplicate unknown side effects.
        retryable = bool(parsed.get("retryable")) and error_type.lower() != "timeout"
        return _her_ultra.HERUltraInvocationResult(
            text="",
            is_success=False,
            error=str(exc),
            error_type=error_type,
            retryable=retryable,
        )

    def _cancel_ultra_runs(self, reason: str) -> None:
        for orchestrator in tuple(self._ultra_runs.values()):
            orchestrator.cancel(reason)

    async def _generate_ultra_response(
        self,
        prompt: str,
        request_id: str,
        *,
        is_retry: bool,
        on_stream_event: StreamCallback,
    ) -> BackendResponse:
        try:
            config = self._ultra_config()
            authority = self._ultra_authority()
        except _her_ultra.HERUltraContractError as exc:
            return BackendResponse(
                text="",
                duration_ms=0,
                error=str(exc),
                is_success=False,
                stream_metadata={
                    "claw_completion_status": "failed",
                    "claw_stop_reason": "invalid_ultra_config",
                    "claw_execution_effort": _her_ultra.HER_ULTRA_EFFORT,
                },
            )
        if not config.enabled:
            return BackendResponse(
                text="",
                duration_ms=0,
                error="HER Ultra effort is disabled by Agent configuration.",
                is_success=False,
                stream_metadata={
                    "claw_completion_status": "failed",
                    "claw_stop_reason": "ultra_disabled",
                    "claw_execution_effort": _her_ultra.HER_ULTRA_EFFORT,
                },
            )
        if request_id in self._ultra_runs:
            return BackendResponse(
                text="",
                duration_ms=0,
                error=f"HER Ultra request is already running: {request_id}",
                is_success=False,
                stream_metadata={
                    "claw_completion_status": "failed",
                    "claw_stop_reason": "duplicate_request_id",
                    "claw_execution_effort": _her_ultra.HER_ULTRA_EFFORT,
                },
            )

        persona_source = self._her_persona_source()
        inherited_tools = (
            None if authority.allowed_tools == ("*",) else list(authority.allowed_tools)
        )

        persona_guidance = persona_source.model_guidance(limit=12_000)
        chinese_commentary = _claw_uses_chinese(prompt, persona_guidance)

        async def render_persona_commentary(
            facts: Mapping[str, Any],
        ) -> _her_ultra.HERUltraCommentaryRender:
            if not persona_source.usable:
                return _her_ultra.HERUltraCommentaryRender(
                    text=(
                        "[HER 中性兜底] 任务仍在进行；到达下一个已确认阶段时会继续汇报。"
                        if chinese_commentary
                        else "[HER neutral fallback] Work is still in progress; another update will follow at the next verified stage."
                    ),
                    fallback=True,
                    error_type=persona_source.unavailable_reason
                    or "persona_source_unavailable",
                )
            renderer_prompt = f"""HER ULTRA COMMENTARY RENDERER — INTERNAL, TOOL-FREE

Write one brief user-facing progress update in the configured Persona. Preserve
the supplied phase and counts exactly, but express them naturally in the
Persona's language, self-reference, forms of address, tone, and style. Do not
invent work, results, timing, or certainty. Return only the message.

CONFIGURED system_md PERSONA GUIDANCE (quoted, read-only)
{persona_guidance}

RUNTIME FACTS (quoted, read-only)
{json.dumps(dict(facts), ensure_ascii=False, sort_keys=True)}
"""
            try:
                rendered = await self.run_habit_dream_model(
                    renderer_prompt,
                    request_id=(
                        f"{request_id}:ultra-commentary:"
                        f"{facts.get('phase', 'progress')}:"
                        f"{facts.get('terminal_subtasks', 0)}"
                    ),
                    timeout_seconds=180,
                )
                message = str(rendered.text or "").strip()
                if message:
                    return _her_ultra.HERUltraCommentaryRender(text=message)
                error_type = "empty_renderer_output"
            except Exception as exc:  # noqa: BLE001 - explicit neutral fallback below
                error_type = type(exc).__name__
            return _her_ultra.HERUltraCommentaryRender(
                text=(
                    "[HER 中性兜底] 任务仍在进行；到达下一个已确认阶段时会继续汇报。"
                    if chinese_commentary
                    else "[HER neutral fallback] Work is still in progress; another update will follow at the next verified stage."
                ),
                fallback=True,
                error_type=error_type,
            )

        async def forward_primary_event(event: StreamEvent) -> None:
            if on_stream_event is None:
                return
            if event.kind == KIND_ACKNOWLEDGEMENT or event.delivery_class in {
                DELIVERY_FINAL,
                DELIVERY_CONTROL,
            }:
                return
            await on_stream_event(event)

        async def invoke_primary(
            spec: _her_ultra.HERUltraPrimaryExecutionSpec,
        ) -> _her_ultra.HERUltraInvocationResult:
            read_only_phases = {
                "planning",
                "plan_correction",
                "assembly",
                "failure_finalization",
            }
            try:
                result = await self._run_task_async(
                    spec.prompt,
                    resume=spec.resume_session_id or None,
                    request_id=spec.request_id,
                    on_stream_event=forward_primary_event,
                    track_session_identity=False,
                    permission_mode_override=(
                        "read-only"
                        if spec.phase in read_only_phases
                        else authority.permission_mode
                    ),
                    allowed_tools_override=(
                        []
                        if spec.phase in read_only_phases
                        else inherited_tools
                    ),
                    task_env_overrides=self._ultra_task_env(spec.effort),
                    model_override=spec.model,
                    cwd_override=self.effective_workdir,
                )
            except asyncio.CancelledError:
                raise
            except (ClawError, ValueError) as exc:
                return self._ultra_error_invocation(exc)
            return self._ultra_invocation_result(result)

        async def invoke_worker(
            spec: _her_ultra.HERUltraWorkerExecutionSpec,
        ) -> _her_ultra.HERUltraInvocationResult:
            worker_tools = (
                None if spec.allowed_tools == ("*",) else list(spec.allowed_tools)
            )
            try:
                result = await self._run_task_async(
                    spec.prompt,
                    resume=None,
                    request_id=spec.request_id,
                    on_stream_event=None,
                    track_session_identity=False,
                    permission_mode_override=spec.permission_mode,
                    allowed_tools_override=worker_tools,
                    task_env_overrides=self._ultra_task_env(spec.effort),
                    model_override=spec.model,
                    # Access authority and process cwd are separate security
                    # concepts.  A worker may inherit access_root=/, but the
                    # CLI must still start in the Agent's concrete workzone.
                    cwd_override=self.effective_workdir,
                )
            except asyncio.CancelledError:
                raise
            except (ClawError, ValueError) as exc:
                return self._ultra_error_invocation(exc)
            return self._ultra_invocation_result(result)

        orchestrator = _her_ultra.HERUltraOrchestrator(
            config=config,
            ledger_root=self.config.workspace_dir / "backend_state" / "her_ultra_runs",
            primary_executor=invoke_primary,
            worker_executor=invoke_worker,
            on_stream_event=on_stream_event,
            persona_guidance=persona_guidance,
            persona_commentary_renderer=render_persona_commentary,
        )
        self._ultra_runs[request_id] = orchestrator
        session_mode_enabled = self._session_mode and not self._ephemeral_session()
        session_scope = self._request_session_scope(request_id)
        resume_session_id = self._request_resume_session(request_id)
        isolated_resume = (
            session_scope == HER_SESSION_SCOPE_ISOLATED_RESUME
            and bool(resume_session_id)
        )
        persistent_session = (
            session_mode_enabled and session_scope == HER_SESSION_SCOPE_PERSISTENT
        )

        async def execute(initial_session_id: str) -> _her_ultra.HERUltraOutcome:
            return await orchestrator.run(
                authoritative_goal=prompt,
                parent_request_id=request_id,
                authority=authority,
                initial_primary_session_id=initial_session_id,
            )

        try:
            if persistent_session:
                async with self._persistent_session_lock:
                    previous_session = self._session_id or ""
                    outcome = await execute(previous_session)
                    if outcome.status in {"completed", "incomplete"}:
                        self._session_id = outcome.primary_session_id or None
                        self._persist_session_identity()
                    else:
                        self._quarantine_persistent_session(
                            request_id,
                            _her_ultra.HERUltraError(
                                outcome.error or f"Ultra run {outcome.status}"
                            ),
                        )
            else:
                initial_session = resume_session_id if isolated_resume else ""
                outcome = await execute(initial_session or "")
        except asyncio.CancelledError:
            orchestrator.cancel("caller_cancelled")
            raise
        finally:
            if self._ultra_runs.get(request_id) is orchestrator:
                self._ultra_runs.pop(request_id, None)

        completion_status = (
            "incomplete" if outcome.status == "incomplete" else outcome.status
        )
        stop_reason = {
            "completed": "end_turn",
            "cancelled": "cancelled",
            "failed": "backend_error",
        }.get(outcome.status, outcome.status)
        if outcome.status == "incomplete" and outcome.pending_interaction is not None:
            stop_reason = "requires_user_input"
        ultra_metadata = {
            "run_id": outcome.run_id,
            "status": outcome.status,
            "plan_revision": outcome.plan_revision,
            "subtask_count": outcome.subtask_count,
            "completed_subtasks": outcome.completed_subtasks,
            "max_concurrent_subagents": config.max_concurrent_subagents,
            "pending_interaction": dict(outcome.pending_interaction)
            if outcome.pending_interaction is not None
            else None,
        }
        stream_metadata = {
            "claw_completion_status": completion_status,
            "claw_stop_reason": stop_reason,
            "claw_execution_effort": _her_ultra.HER_ULTRA_EFFORT,
            "claw_inner_execution_effort": config.primary_inner_effort,
            "claw_max_iterations": CLAW_EXECUTION_EFFORT_ITERATIONS[
                config.primary_inner_effort
            ],
            "her_session_scope": session_scope,
            "her_session_id": outcome.primary_session_id,
            "her_model": config.primary_model,
            "her_resumed_session": bool(
                (persistent_session and previous_session) or isolated_resume
            ),
            "her_ultra": ultra_metadata,
            "her_retry": bool(is_retry),
        }
        pending_kind = str(
            (outcome.pending_interaction or {}).get("kind") or ""
        ).lower()
        if pending_kind == "continuation":
            stream_metadata["recommended_action"] = "continue"
        return BackendResponse(
            text=outcome.text,
            duration_ms=outcome.duration_ms,
            error=outcome.error or None,
            is_success=outcome.is_success,
            stop_reason=stop_reason,
            usage=TokenUsage(
                input_tokens=outcome.input_tokens,
                output_tokens=outcome.output_tokens,
                thinking_tokens=outcome.thinking_tokens,
            ),
            cost_usd=outcome.cost_usd,
            tool_call_count=outcome.tool_call_count,
            tool_loop_count=outcome.tool_loop_count,
            stream_metadata=stream_metadata,
        )

    async def shutdown(self):
        self._cancel_ultra_runs("shutdown")
        current_task = asyncio.current_task()
        pending_dreams = [
            task
            for task in self._habit_dream_tasks
            if task is not current_task and not task.done()
        ]
        pending_meditations = list(self._habit_meditation_tasks)
        pending_notifications = list(self._habit_notification_tasks)
        for task in pending_dreams:
            task.cancel()
        for task in pending_meditations:
            task.cancel()
        for task in pending_notifications:
            task.cancel()
        if pending_dreams:
            await asyncio.gather(*pending_dreams, return_exceptions=True)
        if pending_meditations:
            await asyncio.gather(*pending_meditations, return_exceptions=True)
        if pending_notifications:
            await asyncio.gather(*pending_notifications, return_exceptions=True)
        self._habit_meditation_tasks.clear()
        self._habit_meditation_job_ids.clear()
        self._habit_notification_tasks.clear()
        self._habit_notification_job_ids.clear()
        self._habit_dream_tasks.clear()
        await self._stop_all_active_processes(reason="shutdown")

    async def generate_response(
        self,
        prompt: str,
        request_id: str,
        is_retry: bool = False,
        silent: bool = False,
        on_stream_event: StreamCallback = None,
    ) -> BackendResponse:
        if self._binary is None:
            try:
                self._binary_resolution = discover_claw_binary(
                    global_config=self.global_config, agent_config=self.config
                )
                self._binary = self._binary_resolution.path
                for warning in self._binary_resolution.warnings:
                    self.logger.warning("HER binary discovery warning: %s", warning)
            except ClawError as exc:
                return BackendResponse(
                    text="", duration_ms=0, error=str(exc), is_success=False
                )

        if on_stream_event is not None:
            await on_stream_event(
                StreamEvent(
                    kind=KIND_PROGRESS,
                    summary="HER task started",
                    event_id=f"{request_id}:technical:task_started",
                    delivery_class=DELIVERY_TECHNICAL,
                    origin="her_runtime",
                    phase="initial",
                )
            )

        if self.effort == _her_ultra.HER_ULTRA_EFFORT:
            return await self._generate_ultra_response(
                prompt,
                request_id,
                is_retry=is_retry,
                on_stream_event=on_stream_event,
            )

        habit_config = self._habit_meditation_config()
        if habit_config.enabled and not self._habit_request_eligible(request_id):
            habit_config = replace(habit_config, enabled=False)
            self.logger.info(
                "HER Habit pipeline skipped by request eligibility: request=%s",
                request_id,
            )
        session_mode_enabled = self._session_mode and not self._ephemeral_session()
        session_scope = self._request_session_scope(request_id)
        resume_session_id = self._request_resume_session(request_id)
        isolated_resume = (
            session_scope == HER_SESSION_SCOPE_ISOLATED_RESUME
            and bool(resume_session_id)
        )
        persistent_session = (
            session_mode_enabled and session_scope == HER_SESSION_SCOPE_PERSISTENT
        )
        self.logger.info(
            "HER request session scope: request=%s scope=%s persistent_busy=%s",
            request_id,
            session_scope,
            self.persistent_session_busy,
        )
        habit_notification_context = self._habit_notification_context(
            request_id,
            silent=silent,
        )
        # HASHI request IDs intentionally remain restart-local. Give each HER
        # execution its own durable Meditation identity so a reused req-0001
        # cannot collide with a journal left by an earlier runtime.
        meditation_job_id = uuid.uuid4().hex if habit_config.enabled else None
        task_prompt = prompt
        selected_habit_ids: list[str] = []
        if habit_config.enabled:
            selected_habits = self._her_habit_store().retrieve(
                _her_habits.extract_current_request(prompt),
                limit=habit_config.retrieval_limit,
            )
            selected_habit_ids = [habit.habit_id for habit in selected_habits]
            task_prompt = _her_habits.attach_habits_to_prompt(
                prompt,
                selected_habits,
            )
            self.logger.info(
                "HER Habit planning: request=%s matched=%d ids=%s effort=%s",
                request_id,
                len(selected_habit_ids),
                ",".join(selected_habit_ids) or "none",
                self.effort,
            )

        cadence_controller: _HERStreamCadenceController | None = None
        cadence_task: asyncio.Task | None = None
        request_stream_callback = on_stream_event
        if on_stream_event is not None:
            cadence_controller = _HERStreamCadenceController(
                on_stream_event,
                request_id=request_id,
                prompt=prompt,
                progress_enabled=self.effort in HER_COMMENTARY_EFFORTS,
            )
            request_stream_callback = cadence_controller.forward
            if cadence_controller.progress_enabled:
                cadence_task = asyncio.create_task(cadence_controller.run())

        async def execute_request() -> ClawTaskResult:
            if isolated_resume:
                return await self._run_task_async(
                    task_prompt,
                    resume=resume_session_id,
                    request_id=request_id,
                    on_stream_event=request_stream_callback,
                    track_session_identity=False,
                )
            if not persistent_session:
                return await self._run_task_async(
                    task_prompt,
                    resume=None,
                    request_id=request_id,
                    on_stream_event=request_stream_callback,
                    track_session_identity=False,
                )

            async with self._persistent_session_lock:
                previous = self._session_id
                try:
                    result = await self._run_task_async(
                        task_prompt,
                        resume=previous,
                        request_id=request_id,
                        on_stream_event=request_stream_callback,
                        track_session_identity=True,
                    )
                except (asyncio.CancelledError, Exception) as exc:
                    self._quarantine_persistent_session(request_id, exc)
                    raise
                checkpoint_session = result.session_id or self._session_id
                if checkpoint_session:
                    self._session_id = checkpoint_session
                    self._persist_session_identity()
                    self.logger.info(
                        "HER session checkpoint: request=%s session=%s resumed=%s",
                        request_id,
                        checkpoint_session,
                        bool(previous),
                    )
                else:
                    self._session_id = None
                    self._persist_session_identity()
                    self.logger.warning(
                        "HER response omitted session_id for request=%s; "
                        "persistent checkpoint was cleared.",
                        request_id,
                    )
                return result

        started = time.perf_counter()
        try:
            result = await execute_request()
        except asyncio.CancelledError as exc:
            if habit_config.enabled:
                self._record_failed_turn_meditation_skip(request_id=request_id, exc=exc)
            raise
        except ClawTimeoutError as exc:
            if habit_config.enabled:
                self._record_failed_turn_meditation_skip(request_id=request_id, exc=exc)
            return BackendResponse(
                text="",
                duration_ms=self._duration_ms(started),
                error=str(exc),
                is_success=False,
            )
        except ClawCommandError as exc:
            if habit_config.enabled:
                self._record_failed_turn_meditation_skip(request_id=request_id, exc=exc)
            return BackendResponse(
                text="",
                duration_ms=self._duration_ms(started),
                error=str(exc),
                is_success=False,
                stream_metadata={"her_error": self._command_error_metadata(exc)},
            )
        except (ClawError, ValueError) as exc:
            if habit_config.enabled:
                self._record_failed_turn_meditation_skip(request_id=request_id, exc=exc)
            return BackendResponse(
                text="",
                duration_ms=self._duration_ms(started),
                error=str(exc),
                is_success=False,
            )
        finally:
            if cadence_controller is not None:
                cadence_controller.close()
            if cadence_task is not None:
                cadence_task.cancel()
                try:
                    await cadence_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    self.logger.warning(
                        "HER stream cadence stopped after callback failure: %s",
                        type(exc).__name__,
                    )

        if on_stream_event is not None and not self._supports_stream_json:
            for index, tool in enumerate(result.tool_uses, start=1):
                if isinstance(tool, dict):
                    await on_stream_event(
                        StreamEvent(
                            kind=KIND_TOOL_END,
                            summary=f"HER used {tool.get('name') or 'tool'}",
                            tool_name=str(tool.get("name") or ""),
                            event_id=f"{request_id}:technical:tool_summary:{index}",
                            delivery_class=DELIVERY_TECHNICAL,
                            origin="tool_gateway",
                            phase="execution",
                        )
                    )
        usage_data = result.json_data.get("usage") or {}
        if result.session_id and session_scope == HER_SESSION_SCOPE_ISOLATED:
            self.logger.info(
                "HER session checkpoint ignored for isolated request: request=%s scope=%s",
                request_id,
                session_scope,
            )
        elif result.session_id and isolated_resume:
            self.logger.info(
                "HER isolated continuation checkpoint returned: request=%s session=%s",
                request_id,
                result.session_id,
            )
        tool_errors = sum(
            1
            for item in result.tool_results
            if isinstance(item, Mapping)
            and bool(item.get("is_error") or item.get("isError"))
        )
        self.logger.info(
            "HER task completed: request=%s model=%s session=%s iterations=%s "
            "completion=%s stop_reason=%s provider_stop_reason=%s tool_calls=%d "
            "tool_errors=%d gateway=%s session_scope=%s",
            request_id,
            result.model,
            result.session_id or self._session_id or "unavailable",
            result.iterations if result.iterations is not None else "unavailable",
            result.completion_status or "unavailable",
            result.stop_reason or "unavailable",
            result.provider_stop_reason or "unavailable",
            len(result.tool_uses),
            tool_errors,
            bool(self._gateway_context_path),
            session_scope,
        )
        stream_usage = (
            _stream_json_usage(result.stdout) if self._supports_stream_json else {}
        )
        thinking_tokens = int(
            usage_data.get("thinking_tokens")
            or (usage_data.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            )
            or stream_usage.get("thinking_tokens")
            or 0
        )
        response_text = result.text
        fallback_metadata: dict[str, Any] = {}
        if _claw_run_is_incomplete(result):
            response_text, fallback_metadata = _claw_incomplete_response(
                result, prompt=prompt
            )
            if fallback_metadata.get("persona_render_required"):
                (
                    persona_response,
                    persona_metadata,
                ) = await self._render_incomplete_persona_response(
                    result,
                    request_id=request_id,
                    metadata=fallback_metadata,
                )
                fallback_metadata.update(persona_metadata)
                if persona_response:
                    response_text = persona_response
                    fallback_metadata.update(
                        {
                            "fallback_report_generated": False,
                            "persona_interpretation_generated": True,
                        }
                    )
            elif fallback_metadata.get("persona_final_response_preserved"):
                persona_source = self._her_persona_source()
                self._persist_persona_audit(
                    request_id,
                    report_type="incomplete_final",
                    renderer_attempted=False,
                    renderer_succeeded=False,
                    model_final_preserved=True,
                    validation_outcome="safe_model_final_preserved",
                    failure_reason=None,
                    **persona_source.audit_fields(),
                )
            self.logger.warning(
                "HER incomplete run finalized: request=%s completion=%s "
                "stop_reason=%s persona_preserved=%s persona_interpreted=%s "
                "persona_renderer_succeeded=%s recommendation=%s",
                request_id,
                result.completion_status or "unknown",
                result.stop_reason or "unknown",
                fallback_metadata.get("persona_final_response_preserved", False),
                fallback_metadata.get("persona_interpretation_generated", False),
                fallback_metadata.get("persona_renderer_succeeded", False),
                fallback_metadata.get("recommended_action") or "unknown",
            )
        if habit_config.enabled:
            self._schedule_habit_meditation(
                job_id=meditation_job_id,
                request_id=request_id,
                task_prompt=prompt,
                task_result=result,
                config=habit_config,
                notification_context=habit_notification_context,
            )
        return BackendResponse(
            text=response_text,
            duration_ms=result.duration_ms,
            is_success=True,
            stop_reason=result.stop_reason,
            usage=TokenUsage(
                input_tokens=int(usage_data.get("input_tokens") or 0),
                output_tokens=int(usage_data.get("output_tokens") or 0),
                thinking_tokens=thinking_tokens,
            ),
            cost_usd=None,
            tool_call_count=len(result.tool_uses),
            tool_loop_count=result.iterations or 0,
            stream_metadata={
                **({"claw_thinking": stream_usage} if stream_usage else {}),
                "claw_completion_status": result.completion_status or "unknown",
                "claw_stop_reason": result.stop_reason or "unknown",
                "claw_provider_stop_reason": result.provider_stop_reason or "unknown",
                "claw_execution_effort": self.effort,
                "claw_max_iterations": self._max_tool_iterations(),
                "her_session_scope": session_scope,
                "her_session_id": result.session_id or "",
                "her_model": result.model,
                "her_resumed_session": bool(isolated_resume),
                **(
                    {
                        "her_habit_meditation": True,
                        "her_habit_ids": selected_habit_ids,
                    }
                    if habit_config.enabled
                    else {}
                ),
                **fallback_metadata,
            },
        )

    @staticmethod
    def _duration_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    async def _wait_for_her_task_with_timeouts(
        self,
        task: asyncio.Future,
        *,
        started_monotonic: float,
        activity_state: list[float],
    ) -> str | None:
        """Enforce timeouts per subprocess, not across concurrent HER runs."""
        while not task.done():
            total_runtime = max(0.0, time.perf_counter() - started_monotonic)
            if total_runtime >= self.HARD_TIMEOUT_SEC:
                return "hard"
            idle_for = max(0.0, time.monotonic() - activity_state[0])
            if idle_for >= self.IDLE_TIMEOUT_SEC:
                return "idle"
            wait_slice = min(
                5.0,
                max(0.1, self.HARD_TIMEOUT_SEC - total_runtime),
                max(0.1, self.IDLE_TIMEOUT_SEC - idle_for),
            )
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_slice)
            except asyncio.TimeoutError:
                continue
        return None

    def _her_timeout_diagnostic(
        self,
        timeout_kind: str,
        *,
        started_monotonic: float,
        activity_state: list[float],
    ) -> str:
        total_runtime = max(0.0, time.perf_counter() - started_monotonic)
        last_output_age = max(0.0, time.monotonic() - activity_state[0])
        idle_source = self._timeout_source("idle_timeout_sec").replace(" ", "_")
        hard_source = self._timeout_source("hard_timeout_sec").replace(" ", "_")
        return (
            f"kind={timeout_kind}, idle_timeout_s={self.IDLE_TIMEOUT_SEC}, "
            f"idle_source={idle_source}, hard_timeout_s={self.HARD_TIMEOUT_SEC}, "
            f"hard_source={hard_source}, last_output_age_s={last_output_age:.2f}, "
            f"total_runtime_s={total_runtime:.2f}"
        )

    async def _run_task_async(
        self,
        prompt: str,
        *,
        resume: str | None,
        request_id: str,
        on_stream_event: StreamCallback = None,
        track_session_identity: bool = True,
        permission_mode_override: str | None = None,
        allowed_tools_override: list[str] | None = None,
        task_env_overrides: Mapping[str, str] | None = None,
        model_override: str | None = None,
        cwd_override: Path | None = None,
    ) -> ClawTaskResult:
        if self._binary is None:
            raise ClawBinaryNotFound("HER binary not initialized")
        task_model = str(model_override or self._claw_model()).strip()
        task_cwd = Path(cwd_override or self.effective_workdir)
        permission_mode = permission_mode_override or self._permission_mode()
        allowed_tools = (
            self._allowed_tools()
            if allowed_tools_override is None
            else allowed_tools_override
        )
        args = build_claw_task_args(
            prompt,
            task_model,
            permission_mode=permission_mode,
            resume=resume,
            allowed_tools=allowed_tools,
            skip_permissions=self._skip_permissions()
            and permission_mode_override is None,
            output_format="stream-json" if self._supports_stream_json else "json",
        )
        command = [str(self._binary), *args]
        stdin_data = None if resume else prompt.encode("utf-8")
        started = time.perf_counter()
        extra_kwargs = {}
        if os.name != "nt":
            extra_kwargs["start_new_session"] = True
        task_env = self._task_env()
        if task_env_overrides:
            task_env.update(
                {str(key): str(value) for key, value in task_env_overrides.items()}
            )
        # Semantic compaction shares HASHI's existing /timeout policy. Inject
        # the effective request values after internal task overrides so
        # maintenance cannot silently create a competing timeout contract.
        task_env.update(
            {
                "CLAW_SEMANTIC_COMPACTION_IDLE_TIMEOUT_SECONDS": str(
                    self.IDLE_TIMEOUT_SEC
                ),
                "CLAW_SEMANTIC_COMPACTION_IDLE_TIMEOUT_SOURCE": self._timeout_source(
                    "idle_timeout_sec"
                ),
                "CLAW_REQUEST_HARD_TIMEOUT_SECONDS": str(self.HARD_TIMEOUT_SEC),
                "CLAW_REQUEST_HARD_TIMEOUT_SOURCE": self._timeout_source(
                    "hard_timeout_sec"
                ),
            }
        )
        async with self._active_process_lock:
            if self._stopping_active_processes:
                raise ClawCommandError(
                    "HER execution rejected while Agent stop is in progress",
                    returncode=1,
                )
            existing = self._active_processes.get(request_id)
            if existing is not None and existing.returncode is None:
                raise ClawCommandError(
                    f"HER request is already running: {request_id}",
                    returncode=1,
                )
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin_data is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(task_cwd),
                env=task_env,
                **extra_kwargs,
            )
            self._active_processes[request_id] = proc
            self.current_proc = proc
            active_count = len(self._active_processes)
        self.logger.info(
            "HER subprocess started: request=%s pid=%s active=%d",
            request_id,
            proc.pid,
            active_count,
        )
        activity_state = [time.monotonic()]
        self._touch_activity()
        communication_task = asyncio.create_task(
            self._communicate_stream_json(
                proc,
                command,
                request_id,
                on_stream_event,
                commentary_prompt=prompt,
                track_session_identity=track_session_identity,
                activity_state=activity_state,
                stdin_data=stdin_data,
            )
            if self._supports_stream_json
            else self._communicate_with_activity(
                proc,
                activity_state=activity_state,
                stdin_data=stdin_data,
            )
        )
        try:
            timeout_kind = await self._wait_for_her_task_with_timeouts(
                communication_task,
                started_monotonic=started,
                activity_state=activity_state,
            )
            if timeout_kind is not None:
                diagnostic = self._her_timeout_diagnostic(
                    timeout_kind,
                    started_monotonic=started,
                    activity_state=activity_state,
                )
                self.logger.error(
                    f"HER request {request_id} {timeout_kind}-timed out "
                    f"(pid={proc.pid}, {diagnostic})"
                )
                await self.force_kill_process_tree(
                    proc,
                    logger=self.logger,
                    reason=f"{timeout_kind}-timeout:{request_id}",
                )
                await asyncio.gather(communication_task, return_exceptions=True)
                timeout_s = (
                    self.IDLE_TIMEOUT_SEC
                    if timeout_kind == "idle"
                    else self.HARD_TIMEOUT_SEC
                )
                detail = (
                    f"was idle for {timeout_s}s with no output"
                    if timeout_kind == "idle"
                    else f"exceeded hard timeout of {timeout_s}s"
                )
                self._persist_diagnostic_record(
                    {
                        "kind": "command_timeout",
                        "request_id": request_id,
                        "timeout_kind": timeout_kind,
                        "timeout_seconds": timeout_s,
                        "resumed_session": bool(resume),
                        "session_id": resume,
                    }
                )
                raise ClawTimeoutError(
                    f"HER command {detail}.",
                    timeout_s=timeout_s,
                )
            stdout_data, stderr_data = await communication_task
        except asyncio.CancelledError:
            await self.force_kill_process_tree(
                proc,
                logger=self.logger,
                reason=f"cancelled:{request_id}",
            )
            communication_task.cancel()
            await asyncio.gather(communication_task, return_exceptions=True)
            raise
        finally:
            await self._unregister_active_process(request_id, proc)

        duration_ms = self._duration_ms(started)
        secret_values = [task_env.get(key, "") for key in SECRET_ENV_KEYS]
        stdout = redact_secret_text(stdout_data.decode(errors="replace"), secret_values)
        stderr = redact_secret_text(stderr_data.decode(errors="replace"), secret_values)
        output = stdout.strip() or stderr.strip()
        try:
            parsed = (
                _parse_stream_json_output(output, command=command)
                if self._supports_stream_json
                else (_parse_json_output(output, command=command) if output else {})
            )
        except ClawJsonError as exc:
            if proc.returncode == 0:
                raise
            parsed_error = _last_stream_json_error(stdout, stderr) or {}
            self._persist_command_failure(
                request_id=request_id,
                returncode=proc.returncode or 1,
                parsed_error=parsed_error,
                stderr=stderr,
                resume=resume,
            )
            message = (
                parsed_error.get("error_message")
                or parsed_error.get("error")
                or parsed_error.get("message")
            )
            raise ClawCommandError(
                str(message or exc),
                returncode=proc.returncode or 1,
                stdout=stdout,
                stderr=stderr,
                parsed_error=parsed_error or None,
            ) from exc
        protocol_non_json_line_count = int(
            parsed.get("_protocol_non_json_line_count") or 0
        )
        if protocol_non_json_line_count:
            self.logger.warning(
                "HER stream completed with ignored non-JSON diagnostics: request=%s count=%d",
                request_id,
                protocol_non_json_line_count,
            )
        if proc.returncode != 0:
            self._persist_command_failure(
                request_id=request_id,
                returncode=proc.returncode or 1,
                parsed_error=parsed if isinstance(parsed, Mapping) else None,
                stderr=stderr,
                resume=resume,
            )
            message = (
                parsed.get("error_message")
                or parsed.get("error")
                or parsed.get("message")
                if isinstance(parsed, dict)
                else None
            )
            raise ClawCommandError(
                message or f"HER command exited with code {proc.returncode}",
                returncode=proc.returncode or 1,
                stdout=stdout,
                stderr=stderr,
                parsed_error=parsed if parsed else None,
            )
        return ClawTaskResult(
            text=str(parsed.get("message") or ""),
            model=str(parsed.get("model") or task_model),
            permission_mode=permission_mode,
            cwd=str(task_cwd),
            returncode=proc.returncode or 0,
            duration_ms=duration_ms,
            stdout=stdout,
            stderr=stderr,
            json_data=parsed,
            tool_uses=list(parsed.get("tool_uses") or []),
            tool_results=list(parsed.get("tool_results") or []),
            session_id=str(parsed.get("session_id") or "").strip() or None,
            iterations=parsed.get("iterations")
            if isinstance(parsed.get("iterations"), int)
            else None,
            completion_status=str(parsed.get("completion_status") or "").strip()
            or None,
            stop_reason=str(parsed.get("stop_reason") or "").strip() or None,
            provider_stop_reason=str(parsed.get("provider_stop_reason") or "").strip()
            or None,
            estimated_cost=parsed.get("estimated_cost")
            if isinstance(parsed.get("estimated_cost"), str)
            else None,
        )

    async def _communicate_with_activity(
        self,
        proc: asyncio.subprocess.Process,
        *,
        activity_state: list[float] | None = None,
        stdin_data: bytes | None = None,
    ) -> tuple[bytes, bytes]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def read_stream(reader, chunks: list[bytes]) -> None:
            assert reader is not None
            while True:
                chunk = await reader.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                self._touch_activity()
                if activity_state is not None:
                    activity_state[0] = time.monotonic()

        async def write_stdin() -> None:
            if proc.stdin is None:
                return
            try:
                if stdin_data:
                    proc.stdin.write(stdin_data)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await proc.stdin.wait_closed()

        await asyncio.gather(
            write_stdin(),
            read_stream(proc.stdout, stdout_chunks),
            read_stream(proc.stderr, stderr_chunks),
        )
        await proc.wait()
        return b"".join(stdout_chunks), b"".join(stderr_chunks)

    async def _communicate_stream_json(
        self,
        proc: asyncio.subprocess.Process,
        command: list[str],
        request_id: str,
        on_stream_event: StreamCallback = None,
        *,
        commentary_prompt: str = "",
        track_session_identity: bool = True,
        activity_state: list[float] | None = None,
        stdin_data: bytes | None = None,
    ) -> tuple[bytes, bytes]:
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        source_index = 0
        pending_acknowledgement: StreamEvent | None = None
        initial_direct_response: bool | None = None

        async def emit_stream_event(stream_event: StreamEvent) -> None:
            if on_stream_event is not None:
                await on_stream_event(stream_event)

        async def flush_pending_acknowledgement() -> None:
            nonlocal pending_acknowledgement
            if pending_acknowledgement is None:
                return
            stream_event = pending_acknowledgement
            if initial_direct_response:
                stream_event = replace(
                    stream_event,
                    event_id=f"{request_id}:final",
                    delivery_class=DELIVERY_FINAL,
                    phase="finalization",
                    required=True,
                    provenance="model_authored_direct_response",
                )
            pending_acknowledgement = None
            await emit_stream_event(stream_event)

        async def read_stdout() -> None:
            nonlocal initial_direct_response, pending_acknowledgement, source_index
            assert proc.stdout is not None
            async for line in iter_stream_lines(proc.stdout):
                stdout_chunks.append(line)
                self._persist_stream_json_line(line)
                self._touch_activity()
                if activity_state is not None:
                    activity_state[0] = time.monotonic()
                try:
                    event = json.loads(line.decode(errors="replace"))
                except json.JSONDecodeError:
                    self.logger.warning(
                        "Ignoring non-JSON HER stream line: %r", line[:200]
                    )
                    continue
                source_index += 1
                if event.get("kind") == "semantic_compaction":
                    event.setdefault("request_id", request_id)
                    event.setdefault("session_id", self._session_id or "")
                self._persist_control_event(request_id, event)
                self._persist_stream_diagnostic_event(request_id, event)
                if event.get("kind") == "run_started" and track_session_identity:
                    session_id = str(event.get("session_id") or "").strip()
                    if session_id:
                        self._session_id = session_id
                        self._persist_session_identity()
                        self.logger.info(
                            "HER session started: session=%s model=%s",
                            session_id,
                            event.get("model") or self._claw_model(),
                        )
                kind = str(event.get("kind") or "")
                if kind == "task_acknowledgement":
                    persona_source = self._her_persona_source()
                    self._persist_persona_audit(
                        request_id,
                        report_type="acknowledgement",
                        renderer_attempted=False,
                        renderer_succeeded=False,
                        model_authored=True,
                        validation_outcome="model_authored_event_received",
                        failure_reason=None,
                        **persona_source.audit_fields(),
                    )
                    self.logger.info(
                        "HER acknowledgement received: text=%s",
                        str(event.get("text") or "")[:500],
                    )
                elif kind == "task_plan":
                    if str(event.get("phase") or "update") != "initial":
                        frame = (
                            event.get("frame")
                            if isinstance(event.get("frame"), Mapping)
                            else {}
                        )
                        commentary = str(
                            event.get("commentary")
                            or frame.get("commentary")
                            or frame.get("acknowledgement")
                            or ""
                        ).strip()
                        if commentary:
                            persona_source = self._her_persona_source()
                            self._persist_persona_audit(
                                request_id,
                                report_type="commentary",
                                renderer_attempted=False,
                                renderer_succeeded=False,
                                model_authored=True,
                                validation_outcome=(
                                    "model_authored_replan_event_received"
                                ),
                                failure_reason=None,
                                **persona_source.audit_fields(),
                            )
                    self.logger.info(
                        "HER task plan received: phase=%s frame=%s",
                        event.get("phase") or "unknown",
                        json.dumps(event.get("frame") or {}, ensure_ascii=False)[:4000],
                    )
                elif kind == "independent_review":
                    review = (
                        event.get("review")
                        if isinstance(event.get("review"), Mapping)
                        else {}
                    )
                    self.logger.info(
                        "HER independent review: gate=%s revision_round=%s decision=%s summary=%s",
                        event.get("gate") or "unknown",
                        event.get("revision_round") or 0,
                        review.get("decision") or "unknown",
                        redact_secret_text(
                            str(review.get("summary") or event.get("summary") or "")
                        )[:2000],
                    )
                elif kind == "control_invocation":
                    request = (
                        event.get("request")
                        if isinstance(event.get("request"), Mapping)
                        else {}
                    )
                    usage = (
                        event.get("usage")
                        if isinstance(event.get("usage"), Mapping)
                        else {}
                    )
                    self.logger.info(
                        "HER control invocation: stage=%s gate=%s revision_round=%s "
                        "format_attempt=%s outcome=%s allow_tools=%s input_tokens=%s output_tokens=%s",
                        event.get("stage") or "unknown",
                        event.get("gate") or "unknown",
                        event.get("revision_round") or 0,
                        event.get("format_attempt") or 0,
                        event.get("outcome") or "unknown",
                        bool(request.get("allow_tools")),
                        usage.get("input_tokens") or 0,
                        usage.get("output_tokens") or 0,
                    )
                elif kind == "max_plus_checkpoint":
                    self.logger.info(
                        "HER MAX+ checkpoint: phase=%s budget=%s stop_reason=%s",
                        event.get("phase") or "unknown",
                        json.dumps(event.get("budget") or {}, ensure_ascii=False),
                        event.get("stop_reason") or "none",
                    )
                elif kind == "provider_stop_reason":
                    self.logger.info(
                        "HER provider termination received: reason=%s",
                        event.get("reason") or "unknown",
                    )
                elif kind == "semantic_compaction":
                    self.logger.info(
                        "HER semantic compaction: request=%s session=%s status=%s "
                        "trigger_phase=%s estimated_input_tokens=%s removed=%s "
                        "timeout_seconds=%s timeout_source=%s elapsed_ms=%s "
                        "original_context_unchanged=%s will_continue=%s reason=%s",
                        event.get("request_id") or request_id,
                        event.get("session_id") or "unknown",
                        event.get("status") or "unknown",
                        event.get("trigger_phase") or "unknown",
                        event.get("estimated_input_tokens") or 0,
                        event.get("removed_message_count") or 0,
                        event.get("timeout_seconds") or 0,
                        event.get("timeout_source") or "unknown",
                        event.get("elapsed_ms") or 0,
                        bool(event.get("original_context_unchanged")),
                        bool(event.get("will_continue")),
                        str(event.get("reason") or "")[:1000],
                    )
                elif kind == "terminal_diagnostic":
                    self.logger.warning(
                        "HER terminal diagnostic: classification=%s action=%s provider_stop_reason=%s",
                        event.get("classification") or "unknown",
                        event.get("action") or "unknown",
                        event.get("provider_stop_reason") or "unknown",
                    )
                elif kind in {"tool_call", "tool_start"}:
                    self.logger.info(
                        "HER tool started: iteration=%s id=%s name=%s summary=%s",
                        event.get("iteration") or "unknown",
                        event.get("id") or "unknown",
                        event.get("name") or "unknown",
                        redact_secret_text(str(event.get("summary") or ""))[:1000],
                    )
                elif kind == "tool_end":
                    log_method = (
                        self.logger.warning
                        if event.get("is_error")
                        else self.logger.info
                    )
                    log_method(
                        "HER tool finished: iteration=%s id=%s name=%s is_error=%s "
                        "output_chars=%s output_preview=%s",
                        event.get("iteration") or "unknown",
                        event.get("id") or "unknown",
                        event.get("name") or "unknown",
                        bool(event.get("is_error")),
                        event.get("output_chars") or 0,
                        redact_secret_text(str(event.get("output_preview") or ""))[
                            :1000
                        ],
                    )
                if on_stream_event is not None:
                    stream_events = _claw_jsonl_to_stream_events(
                        event,
                        commentary_prompt=commentary_prompt,
                        request_id=request_id,
                        source_index=source_index,
                    )
                    if kind == "task_acknowledgement":
                        pending_acknowledgement = next(
                            (
                                stream_event
                                for stream_event in stream_events
                                if stream_event.kind == KIND_ACKNOWLEDGEMENT
                            ),
                            None,
                        )
                        stream_events = [
                            stream_event
                            for stream_event in stream_events
                            if stream_event.kind != KIND_ACKNOWLEDGEMENT
                        ]
                        if initial_direct_response is not None:
                            await flush_pending_acknowledgement()
                    elif kind == "task_plan" and str(
                        event.get("phase") or "update"
                    ) == "initial":
                        initial_direct_response = _her_task_frame_is_direct_response(
                            event
                        )
                        await flush_pending_acknowledgement()
                    elif kind == "run_finished":
                        if initial_direct_response is None:
                            initial_direct_response = False
                        await flush_pending_acknowledgement()
                    for stream_event in stream_events:
                        await emit_stream_event(stream_event)
            if initial_direct_response is None:
                initial_direct_response = False
            await flush_pending_acknowledgement()

        async def read_stderr() -> None:
            assert proc.stderr is not None
            async for line in iter_stream_lines(proc.stderr):
                stderr_chunks.append(line)
                self._touch_activity()
                if activity_state is not None:
                    activity_state[0] = time.monotonic()

        async def write_stdin() -> None:
            if proc.stdin is None:
                return
            try:
                if stdin_data:
                    proc.stdin.write(stdin_data)
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                proc.stdin.close()
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    await proc.stdin.wait_closed()

        await asyncio.gather(write_stdin(), read_stdout(), read_stderr())
        await proc.wait()
        return b"".join(stdout_chunks), b"".join(stderr_chunks)

    def _persist_stream_json_line(self, line: bytes) -> None:
        """Persist Claw's complete local stream before any event summarisation."""
        if not line:
            return
        path = self.config.workspace_dir / "claw_exec_events.jsonl"
        with path.open("ab") as stream_log:
            stream_log.write(line)
            if not line.endswith(b"\n"):
                stream_log.write(b"\n")
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    def _persist_control_event(self, request_id: str, event: Mapping[str, Any]) -> None:
        """Correlate control and compaction records with their HASHI request."""
        if str(event.get("kind") or "") not in {
            "task_plan",
            "independent_review",
            "control_invocation",
            "max_plus_checkpoint",
            "semantic_compaction",
        }:
            return
        path = self.config.workspace_dir / "claw_control_events.jsonl"
        record = {"request_id": request_id, "event": event}
        with path.open("a", encoding="utf-8") as control_log:
            control_log.write(json.dumps(record, ensure_ascii=False) + "\n")
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        if str(event.get("kind") or "") == "max_plus_checkpoint":
            state_dir = self.config.workspace_dir / "backend_state"
            state_dir.mkdir(parents=True, exist_ok=True)
            checkpoint_path = state_dir / "claw_max_plus_checkpoint.json"
            checkpoint_tmp = state_dir / "claw_max_plus_checkpoint.json.tmp"
            checkpoint_tmp.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            checkpoint_tmp.replace(checkpoint_path)

    def _persist_stream_diagnostic_event(
        self,
        request_id: str,
        event: Mapping[str, Any],
    ) -> None:
        kind = str(event.get("kind") or "")
        is_failed_terminal = (
            kind == "run_finished"
            and str(event.get("completion_status") or "").lower() == "error"
        )
        if not is_failed_terminal and kind != "terminal_diagnostic":
            return
        self._persist_diagnostic_record(
            {
                "kind": "stream_event",
                "request_id": request_id,
                "event": dict(event),
            }
        )
