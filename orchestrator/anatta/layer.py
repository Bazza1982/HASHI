from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .aggregation import DriveAggregator
from .bridge_adapter import BridgeMemoryAdapter
from .bootstrap import BootstrapManager
from .composer import PromptComposer
from .config import AnattaConfig
from .llm_interpreter import BackendLLMInterpreter
from .memory import AnattaMemoryStore
from .models import DriveContribution, EmotionalAnnotation, PromptInjection, TurnContext
from .relationship import RelationshipInterpreter


class EmotionalSelfLayer:
    def __init__(
        self,
        config: AnattaConfig,
        bridge_adapter: BridgeMemoryAdapter,
        memory_store: AnattaMemoryStore,
        cue_interpreter: BackendLLMInterpreter,
        relationship_interpreter: RelationshipInterpreter,
        aggregator: DriveAggregator,
        composer: PromptComposer,
        bootstrap: BootstrapManager,
    ):
        self.config = config
        self.bridge_adapter = bridge_adapter
        self.memory_store = memory_store
        self.cue_interpreter = cue_interpreter
        self.relationship_interpreter = relationship_interpreter
        self.aggregator = aggregator
        self.composer = composer
        self.bootstrap = bootstrap
        self.logger = logging.getLogger("Anatta.EmotionalSelfLayer")
        self._missing_config_warned = False
        self._ensure_bootstrap_seeded()

    def resolve_relationship_key(self, source: str, chat_id: int | None = None, metadata: dict[str, Any] | None = None) -> str | None:
        if chat_id is not None:
            return f"{source}:{chat_id}"
        if metadata and metadata.get("relationship_key"):
            return str(metadata["relationship_key"])
        return None

    async def build_turn_state(self, turn_context: TurnContext, model_name: str) -> tuple[Any, PromptInjection]:
        self.config.reload()
        if not self.config.is_enabled():
            if not self.config.exists() and not self._missing_config_warned:
                self.logger.warning(
                    "Anatta is off because no anatta_config.json was found at %s. Create the config file to enable shadow/on mode.",
                    self.config.path,
                )
                self._missing_config_warned = True
            state = self.aggregator.aggregate([], contributing_annotation_ids=[], relationship_key=turn_context.relationship_key)
            injection = PromptInjection(
                title=self.composer.SECTION_TITLE,
                body="Anatta mode is off. No transient emotional self-state should be injected.",
                metadata={"mode": self.config.mode(), "inject": False},
            )
            return state, injection
        annotations = self.memory_store.retrieve_relevant_annotations(turn_context, limit=12)
        memory_contributions = list(self.bootstrap.drive_prior_contributions())
        annotation_ids = []
        retrieval_policy = self.config.retrieval_policy()
        min_memory_weight = max(
            0.0,
            min(1.0, float(retrieval_policy.get("minimum_memory_contribution_weight", 0.25))),
        )
        for annotation in annotations:
            annotation_ids.append(int(annotation.annotation_id or 0))
            retrieval_score = float(annotation.metadata.get("_retrieval_score", 1.0))
            retrieval_weight = max(min_memory_weight, min(1.0, retrieval_score))
            for contribution in annotation.contributions:
                scaled_delta = {
                    drive_name: float(delta)
                    * self._contextual_drive_multiplier(drive_name, turn_context, retrieval_score)
                    for drive_name, delta in contribution.drive_delta.items()
                }
                memory_contributions.append(
                    DriveContribution(
                        source=f"memory:{contribution.source}",
                        drive_delta=scaled_delta,
                        weight=float(contribution.weight) * retrieval_weight,
                        rationale=contribution.rationale,
                        metadata={
                            **contribution.metadata,
                            "annotation_id": annotation.annotation_id,
                            "retrieval_score": retrieval_score,
                            "retrieval_weight": retrieval_weight,
                        },
                    )
                )
        cue_contributions = await self.cue_interpreter.interpret_turn(turn_context)
        relationship_contributions = await self.relationship_interpreter.interpret_relationship(turn_context)
        state = self.aggregator.aggregate(
            contributions=[*memory_contributions, *cue_contributions, *relationship_contributions],
            contributing_annotation_ids=[aid for aid in annotation_ids if aid > 0],
            relationship_key=turn_context.relationship_key,
        )
        injection = self.composer.compose(state, model_name)
        injection.metadata["mode"] = self.config.mode()
        injection.metadata["inject"] = self.config.should_inject_prompt()
        return state, injection

    async def record_async(self, turn_context: TurnContext, assistant_response: str, state: Any) -> None:
        self.config.reload()
        if not self.config.should_record_annotations():
            self.logger.info(
                "Anatta record skipped because mode=%s for request_id=%s relationship_key=%s",
                self.config.mode(),
                turn_context.request_id,
                turn_context.relationship_key,
            )
            return
        annotation = await self.cue_interpreter.consolidate_event(turn_context, assistant_response, state)
        if annotation is not None:
            self.memory_store.record_annotation(annotation)
            self.logger.info(
                "Anatta recorded consolidated annotation type=%s intensity=%s request_id=%s relationship_key=%s",
                annotation.event_type,
                annotation.intensity,
                turn_context.request_id,
                turn_context.relationship_key,
            )
            return
        fallback = self._build_fallback_annotation(turn_context, state)
        intensity = fallback.intensity
        event_type = fallback.event_type
        if not self.memory_store.should_record(intensity=intensity, event_type=event_type):
            self.logger.warning(
                "Anatta produced no persisted record for request_id=%s relationship_key=%s dominant_drives=%s max_drive=%.3f fallback_type=%s fallback_intensity=%s",
                turn_context.request_id,
                turn_context.relationship_key,
                ",".join(state.dominant_drives),
                max(state.drive_values.values(), default=0.0),
                event_type,
                intensity,
            )
            return
        self.memory_store.record_annotation(fallback)
        self.logger.info(
            "Anatta recorded fallback annotation type=%s intensity=%s request_id=%s relationship_key=%s dominant_drives=%s",
            fallback.event_type,
            fallback.intensity,
            turn_context.request_id,
            turn_context.relationship_key,
            ",".join(fallback.dominant_drives),
        )

    def should_inject_prompt(self) -> bool:
        return self.config.should_inject_prompt()

    def mode(self) -> str:
        return self.config.mode()

    def _ensure_bootstrap_seeded(self) -> None:
        profile_name = self.config.bootstrap_profile_name()
        if self.memory_store.has_bootstrap_profile(profile_name):
            return
        for annotation in self.bootstrap.build_seed_annotations():
            self.memory_store.record_annotation(annotation)
        self.memory_store.set_meta(
            "bootstrap",
            {
                "profile": profile_name,
                "seeded_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    def _build_fallback_annotation(self, turn_context: TurnContext, state: Any) -> EmotionalAnnotation:
        dominant = list(state.dominant_drives)
        top_drive = dominant[0] if dominant else ""
        max_drive = float(max(state.drive_values.values(), default=0.0))
        scaled_intensity = max_drive / 10.0
        if top_drive == "CARE":
            event_type = "care_bonding"
        elif top_drive == "PANIC_GRIEF":
            event_type = "rupture_risk"
        elif top_drive == "RAGE":
            event_type = "boundary_crossing"
        elif top_drive in {"SEEKING", "FEAR", "PLAY", "LUST"}:
            event_type = "trust_shift"
        else:
            event_type = "general_emotional_event"
        intensity = max(1, min(10, int(round(scaled_intensity))))
        minimum_intensity = int(self.config.recording_policy().get("minimum_intensity", 5))
        if turn_context.relationship_key and dominant and intensity < minimum_intensity:
            intensity = minimum_intensity
        summary = "Fallback emotional annotation recorded from transient turn state."
        if dominant:
            summary = f"Fallback {event_type} inferred from transient turn state dominated by {', '.join(dominant[:2])}."
        return EmotionalAnnotation(
            annotation_id=None,
            bridge_row_type="turn",
            bridge_row_id=0,
            event_ts=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            source=turn_context.source,
            actor_role="system",
            event_type=event_type,
            summary=summary,
            intensity=intensity,
            dominant_drives=dominant,
            contributions=list(state.contributions),
            relationship_key=turn_context.relationship_key,
            tags=["fallback", "shadow"],
            metadata={"request_id": turn_context.request_id, **turn_context.metadata},
            importance=1.0,
        )

    def _contextual_drive_multiplier(
        self,
        drive_name: str,
        turn_context: TurnContext,
        retrieval_score: float,
    ) -> float:
        policy = self.config.drive_context_policy().get(drive_name)
        if not policy:
            return 1.0
        try:
            score_floor = float(policy.get("retrieval_score_floor", 0.0))
        except (TypeError, ValueError):
            score_floor = 0.0
        if retrieval_score >= score_floor:
            return 1.0
        text = (turn_context.user_text or "").lower()
        cue_terms = [str(term).strip().lower() for term in policy.get("cue_terms", []) if str(term).strip()]
        suppression_terms = [
            str(term).strip().lower()
            for term in policy.get("suppression_terms", [])
            if str(term).strip()
        ]
        if cue_terms and suppression_terms:
            has_cue = any(term in text for term in cue_terms)
            has_suppression = any(term in text for term in suppression_terms)
            if has_cue and has_suppression:
                return self._off_context_multiplier(policy)
        if any(term in text for term in cue_terms):
            return 1.0
        return self._off_context_multiplier(policy)

    @staticmethod
    def _off_context_multiplier(policy: dict[str, Any]) -> float:
        try:
            return max(0.0, min(1.0, float(policy.get("off_context_multiplier", 1.0))))
        except (TypeError, ValueError):
            return 1.0


def build_anatta_layer(
    workspace_dir: Path,
    bridge_memory_store: Any,
    backend_invoker: Any | None = None,
    backend_context_getter: Any | None = None,
) -> EmotionalSelfLayer:
    config = AnattaConfig(workspace_dir)
    bridge_adapter = BridgeMemoryAdapter(bridge_memory_store)
    memory_store = AnattaMemoryStore(bridge_adapter=bridge_adapter, config=config)
    cue_interpreter = BackendLLMInterpreter(
        backend_invoker=backend_invoker,
        backend_context_getter=backend_context_getter,
        config=config,
    )
    relationship_interpreter = RelationshipInterpreter(memory_store=memory_store)
    aggregator = DriveAggregator(config=config)
    composer = PromptComposer(config=config)
    bootstrap = BootstrapManager(config=config)
    return EmotionalSelfLayer(
        config=config,
        bridge_adapter=bridge_adapter,
        memory_store=memory_store,
        cue_interpreter=cue_interpreter,
        relationship_interpreter=relationship_interpreter,
        aggregator=aggregator,
        composer=composer,
        bootstrap=bootstrap,
    )
