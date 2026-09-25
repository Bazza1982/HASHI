"""HER v3 sidecars: rate-limited Persona Commentary and Agent Companion.

These services observe the foreground model. They never plan or review the task.
Commentary informs the user; Agent Companion is silent unless a bounded JEV
liveness judgement finds strong evidence that execution is stuck or looping.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .commentary import CommentaryPort, DraftResponseCommentary, NeutralCommentary
from .models import Stage

HEALTH_QUESTION = {
    "type": "choice",
    "instructions": (
        "Judge ONLY execution liveness from observable runtime evidence. Select "
        "continue when credible forward progress exists or a managed long-running "
        "operation is behaving normally. Select trouble only for strong evidence "
        "of a repeated semantic loop, repeated failure without adaptation, prolonged "
        "lack of progress, or a stuck operation. Select unknown when evidence is "
        "insufficient. Do not review task quality, correctness, style, completeness, "
        "or create a new plan."
    ),
    "criteria": {
        "continue": "Credible progress or normal waiting is visible.",
        "trouble": "Strong observable evidence of a loop, stall, or repeated failure.",
        "unknown": "The snapshot does not justify intervention.",
    },
}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def parse_health(payload: Mapping[str, Any], *, threshold: float = 0.8) -> bool:
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        return False
    answer = answers.get("health")
    if not isinstance(answer, Mapping) or answer.get("type") != "choice":
        return False
    choice = str(answer.get("choice") or "unknown").casefold()
    probabilities = answer.get("probabilities")
    if choice not in HEALTH_QUESTION["criteria"] or not isinstance(probabilities, Mapping):
        return False
    values: list[float] = []
    for key in HEALTH_QUESTION["criteria"]:
        raw = probabilities.get(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return False
        value = float(raw)
        if not math.isfinite(value) or not 0 <= value <= 1:
            return False
        values.append(value)
    if not math.isclose(sum(values), 1.0, abs_tol=0.01):
        return False
    return choice == "trouble" and float(probabilities["trouble"]) >= threshold


class TurnServices(CommentaryPort):
    """One request-local observer shared by commentary and Agent Companion."""

    def __init__(
        self,
        *,
        turn_id: str,
        downstream: CommentaryPort | None,
        commentary_interval_s: float = 150.0,
        companion_enabled: bool = False,
        companion_interval_s: float = 300.0,
        companion_judge: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.turn_id = str(turn_id)
        self.downstream = downstream
        self.commentary_interval_s = max(120.0, min(180.0, float(commentary_interval_s)))
        self.companion_enabled = bool(companion_enabled)
        self.companion_interval_s = max(1.0, float(companion_interval_s))
        self.companion_judge = companion_judge
        self.clock = clock
        self.started_at = clock()
        self.last_activity_at = self.started_at
        self.last_commentary_at = 0.0
        self.last_progress_at = self.started_at
        self.progress_revision = 0
        self._event_serial = 0
        self._events: deque[dict[str, Any]] = deque(maxlen=24)
        self._active_tools: dict[str, dict[str, Any]] = {}
        self._result_fingerprints: deque[str] = deque(maxlen=12)
        self._pending_intervention = ""
        self._intervention_progress_revision = -1
        self._tasks: set[asyncio.Task] = set()
        self._companion_task: asyncio.Task | None = None
        self._closed = False
        self._initial_ack_sent = False

    def start(self) -> None:
        if (
            self.companion_enabled
            and self.companion_judge is not None
            and self._companion_task is None
        ):
            self._companion_task = asyncio.create_task(self._companion_loop())

    async def close(self) -> None:
        self._closed = True
        if self._companion_task is not None:
            self._companion_task.cancel()
        for task in tuple(self._tasks):
            task.cancel()
        pending = [task for task in (self._companion_task, *tuple(self._tasks)) if task]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._companion_task = None

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        if self._closed:
            return
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _record(self, kind: str, **payload: Any) -> None:
        self._event_serial += 1
        self.last_activity_at = self.clock()
        self._events.append({"serial": self._event_serial, "kind": kind, **payload})

    def tool_started(
        self, tool_name: str, arguments: Mapping[str, Any] | None, tool_call_id: str = ""
    ) -> None:
        call_id = str(tool_call_id or f"tool-{self._event_serial + 1}")
        name = str(tool_name or "tool")
        self._active_tools[call_id] = {
            "tool": name,
            "arguments_sha256": _digest(dict(arguments or {})),
            "started_at": self.clock(),
        }
        self._record("tool_started", tool=name, call_id=call_id)
        if not self._initial_ack_sent:
            self._initial_ack_sent = True
            self._spawn(
                self._emit(
                    f"Work has started. The current operation is {name}.",
                    required=False,
                    bypass_interval=True,
                )
            )

    def tool_completed(
        self,
        tool_name: str,
        *,
        tool_call_id: str = "",
        output: str = "",
        is_error: bool = False,
        details: Mapping[str, Any] | None = None,
        cognitive_interrupt: Mapping[str, Any] | None = None,
    ) -> None:
        call_id = str(tool_call_id or "")
        self._active_tools.pop(call_id, None)
        fingerprint = _digest(
            {
                "tool": str(tool_name or "tool"),
                "output": str(output or ""),
                "error": bool(is_error),
                "state_changed": (details or {}).get("state_changed"),
            }
        )
        novel = fingerprint not in self._result_fingerprints
        self._result_fingerprints.append(fingerprint)
        if novel:
            self.progress_revision += 1
            self.last_progress_at = self.clock()
        self._record(
            "tool_completed",
            tool=str(tool_name or "tool"),
            call_id=call_id,
            error=bool(is_error),
            result_sha256=fingerprint,
            cognitive_interrupt=bool(cognitive_interrupt),
        )
        if novel and self.clock() - self.last_commentary_at >= self.commentary_interval_s:
            self._spawn(
                self._emit(
                    f"Completed {str(tool_name or 'the current operation')}; new task evidence was observed. Work is continuing.",
                    required=False,
                )
            )

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "elapsed_s": round(max(0.0, now - self.started_at), 3),
            "last_activity_age_s": round(max(0.0, now - self.last_activity_at), 3),
            "last_progress_age_s": round(max(0.0, now - self.last_progress_at), 3),
            "progress_revision": self.progress_revision,
            "active_tools": [
                {
                    "call_id": key,
                    "tool": value["tool"],
                    "arguments_sha256": value["arguments_sha256"],
                    "age_s": round(max(0.0, now - float(value["started_at"])), 3),
                }
                for key, value in self._active_tools.items()
            ],
            "recent_events": list(self._events),
            "recent_result_fingerprints": list(self._result_fingerprints),
        }

    def take_intervention(self) -> str:
        """Return one AC notice at the next safe tool boundary, then consume it."""
        if not self._pending_intervention:
            return ""
        if self.progress_revision != self._intervention_progress_revision:
            self._pending_intervention = ""
            return ""
        notice = self._pending_intervention
        self._pending_intervention = ""
        return notice

    async def publish(self, commentary: NeutralCommentary) -> bool:
        text = str(commentary.text or "").strip()
        if not text:
            return False
        if self.clock() - self.last_commentary_at < self.commentary_interval_s:
            return False
        accepted = await self._forward(commentary)
        if accepted:
            self._initial_ack_sent = True
        return accepted

    async def publish_draft(self, commentary: DraftResponseCommentary) -> bool:
        if self.downstream is None:
            return False
        return bool(await self.downstream.publish_draft(commentary))

    async def _emit(
        self, text: str, *, required: bool, bypass_interval: bool = False
    ) -> bool:
        if self.downstream is None or not str(text).strip():
            return False
        if not bypass_interval and self.clock() - self.last_commentary_at < self.commentary_interval_s:
            return False
        self._event_serial += 1
        commentary = NeutralCommentary(
            event_id=f"{self.turn_id}:herv3-commentary:{self._event_serial}",
            turn_id=self.turn_id,
            stage=Stage.DIRECT,
            attempt=1,
            text=str(text).strip(),
        )
        return await self._forward(commentary)

    async def _forward(self, commentary: NeutralCommentary) -> bool:
        if self.downstream is None:
            return False
        accepted = bool(await self.downstream.publish(commentary))
        if accepted:
            self.last_commentary_at = self.clock()
        return accepted

    async def _companion_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.companion_interval_s)
            if self._closed or self.companion_judge is None:
                return
            state = self.snapshot()
            revision = self.progress_revision
            try:
                result = await self.companion_judge(state)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if not parse_health(result):
                continue
            # A JEV answer is advisory and may arrive after progress resumed.
            if revision != self.progress_revision:
                continue
            self._pending_intervention = (
                "Agent Companion observed apparent execution trouble from runtime "
                "evidence. Reassess the current approach before taking another tool "
                "action. If the blocker cannot be resolved, report it instead of "
                "repeating the same action."
            )
            self._intervention_progress_revision = self.progress_revision
