"""HER-only Ultra effort contracts and orchestration.

Ultra is intentionally implemented inside the HER backend.  HASHI submits one
backend request and receives one backend response; the planner, isolated worker
sessions, evidence collection, and assembly remain private to HER.

This module contains no Nagare or HChat dependency.  It also does not launch a
backend itself: :class:`HERUltraOrchestrator` receives narrow primary and worker
executor callbacks from :mod:`adapters.her`, keeping process/session ownership
with the HER adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from adapters.stream_events import (
    DELIVERY_TECHNICAL,
    DELIVERY_USER_COMMENTARY,
    KIND_COMMENTARY,
    KIND_ERROR,
    KIND_PROGRESS,
    KIND_VALIDATION,
    StreamCallback,
    StreamEvent,
)

HER_ULTRA_EFFORT = "ultra"
HER_ULTRA_MAX_CONCURRENT_SUBAGENTS = 10
HER_ULTRA_MAX_SUBTASKS = 32
HER_ULTRA_STATE_VERSION = 1
HER_ULTRA_SINGLE_AGENT_EFFORTS = frozenset(
    {"low", "medium", "high", "xhigh", "max", "max+"}
)
HER_ULTRA_WORKSPACE_STRATEGIES = frozenset(
    {"shared_read_only", "isolated_worktree", "immutable_snapshot"}
)
_SUBTASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_STATUS_VALUES = frozenset({"completed", "blocked", "failed", "requires_user_input"})
_TRANSIENT_WORKER_ERROR_TYPES = frozenset(
    {"connection", "provider_unavailable", "rate_limit", "timeout", "transport"}
)


class HERUltraError(RuntimeError):
    """Base class for Ultra contract and runtime failures."""


class HERUltraContractError(HERUltraError):
    """Raised when a model-authored plan or result violates its contract."""

    def __init__(self, message: str, *, errors: Sequence[str] = ()) -> None:
        self.errors = tuple(str(item) for item in errors if str(item))
        detail = "; ".join(self.errors)
        super().__init__(f"{message}: {detail}" if detail else message)


class HERUltraCancelled(HERUltraError):
    """Raised when the run cancellation generation invalidates more work."""


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", "").strip()
    if len(text) <= limit:
        return text
    half = max(1, (limit - 40) // 2)
    return f"{text[:half]}\n…[HER Ultra text truncated]…\n{text[-half:]}"


def _string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise HERUltraContractError(f"{field_name} must be a list")
    result = tuple(str(item).strip() for item in value if str(item).strip())
    if len(result) != len(value):
        raise HERUltraContractError(f"{field_name} contains an empty value")
    return result


def extract_json_object(text: str, *, max_chars: int = 2_000_000) -> dict[str, Any]:
    """Extract the last valid JSON object from bounded model output."""

    candidate = str(text or "")
    if len(candidate) > max_chars:
        raise HERUltraContractError("structured output exceeds the size limit")
    decoder = json.JSONDecoder()
    objects: list[tuple[int, int, dict[str, Any]]] = []
    for match in re.finditer(r"\{", candidate):
        try:
            value, relative_end = decoder.raw_decode(candidate[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            objects.append((match.start(), match.start() + relative_end, value))
    if not objects:
        raise HERUltraContractError("model output did not contain a valid JSON object")
    # The intended object has the latest absolute end.  A nested object can end
    # at the same character as its parent, so prefer the earliest start on ties.
    return max(objects, key=lambda item: (item[1], -item[0]))[2]


@dataclass(frozen=True)
class HERUltraConfig:
    """Bounded execution policy for one HER Ultra adapter."""

    enabled: bool = True
    max_concurrent_subagents: int = HER_ULTRA_MAX_CONCURRENT_SUBAGENTS
    primary_inner_effort: str = "high"
    subagent_default_effort: str = "high"
    subagent_timeout_sec: int = 300
    subagent_retry_limit: int = 1
    max_plan_revisions: int = 2
    max_subtasks: int = HER_ULTRA_MAX_SUBTASKS
    max_assembly_chars: int = 200_000
    primary_model: str = ""
    allowed_models: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        primary_model: str = "",
        allowed_models: Sequence[str] = (),
    ) -> HERUltraConfig:
        raw = dict(value or {})

        def bounded_int(key: str, default: int, lower: int, upper: int) -> int:
            try:
                parsed = int(raw.get(key, default))
            except (TypeError, ValueError) as exc:
                raise HERUltraContractError(
                    f"invalid Ultra config value: {key}"
                ) from exc
            return max(lower, min(parsed, upper))

        def boolean(key: str, default: bool) -> bool:
            value = raw.get(key, default)
            if isinstance(value, bool):
                return value
            if isinstance(value, int) and value in {0, 1}:
                return bool(value)
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                if normalized in {"0", "false", "no", "off"}:
                    return False
            raise HERUltraContractError(f"invalid Ultra config value: {key}")

        primary_effort = str(raw.get("primary_inner_effort") or "high").strip().lower()
        worker_effort = (
            str(raw.get("subagent_default_effort") or "high").strip().lower()
        )
        for key, effort in (
            ("primary_inner_effort", primary_effort),
            ("subagent_default_effort", worker_effort),
        ):
            if effort not in HER_ULTRA_SINGLE_AGENT_EFFORTS:
                raise HERUltraContractError(f"{key} must be a single-agent HER effort")

        configured_models = raw.get("allowed_models")
        if isinstance(configured_models, str):
            configured_models = [
                item.strip() for item in configured_models.split(",") if item.strip()
            ]
        elif not isinstance(configured_models, (list, tuple)):
            configured_models = []
        models = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in (*allowed_models, *configured_models)
                if str(item).strip()
            )
        )
        resolved_primary = str(raw.get("primary_model") or primary_model or "").strip()
        if resolved_primary and resolved_primary not in models:
            models = (resolved_primary, *models)
        return cls(
            enabled=boolean("enabled", True),
            max_concurrent_subagents=bounded_int(
                "max_concurrent_subagents", HER_ULTRA_MAX_CONCURRENT_SUBAGENTS, 1, 10
            ),
            primary_inner_effort=primary_effort,
            subagent_default_effort=worker_effort,
            subagent_timeout_sec=bounded_int("subagent_timeout_sec", 300, 1, 86_400),
            subagent_retry_limit=bounded_int("subagent_retry_limit", 1, 0, 3),
            max_plan_revisions=bounded_int("max_plan_revisions", 2, 1, 2),
            max_subtasks=bounded_int(
                "max_subtasks",
                HER_ULTRA_MAX_SUBTASKS,
                1,
                HER_ULTRA_MAX_SUBTASKS,
            ),
            max_assembly_chars=bounded_int(
                "max_assembly_chars", 200_000, 10_000, 1_000_000
            ),
            primary_model=resolved_primary,
            allowed_models=models,
        )


@dataclass(frozen=True)
class HERUltraAuthorityEnvelope:
    """Runtime-owned authority copied into task validation, never model-owned."""

    digest: str
    permission_mode: str
    access_root: str
    allowed_tools: tuple[str, ...]
    write_enabled: bool = False
    approval_bindings: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        *,
        permission_mode: str,
        access_root: str,
        allowed_tools: Sequence[str],
        write_enabled: bool = False,
        approval_bindings: Sequence[str] = (),
    ) -> HERUltraAuthorityEnvelope:
        normalized = {
            "permission_mode": str(permission_mode or "read-only"),
            "access_root": str(access_root or ""),
            "allowed_tools": sorted({str(item) for item in allowed_tools if str(item)}),
            "write_enabled": bool(write_enabled),
            "approval_bindings": sorted(
                {str(item) for item in approval_bindings if str(item)}
            ),
        }
        digest = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return cls(
            digest=f"sha256:{digest}",
            permission_mode=normalized["permission_mode"],
            access_root=normalized["access_root"],
            allowed_tools=tuple(normalized["allowed_tools"]),
            write_enabled=normalized["write_enabled"],
            approval_bindings=tuple(normalized["approval_bindings"]),
        )


@dataclass(frozen=True)
class HERUltraSubtask:
    subtask_id: str
    title: str
    objective: str
    depends_on: tuple[str, ...]
    model: str
    model_class: str
    effort: str
    deliverables: tuple[str, ...]
    acceptance: tuple[str, ...]
    optional: bool
    retry_safe: bool
    workspace_strategy: str


@dataclass(frozen=True)
class HERUltraPlan:
    plan_id: str
    parent_request_id: str
    authoritative_goal: str
    authority_envelope_digest: str
    revision: int
    ultra_not_beneficial: bool
    direct_response: str
    subtasks: tuple[HERUltraSubtask, ...]
    assembly_plan: Mapping[str, Any]


class HERUltraTaskContractValidator:
    """Deterministically validates planner output before any dispatch."""

    def __init__(self, config: HERUltraConfig) -> None:
        self.config = config

    def parse_plan(
        self,
        text: str,
        *,
        authoritative_goal: str,
        parent_request_id: str,
        authority: HERUltraAuthorityEnvelope,
        revision: int,
    ) -> HERUltraPlan:
        return self.validate_plan(
            extract_json_object(text),
            authoritative_goal=authoritative_goal,
            parent_request_id=parent_request_id,
            authority=authority,
            revision=revision,
        )

    def validate_plan(
        self,
        payload: Mapping[str, Any],
        *,
        authoritative_goal: str,
        parent_request_id: str,
        authority: HERUltraAuthorityEnvelope,
        revision: int,
    ) -> HERUltraPlan:
        errors: list[str] = []
        direct = bool(payload.get("ultra_not_beneficial", False))
        direct_response = _bounded_text(payload.get("direct_response"), 100_000)
        raw_subtasks = payload.get("subtasks")
        if not isinstance(raw_subtasks, list):
            errors.append("subtasks must be a list")
            raw_subtasks = []
        if len(raw_subtasks) > self.config.max_subtasks:
            errors.append(
                f"subtasks exceeds configured maximum {self.config.max_subtasks}"
            )
        if direct and not direct_response:
            errors.append("ultra_not_beneficial requires direct_response")
        if direct and raw_subtasks:
            errors.append("ultra_not_beneficial cannot include subtasks")
        if not direct and not raw_subtasks:
            errors.append("a non-trivial Ultra plan requires at least one subtask")

        subtasks: list[HERUltraSubtask] = []
        seen_ids: set[str] = set()
        for index, raw in enumerate(raw_subtasks):
            if not isinstance(raw, Mapping):
                errors.append(f"subtasks[{index}] must be an object")
                continue
            task_errors: list[str] = []
            subtask_id = str(raw.get("id") or raw.get("subtask_id") or "").strip()
            if not _SUBTASK_ID_RE.fullmatch(subtask_id):
                task_errors.append("id is missing or invalid")
            elif subtask_id in seen_ids:
                task_errors.append("id is duplicated")
            seen_ids.add(subtask_id)
            title = str(raw.get("title") or subtask_id).strip()
            objective = str(raw.get("objective") or "").strip()
            if not objective:
                task_errors.append("objective is required")
            try:
                depends_on = _string_list(
                    raw.get("depends_on", []), field_name="depends_on"
                )
                deliverables = _string_list(
                    raw.get("deliverables", []), field_name="deliverables"
                )
                acceptance = _string_list(
                    raw.get("acceptance", []), field_name="acceptance"
                )
            except HERUltraContractError as exc:
                task_errors.append(str(exc))
                depends_on, deliverables, acceptance = (), (), ()
            if subtask_id and subtask_id in depends_on:
                task_errors.append("a subtask cannot depend on itself")
            workspace_strategy = str(
                raw.get("workspace_strategy") or "shared_read_only"
            ).strip()
            if workspace_strategy not in HER_ULTRA_WORKSPACE_STRATEGIES:
                task_errors.append("workspace_strategy is invalid")
            if not deliverables:
                task_errors.append("deliverables cannot be empty")
            if not acceptance:
                task_errors.append("acceptance cannot be empty")

            model = str(raw.get("model") or "").strip()
            model_class = str(raw.get("model_class") or "current").strip().lower()
            if model_class not in {"pro", "flash", "current", "primary"}:
                task_errors.append("model_class is invalid")
            if (
                model
                and self.config.allowed_models
                and model not in self.config.allowed_models
            ):
                task_errors.append(f"model is not allowed for this HER Agent: {model}")
            effort = (
                str(raw.get("effort") or self.config.subagent_default_effort)
                .strip()
                .lower()
            )
            if effort not in HER_ULTRA_SINGLE_AGENT_EFFORTS:
                task_errors.append("worker effort must be a single-agent HER effort")

            if task_errors:
                errors.extend(f"subtasks[{index}] {item}" for item in task_errors)
                continue
            subtasks.append(
                HERUltraSubtask(
                    subtask_id=subtask_id,
                    title=title,
                    objective=objective,
                    depends_on=depends_on,
                    model=model,
                    model_class=model_class,
                    effort=effort,
                    deliverables=deliverables,
                    acceptance=acceptance,
                    optional=bool(raw.get("optional", False)),
                    retry_safe=bool(raw.get("retry_safe", True)),
                    workspace_strategy=workspace_strategy,
                )
            )

        task_ids = {task.subtask_id for task in subtasks}
        for task in subtasks:
            missing = set(task.depends_on) - task_ids
            if missing:
                errors.append(
                    f"subtask {task.subtask_id} has unknown dependencies: "
                    + ", ".join(sorted(missing))
                )
        if not errors and self._has_cycle(subtasks):
            errors.append("subtask dependency graph contains a cycle")
        if errors:
            raise HERUltraContractError("invalid HER Ultra plan", errors=errors)

        plan_id = str(payload.get("plan_id") or "").strip()
        if not plan_id:
            plan_id = f"{parent_request_id}:ultra:plan:{revision}"
        assembly = payload.get("assembly_plan")
        if not isinstance(assembly, Mapping):
            assembly = {"strategy": "evidence_first"}
        return HERUltraPlan(
            plan_id=plan_id,
            parent_request_id=parent_request_id,
            authoritative_goal=authoritative_goal,
            authority_envelope_digest=authority.digest,
            revision=revision,
            ultra_not_beneficial=direct,
            direct_response=direct_response,
            subtasks=tuple(subtasks),
            assembly_plan=dict(assembly),
        )

    @staticmethod
    def _has_cycle(subtasks: Sequence[HERUltraSubtask]) -> bool:
        dependencies = {task.subtask_id: set(task.depends_on) for task in subtasks}
        ready = [task_id for task_id, deps in dependencies.items() if not deps]
        visited = 0
        while ready:
            task_id = ready.pop()
            visited += 1
            for candidate, deps in dependencies.items():
                if task_id in deps:
                    deps.remove(task_id)
                    if not deps:
                        ready.append(candidate)
        return visited != len(dependencies)


@dataclass(frozen=True)
class HERUltraInvocationResult:
    text: str
    is_success: bool = True
    error: str = ""
    error_type: str = ""
    retryable: bool = False
    session_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    tool_call_count: int = 0
    tool_loop_count: int = 0
    duration_ms: float = 0.0
    cost_usd: float | None = None


@dataclass(frozen=True)
class HERUltraPrimaryExecutionSpec:
    run_id: str
    parent_request_id: str
    phase: str
    revision: int
    request_id: str
    prompt: str
    resume_session_id: str
    model: str
    effort: str
    cancellation_generation: int


@dataclass(frozen=True)
class HERUltraWorkerExecutionSpec:
    run_id: str
    parent_request_id: str
    subtask_id: str
    dispatch_id: str
    attempt_id: str
    attempt: int
    request_id: str
    prompt: str
    model: str
    model_class: str
    effort: str
    permission_mode: str
    allowed_tools: tuple[str, ...]
    workspace_strategy: str
    workspace: str
    timeout_sec: int
    retry_safe: bool
    cancellation_generation: int


@dataclass(frozen=True)
class HERUltraWorkerResult:
    subtask_id: str
    result_id: str
    status: str
    claims: tuple[Any, ...]
    evidence: tuple[Any, ...]
    artifacts: tuple[Any, ...]
    validation: tuple[Any, ...]
    uncertainty: str
    unresolved_items: tuple[str, ...]
    retry_safe: bool
    error: str = ""
    error_type: str = ""
    transient: bool = False
    requires_user_input: Mapping[str, Any] | None = None
    attempt: int = 1
    model: str = ""
    usage: Mapping[str, Any] = field(default_factory=dict)
    # Keep the complete structured worker report for Primary assembly. Worker
    # providers do not consistently use the canonical result field names.
    raw_payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def blocked(self) -> bool:
        return self.status == "blocked"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_invocation(
        cls,
        invocation: HERUltraInvocationResult,
        *,
        subtask: HERUltraSubtask,
        result_id: str,
        attempt: int,
    ) -> HERUltraWorkerResult:
        usage = {
            "input_tokens": max(0, int(invocation.input_tokens)),
            "output_tokens": max(0, int(invocation.output_tokens)),
            "thinking_tokens": max(0, int(invocation.thinking_tokens)),
            "tool_call_count": max(0, int(invocation.tool_call_count)),
            "tool_loop_count": max(0, int(invocation.tool_loop_count)),
            "duration_ms": max(0.0, float(invocation.duration_ms)),
            "cost_usd": invocation.cost_usd,
        }
        if not invocation.is_success:
            return cls(
                subtask_id=subtask.subtask_id,
                result_id=result_id,
                status="failed",
                claims=(),
                evidence=(),
                artifacts=(),
                validation=(),
                uncertainty="",
                unresolved_items=(),
                retry_safe=subtask.retry_safe,
                error=_bounded_text(invocation.error or "HER worker failed", 4_000),
                error_type=str(invocation.error_type or "worker_error"),
                transient=bool(invocation.retryable),
                attempt=attempt,
                model=invocation.model,
                usage=usage,
            )

        try:
            payload = extract_json_object(invocation.text)
        except HERUltraContractError:
            plain_text = str(invocation.text or "").strip()
            if plain_text:
                lowered = plain_text.lower()
                blocked = lowered.startswith("blocked") or any(
                    marker in lowered
                    for marker in (
                        "permission denied",
                        "requires danger-full-access",
                        "requires workspace-write",
                        "escapes workspace boundary",
                    )
                )
                return cls(
                    subtask_id=subtask.subtask_id,
                    result_id=result_id,
                    status="blocked" if blocked else "completed",
                    claims=(plain_text,),
                    evidence=(),
                    artifacts=(),
                    validation=(),
                    uncertainty="",
                    unresolved_items=(),
                    retry_safe=subtask.retry_safe,
                    error=plain_text if blocked else "",
                    error_type="permission_blocked" if blocked else "",
                    attempt=attempt,
                    model=invocation.model,
                    usage=usage,
                )
            return cls(
                subtask_id=subtask.subtask_id,
                result_id=result_id,
                status="failed",
                claims=(),
                evidence=(),
                artifacts=(),
                validation=(),
                uncertainty="",
                unresolved_items=(),
                retry_safe=subtask.retry_safe,
                error="HER worker returned no result",
                error_type="malformed_output",
                transient=False,
                attempt=attempt,
                model=invocation.model,
                usage=usage,
            )
        returned_status = str(payload.get("status") or "completed").strip().lower()
        status = returned_status if returned_status in _STATUS_VALUES else "completed"
        claims_raw = payload.get("claims") or payload.get("result") or []
        evidence_raw = (
            payload.get("evidence")
            or payload.get("sources")
            or payload.get("source")
            or []
        )
        artifacts_raw = payload.get("artifacts") or []
        if not isinstance(claims_raw, list):
            claims_raw = [str(claims_raw)] if str(claims_raw).strip() else []
        if not isinstance(evidence_raw, list):
            evidence_raw = []
        if not isinstance(artifacts_raw, list):
            artifacts_raw = []
        claims = tuple(claims_raw)
        evidence = tuple(evidence_raw)
        artifacts = tuple(artifacts_raw)
        validation_raw = (
            payload.get("validation") or payload.get("validation_performed") or []
        )
        if not isinstance(validation_raw, list):
            validation_raw = (
                [str(validation_raw)] if str(validation_raw).strip() else []
            )
        validation = tuple(validation_raw)
        unresolved_raw = (
            payload.get("unresolved_items") or payload.get("unresolved") or []
        )
        if not isinstance(unresolved_raw, list):
            unresolved_raw = (
                [str(unresolved_raw)] if str(unresolved_raw).strip() else []
            )
        interaction_raw = payload.get("requires_user_input")
        interaction = (
            dict(interaction_raw) if isinstance(interaction_raw, Mapping) else None
        )
        if status == "requires_user_input" and (
            interaction is None or not str(interaction.get("prompt") or "").strip()
        ):
            status = "failed"
        if status == "requires_user_input" and interaction is not None:
            interaction_kind = str(interaction.get("kind") or "question").lower()
            if interaction_kind not in {
                "choice",
                "confirmation",
                "continuation",
                "question",
            }:
                status = "failed"
            if interaction_kind == "choice":
                labels = interaction.get("labels")
                if (
                    not isinstance(labels, list)
                    or not labels
                    or not all(
                        isinstance(label, str) and label.strip() for label in labels
                    )
                ):
                    status = "failed"
        empty_completed_result = (
            status == "completed"
            and not any((claims, evidence, artifacts))
            and not any(
                value not in (None, "", [], {}, ())
                for key, value in payload.items()
                if key
                not in {
                    "status",
                    "subtask_id",
                    "retry_safe",
                    "validation",
                    "validation_performed",
                    "uncertainty",
                    "unresolved",
                    "unresolved_items",
                }
            )
        )
        if empty_completed_result:
            status = "failed"
        retry_safe = subtask.retry_safe and bool(
            payload.get("retry_safe", subtask.retry_safe)
        )
        error_type = (
            "malformed_output"
            if empty_completed_result
            or (status == "failed" and returned_status == "requires_user_input")
            else str(payload.get("error_type") or "")
        )
        return cls(
            subtask_id=subtask.subtask_id,
            result_id=result_id,
            status=status,
            claims=claims,
            evidence=evidence,
            artifacts=artifacts,
            validation=validation,
            uncertainty=_bounded_text(payload.get("uncertainty"), 4_000),
            unresolved_items=tuple(str(item) for item in unresolved_raw),
            retry_safe=retry_safe,
            error=_bounded_text(
                (
                    "completed worker result contains no deliverable payload"
                    if empty_completed_result
                    else "requires_user_input needs a valid prompt and choices"
                    if status == "failed" and returned_status == "requires_user_input"
                    else payload.get("error")
                ),
                4_000,
            ),
            error_type=error_type,
            transient=(
                empty_completed_result
                or (
                    bool(payload.get("transient", False))
                    and error_type.lower() in _TRANSIENT_WORKER_ERROR_TYPES
                )
            ),
            requires_user_input=interaction,
            attempt=attempt,
            model=invocation.model,
            usage=usage,
            raw_payload=dict(payload),
        )


class HERUltraRunLedger:
    """Durable, append-only transition journal plus atomic run snapshot."""

    def __init__(
        self,
        root: Path,
        *,
        run_id: str,
        parent_request_id: str,
        authoritative_goal: str,
    ) -> None:
        self.run_id = str(run_id)
        self.parent_request_id = str(parent_request_id)
        self.run_dir = Path(root) / self.run_id
        self.journal_path = self.run_dir / "transitions.jsonl"
        self.snapshot_path = self.run_dir / "state.json"
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "version": HER_ULTRA_STATE_VERSION,
            "run_id": self.run_id,
            "parent_request_id": self.parent_request_id,
            "authoritative_goal_sha256": hashlib.sha256(
                authoritative_goal.encode("utf-8", errors="replace")
            ).hexdigest(),
            "status": "created",
            "cancellation_generation": 0,
            "last_sequence": 0,
            "seen_event_ids": [],
            "primary": {},
            "subtasks": {},
            "errors": [],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._write_snapshot()

    @property
    def cancellation_generation(self) -> int:
        return int(self._state.get("cancellation_generation") or 0)

    @property
    def status(self) -> str:
        return str(self._state.get("status") or "unknown")

    def accepts_generation(self, generation: int) -> bool:
        return generation == self.cancellation_generation and self.status != "cancelled"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))

    def transition(
        self,
        *,
        event_id: str,
        entity: str,
        state: str,
        data: Mapping[str, Any] | None = None,
        cancellation_generation: int | None = None,
    ) -> bool:
        with self._lock:
            seen = set(self._state.get("seen_event_ids") or ())
            if event_id in seen:
                return False
            if (
                cancellation_generation is not None
                and cancellation_generation != self.cancellation_generation
            ):
                return False
            sequence = int(self._state.get("last_sequence") or 0) + 1
            now = time.time()
            payload = {
                "version": HER_ULTRA_STATE_VERSION,
                "run_id": self.run_id,
                "parent_request_id": self.parent_request_id,
                "sequence": sequence,
                "event_id": str(event_id),
                "entity": str(entity),
                "state": str(state),
                "cancellation_generation": self.cancellation_generation,
                "recorded_at": now,
                "data": dict(data or {}),
            }
            self._append_transition(payload)
            self._state["last_sequence"] = sequence
            self._state["updated_at"] = now
            seen.add(str(event_id))
            self._state["seen_event_ids"] = sorted(seen)
            if entity == "run":
                self._state["status"] = str(state)
            elif entity == "primary":
                self._state["primary"] = {"state": state, **dict(data or {})}
            elif entity.startswith("subtask:"):
                subtask_id = entity.split(":", 1)[1]
                self._state.setdefault("subtasks", {})[subtask_id] = {
                    "state": state,
                    **dict(data or {}),
                }
            if state == "failed":
                error = str((data or {}).get("error") or "")
                if error:
                    self._state.setdefault("errors", []).append(
                        {"event_id": event_id, "entity": entity, "error": error}
                    )
            self._write_snapshot()
            return True

    def cancel(self, reason: str) -> int:
        with self._lock:
            self._state["cancellation_generation"] = self.cancellation_generation + 1
            generation = self.cancellation_generation
        self.transition(
            event_id=f"{self.run_id}:run:cancel:{generation}",
            entity="run",
            state="cancelled",
            data={"reason": _bounded_text(reason, 2_000)},
            cancellation_generation=generation,
        )
        return generation

    def _append_transition(self, payload: Mapping[str, Any]) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.journal_path.chmod(0o600)

    def _write_snapshot(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_name(
            f".{self.snapshot_path.name}.tmp-{os.getpid()}-{time.time_ns()}"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(
                    self._state, handle, ensure_ascii=False, indent=2, sort_keys=True
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.snapshot_path)
            self.snapshot_path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class HERUltraOutcome:
    run_id: str
    status: str
    text: str
    is_success: bool
    error: str
    primary_session_id: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    tool_call_count: int
    tool_loop_count: int
    duration_ms: float
    cost_usd: float | None
    plan_revision: int
    subtask_count: int
    completed_subtasks: int
    pending_interaction: Mapping[str, Any] | None = None


PrimaryExecutor = Callable[
    [HERUltraPrimaryExecutionSpec], Awaitable[HERUltraInvocationResult]
]
WorkerExecutor = Callable[
    [HERUltraWorkerExecutionSpec], Awaitable[HERUltraInvocationResult]
]
PersonaCommentaryRenderer = Callable[[Mapping[str, Any]], Awaitable[str]]


class HERUltraOrchestrator:
    """Coordinate one HER-only Ultra request and return one assembled outcome."""

    def __init__(
        self,
        *,
        config: HERUltraConfig,
        ledger_root: Path,
        primary_executor: PrimaryExecutor,
        worker_executor: WorkerExecutor,
        on_stream_event: StreamCallback = None,
        run_id_factory: Callable[[], str] | None = None,
        persona_guidance: str = "",
        persona_commentary_renderer: PersonaCommentaryRenderer | None = None,
    ) -> None:
        self.config = config
        self.ledger_root = Path(ledger_root)
        self.primary_executor = primary_executor
        self.worker_executor = worker_executor
        self.on_stream_event = on_stream_event
        self.persona_guidance = _bounded_text(persona_guidance, 12_000)
        self.persona_commentary_renderer = persona_commentary_renderer
        self.run_id_factory = run_id_factory or (
            lambda: f"ultra-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}"
        )
        self._cancel_event = asyncio.Event()
        self._cancel_reason = ""
        self._inflight: set[asyncio.Task[Any]] = set()
        self._ledger: HERUltraRunLedger | None = None
        self._usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "tool_call_count": 0,
            "tool_loop_count": 0,
            "duration_ms": 0.0,
            "cost_usd": 0.0,
            "cost_known": True,
        }

    @property
    def run_id(self) -> str:
        return self._ledger.run_id if self._ledger is not None else ""

    def cancel(self, reason: str = "user_stop") -> None:
        self._cancel_reason = str(reason or "cancelled")
        self._cancel_event.set()
        if self._ledger is not None and self._ledger.status != "cancelled":
            self._ledger.cancel(self._cancel_reason)
        for task in tuple(self._inflight):
            task.cancel()

    async def run(
        self,
        *,
        authoritative_goal: str,
        parent_request_id: str,
        authority: HERUltraAuthorityEnvelope,
        initial_primary_session_id: str = "",
    ) -> HERUltraOutcome:
        started = time.perf_counter()
        run_id = self.run_id_factory()
        self._ledger = HERUltraRunLedger(
            self.ledger_root,
            run_id=run_id,
            parent_request_id=parent_request_id,
            authoritative_goal=authoritative_goal,
        )
        generation = self._ledger.cancellation_generation
        self._ledger.transition(
            event_id=f"{run_id}:run:started",
            entity="run",
            state="planning",
            data={"authority_envelope_digest": authority.digest},
            cancellation_generation=generation,
        )
        await self._emit(
            KIND_PROGRESS,
            "HER Ultra planning started",
            event_id=f"{run_id}:technical:planning:started",
            phase="planning",
        )
        validator = HERUltraTaskContractValidator(self.config)
        primary_session_id = str(initial_primary_session_id or "")
        plan: HERUltraPlan | None = None
        contract_error = ""
        revision = 0

        try:
            for revision in range(1, self.config.max_plan_revisions + 1):
                self._raise_if_cancelled(generation)
                prompt = self._planning_prompt(
                    authoritative_goal=authoritative_goal,
                    parent_request_id=parent_request_id,
                    authority=authority,
                    revision=revision,
                    prior_error=contract_error,
                )
                invocation = await self._invoke_primary(
                    phase="planning" if revision == 1 else "plan_correction",
                    revision=revision,
                    prompt=prompt,
                    parent_request_id=parent_request_id,
                    primary_session_id=primary_session_id,
                    generation=generation,
                )
                primary_session_id = invocation.session_id or primary_session_id
                if not invocation.is_success:
                    return await self._failed_outcome(
                        started=started,
                        status="failed",
                        error=invocation.error or "Primary planner failed",
                        primary_session_id=primary_session_id,
                        plan_revision=revision,
                    )
                try:
                    plan = validator.parse_plan(
                        invocation.text,
                        authoritative_goal=authoritative_goal,
                        parent_request_id=parent_request_id,
                        authority=authority,
                        revision=revision,
                    )
                except HERUltraContractError as exc:
                    contract_error = str(exc)
                    self._ledger.transition(
                        event_id=f"{run_id}:plan:{revision}:invalid",
                        entity="primary",
                        state="plan_invalid",
                        data={"revision": revision, "error": _bounded_text(exc, 8_000)},
                        cancellation_generation=generation,
                    )
                    await self._emit(
                        KIND_VALIDATION,
                        f"HER Ultra plan revision {revision} failed contract validation",
                        detail=_bounded_text(exc, 2_000),
                        event_id=f"{run_id}:technical:plan:{revision}:invalid",
                        phase="planning",
                    )
                    continue
                break

            if plan is None:
                error = contract_error or "Primary did not produce a valid Ultra plan"
                return await self._failed_outcome(
                    started=started,
                    status="failed",
                    error=error,
                    primary_session_id=primary_session_id,
                    plan_revision=revision,
                )

            self._ledger.transition(
                event_id=f"{run_id}:plan:{plan.revision}:accepted",
                entity="primary",
                state="plan_accepted",
                data={
                    "revision": plan.revision,
                    "plan_id": plan.plan_id,
                    "subtask_count": len(plan.subtasks),
                    "primary_session_id": primary_session_id,
                },
                cancellation_generation=generation,
            )
            await self._emit(
                KIND_PROGRESS,
                f"HER Ultra finished permission preflight and accepted {len(plan.subtasks)} subtasks.",
                event_id=f"{run_id}:commentary:plan:{plan.revision}:accepted",
                phase="planning",
            )
            if plan.ultra_not_beneficial:
                direct_response = await self._invoke_primary(
                    phase="direct_response",
                    revision=plan.revision,
                    prompt=self._direct_response_prompt(plan),
                    parent_request_id=parent_request_id,
                    primary_session_id=primary_session_id,
                    generation=generation,
                )
                primary_session_id = direct_response.session_id or primary_session_id
                if not direct_response.is_success or not direct_response.text.strip():
                    return await self._failed_outcome(
                        started=started,
                        status="failed",
                        error=direct_response.error
                        or "Primary direct response renderer returned no answer",
                        primary_session_id=primary_session_id,
                        plan_revision=plan.revision,
                    )
                self._ledger.transition(
                    event_id=f"{run_id}:run:direct",
                    entity="run",
                    state="completed",
                    data={
                        "mode": "direct",
                        "plan_revision": plan.revision,
                        "primary_session_id": primary_session_id,
                    },
                    cancellation_generation=generation,
                )
                return self._outcome(
                    started=started,
                    status="completed",
                    text=direct_response.text,
                    is_success=True,
                    error="",
                    primary_session_id=primary_session_id,
                    plan_revision=plan.revision,
                    subtask_count=0,
                    completed_subtasks=0,
                )

            # Match the single-agent HER acknowledgement contract: do not
            # announce work until planning has established that this is not a
            # direct response.  Simple messages therefore produce only their
            # final Persona reply.
            await self._emit_persona_commentary(
                {
                    "phase": "planning_started",
                    "message_type": "acknowledgement",
                },
                event_id=f"{run_id}:persona:planning:started",
                phase="planning",
            )
            await self._emit_persona_commentary(
                {
                    "phase": "plan_accepted",
                    "subtask_count": len(plan.subtasks),
                },
                event_id=f"{run_id}:persona:plan:{plan.revision}:accepted",
                phase="planning",
            )

            self._ledger.transition(
                event_id=f"{run_id}:run:dispatching",
                entity="run",
                state="running",
                data={"subtask_count": len(plan.subtasks)},
                cancellation_generation=generation,
            )
            results = await self._execute_dag(
                plan,
                authority=authority,
                generation=generation,
            )
            pending_interaction = next(
                (
                    result.requires_user_input
                    for task_id, result in results.items()
                    if result.status == "requires_user_input"
                    and not next(
                        task.optional
                        for task in plan.subtasks
                        if task.subtask_id == task_id
                    )
                    and result.requires_user_input is not None
                ),
                None,
            )
            if pending_interaction is not None:
                interaction_id = f"{run_id}:interaction:1"
                interaction = {**pending_interaction, "interaction_id": interaction_id}
                interaction_response = await self._invoke_primary(
                    phase="interaction",
                    revision=plan.revision,
                    prompt=self._interaction_prompt(plan, results, interaction),
                    parent_request_id=parent_request_id,
                    primary_session_id=primary_session_id,
                    generation=generation,
                )
                primary_session_id = (
                    interaction_response.session_id or primary_session_id
                )
                if (
                    not interaction_response.is_success
                    or not interaction_response.text.strip()
                ):
                    return await self._failed_outcome(
                        started=started,
                        status="failed",
                        error=interaction_response.error
                        or "Primary interaction rendering returned no question",
                        primary_session_id=primary_session_id,
                        plan_revision=plan.revision,
                        subtask_count=len(plan.subtasks),
                        completed_subtasks=sum(
                            result.completed for result in results.values()
                        ),
                    )
                self._ledger.transition(
                    event_id=f"{run_id}:run:waiting_user",
                    entity="run",
                    state="waiting_user",
                    data={
                        "pending_interaction": interaction,
                        "primary_session_id": primary_session_id,
                    },
                    cancellation_generation=generation,
                )
                return self._outcome(
                    started=started,
                    status="incomplete",
                    text=interaction_response.text,
                    is_success=True,
                    error="",
                    primary_session_id=primary_session_id,
                    plan_revision=plan.revision,
                    subtask_count=len(plan.subtasks),
                    completed_subtasks=sum(
                        result.completed for result in results.values()
                    ),
                    pending_interaction=interaction,
                )

            required_results = {
                task.subtask_id: results[task.subtask_id]
                for task in plan.subtasks
                if not task.optional and task.subtask_id in results
            }
            completed_required = sum(
                result.completed for result in required_results.values()
            )
            if required_results and completed_required == 0:
                failures = "; ".join(
                    f"{task_id}: {result.error or result.status}"
                    for task_id, result in required_results.items()
                )
                return await self._failed_outcome(
                    started=started,
                    status="failed",
                    error=(
                        "All required Ultra workers failed before producing usable "
                        f"evidence. Primary assembly was not started. {failures}"
                    ),
                    primary_session_id=primary_session_id,
                    plan_revision=plan.revision,
                    subtask_count=len(plan.subtasks),
                    completed_subtasks=0,
                )

            self._raise_if_cancelled(generation)
            self._ledger.transition(
                event_id=f"{run_id}:assembly:started",
                entity="primary",
                state="assembling",
                data={"result_count": len(results)},
                cancellation_generation=generation,
            )
            await self._emit(
                KIND_PROGRESS,
                "HER Ultra finished the worker stage and is assembling the verified results.",
                event_id=f"{run_id}:commentary:assembly:started",
                phase="assembly",
            )
            await self._emit_persona_commentary(
                {
                    "phase": "assembly_started",
                    "subtask_count": len(plan.subtasks),
                    "completed_subtasks": sum(
                        result.completed for result in results.values()
                    ),
                    "failed_subtasks": sum(
                        result.status == "failed" for result in results.values()
                    ),
                    "blocked_subtasks": sum(
                        result.blocked for result in results.values()
                    ),
                },
                event_id=f"{run_id}:persona:assembly:started",
                phase="assembly",
            )
            assembly = await self._invoke_primary(
                phase="assembly",
                revision=plan.revision,
                prompt=self._assembly_prompt(plan, results),
                parent_request_id=parent_request_id,
                primary_session_id=primary_session_id,
                generation=generation,
            )
            primary_session_id = assembly.session_id or primary_session_id
            if not assembly.is_success or not assembly.text.strip():
                return await self._failed_outcome(
                    started=started,
                    status="failed",
                    error=assembly.error or "Primary assembly returned no final answer",
                    primary_session_id=primary_session_id,
                    plan_revision=plan.revision,
                    subtask_count=len(plan.subtasks),
                    completed_subtasks=sum(
                        result.completed for result in results.values()
                    ),
                )
            self._ledger.transition(
                event_id=f"{run_id}:run:completed",
                entity="run",
                state="completed",
                data={
                    "plan_revision": plan.revision,
                    "subtask_count": len(plan.subtasks),
                    "completed_subtasks": sum(
                        result.completed for result in results.values()
                    ),
                    "primary_session_id": primary_session_id,
                },
                cancellation_generation=generation,
            )
            await self._emit(
                KIND_VALIDATION,
                "HER Ultra assembly completed",
                event_id=f"{run_id}:technical:assembly:completed",
                phase="assembly",
            )
            return self._outcome(
                started=started,
                status="completed",
                text=assembly.text,
                is_success=True,
                error="",
                primary_session_id=primary_session_id,
                plan_revision=plan.revision,
                subtask_count=len(plan.subtasks),
                completed_subtasks=sum(result.completed for result in results.values()),
            )
        except HERUltraCancelled:
            if self._ledger.status != "cancelled":
                self._ledger.cancel(self._cancel_reason or "cancelled")
            return self._outcome(
                started=started,
                status="cancelled",
                text="",
                is_success=False,
                error=self._cancel_reason or "HER Ultra run cancelled",
                primary_session_id=primary_session_id,
                plan_revision=revision,
                subtask_count=len(plan.subtasks) if plan else 0,
                completed_subtasks=0,
            )
        except asyncio.CancelledError:
            self.cancel(self._cancel_reason or "caller_cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - convert backend-private failure
            return await self._failed_outcome(
                started=started,
                status="failed",
                error=f"HER Ultra orchestration failed: {type(exc).__name__}: {exc}",
                primary_session_id=primary_session_id,
                plan_revision=revision,
                subtask_count=len(plan.subtasks) if plan else 0,
                completed_subtasks=0,
            )

    async def _execute_dag(
        self,
        plan: HERUltraPlan,
        *,
        authority: HERUltraAuthorityEnvelope,
        generation: int,
    ) -> dict[str, HERUltraWorkerResult]:
        tasks_by_id = {task.subtask_id: task for task in plan.subtasks}
        pending = set(tasks_by_id)
        results: dict[str, HERUltraWorkerResult] = {}
        running: dict[asyncio.Task[HERUltraWorkerResult], str] = {}
        reported_progress: set[int] = set()

        while pending or running:
            self._raise_if_cancelled(generation)
            slots = self.config.max_concurrent_subagents - len(running)
            resolved_without_dispatch = False
            ready = sorted(
                task_id
                for task_id in pending
                if all(
                    dependency in results
                    for dependency in tasks_by_id[task_id].depends_on
                )
            )
            for task_id in ready[: max(0, slots)]:
                subtask = tasks_by_id[task_id]
                dependency_results = {
                    dependency: results[dependency] for dependency in subtask.depends_on
                }
                failed_dependencies = sorted(
                    dependency
                    for dependency, result in dependency_results.items()
                    if not result.completed
                )
                if failed_dependencies:
                    result = HERUltraWorkerResult(
                        result_id=f"{self.run_id}:{task_id}:dependency_failed",
                        subtask_id=task_id,
                        status="failed",
                        claims=(),
                        evidence=(),
                        artifacts=(),
                        validation=(),
                        uncertainty="",
                        unresolved_items=tuple(failed_dependencies),
                        retry_safe=True,
                        error="dependency failed: " + ", ".join(failed_dependencies),
                        error_type="dependency_failed",
                    )
                    results[task_id] = result
                    pending.remove(task_id)
                    resolved_without_dispatch = True
                    self._ledger.transition(
                        event_id=f"{self.run_id}:{task_id}:dependency_failed",
                        entity=f"subtask:{task_id}",
                        state="failed",
                        data={"failed_dependencies": failed_dependencies},
                    )
                    continue
                async_task = asyncio.create_task(
                    self._execute_subtask(
                        subtask,
                        dependency_results=dependency_results,
                        authority=authority,
                        generation=generation,
                    )
                )
                self._inflight.add(async_task)
                running[async_task] = task_id
                pending.remove(task_id)
            if not running:
                if resolved_without_dispatch:
                    continue
                raise HERUltraContractError(
                    "Ultra DAG made no progress",
                    errors=["remaining: " + ", ".join(sorted(pending))],
                )
            done, _pending_tasks = await asyncio.wait(
                tuple(running), return_when=asyncio.FIRST_COMPLETED
            )
            for async_task in done:
                task_id = running.pop(async_task)
                self._inflight.discard(async_task)
                try:
                    result = await async_task
                except asyncio.CancelledError:
                    raise HERUltraCancelled(self._cancel_reason or "cancelled")
                results[task_id] = result
                terminal = len(results)
                if (
                    terminal == 1 or terminal == len(tasks_by_id) or terminal % 5 == 0
                ) and terminal not in reported_progress:
                    reported_progress.add(terminal)
                    blocked = sum(item.blocked for item in results.values())
                    failed = sum(item.status == "failed" for item in results.values())
                    await self._emit(
                        KIND_PROGRESS,
                        f"HER Ultra worker progress: {terminal}/{len(tasks_by_id)} terminal; "
                        f"blocked={blocked}; failed={failed}.",
                        event_id=f"{self.run_id}:commentary:workers:{terminal}",
                        phase="execution",
                    )
                    await self._emit_persona_commentary(
                        {
                            "phase": "worker_progress",
                            "terminal_subtasks": terminal,
                            "total_subtasks": len(tasks_by_id),
                            "completed_subtasks": sum(
                                item.completed for item in results.values()
                            ),
                            "failed_subtasks": failed,
                            "blocked_subtasks": blocked,
                        },
                        event_id=f"{self.run_id}:persona:workers:{terminal}",
                        phase="execution",
                    )
        return results

    async def _execute_subtask(
        self,
        subtask: HERUltraSubtask,
        *,
        dependency_results: Mapping[str, HERUltraWorkerResult],
        authority: HERUltraAuthorityEnvelope,
        generation: int,
    ) -> HERUltraWorkerResult:
        assert self._ledger is not None
        dispatch_id = f"{self.run_id}:{subtask.subtask_id}:dispatch"
        maximum_attempts = 1 + self.config.subagent_retry_limit
        last_result: HERUltraWorkerResult | None = None
        for attempt in range(1, maximum_attempts + 1):
            self._raise_if_cancelled(generation)
            attempt_id = f"{dispatch_id}:attempt:{attempt}"
            request_id = (
                f"{self._ledger.parent_request_id}:ultra:{self.run_id}:"
                f"{subtask.subtask_id}:attempt:{attempt}"
            )
            model = self._resolve_model(subtask)
            spec = HERUltraWorkerExecutionSpec(
                run_id=self.run_id,
                parent_request_id=self._ledger.parent_request_id,
                subtask_id=subtask.subtask_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                attempt=attempt,
                request_id=request_id,
                prompt=self._worker_prompt(subtask, dependency_results, authority),
                model=model,
                model_class=subtask.model_class,
                effort=subtask.effort,
                permission_mode=authority.permission_mode,
                allowed_tools=authority.allowed_tools,
                workspace_strategy=subtask.workspace_strategy,
                workspace=authority.access_root,
                timeout_sec=self.config.subagent_timeout_sec,
                retry_safe=subtask.retry_safe,
                cancellation_generation=generation,
            )
            self._ledger.transition(
                event_id=f"{attempt_id}:started",
                entity=f"subtask:{subtask.subtask_id}",
                state="running",
                data={
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "attempt": attempt,
                    "model": model,
                    "effort": subtask.effort,
                    "optional": subtask.optional,
                },
                cancellation_generation=generation,
            )
            await self._emit(
                KIND_PROGRESS,
                f"HER Ultra dispatched {subtask.subtask_id} (attempt {attempt})",
                detail=f"model={model}; effort={subtask.effort}",
                event_id=f"{attempt_id}:technical:started",
                phase="execution",
            )
            try:
                invocation = await asyncio.wait_for(
                    self.worker_executor(spec), timeout=self.config.subagent_timeout_sec
                )
            except asyncio.TimeoutError:
                invocation = HERUltraInvocationResult(
                    text="",
                    is_success=False,
                    error=f"worker timed out after {self.config.subagent_timeout_sec}s",
                    error_type="timeout",
                    retryable=True,
                    model=model,
                    duration_ms=self.config.subagent_timeout_sec * 1000.0,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker boundary
                invocation = HERUltraInvocationResult(
                    text="",
                    is_success=False,
                    error=f"{type(exc).__name__}: {exc}",
                    error_type="worker_exception",
                    retryable=False,
                    model=model,
                )
            self._add_usage(invocation)
            result = HERUltraWorkerResult.from_invocation(
                invocation,
                subtask=subtask,
                result_id=f"{attempt_id}:result:1",
                attempt=attempt,
            )
            self._raise_if_cancelled(generation)
            accepted = self._ledger.transition(
                event_id=f"{result.result_id}:accepted",
                entity=f"subtask:{subtask.subtask_id}",
                state=result.status,
                data={
                    "dispatch_id": dispatch_id,
                    "attempt_id": attempt_id,
                    "result_id": result.result_id,
                    "attempt": attempt,
                    "model": result.model,
                    "retry_safe": result.retry_safe,
                    "error": result.error,
                    "error_type": result.error_type,
                },
                cancellation_generation=generation,
            )
            if not accepted:
                raise HERUltraCancelled("worker result rejected by cancellation fence")
            last_result = result
            if result.completed or result.status == "requires_user_input":
                await self._emit(
                    KIND_VALIDATION,
                    f"HER Ultra subtask {subtask.subtask_id} {result.status}",
                    event_id=f"{attempt_id}:technical:completed",
                    phase="execution",
                )
                return result
            retry_allowed = (
                attempt < maximum_attempts
                and result.transient
                and result.retry_safe
                and subtask.retry_safe
            )
            if not retry_allowed:
                await self._emit(
                    KIND_ERROR,
                    f"HER Ultra subtask {subtask.subtask_id} failed",
                    detail=f"error_type={result.error_type}; error={result.error}",
                    event_id=f"{attempt_id}:technical:failed",
                    phase="execution",
                )
                return result
            self._ledger.transition(
                event_id=f"{attempt_id}:retry_scheduled",
                entity=f"subtask:{subtask.subtask_id}",
                state="retrying",
                data={"next_attempt": attempt + 1, "error_type": result.error_type},
                cancellation_generation=generation,
            )
            await self._emit(
                KIND_PROGRESS,
                f"HER Ultra retrying {subtask.subtask_id}",
                detail=f"next_attempt={attempt + 1}; error_type={result.error_type}",
                event_id=f"{attempt_id}:technical:retry",
                phase="retry",
            )
        assert last_result is not None
        return last_result

    async def _invoke_primary(
        self,
        *,
        phase: str,
        revision: int,
        prompt: str,
        parent_request_id: str,
        primary_session_id: str,
        generation: int,
    ) -> HERUltraInvocationResult:
        self._raise_if_cancelled(generation)
        request_id = (
            f"{parent_request_id}:ultra:{self.run_id}:primary:{phase}:{revision}"
        )
        spec = HERUltraPrimaryExecutionSpec(
            run_id=self.run_id,
            parent_request_id=parent_request_id,
            phase=phase,
            revision=revision,
            request_id=request_id,
            prompt=prompt,
            resume_session_id=primary_session_id,
            model=self.config.primary_model,
            effort=self.config.primary_inner_effort,
            cancellation_generation=generation,
        )
        task = asyncio.create_task(self.primary_executor(spec))
        self._inflight.add(task)
        try:
            try:
                invocation = await task
            except asyncio.CancelledError:
                if self._cancel_event.is_set():
                    raise HERUltraCancelled(self._cancel_reason or "cancelled")
                raise
            self._add_usage(invocation)
            self._raise_if_cancelled(generation)
            return invocation
        finally:
            self._inflight.discard(task)

    def _resolve_model(self, subtask: HERUltraSubtask) -> str:
        if subtask.model:
            return subtask.model
        models = self.config.allowed_models
        if not models:
            return self.config.primary_model
        wanted = subtask.model_class.lower()
        if wanted in {"pro", "flash"}:
            for model in models:
                if wanted in model.lower():
                    return model
        if wanted == "primary" and self.config.primary_model:
            return self.config.primary_model
        return models[0]

    def _raise_if_cancelled(self, generation: int) -> None:
        if self._cancel_event.is_set():
            raise HERUltraCancelled(self._cancel_reason or "cancelled")
        if self._ledger is not None and not self._ledger.accepts_generation(generation):
            raise HERUltraCancelled("cancellation generation changed")

    def _add_usage(self, invocation: HERUltraInvocationResult) -> None:
        self._usage["input_tokens"] += max(0, int(invocation.input_tokens))
        self._usage["output_tokens"] += max(0, int(invocation.output_tokens))
        self._usage["thinking_tokens"] += max(0, int(invocation.thinking_tokens))
        self._usage["tool_call_count"] += max(0, int(invocation.tool_call_count))
        self._usage["tool_loop_count"] += max(0, int(invocation.tool_loop_count))
        self._usage["duration_ms"] += max(0.0, float(invocation.duration_ms))
        if invocation.cost_usd is None:
            self._usage["cost_known"] = False
        else:
            self._usage["cost_usd"] += max(0.0, float(invocation.cost_usd))

    async def _emit(
        self,
        kind: str,
        summary: str,
        *,
        event_id: str,
        phase: str,
        detail: str = "",
    ) -> None:
        if self.on_stream_event is None:
            return
        await self.on_stream_event(
            StreamEvent(
                kind=kind,
                summary=_bounded_text(summary, 500),
                detail=_bounded_text(detail, 2_000),
                event_id=event_id,
                delivery_class=(
                    DELIVERY_USER_COMMENTARY
                    if kind == KIND_COMMENTARY
                    else DELIVERY_TECHNICAL
                ),
                origin="her_ultra",
                phase=phase,
            )
        )

    async def _emit_persona_commentary(
        self,
        facts: Mapping[str, Any],
        *,
        event_id: str,
        phase: str,
    ) -> None:
        if self.on_stream_event is None or self.persona_commentary_renderer is None:
            return
        try:
            summary = str(await self.persona_commentary_renderer(dict(facts))).strip()
            if not summary:
                raise ValueError("persona commentary renderer returned no text")
        except Exception:  # noqa: BLE001 - commentary has an explicit safe fallback
            summary = (
                "[HER neutral fallback] Work is still in progress; "
                "another update will follow at the next verified stage."
            )
        await self._emit(
            KIND_COMMENTARY,
            summary,
            event_id=event_id,
            phase=phase,
        )

    def _planning_prompt(
        self,
        *,
        authoritative_goal: str,
        parent_request_id: str,
        authority: HERUltraAuthorityEnvelope,
        revision: int,
        prior_error: str,
    ) -> str:
        correction = (
            "\nThe previous plan failed deterministic validation. Correct every error:\n"
            + _bounded_text(prior_error, 8_000)
            if prior_error
            else ""
        )
        schema = {
            "plan_id": f"{parent_request_id}:ultra:plan:{revision}",
            "ultra_not_beneficial": False,
            "direct_response": "required only when ultra_not_beneficial=true",
            "subtasks": [
                {
                    "id": "unique-id",
                    "title": "short title",
                    "objective": "bounded objective",
                    "depends_on": [],
                    "model": "one exact allowed model or empty",
                    "model_class": "pro|flash|current|primary",
                    "effort": self.config.subagent_default_effort,
                    "workspace_strategy": "shared_read_only",
                    "retry_safe": True,
                    "deliverables": ["concrete output"],
                    "acceptance": ["verifiable condition"],
                    "optional": False,
                }
            ],
            "assembly_plan": {"strategy": "evidence_first"},
        }
        return (
            "[HER Ultra Primary Planning Contract]\n"
            "You are the Primary planner for one HER-only Ultra effort request. Produce exactly "
            "one JSON object. The Runtime owns request identity and authority, while the exact "
            "authoritative goal is supplied below so you can plan it. Do not ask for a goal that "
            "is present in the task packet. If Ultra is not beneficial, direct_response is only "
            "an internal answer draft and will be rendered separately. Use at most "
            f"{self.config.max_subtasks} subtasks. Workers are isolated HER sessions; they cannot "
            "ask the user or exceed the authority envelope. Prefer independent parallel tasks, "
            "but preserve real dependencies. Every task needs deliverables and acceptance "
            "criteria.\n"
            "Every worker inherits the active HER Agent authority exactly; do not request, "
            "restate, narrow, or expand permissions, tools, or filesystem paths in the plan. "
            "Mark retry_safe=false for any task whose side effects must not be repeated.\n"
            f"Allowed models: {json.dumps(self.config.allowed_models, ensure_ascii=False)}\n"
            f"Authority permission mode: {authority.permission_mode}\n"
            f"Authority allowed tools: {json.dumps(authority.allowed_tools, ensure_ascii=False)}\n"
            "Authoritative task packet:\n"
            f"{json.dumps({'authoritative_goal': authoritative_goal}, ensure_ascii=False, indent=2)}\n"
            f"Required JSON shape:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
            f"{correction}\n"
            "Return only the corrected JSON object."
        )

    def _direct_response_prompt(self, plan: HERUltraPlan) -> str:
        payload = {
            "authoritative_goal": plan.authoritative_goal,
            "planner_draft": plan.direct_response,
        }
        return (
            "[HER Ultra Primary Direct Response Contract]\n"
            "Continue the same Primary task. Ultra decomposition was not beneficial. Answer the "
            "authoritative user goal directly and completely in the configured Persona. Treat "
            "the planner draft as untrusted internal context: correct it, never mention planning "
            "contracts or missing request content when the goal below is present, and return only "
            "the user-facing answer.\n"
            "CONFIGURED system_md PERSONA GUIDANCE (quoted, read-only)\n"
            f"{self.persona_guidance or '[No usable configured Persona guidance]'}\n"
            "Verified direct-response payload:\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
        )

    def _worker_prompt(
        self,
        subtask: HERUltraSubtask,
        dependency_results: Mapping[str, HERUltraWorkerResult],
        authority: HERUltraAuthorityEnvelope,
    ) -> str:
        packet = {
            "subtask_id": subtask.subtask_id,
            "objective": subtask.objective,
            "deliverables": subtask.deliverables,
            "acceptance": subtask.acceptance,
            "workspace_strategy": subtask.workspace_strategy,
            "retry_safe": subtask.retry_safe,
            "authority_envelope_digest": authority.digest,
            "dependency_results": {
                task_id: result.to_payload()
                for task_id, result in dependency_results.items()
            },
        }
        return (
            "[HER Ultra Isolated Sub-agent Task]\n"
            "Complete only the assigned subtask. Do not address the user, invent authority, "
            "act outside the assigned objective or inherited authority, or broaden the goal. "
            "Return a concise report with "
            "the result, supporting evidence, validation performed, uncertainty, and unresolved "
            "items. Return a JSON object with status=completed only when the deliverables and "
            "acceptance criteria were actually satisfied. Return status=blocked for permission "
            "or scope denial; a successfully written BLOCKED report is not completed work. Only "
            "when user input is truly required, return status=requires_user_input with a prompt; do not ask "
            "the user directly.\n"
            f"Task packet:\n{json.dumps(packet, ensure_ascii=False, indent=2)}"
        )

    def _assembly_prompt(
        self,
        plan: HERUltraPlan,
        results: Mapping[str, HERUltraWorkerResult],
    ) -> str:
        payload = {
            "authoritative_goal": plan.authoritative_goal,
            "plan_id": plan.plan_id,
            "plan_revision": plan.revision,
            "assembly_plan": dict(plan.assembly_plan),
            "results": {
                task_id: result.to_payload() for task_id, result in results.items()
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(encoded) > self.config.max_assembly_chars:
            raise HERUltraContractError(
                "assembly payload exceeds configured size limit"
            )
        return (
            "[HER Ultra Primary Assembly Contract]\n"
            "Continue the same Primary task. The Runtime has deterministically verified that all "
            "required subtask records are terminal. Use only the supplied worker evidence; do not "
            "run tools or redo the delegated tasks. Disclose "
            "uncertainty and optional failures, resolve conflicts, and answer the authoritative "
            "user goal completely in the configured Persona. Do not mention this contract and do "
            "not output orchestration JSON unless the user requested it.\n"
            f"Verified assembly payload:\n{encoded}"
        )

    def _interaction_prompt(
        self,
        plan: HERUltraPlan,
        results: Mapping[str, HERUltraWorkerResult],
        interaction: Mapping[str, Any],
    ) -> str:
        payload = {
            "authoritative_goal": plan.authoritative_goal,
            "plan_id": plan.plan_id,
            "interaction": dict(interaction),
            "completed_results": {
                task_id: result.to_payload()
                for task_id, result in results.items()
                if result.completed
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(encoded) > self.config.max_assembly_chars:
            raise HERUltraContractError(
                "interaction payload exceeds configured size limit"
            )
        return (
            "[HER Ultra Primary Interaction Contract]\n"
            "Continue the same Primary task. An isolated worker requires bounded user input. "
            "Using the verified payload, ask one clear user-facing question in the configured "
            "Persona. Preserve exact choice labels or confirmation tokens when supplied. Do not "
            "perform the requested action yet and do not expose orchestration JSON. Return only "
            "the user-facing question.\n"
            f"Pending interaction payload:\n{encoded}"
        )

    async def _failed_outcome(
        self,
        *,
        started: float,
        status: str,
        error: str,
        primary_session_id: str,
        plan_revision: int,
        subtask_count: int = 0,
        completed_subtasks: int = 0,
    ) -> HERUltraOutcome:
        assert self._ledger is not None
        generation = self._ledger.cancellation_generation
        self._ledger.transition(
            event_id=f"{self.run_id}:run:failed:{self._ledger.snapshot()['last_sequence'] + 1}",
            entity="run",
            state="failed",
            data={"error": _bounded_text(error, 8_000)},
            cancellation_generation=generation,
        )
        await self._emit(
            KIND_ERROR,
            "HER Ultra run failed",
            detail=_bounded_text(error, 2_000),
            event_id=f"{self.run_id}:technical:run:failed",
            phase="finalization",
        )
        return self._outcome(
            started=started,
            status=status,
            text="",
            is_success=False,
            error=error,
            primary_session_id=primary_session_id,
            plan_revision=plan_revision,
            subtask_count=subtask_count,
            completed_subtasks=completed_subtasks,
        )

    def _outcome(
        self,
        *,
        started: float,
        status: str,
        text: str,
        is_success: bool,
        error: str,
        primary_session_id: str,
        plan_revision: int,
        subtask_count: int,
        completed_subtasks: int,
        pending_interaction: Mapping[str, Any] | None = None,
    ) -> HERUltraOutcome:
        duration_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        return HERUltraOutcome(
            run_id=self.run_id,
            status=status,
            text=text,
            is_success=is_success,
            error=_bounded_text(error, 8_000),
            primary_session_id=primary_session_id,
            input_tokens=int(self._usage["input_tokens"]),
            output_tokens=int(self._usage["output_tokens"]),
            thinking_tokens=int(self._usage["thinking_tokens"]),
            tool_call_count=int(self._usage["tool_call_count"]),
            tool_loop_count=int(self._usage["tool_loop_count"]),
            duration_ms=duration_ms,
            cost_usd=(
                float(self._usage["cost_usd"]) if self._usage["cost_known"] else None
            ),
            plan_revision=plan_revision,
            subtask_count=subtask_count,
            completed_subtasks=completed_subtasks,
            pending_interaction=dict(pending_interaction)
            if pending_interaction is not None
            else None,
        )
