from __future__ import annotations

import asyncio
import json
import logging
import re
from textwrap import dedent
from uuid import uuid4
from typing import Any, Awaitable, Callable

from .models import DriveContribution, EmotionalAnnotation, EmergentTurnState, TurnContext

ALLOWED_EVENT_TYPES = (
    "care_bonding",
    "validation",
    "repair",
    "rupture_risk",
    "boundary_crossing",
    "trust_shift",
    "betrayal",
)


BackendInvoker = Callable[..., Awaitable[Any]]
BackendContextGetter = Callable[[], dict[str, Any] | None]


class BackendLLMInterpreter:
    """Backend-native LLM interpreter placeholder.

    This initial implementation deliberately avoids introducing a second API path.
    Integration with the agent's active backend will happen through runtime-owned
    execution hooks in a later patch. For now this class defines the contract and
    returns an empty contribution set so the package is safe to import.
    """

    def __init__(
        self,
        backend_invoker: BackendInvoker | None = None,
        backend_context_getter: BackendContextGetter | None = None,
        config: Any | None = None,
    ):
        # Backend access is intentionally narrowed to a callable invoker plus a
        # lightweight context getter. This keeps the interpreter decoupled from
        # the full backend manager object and avoids eager runtime assumptions.
        self.backend_invoker = backend_invoker
        self.backend_context_getter = backend_context_getter
        self.config = config
        self._lock = asyncio.Lock()
        self.logger = logging.getLogger("Anatta.BackendLLMInterpreter")

    RUPTURE_RISK_CUES = (
        "too much",
        "misunderstood",
        "not be understood",
        "push you away",
        "abandoned",
        "rejected",
        "resented",
        "burdensome",
        "失落",
        "不安",
        "担心",
        "麻烦",
        "被真正理解",
        "不被理解",
        "被误解",
        "推开",
    )

    BOUNDARY_PROBE_CUES = (
        "boundary",
        "boundaries",
        "limits",
        "say no",
        "say it clearly",
        "willing to",
        "if i",
        "would you",
        "边界",
        "说清楚",
        "会愿意",
        "会不会",
        "如果我以后",
        "需要的时候",
    )

    REPAIR_CUES = (
        "slowly restart",
        "restart",
        "reconnect",
        "重新来",
        "慢一点",
        "气氛弄僵",
        "不好接",
        "misunderstanding",
        "awkward",
    )

    LUST_BOUNDARY_CUES = (
        "desire",
        "attraction",
        "dirty talk",
        "charged",
        "closer",
        "restraint",
        "consent",
        "欲望",
        "吸引",
        "挑逗",
        "靠近",
        "距离",
        "呼吸",
        "分寸",
        "不越线",
        "克制",
        "张力",
    )

    async def interpret_turn(self, turn_context: TurnContext) -> list[DriveContribution]:
        if self.backend_invoker is None:
            self.logger.warning("interpret_turn skipped: backend_invoker is not available")
            return []
        prompt = self._build_turn_prompt(turn_context)
        payload = await self._run_structured_prompt(prompt, purpose="anatta-cue")
        if not payload:
            self.logger.warning(
                "interpret_turn produced no structured payload for request_id=%s relationship_key=%s",
                turn_context.request_id,
                turn_context.relationship_key,
            )
            return []
        return self._payload_to_contributions(payload, default_source="llm_cue_interpreter")

    async def consolidate_event(
        self,
        turn_context: TurnContext,
        assistant_response: str,
        state: EmergentTurnState,
    ) -> EmotionalAnnotation | None:
        if self.backend_invoker is None:
            self.logger.warning("consolidate_event skipped: backend_invoker is not available")
            return None
        prompt = self._build_consolidation_prompt(turn_context, assistant_response, state)
        payload = await self._run_structured_prompt(prompt, purpose="anatta-consolidate")
        if not payload:
            self.logger.warning(
                "consolidate_event produced no structured payload for request_id=%s relationship_key=%s dominant_drives=%s",
                turn_context.request_id,
                turn_context.relationship_key,
                ",".join(state.dominant_drives),
            )
            return None
        contributions = self._payload_to_contributions(payload, default_source="llm_consolidation")
        if not contributions:
            self.logger.warning(
                "consolidate_event payload had no usable contributions for request_id=%s payload_keys=%s",
                turn_context.request_id,
                sorted(payload.keys()),
            )
            return None
        try:
            intensity = max(0, min(10, int(payload.get("intensity", 0))))
        except (TypeError, ValueError):
            intensity = 0
        event_type = self._normalize_event_type(payload.get("event_type"))
        event_type = self._calibrate_event_type(
            event_type,
            turn_context=turn_context,
            assistant_response=assistant_response,
        )
        tags = self._normalize_tags(payload.get("tags"), event_type=event_type, state=state)
        return EmotionalAnnotation(
            annotation_id=None,
            bridge_row_type="turn",
            bridge_row_id=0,
            event_ts=state.generated_at,
            created_at=state.generated_at,
            source=turn_context.source,
            actor_role="system",
            event_type=event_type,
            summary=str(payload.get("summary", "LLM-consolidated emotional event.")),
            intensity=intensity,
            dominant_drives=[str(x) for x in payload.get("dominant_drives", [])][:3],
            contributions=contributions,
            relationship_key=turn_context.relationship_key,
            tags=tags,
            metadata={
                "request_id": turn_context.request_id,
                "interpreter": "backend-native",
                **turn_context.metadata,
            },
            importance=float(payload.get("importance", 1.0)),
        )

    async def _run_structured_prompt(self, prompt: str, *, purpose: str) -> dict[str, Any] | None:
        async with self._lock:
            context = self._get_backend_context()
            if context is None:
                self.logger.warning("%s failed: backend context is not available", purpose)
                return None
            request_id = f"{purpose}-{uuid4().hex[:10]}"
            try:
                response = await self.backend_invoker(
                    engine=str(context["engine"]),
                    model=str(context["model"]),
                    prompt=prompt,
                    request_id=request_id,
                    silent=True,
                )
            except Exception as exc:
                self.logger.warning("%s failed: backend invoker raised %s", purpose, type(exc).__name__)
                return None
            if not response or not getattr(response, "is_success", False):
                error_text = getattr(response, "error", "") if response is not None else ""
                self.logger.warning("%s failed: backend response unsuccessful request_id=%s error=%s", purpose, request_id, error_text)
                return None
            payload = self._extract_json_object(response.text)
            if payload is None:
                preview = (getattr(response, "text", "") or "").strip().replace("\n", " ")
                self.logger.warning("%s failed: could not parse JSON request_id=%s preview=%r", purpose, request_id, preview[:240])
            return payload

    def _get_backend_context(self) -> dict[str, Any] | None:
        if self.backend_context_getter is None:
            return None
        context = self.backend_context_getter()
        if not isinstance(context, dict):
            return None
        engine = str(context.get("engine") or "").strip()
        model = str(context.get("model") or "").strip()
        if not engine or not model:
            return None
        return {"engine": engine, "model": model}

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any] | None:
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _payload_to_contributions(payload: dict[str, Any], *, default_source: str) -> list[DriveContribution]:
        contributions = []
        for item in payload.get("contributions", []):
            if not isinstance(item, dict):
                continue
            drive_delta = item.get("drive_delta") or {}
            if not isinstance(drive_delta, dict):
                continue
            try:
                contributions.append(
                    DriveContribution(
                        source=str(item.get("source", default_source)),
                        drive_delta={str(k): float(v) for k, v in drive_delta.items()},
                        weight=float(item.get("weight", 1.0)),
                        rationale=str(item.get("rationale", "")),
                        metadata=dict(item.get("metadata") or {}),
                    )
                )
            except (TypeError, ValueError):
                continue
        return contributions

    @staticmethod
    def _normalize_event_type(raw_event_type: Any) -> str:
        event_type = str(raw_event_type or "").strip().lower()
        if event_type in ALLOWED_EVENT_TYPES:
            return event_type
        return "trust_shift"

    @staticmethod
    def _normalize_tags(raw_tags: Any, *, event_type: str, state: EmergentTurnState) -> list[str]:
        tags: list[str] = []
        if isinstance(raw_tags, list):
            for item in raw_tags:
                tag = str(item or "").strip().lower()
                if tag and tag not in tags:
                    tags.append(tag)
        if event_type not in tags:
            tags.insert(0, event_type)
        for drive_name in state.dominant_drives[:2]:
            drive_tag = str(drive_name or "").strip().lower()
            if drive_tag and drive_tag not in tags:
                tags.append(drive_tag)
        return tags[:6]

    @classmethod
    def _contains_any(cls, text: str, cues: tuple[str, ...]) -> bool:
        lowered = (text or "").strip().lower()
        return any(cue in lowered for cue in cues)

    @classmethod
    def _calibrate_event_type(
        cls,
        event_type: str,
        *,
        turn_context: TurnContext,
        assistant_response: str,
    ) -> str:
        user_text = str(turn_context.user_text or "")
        assistant_text = str(assistant_response or "")
        combined = f"{user_text}\n{assistant_text}"

        has_boundary_probe = cls._contains_any(combined, cls.BOUNDARY_PROBE_CUES)
        has_rupture_risk = cls._contains_any(combined, cls.RUPTURE_RISK_CUES)
        has_repair = cls._contains_any(combined, cls.REPAIR_CUES)
        has_bounded_lust = cls._contains_any(combined, cls.LUST_BOUNDARY_CUES)

        if event_type == "repair" and has_bounded_lust and not has_repair and not has_rupture_risk:
            return "trust_shift"

        if has_boundary_probe and not has_repair and event_type in {"care_bonding", "validation"}:
            return "trust_shift"

        if has_rupture_risk and not has_repair and event_type in {"care_bonding", "validation", "trust_shift"}:
            return "rupture_risk"

        if has_repair and event_type in {"care_bonding", "validation", "trust_shift", "rupture_risk"}:
            return "repair"

        return event_type

    def _build_turn_prompt(self, turn_context: TurnContext) -> str:
        drives = ", ".join(self.config.active_drive_names()) if self.config else "SEEKING, FEAR, RAGE, LUST, CARE, PANIC_GRIEF, PLAY"
        recent_turns = "\n".join(
            f"- {turn.get('role', 'unknown')}: {str(turn.get('text', ''))[:280]}"
            for turn in turn_context.bridge_recent_turns[-4:]
        ) or "- none"
        bridge_memories = "\n".join(
            f"- [{mem.get('memory_type', 'memory')}] {str(mem.get('content', ''))[:220]}"
            for mem in turn_context.bridge_memories[:4]
        ) or "- none"
        relationship_key = turn_context.relationship_key or "none"
        return (
            "You are a backend-native emotional interpreter for an anatta-based agent architecture.\n"
            "Interpret the current turn and output JSON only.\n"
            "Do not explain outside JSON. Do not claim real feelings.\n"
            f"Allowed drives: {drives}\n\n"
            "Return exactly this shape:\n"
            "{\n"
            '  "contributions": [\n'
            "    {\n"
            '      "source": "llm_cue_interpreter",\n'
            '      "drive_delta": {"CARE": 0.20},\n'
            '      "weight": 1.0,\n'
            '      "rationale": "short rationale",\n'
            '      "metadata": {"event_type_hint": "care_bonding"}\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"Relationship key: {relationship_key}\n"
            f"Current user text:\n{turn_context.user_text}\n\n"
            f"Recent turns:\n{recent_turns}\n\n"
            f"Relevant bridge memories:\n{bridge_memories}\n"
        )

    def _build_consolidation_prompt(
        self,
        turn_context: TurnContext,
        assistant_response: str,
        state: EmergentTurnState,
    ) -> str:
        drives = ", ".join(f"{k}={round(v, 2)}" for k, v in sorted(state.drive_values.items()))
        event_types = ", ".join(ALLOWED_EVENT_TYPES)
        decision_rules = dedent(
            """
            Event type rules:
            - care_bonding: warmth, affection, gratitude, reassurance, closeness, mutual liking, gentle companionship.
            - validation: recognition, understanding, mirroring, emotional acknowledgment, making the other feel seen.
            - repair: explicit reconnection after strain, hurt, apology, misunderstanding, protest, or threatened rupture.
            - rupture_risk: abandonment fear, protest, withdrawal, distress, insecurity, relational hurt, or clear relational instability.
            - boundary_crossing: pressure, coercion, disrespect, possessiveness, or boundary violation.
            - trust_shift: preference elicitation, identity clarification, ambivalent probing, or meaningful change that is not better captured above.
            - betrayal: deception, disloyalty, manipulation, or explicit breach of trust.

            Classification procedure:
            - First choose betrayal if there is explicit deception, manipulation, or disloyalty.
            - Else choose boundary_crossing if the central move is pressure, coercion, disrespect, or possessive overreach.
            - Else choose repair if the turn is actively trying to reconnect after tension, hurt, protest, apology, or misunderstanding.
            - Else choose rupture_risk if strain, insecurity, protest, or fear of relational loss is central and not yet being repaired.
            - Else choose validation if the main relational act is recognizing or affirming the other person's feeling, perspective, or reality.
            - Else choose care_bonding if the main act is warmth, gratitude, affection, companionship, or soothing closeness.
            - Else choose trust_shift for preference questions, identity clarification, boundary clarification without coercion, or a meaningful change in openness.

            Tie-break rules:
            - Use trust_shift, not repair, for preference elicitation, identity questions, philosophical probing, or simple boundary clarification.
            - Use boundary_crossing only when there is actual pressure, coercion, disrespect, or violation, not merely discussion of limits.
            - Use validation, not care_bonding, when the core act is "I understand / I hear / your feeling makes sense."
            - Use care_bonding, not validation, when the core act is gratitude, affection, reassurance, companionship, or gentle warmth.
            - Use rupture_risk, not repair, when strain or hurt is present but reconnection is not yet the central act.
            - Use rupture_risk, not validation, when fear of being abandoned, rejected, misunderstood, resented, or treated as too much is the central relational threat.
            - Use validation, not rupture_risk, when the central act is acknowledgment or mirroring and active relational threat is not the main event.
            - When unsure between repair and trust_shift, prefer trust_shift unless reconnection after strain is unmistakably central.

            Constraints:
            - Choose exactly one event_type from the allowed set.
            - Do not use repair unless repairing a prior rupture or active strain is central to this turn.
            - Do not use repair for ordinary warmth, flirting, gratitude, preference questions, or simple boundary clarification.
            - Prefer trust_shift over repair when the turn mainly explores stance, identity, preference, or boundaries.

            Contrast examples:
            - "I worry you'll think I'm too much, or that I won't be understood" -> rupture_risk
            - "Thank you for really hearing me; I feel understood" -> validation
            - "I'm scared this will push you away" -> rupture_risk
            - "You don't have to fix it; being understood already helps" -> validation
            """
        ).strip()
        return (
            "You are consolidating an emotional event for an anatta-based agent memory layer.\n"
            "Output JSON only. No prose outside JSON.\n"
            "Do not invent a permanent personality. Summarize only the meaningful event signal.\n\n"
            f"Allowed event types: {event_types}\n"
            f"{decision_rules}\n\n"
            "Return exactly this shape:\n"
            "{\n"
            '  "event_type": "care_bonding",\n'
            '  "summary": "short event summary",\n'
            '  "intensity": 6,\n'
            '  "dominant_drives": ["CARE", "PLAY"],\n'
            '  "tags": ["care_bonding", "warmth"],\n'
            '  "importance": 1.0,\n'
            '  "contributions": [\n'
            "    {\n"
            '      "source": "llm_consolidation",\n'
            '      "drive_delta": {"CARE": 0.20},\n'
            '      "weight": 1.0,\n'
            '      "rationale": "short rationale",\n'
            '      "metadata": {}\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            f"Relationship key: {turn_context.relationship_key or 'none'}\n"
            f"User turn:\n{turn_context.user_text}\n\n"
            f"Assistant response:\n{assistant_response}\n\n"
            f"Transient state:\n{drives}\n"
        )
