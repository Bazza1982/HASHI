"""Evidence-aware, provider-neutral working state for one HER v2 Turn.

The projection stores task conclusions and evidence references, never hidden
chain-of-thought.  Models own the semantics of each delta; runtime only applies
bounded structural updates, validates evidence references, and exposes a
stable progress signature for the lifecycle watchdog.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

HASHI_TASK_DELTA_ARGUMENT = "_hashi_task_delta"
TASK_STATE_VERSION = 1

_MAX_GOAL_CHARS = 1200
_MAX_TEXT_CHARS = 480
_MAX_ID_CHARS = 120
_MAX_STATE_ITEMS = 64
_MAX_PROMPT_ITEMS = 12
_MAX_STAGE_HISTORY = 16


def _bounded_text(value: Any, *, limit: int = _MAX_TEXT_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _stable_id(prefix: str, value: Any) -> str:
    normalized = _bounded_text(value, limit=2000).casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _item_id(value: Any) -> str:
    return _bounded_text(value, limit=_MAX_ID_CHARS)


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _string_items(value: Any) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item
            for item in (_bounded_text(raw, limit=600) for raw in _sequence(value))
            if item
        )
    )


def _digest(value: Any) -> str:
    rendered = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _bounded_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep an optional working model small without interpreting its semantics."""

    rendered = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, default=str)
    if len(rendered) <= 4000:
        parsed = json.loads(rendered)
        return parsed if isinstance(parsed, dict) else {}
    return {"summary": _bounded_text(rendered, limit=3800), "truncated": True}


@dataclass(frozen=True)
class TaskDeltaApplication:
    status: str
    delta_id: str
    version: int
    changed: bool = False
    meaningful_progress: bool = False
    accepted_evidence_refs: tuple[str, ...] = ()
    rejected_evidence_refs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "delta_id": self.delta_id or None,
            "version": self.version,
            "changed": self.changed,
            "meaningful_progress": self.meaningful_progress,
            "accepted_evidence_refs": list(self.accepted_evidence_refs),
            "rejected_evidence_refs": list(self.rejected_evidence_refs),
            "warnings": list(self.warnings),
        }


