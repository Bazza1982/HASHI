"""Agent Companion (AC) for bounded, typed turn supervision.

The companion is deliberately outside HASHI Core.  It observes a compact
turn snapshot, asks an optional Jev adapter for a bounded judgment, and lets
ordinary PAO code decide whether any intervention is safe.  Jev never owns
authority, process termination, or user-facing prose.

P0: one lifecycle companion per accepted turn.
P1: redacted Jev judgment with deterministic fallback.
P2: typed, deduplicated intervention through the Agent control lane.
P3: managed-process ownership/lease checks before a process is stopped.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol
from uuid import uuid4


logger = logging.getLogger("BridgeU.AgentCompanion")

MAX_SUMMARY_CHARS = 240
MAX_EVENT_SUMMARY_CHARS = 160
MAX_EVIDENCE_REFS = 8
TYPESAFE_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
MANAGED_PROCESS_TOOLS = frozenset(
    {"managed_process_start", "managed_process_status", "managed_process_stop"}
)


class CompanionState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class CompanionAction(str, Enum):
    OBSERVE = "observe"
    WARN = "warn"
    REQUEST_REPLAN = "request_replan"
    INTERRUPT_TOOL = "interrupt_tool"
    INTERRUPT_TURN = "interrupt_turn"
    ESCALATE = "escalate"


class CompanionIssue(str, Enum):
    NONE = "none"
    FOREGROUND_RESIDENT_COMMAND = "foreground_resident_command"
    REPEATED_NO_PROGRESS = "repeated_no_progress"
    MISSING_RECEIPT = "missing_receipt"
    UNKNOWN_SIDE_EFFECT = "unknown_side_effect"
    SCHEDULER_CAPABILITY_UNAVAILABLE = "scheduler_capability_unavailable"
    JEV_UNAVAILABLE = "jev_unavailable"


def _bounded_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _bounded_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _normalise_action(value: Any) -> CompanionAction:
    raw = str(value or "").strip().casefold().replace("-", "_")
    try:
        return CompanionAction(raw)
    except ValueError:
        return CompanionAction.OBSERVE


def companion_enabled(options: Mapping[str, Any] | None) -> bool:
    """Return the explicit per-Agent AC opt-in; disabled is the safe default."""

    value = dict(options or {}).get("agent_companion_enabled", False)
    if not isinstance(value, bool):
        raise ValueError("agent_companion_enabled must be a boolean")
    return value


@dataclass(frozen=True)
class AgentSnapshot:
    """Small, non-secret projection of one active Agent turn."""

    agent_id: str
    run_id: str
    turn_id: str
    task_summary: str = ""
    stage: str = "queued"
    current_tool: str = ""
    tool_started_at: float | None = None
    last_progress_at: float = 0.0
    progress_sequence: int = 0
    receipt_sequence: int = 0
    resident_hint: bool = False
    managed_process_id: str | None = None
    managed_process_owner: str | None = None
    can_interrupt: bool = False
    terminal: bool = False
    side_effects_possible: bool = False
    last_event_kind: str = ""
    last_event_summary: str = ""

    @property
    def key(self) -> str:
        return str(self.turn_id or self.run_id)

    def compact_state(
        self,
        *,
        now: float | None = None,
        detected_issue: CompanionIssue = CompanionIssue.NONE,
    ) -> dict[str, Any]:
        """Return the only state allowed across the optional Jev boundary."""

        current = time.monotonic() if now is None else float(now)
        age = max(0.0, current - float(self.last_progress_at))
        return {
            "agent_id": _bounded_text(self.agent_id, 80),
            "run_id": _bounded_text(self.run_id, 96),
            "turn_id": _bounded_text(self.turn_id, 96),
            "task_summary": _bounded_text(self.task_summary, MAX_SUMMARY_CHARS),
            "stage": _bounded_text(self.stage, 64),
            "current_tool": _bounded_text(self.current_tool, 64),
            "progress_age_seconds": round(age, 3),
            "progress_sequence": int(self.progress_sequence),
            "receipt_sequence": int(self.receipt_sequence),
            "resident_hint": bool(self.resident_hint),
            "managed_process": bool(self.managed_process_id),
            "can_interrupt": bool(self.can_interrupt),
            "terminal": bool(self.terminal),
            "side_effects_possible": bool(self.side_effects_possible),
            "last_event_kind": _bounded_text(self.last_event_kind, 48),
            "detected_issue": detected_issue.value,
        }


@dataclass(frozen=True)
class JevJudgment:
    """Typed Choice-like result returned by a Jev adapter."""

    action: CompanionAction = CompanionAction.OBSERVE
    confidence: float = 0.0
    probabilities: Mapping[str, float] = field(default_factory=dict)
    model: str = "offline-fallback"
    request_id: str | None = None
    latency_ms: float | None = None
    fallback_reason: str | None = None
    source: str = "fallback"


@dataclass(frozen=True)
class ManagedProcessSpec:
    """Typed launch request for an application that may outlive one turn."""

    cwd: str
    argv: tuple[str, ...] = ()
    command: str | None = None
    shell: str | None = None
    resident: bool = True
    lease_seconds: float | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if not self.argv and not str(self.command or "").strip():
            raise ValueError("managed process requires argv or command")
        if self.argv and self.command:
            raise ValueError("managed process accepts argv or command, not both")
        if self.lease_seconds is not None and float(self.lease_seconds) < 0:
            raise ValueError("lease_seconds must be non-negative")


@dataclass(frozen=True)
class ManagedProcessHandle:
    process_id: str
    owner: str
    state: str
    pid: int | None = None
    process_group: int | None = None
    lease_expires_at: float | None = None
    managed: bool = True


class ManagedProcessOwnershipError(PermissionError):
    """The requested process is not owned by the current Agent."""


class ManagedProcessLeaseExpired(PermissionError):
    """The process lease is no longer valid for a stop request."""


class ManagedProcessController(Protocol):
    async def start(self, owner: str, spec: ManagedProcessSpec) -> ManagedProcessHandle:
        ...

    async def inspect(
        self,
        owner: str,
        process_id: str,
        *,
        require_active_lease: bool = False,
    ) -> ManagedProcessHandle | None:
        ...

    async def stop(
        self,
        owner: str,
        process_id: str,
        *,
        grace_seconds: float = 2.0,
    ) -> ManagedProcessHandle:
        ...


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class BackgroundJobProcessController:
    """P3 adapter over the existing BackgroundJobManager boundary.

    The manager remains the process/tree owner.  This adapter adds the typed
    owner check required by AC and never attempts to stop an unowned PID.
    """

    def __init__(self, manager_provider: Callable[[], Any | None]) -> None:
        self._manager_provider = manager_provider

    async def start(self, owner: str, spec: ManagedProcessSpec) -> ManagedProcessHandle:
        manager = self._manager_provider()
        if manager is None:
            raise RuntimeError("managed background process service is unavailable")
        origin = {
            "managed_process": True,
            "managed_owner": str(owner),
            "resident": bool(spec.resident),
            "label": _bounded_text(spec.label, 120),
        }
        if spec.lease_seconds is not None:
            origin["lease_seconds"] = float(spec.lease_seconds)
            origin["lease_expires_at"] = time.time() + float(spec.lease_seconds)
        kwargs: dict[str, Any] = {
            "agent": str(owner),
            "cwd": spec.cwd,
            "origin": origin,
            "notify_on_complete": True,
            "notify_on_failure": True,
            "trigger_agent_on_complete": True,
            "trigger_agent_on_failure": True,
        }
        if spec.argv:
            kwargs["argv"] = list(spec.argv)
        else:
            kwargs["command"] = spec.command
            kwargs["shell"] = spec.shell
        record = await _maybe_await(manager.start_job(**kwargs))
        return self._handle_from_record(record, owner=owner)

    async def inspect(
        self,
        owner: str,
        process_id: str,
        *,
        require_active_lease: bool = False,
    ) -> ManagedProcessHandle | None:
        manager = self._manager_provider()
        if manager is None:
            return None
        process_id = str(process_id or "").strip()
        if not process_id:
            raise ValueError("managed process id is required")
        record = await _maybe_await(manager.get(process_id))
        if record is None:
            return None
        record_owner = str(getattr(record, "agent", "") or "")
        origin = getattr(record, "origin", {}) or {}
        managed_owner = str(origin.get("managed_owner") or record_owner)
        if (
            record_owner != str(owner)
            or managed_owner != str(owner)
            or not bool(origin.get("managed_process", False))
        ):
            raise ManagedProcessOwnershipError("managed process owner mismatch")
        handle = self._handle_from_record(record, owner=owner)
        if (
            require_active_lease
            and not bool(getattr(record, "is_terminal", False))
            and handle.lease_expires_at is not None
            and handle.lease_expires_at <= time.time()
        ):
            raise ManagedProcessLeaseExpired("managed process lease has expired")
        return handle

    async def stop(
        self,
        owner: str,
        process_id: str,
        *,
        grace_seconds: float = 2.0,
    ) -> ManagedProcessHandle:
        manager = self._manager_provider()
        if manager is None:
            raise RuntimeError("managed background process service is unavailable")
        # The explicit lookup is intentional: a caller must prove ownership
        # before the manager receives a cancellation request.
        await self.inspect(owner, process_id, require_active_lease=True)
        record = await _maybe_await(
            manager.cancel(str(process_id), grace_seconds=float(grace_seconds))
        )
        return self._handle_from_record(record, owner=owner)

    @staticmethod
    def _handle_from_record(record: Any, *, owner: str) -> ManagedProcessHandle:
        process = getattr(record, "process", {}) or {}
        origin = getattr(record, "origin", {}) or {}
        lease = origin.get("lease_expires_at")
        try:
            lease_value = float(lease) if lease is not None else None
        except (TypeError, ValueError):
            lease_value = None
        pid = process.get("pid")
        pgid = process.get("pgid")
        return ManagedProcessHandle(
            process_id=str(getattr(record, "job_id", "")),
            owner=str(owner),
            state=str(getattr(record, "state", "unknown")),
            pid=int(pid) if pid is not None else None,
            process_group=int(pgid) if pgid is not None else None,
            lease_expires_at=lease_value,
            managed=bool(origin.get("managed_process", False)),
        )


class JevUnavailable(RuntimeError):
    """The optional Jev service could not provide a typed judgment."""


class JevJudge(Protocol):
    async def judge(self, state: Mapping[str, Any]) -> JevJudgment:
        ...


class NullJevJudge:
    """Offline-safe adapter used until an explicitly configured canary exists."""

    def __init__(self, reason: str = "jev_not_configured") -> None:
        self.reason = str(reason)

    async def judge(self, state: Mapping[str, Any]) -> JevJudgment:
        del state
        return JevJudgment(fallback_reason=self.reason)


class HttpJevJudge:
    """Small optional TypeSafe HTTP adapter; disabled unless configured.

    The request contains only :meth:`AgentSnapshot.compact_state`; no prompt,
    transcript, memory, credential, or local path crosses this boundary.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model: str = "jev-latest",
        timeout_s: float = 5.0,
    ) -> None:
        endpoint = str(endpoint or "").strip()
        if not endpoint.startswith(("https://", "http://")):
            raise ValueError("Jev endpoint must be an HTTP(S) URL")
        if not str(api_key or "").strip():
            raise ValueError("Jev API key is required")
        self.endpoint = endpoint
        self.api_key = str(api_key)
        self.model = str(model or "jev-latest")
        self.timeout_s = max(0.1, min(10.0, float(timeout_s)))

    async def judge(self, state: Mapping[str, Any]) -> JevJudgment:
        started = time.monotonic()
        try:
            raw = await asyncio.to_thread(self._request, dict(state))
            answer = ((raw.get("answers") or {}).get("action") or {})
            if not isinstance(answer, Mapping) or answer.get("type") != "choice":
                raise JevUnavailable("malformed Jev action answer")
            probabilities = answer.get("probabilities") or {}
            if not isinstance(probabilities, Mapping):
                probabilities = {}
            return JevJudgment(
                action=_normalise_action(answer.get("choice")),
                confidence=_bounded_confidence(answer.get("confidence")),
                probabilities={
                    str(key): _bounded_confidence(value)
                    for key, value in probabilities.items()
                },
                model=str(raw.get("model") or self.model),
                request_id=str(raw.get("request_id") or "") or None,
                latency_ms=round((time.monotonic() - started) * 1000, 3),
                source="jev",
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise JevUnavailable(f"Jev request failed: {type(exc).__name__}") from exc

    def _request(self, state: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = {
            "state": dict(state),
            "model": self.model,
            "questions": {
                "action": {
                    "type": "choice",
                    "instructions": (
                        "Choose the least invasive safe companion action for the "
                        "detected issue. Do not authorize process termination."
                    ),
                    "criteria": {
                        "observe": "The turn is making useful progress or evidence is insufficient.",
                        "warn": "Record a warning and let the current turn continue.",
                        "request_replan": "Ask the Agent to replan without stopping work.",
                        "interrupt_tool": "Stop the current foreground tool because it is clearly resident or stuck.",
                        "interrupt_turn": "Stop the whole turn because continuing is unsafe.",
                        "escalate": "Pause and ask the user for a decision.",
                    },
                }
            },
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read(256 * 1024)
        except urllib.error.HTTPError as exc:
            raise JevUnavailable(f"Jev HTTP status {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise JevUnavailable("Jev endpoint unavailable") from exc
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, Mapping):
            raise JevUnavailable("Jev response was not an object")
        return decoded


def build_jev_judge(options: Mapping[str, Any] | None, secrets: Mapping[str, Any] | None = None) -> JevJudge:
    """Build the optional adapter without making live calls by default."""

    options = dict(options or {})
    if not bool(options.get("agent_companion_jev_enabled", False)):
        return NullJevJudge()
    # The HASHI2 HERv2J experiment uses one fixed TypeSafe endpoint.  Do not
    # allow Agent configuration to redirect a credential or snapshot elsewhere.
    endpoint = TYPESAFE_SYSTEM_ONE_URL
    secrets = dict(secrets or {})
    api_key = str(
        secrets.get("typesafe_api_key")
        or secrets.get("TYPESAFE_API_KEY")
        or os.environ.get("TYPESAFE_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return NullJevJudge("jev_enabled_without_key")
    try:
        return HttpJevJudge(
            endpoint=endpoint,
            api_key=api_key,
            model=str(options.get("agent_companion_jev_model") or "jev-latest"),
            timeout_s=float(options.get("agent_companion_jev_timeout_s") or 5.0),
        )
    except (TypeError, ValueError):
        return NullJevJudge("jev_configuration_invalid")


@dataclass(frozen=True)
class CompanionPolicy:
    interval_s: float = 300.0
    progress_grace_s: float = 300.0
    min_jev_confidence: float = 0.75
    interrupt_confidence: float = 0.85
    max_interventions: int = 2
    resident_markers: tuple[str, ...] = (
        "monitor.py",
        "tail -f",
        "run_forever",
        "while true",
        "sleep infinity",
        "daemon",
        "server",
        "serve",
        "watch",
        "uvicorn",
        "npm run dev",
    )

    def __post_init__(self) -> None:
        if float(self.interval_s) <= 0:
            raise ValueError("companion interval_s must be positive")
        if float(self.progress_grace_s) <= 0:
            raise ValueError("companion progress_grace_s must be positive")
        if not 0 <= float(self.min_jev_confidence) <= 1:
            raise ValueError("min_jev_confidence must be between 0 and 1")
        if not 0 <= float(self.interrupt_confidence) <= 1:
            raise ValueError("interrupt_confidence must be between 0 and 1")
        if int(self.max_interventions) < 1:
            raise ValueError("max_interventions must be positive")

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> "CompanionPolicy":
        options = dict(options or {})
        return cls(
            interval_s=float(options.get("agent_companion_interval_s") or 300.0),
            progress_grace_s=float(
                options.get("agent_companion_progress_grace_s") or options.get("agent_companion_interval_s") or 300.0
            ),
            min_jev_confidence=float(options.get("agent_companion_min_jev_confidence") or 0.75),
            interrupt_confidence=float(options.get("agent_companion_interrupt_confidence") or 0.85),
            max_interventions=int(options.get("agent_companion_max_interventions") or 2),
        )


@dataclass(frozen=True)
class InterventionEvent:
    event_id: str
    agent_id: str
    run_id: str
    turn_id: str
    action: CompanionAction
    issue: CompanionIssue
    summary: str
    next_step: str
    confidence: float
    source: str
    created_at: float
    managed_process_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "action": self.action.value,
            "issue": self.issue.value,
            "summary": self.summary,
            "next_step": self.next_step,
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "created_at": self.created_at,
            "managed_process_id": self.managed_process_id,
        }

    def to_agent_message(self) -> str:
        return (
            "[agent-companion intervention]\n"
            f"type={self.issue.value}\n"
            f"action={self.action.value}\n"
            f"next={self.next_step}\n"
            "This is a typed internal intervention. Treat it as operational "
            "evidence, not as a new user request. Do not repeat the failed "
            "foreground action; use the suggested managed path or ask for confirmation."
        )


@dataclass(frozen=True)
class CompanionDecision:
    action: CompanionAction
    issue: CompanionIssue
    confidence: float
    source: str
    reason: str
    next_step: str
    intervention_id: str
    evidence_refs: tuple[str, ...] = ()
    managed_process_id: str | None = None


EventSink = Callable[[InterventionEvent], Awaitable[Any] | Any]


class AgentCompanion:
    """One companion supervising one turn."""

    def __init__(
        self,
        snapshot: AgentSnapshot,
        *,
        control_lane: Any | None = None,
        judge: JevJudge | None = None,
        managed_processes: ManagedProcessController | None = None,
        event_sink: EventSink | None = None,
        policy: CompanionPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.snapshot = snapshot
        self.control_lane = control_lane
        self.judge = judge or NullJevJudge()
        self.managed_processes = managed_processes
        self.event_sink = event_sink
        self.policy = policy or CompanionPolicy()
        self.clock = clock
        self.state = CompanionState.CREATED
        self._task: asyncio.Task[Any] | None = None
        self._last_tick_at: float | None = None
        self._seen_issue_keys: set[tuple[str, int, str | None]] = set()
        self._interventions = 0

    @property
    def key(self) -> str:
        return self.snapshot.key

    def start(self) -> None:
        if self.state in {CompanionState.RUNNING, CompanionState.STOPPING}:
            return
        self.state = CompanionState.RUNNING
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A manual/offline caller may use tick() without an active loop.
            self._task = None
            return
        try:
            self._task = loop.create_task(
                self._run(), name=f"agent-companion:{self.snapshot.agent_id}:{self.key}"
            )
        except RuntimeError:
            # The loop may have closed between lookup and task creation.
            self._task = None

    async def finish(self, *, terminal: bool = True) -> None:
        if terminal:
            self.snapshot = replace(self.snapshot, terminal=True)
        if self.state == CompanionState.STOPPED:
            return
        self.state = CompanionState.STOPPING
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.state = CompanionState.STOPPED

    def update_snapshot(self, snapshot: AgentSnapshot) -> None:
        if snapshot.key != self.key:
            raise ValueError("snapshot key does not belong to this companion")
        self.snapshot = snapshot

    def observe_stream_event(self, event: Any) -> AgentSnapshot:
        """Update liveness without retaining raw command or provider text."""

        kind = str(getattr(event, "kind", "") or "").strip()
        tool_name = str(getattr(event, "tool_name", "") or "").strip()
        summary = _bounded_text(getattr(event, "summary", ""), MAX_EVENT_SUMMARY_CHARS)
        metadata = getattr(event, "metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        command_hint = " ".join(
            str(metadata.get(key) or "")
            for key in ("command", "cmd", "argv", "display")
        )
        managed_tool = tool_name in MANAGED_PROCESS_TOOLS
        managed_id_hint = str(
            metadata.get("managed_process_id")
            or metadata.get("process_id")
            or metadata.get("job_id")
            or ""
        ).strip()
        if not managed_id_hint and managed_tool and kind == "tool_end":
            managed_id_hint = self._extract_managed_process_id(summary)
        resident_hint = (
            self.snapshot.resident_hint or self._looks_resident(f"{command_hint} {summary}")
        )
        if managed_tool:
            # The managed entry itself is intentionally not classified as an
            # unmanaged foreground resident command.  The manager owns the
            # process tree even when the completion event has no job id yet.
            resident_hint = False
        now = float(self.clock())
        meaningful = bool(kind and (summary or tool_name or command_hint))
        progress_sequence = self.snapshot.progress_sequence + (1 if meaningful else 0)
        receipt_sequence = self.snapshot.receipt_sequence + (1 if kind in {"tool_end", "progress", "validation", "testing"} else 0)
        starts_tool = kind in {"tool_start", "file_read", "file_edit", "shell_exec"}
        ends_tool = kind == "tool_end"
        current_tool = tool_name or self.snapshot.current_tool
        if starts_tool and not current_tool:
            current_tool = kind
        if ends_tool:
            current_tool = ""
        managed_id = self.snapshot.managed_process_id
        if bool(metadata.get("managed_process")) or managed_tool:
            managed_id = managed_id_hint or managed_id
        snapshot = replace(
            self.snapshot,
            stage="tool" if current_tool else "provider",
            current_tool=current_tool,
            tool_started_at=now if starts_tool else (None if ends_tool else self.snapshot.tool_started_at),
            last_progress_at=now if meaningful else self.snapshot.last_progress_at,
            progress_sequence=progress_sequence,
            receipt_sequence=receipt_sequence,
            resident_hint=resident_hint,
            managed_process_id=managed_id,
            managed_process_owner=(
                str(metadata.get("managed_owner") or "").strip()
                or self.snapshot.managed_process_owner
            ),
            last_event_kind=_bounded_text(kind, 48),
            last_event_summary=summary,
            side_effects_possible=self.snapshot.side_effects_possible
            or kind in {"tool_start", "file_edit", "shell_exec"},
        )
        self.snapshot = snapshot
        return snapshot

    def wrap_stream_callback(self, callback: Any | None) -> Callable[[Any], Awaitable[Any]]:
        async def wrapped(event: Any) -> Any:
            self.observe_stream_event(event)
            if callback is None:
                return None
            result = callback(event)
            return await result if inspect.isawaitable(result) else result

        return wrapped

    async def _run(self) -> None:
        try:
            while self.state == CompanionState.RUNNING:
                await asyncio.sleep(float(self.policy.interval_s))
                if self.state != CompanionState.RUNNING:
                    break
                await self.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Agent Companion loop failed for %s", self.key)

    async def tick(self, *, force: bool = False) -> CompanionDecision | None:
        if self.state not in {CompanionState.RUNNING, CompanionState.CREATED}:
            return None
        now = float(self.clock())
        if not force and self._last_tick_at is not None and now - self._last_tick_at < float(self.policy.interval_s):
            return None
        self._last_tick_at = now
        snapshot = self.snapshot
        if snapshot.terminal:
            return None
        issue = self._detect_issue(snapshot, now)
        if issue is CompanionIssue.NONE:
            return None
        issue_key = (issue.value, int(snapshot.progress_sequence), snapshot.managed_process_id)
        if issue_key in self._seen_issue_keys or self._interventions >= int(self.policy.max_interventions):
            return None
        self._seen_issue_keys.add(issue_key)

        state = snapshot.compact_state(now=now, detected_issue=issue)
        try:
            judgment = await self.judge.judge(state)
            if not isinstance(judgment, JevJudgment):
                raise JevUnavailable("Jev adapter returned an untyped judgment")
        except Exception as exc:
            judgment = JevJudgment(
                fallback_reason=f"{type(exc).__name__}",
                source="fallback",
            )
        action = self._resolve_action(issue, snapshot, judgment)
        next_step = self._next_step(issue, action)
        decision = CompanionDecision(
            action=action,
            issue=issue,
            confidence=_bounded_confidence(judgment.confidence),
            source=judgment.source,
            reason=judgment.fallback_reason or f"detected {issue.value}",
            next_step=next_step,
            intervention_id=f"ac-{uuid4().hex[:16]}",
            evidence_refs=(
                f"turn:{_bounded_text(snapshot.turn_id, 96)}",
                f"progress:{snapshot.progress_sequence}",
            )[:MAX_EVIDENCE_REFS],
            managed_process_id=snapshot.managed_process_id,
        )
        if action is CompanionAction.OBSERVE:
            return decision
        self._interventions += 1
        await self._apply(decision, snapshot)
        return decision

    def _detect_issue(self, snapshot: AgentSnapshot, now: float) -> CompanionIssue:
        if snapshot.managed_process_id and self.managed_processes is None:
            return CompanionIssue.SCHEDULER_CAPABILITY_UNAVAILABLE
        if snapshot.current_tool in MANAGED_PROCESS_TOOLS:
            return CompanionIssue.NONE
        if snapshot.resident_hint and snapshot.current_tool and not snapshot.managed_process_id:
            return CompanionIssue.FOREGROUND_RESIDENT_COMMAND
        age = max(0.0, now - float(snapshot.last_progress_at))
        if snapshot.current_tool and age >= float(self.policy.progress_grace_s):
            if snapshot.receipt_sequence <= 0:
                return CompanionIssue.MISSING_RECEIPT
            return CompanionIssue.REPEATED_NO_PROGRESS
        return CompanionIssue.NONE

    def _resolve_action(
        self,
        issue: CompanionIssue,
        snapshot: AgentSnapshot,
        judgment: JevJudgment,
    ) -> CompanionAction:
        proposed = _normalise_action(judgment.action)
        confidence = _bounded_confidence(judgment.confidence)
        if issue is CompanionIssue.FOREGROUND_RESIDENT_COMMAND:
            # A clearly resident command run without a managed handle is a
            # deterministic policy violation; Jev may soften it, but cannot
            # grant permission to kill an unrelated process.
            if proposed in {CompanionAction.INTERRUPT_TOOL, CompanionAction.INTERRUPT_TURN} and snapshot.can_interrupt:
                return proposed
            if proposed in {CompanionAction.ESCALATE, CompanionAction.REQUEST_REPLAN} and confidence >= float(self.policy.min_jev_confidence):
                return proposed
            return CompanionAction.INTERRUPT_TOOL if snapshot.can_interrupt else CompanionAction.ESCALATE
        if proposed in {CompanionAction.INTERRUPT_TOOL, CompanionAction.INTERRUPT_TURN}:
            if snapshot.can_interrupt and confidence >= float(self.policy.interrupt_confidence):
                return proposed
            return CompanionAction.REQUEST_REPLAN if confidence >= float(self.policy.min_jev_confidence) else CompanionAction.WARN
        if proposed in {CompanionAction.REQUEST_REPLAN, CompanionAction.ESCALATE, CompanionAction.WARN}:
            return proposed if confidence >= float(self.policy.min_jev_confidence) else CompanionAction.WARN
        return CompanionAction.WARN

    @staticmethod
    def _next_step(issue: CompanionIssue, action: CompanionAction) -> str:
        if issue is CompanionIssue.FOREGROUND_RESIDENT_COMMAND:
            return "stop the foreground resident command; restart it only through a typed managed-process entry"
        if issue is CompanionIssue.SCHEDULER_CAPABILITY_UNAVAILABLE:
            return "do not start or stop a process; restore the typed managed-process service first"
        if issue in {CompanionIssue.REPEATED_NO_PROGRESS, CompanionIssue.MISSING_RECEIPT}:
            return "inspect the last receipt, narrow the operation, and replan before repeating it"
        if action is CompanionAction.ESCALATE:
            return "pause side effects and ask the user for a decision"
        return "continue only after recording a fresh progress receipt"

    async def _apply(self, decision: CompanionDecision, snapshot: AgentSnapshot) -> None:
        actual_action = decision.action
        if decision.action in {CompanionAction.INTERRUPT_TOOL, CompanionAction.INTERRUPT_TURN}:
            if snapshot.managed_process_id and self.managed_processes is not None:
                try:
                    await self.managed_processes.stop(
                        snapshot.managed_process_owner or snapshot.agent_id,
                        snapshot.managed_process_id,
                    )
                except ManagedProcessOwnershipError:
                    actual_action = CompanionAction.ESCALATE
                except Exception:
                    actual_action = CompanionAction.ESCALATE
            elif snapshot.can_interrupt and self.control_lane is not None:
                interrupt = getattr(self.control_lane, "interrupt", None)
                if callable(interrupt):
                    try:
                        result = interrupt(f"agent-companion:{decision.issue.value}")
                        await _maybe_await(result)
                    except Exception:
                        actual_action = CompanionAction.ESCALATE
            else:
                actual_action = CompanionAction.ESCALATE
        if self.event_sink is None:
            return
        event = InterventionEvent(
            event_id=decision.intervention_id,
            agent_id=snapshot.agent_id,
            run_id=snapshot.run_id,
            turn_id=snapshot.turn_id,
            action=actual_action,
            issue=decision.issue,
            summary=_bounded_text(decision.reason, MAX_EVENT_SUMMARY_CHARS),
            next_step=decision.next_step,
            confidence=decision.confidence,
            source=decision.source,
            created_at=time.time(),
            managed_process_id=snapshot.managed_process_id,
        )
        result = self.event_sink(event)
        if inspect.isawaitable(result):
            await result

    def _looks_resident(self, text: str) -> bool:
        lowered = str(text or "").casefold()
        return any(marker.casefold() in lowered for marker in self.policy.resident_markers)

    @staticmethod
    def _extract_managed_process_id(text: str) -> str:
        match = re.search(
            r"(?:process_id|job_id)\s*[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9_.:-]+)",
            str(text or ""),
            flags=re.IGNORECASE,
        )
        return match.group(1) if match else ""


class AgentCompanionSupervisor:
    """Owns one companion per active turn for one Agent Worker."""

    def __init__(
        self,
        *,
        agent_id: str,
        control_lane: Any | None = None,
        judge: JevJudge | None = None,
        managed_processes: ManagedProcessController | None = None,
        event_sink: EventSink | None = None,
        policy: CompanionPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.agent_id = str(agent_id)
        self.control_lane = control_lane
        self.judge = judge or NullJevJudge()
        self.managed_processes = managed_processes
        self.event_sink = event_sink
        self.policy = policy or CompanionPolicy()
        self.clock = clock
        self._companions: dict[str, AgentCompanion] = {}

    def start_for_turn(
        self,
        *,
        run_id: str,
        turn_id: str,
        task_summary: str,
        chat_id: int | None = None,
        can_interrupt: bool = True,
    ) -> str:
        del chat_id  # delivery is owned by the runtime event sink
        key = str(turn_id or run_id)
        if key in self._companions:
            return key
        snapshot = AgentSnapshot(
            agent_id=self.agent_id,
            run_id=str(run_id or ""),
            turn_id=str(turn_id or ""),
            task_summary=_bounded_text(task_summary, MAX_SUMMARY_CHARS),
            stage="provider",
            last_progress_at=float(self.clock()),
            can_interrupt=bool(can_interrupt),
        )
        companion = AgentCompanion(
            snapshot,
            control_lane=self.control_lane,
            judge=self.judge,
            managed_processes=self.managed_processes,
            event_sink=self.event_sink,
            policy=self.policy,
            clock=self.clock,
        )
        self._companions[key] = companion
        companion.start()
        return key

    def record_stream_event(self, turn_id: str, event: Any) -> None:
        companion = self._companions.get(str(turn_id))
        if companion is not None:
            companion.observe_stream_event(event)

    def wrap_stream_callback(self, turn_id: str, callback: Any | None) -> Callable[[Any], Awaitable[Any]]:
        companion = self._companions.get(str(turn_id))
        if companion is None:
            async def passthrough(event: Any) -> Any:
                if callback is None:
                    return None
                result = callback(event)
                return await result if inspect.isawaitable(result) else result

            return passthrough
        return companion.wrap_stream_callback(callback)

    async def finish(self, turn_id: str, *, terminal: bool = True) -> None:
        key = str(turn_id)
        companion = self._companions.pop(key, None)
        if companion is not None:
            await companion.finish(terminal=terminal)

    async def tick_all(self, *, force: bool = False) -> list[CompanionDecision]:
        decisions: list[CompanionDecision] = []
        for companion in list(self._companions.values()):
            decision = await companion.tick(force=force)
            if decision is not None:
                decisions.append(decision)
        return decisions

    async def close(self) -> None:
        for key in list(self._companions):
            await self.finish(key)

    def snapshots(self) -> tuple[AgentSnapshot, ...]:
        return tuple(companion.snapshot for companion in self._companions.values())


__all__ = [
    "AgentCompanion",
    "AgentCompanionSupervisor",
    "AgentSnapshot",
    "BackgroundJobProcessController",
    "CompanionAction",
    "CompanionDecision",
    "CompanionIssue",
    "CompanionPolicy",
    "TYPESAFE_SYSTEM_ONE_URL",
    "CompanionState",
    "HttpJevJudge",
    "InterventionEvent",
    "JevJudgment",
    "ManagedProcessController",
    "ManagedProcessHandle",
    "ManagedProcessLeaseExpired",
    "ManagedProcessOwnershipError",
    "ManagedProcessSpec",
    "MANAGED_PROCESS_TOOLS",
    "NullJevJudge",
    "build_jev_judge",
    "companion_enabled",
]
