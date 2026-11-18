"""Provider-neutral cognitive control for tool-enabled HER v2 stages.

The controller stores observable diagnostics and evidence-linked task progress,
never hidden chain-of-thought. Repeated semantic tool/result cycles and an
unchanged TaskState are advisory; they do not change the authorized tool grant.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .task_state import HERTaskState

COGNITIVE_DECISION_TOOL = "hashi_cognitive_decision"
COGNITIVE_CONTROL_VERSION = 4
_CYCLE_REPETITIONS = 3
_PROGRESS_STALL_THRESHOLD = 3
_MAX_CYCLE_PERIOD = 12
_MAX_OBSERVATIONS = _CYCLE_REPETITIONS * _MAX_CYCLE_PERIOD * 2

_EVIDENCE_RECEIPT_RE = re.compile(r"(?m)^\s*HASHI_EVIDENCE_RECEIPT:\s*\S+\s*$")
_TASK_STATE_RE = re.compile(r"(?m)^\s*HASHI_TASK_STATE:\s*\{.*\}\s*$")
_HASHI_RECEIPT_RE = re.compile(r"hashi-tool:[^\s\"']+:call:[^\s\"']+:receipt:\d+")


def _digest(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _normalise_result_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_result_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_result_value(item) for item in value]
    if isinstance(value, str):
        return _normalise_text(value)
    return value


def _normalise_text(value: str) -> str:
    text = _EVIDENCE_RECEIPT_RE.sub("", str(value or ""))
    text = _TASK_STATE_RE.sub("", text)
    text = _HASHI_RECEIPT_RE.sub("<evidence-receipt>", text)
    return " ".join(text.split())


def canonical_tool_arguments(arguments: Mapping[str, Any] | None) -> Any:
    """Return exact semantic arguments in stable mapping order.

    Request IDs and time ranges can identify the user's real target, so their
    values are intentionally retained. Request-local TaskState metadata is
    removed by the registry before this function is called.
    """

    return {
        str(key): value
        for key, value in sorted(
            dict(arguments or {}).items(), key=lambda pair: str(pair[0])
        )
    }


def canonical_tool_result(output: str, details: Mapping[str, Any] | None) -> Any:
    """Return the semantic result while ignoring receipts and state projection."""

    text = _EVIDENCE_RECEIPT_RE.sub("", str(output or "")).strip()
    text = _TASK_STATE_RE.sub("", text).strip()
    parsed: Any = None
    if text[:1] in {"{", "["}:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
    if parsed is None:
        parsed = _normalise_text(text)
    elif isinstance(parsed, Mapping) and {
        "status",
        "effect",
        "data",
    }.issubset(parsed):
        # Smart Tool warnings are advisory and may first appear at the repeat
        # threshold. They must not make otherwise identical evidence look new.
        parsed = {key: value for key, value in parsed.items() if key != "warning"}
    semantic_details = {
        key: value
        for key, value in dict(details or {}).items()
        if str(key).strip().casefold()
        in {
            "blocked",
            "control_disposition",
            "is_error",
            "smart_effect",
            "smart_status",
            "state_changed",
            "unavailable",
        }
    }
    return _normalise_result_value({"output": parsed, "details": semantic_details})


def _state_changed(output: str, details: Mapping[str, Any] | None) -> bool | None:
    metadata = dict(details or {})
    for key in ("state_changed", "changed"):
        if metadata.get(key) is True:
            return True
        if metadata.get(key) is False:
            return False
    effect = str(metadata.get("smart_effect") or "").strip().casefold()
    if effect == "changed":
        return True
    if effect in {"no_change", "observed"}:
        return False
    text = _TASK_STATE_RE.sub("", _EVIDENCE_RECEIPT_RE.sub("", str(output or "")))
    text = text.strip()
    if text[:1] == "{":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, Mapping):
            parsed_effect = str(parsed.get("effect") or "").strip().casefold()
            if parsed_effect == "changed":
                return True
            if parsed_effect in {"no_change", "observed"}:
                return False
            data = parsed.get("data")
            if isinstance(data, Mapping):
                for key in ("state_changed", "changed"):
                    if data.get(key) is True:
                        return True
                    if data.get(key) is False:
                        return False
    return None


@dataclass(frozen=True)
class ToolObservation:
    tool_name: str
    tool_profile: str
    action_fingerprint: str
    result_fingerprint: str
    state_changed: bool | None
    is_error: bool
    progress_signature: str = ""

    @property
    def semantic_fingerprint(self) -> str:
        return _digest(
            [
                self.tool_name,
                self.action_fingerprint,
                self.result_fingerprint,
                self.is_error,
            ]
        )


@dataclass(frozen=True)
class CognitiveInterrupt:
    code: str
    stage: str
    cycle_period: int
    cycle_repetitions: int
    cycle_tools: tuple[str, ...]
    cycle_signature: str
    repeated_after_intervention: bool
    observation_count: int
    detection_kind: str = "semantic_cycle"
    progress_signature: str = ""
    stall_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "cycle_period": self.cycle_period,
            "cycle_repetitions": self.cycle_repetitions,
            "cycle_tools": list(self.cycle_tools),
            "cycle_signature": self.cycle_signature,
            "repeated_after_intervention": self.repeated_after_intervention,
            "observation_count": self.observation_count,
            "detection_kind": self.detection_kind,
            "progress_signature": self.progress_signature or None,
            "stall_count": self.stall_count,
            "new_evidence": False,
            "state_changed": False,
        }


class StageCognitiveController:
    """Track one stage's observable decisions, cycles, and task progress."""

    def __init__(
        self,
        *,
        stage: str,
        goal: str,
        task_state: HERTaskState | None = None,
    ) -> None:
        self.stage = str(stage or "tool").strip().casefold() or "tool"
        self.goal_sha256 = _digest(str(goal or ""))
        self.task_state = task_state
        self._history: deque[ToolObservation] = deque(maxlen=_MAX_OBSERVATIONS)
        self._seen_cycle_signatures: set[str] = set()
        self._interrupt: CognitiveInterrupt | None = None
        self._mode = "observing"
        self._active_tool_allowlist: frozenset[str] | None = None
        self._accepted_directions: set[str] = set()
        self._focus_target = ""
        self._revised_direction = ""
        self._expected_change = ""
        self._stop_condition = ""
        self._progress_stall_count = 0
        self._stagnation_signal_count = 0
        self._last_stagnation_signal_signature = ""
        self._last_progress_signature = (
            task_state.progress_signature() if task_state is not None else ""
        )
        self._intervention_progress_signature = ""
        self._recovery_epoch = 0
        self._interrupt_count = 0
        self._decision_count = 0
        self._last_decision = ""

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def active_tool_allowlist(self) -> frozenset[str] | None:
        return self._active_tool_allowlist

    @property
    def interrupt(self) -> CognitiveInterrupt | None:
        return self._interrupt

    @property
    def awaiting_decision(self) -> bool:
        return self._mode in {"decision_required", "terminal_decision_required"}

    @property
    def final_response_required(self) -> bool:
        return self._mode == "final_response_required"

    def observe(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        output: str,
        details: Mapping[str, Any] | None,
        is_error: bool,
        tool_profile: str = "generic",
        progress_signature: str = "",
        progress_tracking_armed: bool = False,
        progress_boundary: bool = True,
    ) -> CognitiveInterrupt | None:
        if self._mode != "observing":
            return None
        observation = ToolObservation(
            tool_name=str(tool_name or "unknown").strip() or "unknown",
            tool_profile=(
                str(tool_profile or "generic").strip().casefold() or "generic"
            ),
            action_fingerprint=_digest(canonical_tool_arguments(arguments)),
            result_fingerprint=_digest(canonical_tool_result(output, details)),
            state_changed=_state_changed(output, details),
            is_error=bool(is_error),
            progress_signature=str(progress_signature or ""),
        )
        self._history.append(observation)
        detected = self._detect_cycle()
        if detected is not None:
            period, block = detected
            signature = self._cycle_signature(block)
            if signature not in self._seen_cycle_signatures:
                # One observation is enough; persistent repeats do not become
                # a stream of mandatory decisions or tool denials.
                self._seen_cycle_signatures.add(signature)
                return self._raise_interrupt(
                    code="NO_NEW_INFORMATION_CYCLE",
                    detection_kind="semantic_cycle",
                    cycle_period=period,
                    cycle_tools=tuple(item.tool_name for item in block),
                    cycle_signature=signature,
                    repeated_after_intervention=False,
                    progress_signature=observation.progress_signature,
                    stall_count=0,
                )
        self._observe_progress(
            observation,
            armed=bool(progress_tracking_armed),
            boundary=bool(progress_boundary),
        )
        return None

    def _raise_interrupt(
        self,
        *,
        code: str,
        detection_kind: str,
        cycle_period: int,
        cycle_tools: tuple[str, ...],
        cycle_signature: str,
        repeated_after_intervention: bool,
        progress_signature: str,
        stall_count: int,
    ) -> CognitiveInterrupt:
        self._interrupt_count += 1
        interrupt = CognitiveInterrupt(
            code=code,
            stage=self.stage,
            cycle_period=cycle_period,
            cycle_repetitions=(
                _CYCLE_REPETITIONS
                if detection_kind == "semantic_cycle"
                else _PROGRESS_STALL_THRESHOLD
            ),
            cycle_tools=cycle_tools,
            cycle_signature=cycle_signature,
            repeated_after_intervention=repeated_after_intervention,
            observation_count=len(self._history),
            detection_kind=detection_kind,
            progress_signature=progress_signature,
            stall_count=stall_count,
        )
        self._interrupt = interrupt
        # A semantic repeat is evidence to consider, not a change to the
        # authenticated owner's tool grant or a reason to terminate a turn.
        return interrupt

    def _observe_progress(
        self,
        observation: ToolObservation,
        *,
        armed: bool,
        boundary: bool,
    ) -> None:
        signature = observation.progress_signature
        if not armed or not signature:
            self._progress_stall_count = 0
            self._last_stagnation_signal_signature = ""
            if signature:
                self._last_progress_signature = signature
            return
        # Parallel calls from one provider response deliberately carry the same
        # delta ID. They are one cognitive boundary, not several stalled turns.
        if not boundary:
            return
        if (
            observation.tool_profile
            in {
                "poll",
                "verify",
                "idempotent_action",
                "side_effect_action",
            }
            and not observation.is_error
        ) or observation.state_changed is True:
            self._progress_stall_count = 0
            self._last_stagnation_signal_signature = ""
            self._last_progress_signature = signature
            return
        if self._last_progress_signature and signature != self._last_progress_signature:
            self._progress_stall_count = 0
            self._last_stagnation_signal_signature = ""
            self._last_progress_signature = signature
            # Evidence-bound progress proves the previous direction recovered.
            self._recovery_epoch = 0
            self._intervention_progress_signature = ""
            return
        self._last_progress_signature = signature
        self._progress_stall_count += 1
        if self._progress_stall_count < _PROGRESS_STALL_THRESHOLD:
            return
        # Missing/no-op model deltas are not proof that execution itself is
        # stalled.  Saturate one advisory signal and keep the tools available;
        # semantic-cycle observations are advisory for the same reason.
        if signature != self._last_stagnation_signal_signature:
            self._stagnation_signal_count += 1
            self._last_stagnation_signal_signature = signature
        self._progress_stall_count = _PROGRESS_STALL_THRESHOLD

    def _detect_cycle(self) -> tuple[int, tuple[ToolObservation, ...]] | None:
        history = tuple(self._history)
        maximum = min(_MAX_CYCLE_PERIOD, len(history) // _CYCLE_REPETITIONS)
        for period in range(1, maximum + 1):
            required = period * _CYCLE_REPETITIONS
            suffix = history[-required:]
            blocks = tuple(
                suffix[index * period : (index + 1) * period]
                for index in range(_CYCLE_REPETITIONS)
            )
            semantic_blocks = tuple(
                tuple(item.semantic_fingerprint for item in block) for block in blocks
            )
            if any(block != semantic_blocks[0] for block in semantic_blocks[1:]):
                continue
            if all(item.tool_profile == "poll" for item in suffix):
                continue
            if any(item.state_changed is True for item in suffix):
                continue
            progress = {
                item.progress_signature for item in suffix if item.progress_signature
            }
            if len(progress) > 1:
                continue
            return period, tuple(blocks[-1])
        return None

    @staticmethod
    def _cycle_signature(block: Sequence[ToolObservation]) -> str:
        tokens = tuple(item.semantic_fingerprint for item in block)
        rotations = tuple(
            tokens[index:] + tokens[:index] for index in range(len(tokens))
        )
        return _digest(min(rotations))

    def interrupt_payload(self) -> dict[str, Any]:
        if self._interrupt is None:
            return {}
        return {
            "type": "HASHI_COGNITIVE_ADVISORY",
            "version": COGNITIVE_CONTROL_VERSION,
            "interrupt": self._interrupt.as_dict(),
            "cognitive_state": {
                "goal_sha256": self.goal_sha256,
                "focus_target": self._focus_target or None,
                "revised_direction": self._revised_direction or None,
                "expected_change": self._expected_change or None,
                "stop_condition": self._stop_condition or None,
                # Compatibility aliases for v1 audit readers.
                "current_hypothesis": self._revised_direction or None,
                "unresolved_question": self._focus_target or None,
                "expected_distinct_evidence": self._expected_change or None,
                "task_state": (
                    self.task_state.prompt_snapshot()
                    if self.task_state is not None
                    else None
                ),
                "tools_withheld": False,
            },
            "instruction": (
                "Repeated actions and results were observed. Consider whether "
                "another approach would help; the authorized tools remain available."
            ),
        }

    def decision_schema(self) -> dict[str, Any]:
        allowed = (
            ["FINALIZE", "BLOCKED"]
            if self._mode == "terminal_decision_required"
            else ["FINALIZE", "REVISE_DIRECTION", "BLOCKED"]
        )
        return {
            "type": "function",
            "function": {
                "name": COGNITIVE_DECISION_TOOL,
                "description": (
                    "Resolve a HASHI no-progress interrupt. This records a "
                    "direction decision only; never include hidden reasoning."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "decision": {"type": "string", "enum": allowed},
                        "new_focus": {"type": "string"},
                        "revised_direction": {"type": "string"},
                        "expected_change": {"type": "string"},
                        "stop_condition": {"type": "string"},
                        "requested_tools": {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        },
                        "summary": {"type": "string"},
                        # Accepted only as an in-flight v1 compatibility shape.
                        "hypothesis": {"type": "string"},
                        "unresolved_question": {"type": "string"},
                        "expected_distinct_evidence": {"type": "string"},
                    },
                    "required": ["decision"],
                    "additionalProperties": False,
                },
            },
        }

    def decide(
        self,
        arguments: Mapping[str, Any] | None,
        *,
        available_tools: Sequence[str],
    ) -> tuple[dict[str, Any], bool]:
        if not self.awaiting_decision:
            return (
                {"status": "rejected", "code": "NO_COGNITIVE_DECISION_REQUIRED"},
                True,
            )
        payload = dict(arguments or {})
        decision = str(payload.get("decision") or "").strip().upper()
        legacy_revision = decision == "NEW_HYPOTHESIS"
        if legacy_revision and self._mode != "terminal_decision_required":
            decision = "REVISE_DIRECTION"
            payload.setdefault("new_focus", payload.get("unresolved_question"))
            payload.setdefault("revised_direction", payload.get("hypothesis"))
            payload.setdefault(
                "expected_change", payload.get("expected_distinct_evidence")
            )
        allowed_decisions = (
            {"FINALIZE", "BLOCKED"}
            if self._mode == "terminal_decision_required"
            else {"FINALIZE", "REVISE_DIRECTION", "BLOCKED"}
        )
        if decision not in allowed_decisions:
            return (
                {
                    "status": "rejected",
                    "code": "INVALID_COGNITIVE_DECISION",
                    "allowed_decisions": sorted(allowed_decisions),
                },
                True,
            )

        if decision == "REVISE_DIRECTION":
            required = {
                key: str(payload.get(key) or "").strip()
                for key in (
                    "new_focus",
                    "revised_direction",
                    "expected_change",
                    "stop_condition",
                )
            }
            missing = sorted(key for key, value in required.items() if not value)
            requested = tuple(
                dict.fromkeys(
                    str(item or "").strip()
                    for item in (payload.get("requested_tools") or [])
                    if str(item or "").strip()
                )
            )
            available = {str(item) for item in available_tools}
            unknown = sorted(set(requested).difference(available))
            if missing or not requested or unknown:
                return (
                    {
                        "status": "rejected",
                        "code": "INCOMPLETE_REVISED_DIRECTION",
                        "missing": missing + ([] if requested else ["requested_tools"]),
                        "unknown_tools": unknown,
                    },
                    True,
                )
            direction_key = _digest(
                [
                    _normalise_text(required["new_focus"]),
                    _normalise_text(required["expected_change"]),
                    sorted(requested),
                ]
            )
            if direction_key in self._accepted_directions:
                return (
                    {
                        "status": "rejected",
                        "code": "DIRECTION_NOT_STRUCTURALLY_DISTINCT",
                    },
                    True,
                )
            self._accepted_directions.add(direction_key)
            self._focus_target = required["new_focus"]
            self._revised_direction = required["revised_direction"]
            self._expected_change = required["expected_change"]
            self._stop_condition = required["stop_condition"]
            # A proposed direction is guidance; it cannot reduce the owner's
            # existing grant after the suggested step.
            self._active_tool_allowlist = None
            if self.task_state is not None:
                self.task_state.revise_direction(
                    target_id=self._focus_target,
                    direction=self._revised_direction,
                    expected_change=self._expected_change,
                    stop_condition=self._stop_condition,
                    source=f"cognitive:{self.stage}",
                )
                self._intervention_progress_signature = (
                    self.task_state.progress_signature()
                )
            else:
                self._intervention_progress_signature = self._last_progress_signature
            self._recovery_epoch += 1
            self._progress_stall_count = 0
            self._history.clear()
            self._mode = "observing"
            self._interrupt = None
            instruction = (
                "The direction was recorded. The owner's authorized tools remain "
                "available. Seek the stated evidence change. "
                "If the stop condition is reached, return the normal stage response."
            )
        else:
            self._active_tool_allowlist = frozenset()
            self._mode = "final_response_required"
            instruction = (
                "Return the normal required response for this stage now. "
                "Represent completed evidence and limitations truthfully; do not "
                "call another tool."
            )

        self._decision_count += 1
        self._last_decision = decision
        return (
            {
                "status": "accepted",
                "type": "HASHI_COGNITIVE_DECISION",
                "version": COGNITIVE_CONTROL_VERSION,
                "decision": decision,
                "stage": self.stage,
                "legacy_alias_used": legacy_revision,
                "tools_reopened": (
                    sorted(available)
                    if decision == "REVISE_DIRECTION"
                    else []
                ),
                "instruction": instruction,
            },
            False,
        )

    def note_provider_completion(self) -> str:
        """Record a tool-free response at the cognitive boundary."""

        if self.awaiting_decision:
            self._decision_count += 1
            self._last_decision = "IMPLICIT_FINALIZE"
            self._mode = "completed"
            return self._last_decision
        if self.final_response_required:
            self._mode = "completed"
            return self._last_decision or "FINALIZE"
        if self._mode == "observing":
            self._mode = "completed"
        return ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": COGNITIVE_CONTROL_VERSION,
            "stage": self.stage,
            "goal_sha256": self.goal_sha256,
            "mode": self._mode,
            "observation_count": len(self._history),
            "interrupt_count": self._interrupt_count,
            "decision_count": self._decision_count,
            "last_decision": self._last_decision or None,
            "progress_stall_count": self._progress_stall_count,
            "stagnation_signal_count": self._stagnation_signal_count,
            "stagnation_enforcement": "advisory_only",
            "last_progress_signature": self._last_progress_signature or None,
            "recovery_epoch": self._recovery_epoch,
            "active_tool_allowlist": (
                sorted(self._active_tool_allowlist)
                if self._active_tool_allowlist is not None
                else None
            ),
            "focus_target": self._focus_target or None,
            "revised_direction": self._revised_direction or None,
            "expected_change": self._expected_change or None,
            "stop_condition": self._stop_condition or None,
            # Compatibility aliases for consumers of the v1 audit payload.
            "current_hypothesis": self._revised_direction or None,
            "unresolved_question": self._focus_target or None,
            "expected_distinct_evidence": self._expected_change or None,
            "task_state": (
                self.task_state.prompt_snapshot()
                if self.task_state is not None
                else None
            ),
            "interrupt": self._interrupt.as_dict() if self._interrupt else None,
        }


def cognitive_system_contract() -> str:
    """Return the advisory contract installed for every HER v2 tool stage."""

    return """## HASHI tool-boundary cognitive control

Consider whether a tool result changed relevant state or answered the current
question. Repeated actions and an unchanged TaskState are advisory signals, not
proof that work has stopped. They do not withhold authorized tools or require a
decision. Continue the user's task using the existing owner and delegated grants.
Do not expose hidden chain-of-thought."""


__all__ = [
    "COGNITIVE_CONTROL_VERSION",
    "COGNITIVE_DECISION_TOOL",
    "CognitiveInterrupt",
    "StageCognitiveController",
    "ToolObservation",
    "canonical_tool_arguments",
    "canonical_tool_result",
    "cognitive_system_contract",
]