class HERTaskState:
    """One bounded TaskState shared by every stage in a HER v2 Turn."""

    def __init__(self, *, goal: str) -> None:
        self._lock = threading.RLock()
        self._goal = _bounded_text(goal, limit=_MAX_GOAL_CHARS)
        self._version = 0
        self._meaningful_revision = 0
        self._model_delta_count = 0
        self._criteria: dict[str, dict[str, Any]] = {}
        self._facts: dict[str, dict[str, Any]] = {}
        self._questions: dict[str, dict[str, Any]] = {}
        self._discarded_paths: dict[str, dict[str, Any]] = {}
        self._blockers: dict[str, dict[str, Any]] = {}
        self._focus: dict[str, Any] | None = None
        self._working_model: dict[str, Any] | None = None
        self._available_evidence_refs: dict[str, None] = {}
        self._applied_delta_ids: dict[str, None] = {}
        self._stage_history: list[dict[str, Any]] = []
        self._last_stage = ""

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    @property
    def model_delta_count(self) -> int:
        with self._lock:
            return self._model_delta_count

    def observe_evidence(self, evidence_refs: Sequence[str]) -> None:
        with self._lock:
            for ref in _string_items(evidence_refs):
                self._available_evidence_refs[ref] = None
            self._trim(self._available_evidence_refs, _MAX_STATE_ITEMS * 4)

    @staticmethod
    def _trim(items: dict[str, Any], limit: int = _MAX_STATE_ITEMS) -> None:
        while len(items) > limit:
            items.pop(next(iter(items)))

    @staticmethod
    def _recent(items: Mapping[str, Any], limit: int = _MAX_PROMPT_ITEMS) -> list[Any]:
        values = list(items.values())
        return [dict(item) for item in values[-limit:]]

    @staticmethod
    def _refs_from_row(row: Mapping[str, Any]) -> tuple[str, ...]:
        return _string_items(row.get("evidence_refs"))

    def _validated_refs(
        self,
        row: Mapping[str, Any],
        known: set[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = self._refs_from_row(row)
        valid = tuple(ref for ref in requested if ref in known)
        invalid = tuple(ref for ref in requested if ref not in known)
        return valid, invalid

    @staticmethod
    def _put(items: dict[str, dict[str, Any]], key: str, row: dict[str, Any]) -> bool:
        previous = items.get(key)
        if previous == row:
            return False
        items[key] = row
        HERTaskState._trim(items)
        return True

    def apply_delta(
        self,
        delta: Mapping[str, Any] | None,
        *,
        source: str,
        known_evidence_refs: Sequence[str] = (),
        allow_focus: bool = True,
    ) -> TaskDeltaApplication:
        """Apply one model-authored conclusion delta without judging its meaning."""

        if not isinstance(delta, Mapping):
            return TaskDeltaApplication(
                status="missing",
                delta_id="",
                version=self.version,
                warnings=("TASK_DELTA_MISSING",),
            )

        raw = dict(delta)
        supplied_id = _item_id(raw.get("delta_id"))
        delta_id = supplied_id or _stable_id(
            "delta",
            {"source": source, "delta": raw},
        )
        with self._lock:
            if delta_id in self._applied_delta_ids:
                return TaskDeltaApplication(
                    status="duplicate",
                    delta_id=delta_id,
                    version=self._version,
                )

            progress_before = self._progress_payload()
            known = set(self._available_evidence_refs)
            known.update(_string_items(known_evidence_refs))
            changed = False
            meaningful = False
            accepted_refs: set[str] = set()
            rejected_refs: set[str] = set()
            warnings: list[str] = []

            for raw_row in _sequence(raw.get("add_questions")):
                if not isinstance(raw_row, Mapping):
                    warnings.append("INVALID_QUESTION_ROW")
                    continue
                question = _bounded_text(raw_row.get("question") or raw_row.get("text"))
                identifier = _item_id(raw_row.get("id"))
                if not identifier or not question:
                    warnings.append("QUESTION_REQUIRES_ID_AND_TEXT")
                    continue
                previous = self._questions.get(identifier)
                status = (
                    str((previous or {}).get("status") or "open")
                    if previous
                    else "open"
                )
                changed |= self._put(
                    self._questions,
                    identifier,
                    {"id": identifier, "question": question, "status": status},
                )

            criteria_rows = tuple(_sequence(raw.get("criteria_updates"))) + tuple(
                _sequence(raw.get("add_criteria"))
            )
            for raw_row in criteria_rows:
                if not isinstance(raw_row, Mapping):
                    warnings.append("INVALID_CRITERION_ROW")
                    continue
                identifier = _item_id(raw_row.get("id"))
                description = _bounded_text(
                    raw_row.get("criterion")
                    or raw_row.get("description")
                    or raw_row.get("text")
                )
                if not identifier:
                    warnings.append("CRITERION_REQUIRES_ID")
                    continue
                previous = self._criteria.get(identifier) or {}
                status = (
                    str(raw_row.get("status") or previous.get("status") or "open")
                    .strip()
                    .casefold()
                )
                if status not in {"open", "satisfied", "blocked"}:
                    warnings.append(f"INVALID_CRITERION_STATUS:{identifier}")
                    continue
                valid, invalid = self._validated_refs(raw_row, known)
                accepted_refs.update(valid)
                rejected_refs.update(invalid)
                if status == "satisfied" and not valid:
                    warnings.append(
                        f"SATISFIED_CRITERION_REQUIRES_EVIDENCE:{identifier}"
                    )
                    continue
                combined_refs = tuple(
                    dict.fromkeys(_string_items(previous.get("evidence_refs")) + valid)
                )
                row = {
                    "id": identifier,
                    "criterion": description
                    or _bounded_text(previous.get("criterion")),
                    "status": status,
                    "evidence_refs": list(combined_refs),
                }
                row_changed = self._put(self._criteria, identifier, row)
                changed |= row_changed
                if row_changed and status == "satisfied":
                    meaningful = True

            for raw_row in _sequence(raw.get("add_facts")):
                if not isinstance(raw_row, Mapping):
                    warnings.append("INVALID_FACT_ROW")
                    continue
                identifier = _item_id(raw_row.get("id"))
                claim = _bounded_text(raw_row.get("claim") or raw_row.get("fact"))
                valid, invalid = self._validated_refs(raw_row, known)
                accepted_refs.update(valid)
                rejected_refs.update(invalid)
                if not identifier or not claim or not valid:
                    warnings.append("FACT_REQUIRES_ID_CLAIM_AND_EVIDENCE")
                    continue
                previous = self._facts.get(identifier) or {}
                combined_refs = tuple(
                    dict.fromkeys(_string_items(previous.get("evidence_refs")) + valid)
                )
                row_changed = self._put(
                    self._facts,
                    identifier,
                    {
                        "id": identifier,
                        "claim": claim,
                        "evidence_refs": list(combined_refs),
                    },
                )
                changed |= row_changed
                meaningful |= row_changed

            for raw_row in _sequence(raw.get("resolve_questions")):
                row = (
                    dict(raw_row)
                    if isinstance(raw_row, Mapping)
                    else {"id": raw_row, "evidence_refs": raw.get("evidence_refs")}
                )
                identifier = _item_id(row.get("id"))
                previous = self._questions.get(identifier)
                valid, invalid = self._validated_refs(row, known)
                accepted_refs.update(valid)
                rejected_refs.update(invalid)
                if not identifier or previous is None or not valid:
                    warnings.append(
                        f"RESOLVED_QUESTION_REQUIRES_OPEN_ID_AND_EVIDENCE:{identifier or 'missing'}"
                    )
                    continue
                updated = dict(previous)
                updated["status"] = "resolved"
                updated["evidence_refs"] = list(
                    dict.fromkeys(_string_items(previous.get("evidence_refs")) + valid)
                )
                row_changed = self._put(self._questions, identifier, updated)
                changed |= row_changed
                meaningful |= row_changed

            for field_name, target, text_keys in (
                ("discard_paths", self._discarded_paths, ("path", "description")),
                ("add_blockers", self._blockers, ("blocker", "description")),
            ):
                for raw_row in _sequence(raw.get(field_name)):
                    if not isinstance(raw_row, Mapping):
                        warnings.append(f"INVALID_{field_name.upper()}_ROW")
                        continue
                    identifier = _item_id(raw_row.get("id"))
                    description = ""
                    for key in text_keys:
                        description = _bounded_text(raw_row.get(key))
                        if description:
                            break
                    valid, invalid = self._validated_refs(raw_row, known)
                    accepted_refs.update(valid)
                    rejected_refs.update(invalid)
                    if not identifier or not description or not valid:
                        warnings.append(
                            f"{field_name.upper()}_REQUIRES_ID_TEXT_AND_EVIDENCE"
                        )
                        continue
                    previous = target.get(identifier) or {}
                    combined_refs = tuple(
                        dict.fromkeys(
                            _string_items(previous.get("evidence_refs")) + valid
                        )
                    )
                    row_changed = self._put(
                        target,
                        identifier,
                        {
                            "id": identifier,
                            text_keys[0]: description,
                            "evidence_refs": list(combined_refs),
                        },
                    )
                    changed |= row_changed
                    meaningful |= row_changed

            raw_focus = raw.get("set_focus")
            if isinstance(raw_focus, Mapping):
                if allow_focus:
                    focus = {
                        "target_id": _item_id(
                            raw_focus.get("target_id") or raw_focus.get("target")
                        ),
                        "intent": _bounded_text(raw_focus.get("intent")),
                        "expected_change": _bounded_text(
                            raw_focus.get("expected_change")
                        ),
                    }
                    if focus["target_id"] and focus["expected_change"]:
                        if focus != self._focus:
                            self._focus = focus
                            changed = True
                    else:
                        warnings.append("FOCUS_REQUIRES_TARGET_AND_EXPECTED_CHANGE")
                else:
                    warnings.append("SUB_AGENT_FOCUS_IGNORED")
            if (
                raw.get("clear_focus") is True
                and allow_focus
                and self._focus is not None
            ):
                self._focus = None
                changed = True

            working_model = raw.get("working_model")
            if isinstance(working_model, Mapping):
                bounded = _bounded_mapping(working_model)
                if bounded != self._working_model:
                    self._working_model = bounded
                    changed = True

            self._applied_delta_ids[delta_id] = None
            self._trim(self._applied_delta_ids, _MAX_STATE_ITEMS * 8)
            self._model_delta_count += 1
            meaningful = self._progress_payload() != progress_before
            if changed:
                self._version += 1
            if meaningful:
                self._meaningful_revision += 1
            return TaskDeltaApplication(
                status="accepted" if changed else "accepted_no_change",
                delta_id=delta_id,
                version=self._version,
                changed=changed,
                meaningful_progress=meaningful,
                accepted_evidence_refs=tuple(sorted(accepted_refs)),
                rejected_evidence_refs=tuple(sorted(rejected_refs)),
                warnings=tuple(dict.fromkeys(warnings)),
            )

    def revise_direction(
        self,
        *,
        target_id: str,
        direction: str,
        expected_change: str,
        stop_condition: str,
        source: str,
    ) -> None:
        with self._lock:
            focus = {
                "target_id": _item_id(target_id),
                "intent": _bounded_text(direction),
                "expected_change": _bounded_text(expected_change),
                "stop_condition": _bounded_text(stop_condition),
                "source": _bounded_text(source, limit=160),
            }
            if focus != self._focus:
                self._focus = focus
                self._version += 1

    def record_stage_completion(
        self,
        *,
        stage: str,
        output: Mapping[str, Any] | str | None,
        cited_evidence_refs: Sequence[str] = (),
    ) -> None:
        """Project only validated stage outputs into the shared working state."""

        stage_name = _bounded_text(stage, limit=80).casefold() or "unknown"
        data = dict(output) if isinstance(output, Mapping) else {}
        with self._lock:
            resolved_goal = _bounded_text(data.get("real_goal"), limit=_MAX_GOAL_CHARS)
            changed = False
            if resolved_goal and resolved_goal != self._goal:
                self._goal = resolved_goal
                changed = True

            raw_criteria: Any = data.get("success_criteria")
            brief = data.get("execution_brief")
            if isinstance(brief, Mapping):
                raw_criteria = brief.get("success_criteria")
            if isinstance(raw_criteria, str):
                raw_criteria = [raw_criteria]
            for criterion in _string_items(raw_criteria):
                identifier = _stable_id("criterion", criterion)
                changed |= self._put(
                    self._criteria,
                    identifier,
                    {
                        "id": identifier,
                        "criterion": criterion,
                        "status": str(
                            (self._criteria.get(identifier) or {}).get("status")
                            or "open"
                        ),
                        "evidence_refs": list(
                            _string_items(
                                (self._criteria.get(identifier) or {}).get(
                                    "evidence_refs"
                                )
                            )
                        ),
                    },
                )

            cited = tuple(
                ref
                for ref in _string_items(cited_evidence_refs)
                if ref in self._available_evidence_refs
            )
            conclusion = _bounded_text(
                data.get("summary") or data.get("reason") or data.get("result")
            )
            if conclusion and cited:
                identifier = _stable_id(f"{stage_name}-conclusion", conclusion)
                row_changed = self._put(
                    self._facts,
                    identifier,
                    {
                        "id": identifier,
                        "claim": conclusion,
                        "evidence_refs": list(cited),
                    },
                )
                changed |= row_changed
                if row_changed:
                    self._meaningful_revision += 1

            output_digest = _digest(data if data else _bounded_text(output, limit=2000))
            self._last_stage = stage_name
            self._stage_history.append(
                {
                    "stage": stage_name,
                    "output_digest": output_digest,
                    "cited_evidence_refs": list(cited),
                }
            )
            self._stage_history = self._stage_history[-_MAX_STAGE_HISTORY:]
            if changed:
                self._version += 1

    def _progress_payload(self) -> dict[str, Any]:
        return {
            "satisfied_criteria": sorted(
                (
                    row["id"],
                    tuple(sorted(_string_items(row.get("evidence_refs")))),
                )
                for row in self._criteria.values()
                if row.get("status") == "satisfied"
            ),
            "resolved_questions": sorted(
                (
                    row["id"],
                    tuple(sorted(_string_items(row.get("evidence_refs")))),
                )
                for row in self._questions.values()
                if row.get("status") == "resolved"
            ),
            "facts": sorted(
                (
                    row["id"],
                    tuple(sorted(_string_items(row.get("evidence_refs")))),
                )
                for row in self._facts.values()
            ),
            "discarded_paths": sorted(
                (
                    row["id"],
                    tuple(sorted(_string_items(row.get("evidence_refs")))),
                )
                for row in self._discarded_paths.values()
            ),
            "blockers": sorted(
                (
                    row["id"],
                    tuple(sorted(_string_items(row.get("evidence_refs")))),
                )
                for row in self._blockers.values()
            ),
        }

    def progress_signature(self) -> str:
        with self._lock:
            return _digest(self._progress_payload())

    def prompt_snapshot(self) -> dict[str, Any]:
        """Return the compact state projection that models need while working."""

        snapshot = self.snapshot()
        snapshot.pop("stage_history", None)
        snapshot.pop("truncated_counts", None)
        return snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            available = list(self._available_evidence_refs)
            return {
                "schema_version": TASK_STATE_VERSION,
                "version": self._version,
                "meaningful_revision": self._meaningful_revision,
                "model_delta_count": self._model_delta_count,
                "goal": self._goal,
                "criteria": self._recent(self._criteria),
                "facts": self._recent(self._facts),
                "open_questions": self._recent(self._questions),
                "focus": dict(self._focus) if self._focus else None,
                "discarded_paths": self._recent(self._discarded_paths, 8),
                "blockers": self._recent(self._blockers, 8),
                "working_model": (
                    dict(self._working_model) if self._working_model else None
                ),
                "recent_available_evidence_refs": available[-12:],
                "last_stage": self._last_stage or None,
                "stage_history": [dict(item) for item in self._stage_history[-8:]],
                "progress_signature": _digest(self._progress_payload()),
                "truncated_counts": {
                    "criteria": max(0, len(self._criteria) - _MAX_PROMPT_ITEMS),
                    "facts": max(0, len(self._facts) - _MAX_PROMPT_ITEMS),
                    "open_questions": max(0, len(self._questions) - _MAX_PROMPT_ITEMS),
                    "discarded_paths": max(0, len(self._discarded_paths) - 8),
                    "blockers": max(0, len(self._blockers) - 8),
                },
            }


def task_state_contract(
    task_state: Mapping[str, Any],
    *,
    stage: str,
    tool_enabled: bool,
) -> str:
    """Render the compact lifecycle contract installed beside each stage prompt."""

    stage_name = str(stage or "tool").strip().casefold() or "tool"
    lens = {
        "direct": "Prefer the shortest path and define what evidence is sufficient.",
        "triage": "Identify the decisive questions, evidence standard, and strategic direction.",
        "planning": "Resolve execution-critical unknowns and choose discriminating checks.",
        "execution": "Interpret each result, update evidence, and choose the smallest useful next action.",
        "replanning": "Name the evidence that invalidated the old direction and preserve what remains valid.",
        "review": "Check which completion claim lacks independent evidence or has a regression risk.",
    }.get(stage_name, "Keep conclusions, open questions, and evidence current.")
    tool_clause = (
        f"""
For every ordinary tool call, include a sibling object argument named
`{HASHI_TASK_DELTA_ARGUMENT}`. It updates conclusions from results received
before that action and is stripped before the real tool executes. Do not add a
reflection call. Use one stable `delta_id`; parallel calls from the same model
turn may repeat that ID safely. The delta may contain:

```json
{{
  "delta_id": "stable-stage-step-id",
  "add_questions": [{{"id": "q1", "question": "..."}}],
  "criteria_updates": [{{"id": "c1", "status": "satisfied", "evidence_refs": ["exact receipt"]}}],
  "add_facts": [{{"id": "f1", "claim": "...", "evidence_refs": ["exact receipt"]}}],
  "resolve_questions": [{{"id": "q1", "evidence_refs": ["exact receipt"]}}],
  "discard_paths": [{{"id": "p1", "path": "...", "evidence_refs": ["exact receipt"]}}],
  "add_blockers": [{{"id": "b1", "blocker": "...", "evidence_refs": ["exact receipt"]}}],
  "set_focus": {{"target_id": "q1", "intent": "...", "expected_change": "..."}},
  "working_model": {{"hypotheses": []}}
}}
```

Facts, resolved questions, satisfied criteria, discarded paths, and blockers
count as progress only when bound to an exact HASHI evidence receipt. Merely
rewriting focus, plans, confidence, or natural-language labels does not.
""".strip()
        if tool_enabled
        else (
            "This stage has no tool-boundary delta. Read this state as the shared "
            "working map; the validated stage result will update its projection."
        )
    )
    return (
        "## HASHI persistent TaskState\n\n"
        "This is an auditable task-conclusion projection, not hidden reasoning. "
        "Model owns semantics; runtime only persists evidence-linked deltas.\n\n"
        f"Stage lens: {lens}\n\n"
        f"{tool_clause}\n\n"
        "Current TaskState:\n```json\n"
        + json.dumps(dict(task_state), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n```"
    )


__all__ = [
    "HASHI_TASK_DELTA_ARGUMENT",
    "HERTaskState",
    "TASK_STATE_VERSION",
    "TaskDeltaApplication",
    "task_state_contract",
]
