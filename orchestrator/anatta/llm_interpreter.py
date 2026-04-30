from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from uuid import uuid4
from typing import Any

from orchestrator.config import AgentConfig
from .models import DriveContribution, EmotionalAnnotation, EmergentTurnState, TurnContext


class BackendLLMInterpreter:
    """Backend-native LLM interpreter placeholder.

    This initial implementation deliberately avoids introducing a second API path.
    Integration with the agent's active backend will happen through runtime-owned
    execution hooks in a later patch. For now this class defines the contract and
    returns an empty contribution set so the package is safe to import.
    """

    def __init__(self, backend_manager: Any | None = None, config: Any | None = None):
        self.backend_manager = backend_manager
        self.config = config
        self._lock = asyncio.Lock()

    async def interpret_turn(self, turn_context: TurnContext) -> list[DriveContribution]:
        if self.backend_manager is None:
            return []
        prompt = self._build_turn_prompt(turn_context)
        payload = await self._run_structured_prompt(prompt, purpose="anatta-cue")
        if not payload:
            return []
        return self._payload_to_contributions(payload, default_source="llm_cue_interpreter")

    async def consolidate_event(
        self,
        turn_context: TurnContext,
        assistant_response: str,
        state: EmergentTurnState,
    ) -> EmotionalAnnotation | None:
        if self.backend_manager is None:
            return None
        prompt = self._build_consolidation_prompt(turn_context, assistant_response, state)
        payload = await self._run_structured_prompt(prompt, purpose="anatta-consolidate")
        if not payload:
            return None
        contributions = self._payload_to_contributions(payload, default_source="llm_consolidation")
        if not contributions:
            return None
        try:
            intensity = max(0, min(10, int(payload.get("intensity", 0))))
        except (TypeError, ValueError):
            intensity = 0
        return EmotionalAnnotation(
            annotation_id=None,
            bridge_row_type="turn",
            bridge_row_id=0,
            event_ts=state.generated_at,
            created_at=state.generated_at,
            source=turn_context.source,
            actor_role="system",
            event_type=str(payload.get("event_type", "general_emotional_event")),
            summary=str(payload.get("summary", "LLM-consolidated emotional event.")),
            intensity=intensity,
            dominant_drives=[str(x) for x in payload.get("dominant_drives", [])][:3],
            contributions=contributions,
            relationship_key=turn_context.relationship_key,
            tags=[str(x) for x in payload.get("tags", [])],
            metadata={
                "request_id": turn_context.request_id,
                "interpreter": "backend-native",
                **turn_context.metadata,
            },
            importance=float(payload.get("importance", 1.0)),
        )

    async def _run_structured_prompt(self, prompt: str, *, purpose: str) -> dict[str, Any] | None:
        async with self._lock:
            backend = await self._build_transient_backend()
            if backend is None:
                return None
            request_id = f"{purpose}-{uuid4().hex[:10]}"
            try:
                response = await backend.generate_response(prompt, request_id, silent=True)
            finally:
                await backend.shutdown()
            if not response or not getattr(response, "is_success", False):
                return None
            return self._extract_json_object(response.text)

    async def _build_transient_backend(self):
        current_backend = getattr(self.backend_manager, "current_backend", None)
        if current_backend is None:
            return None
        backend_cls = type(current_backend)
        base_cfg = current_backend.config
        extra = deepcopy(getattr(base_cfg, "extra", {}) or {})
        # Never let the interpreter piggy-back on a user-visible persistent session.
        extra["session_mode"] = False
        temp_cfg = AgentConfig(
            name=f"{base_cfg.name}-anatta",
            engine=base_cfg.engine,
            workspace_dir=base_cfg.workspace_dir,
            system_md=base_cfg.system_md,
            model=base_cfg.model,
            is_active=True,
            extra=extra,
            access_scope=base_cfg.access_scope,
            project_root=base_cfg.project_root,
        )
        backend = backend_cls(temp_cfg, current_backend.global_config, current_backend.api_key)
        ok = await backend.initialize()
        if not ok:
            return None
        if getattr(backend.capabilities, "supports_sessions", False):
            await backend.handle_new_session()
        return backend

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
            '      "metadata": {"event_type_hint": "repair"}\n'
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
        return (
            "You are consolidating an emotional event for an anatta-based agent memory layer.\n"
            "Output JSON only. No prose outside JSON.\n"
            "Do not invent a permanent personality. Summarize only the meaningful event signal.\n\n"
            "Return exactly this shape:\n"
            "{\n"
            '  "event_type": "repair",\n'
            '  "summary": "short event summary",\n'
            '  "intensity": 6,\n'
            '  "dominant_drives": ["CARE", "PANIC_GRIEF"],\n'
            '  "tags": ["repair", "validation"],\n'
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
