from __future__ import annotations

import asyncio
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
from .models import EmotionalAnnotation, PromptInjection, TurnContext
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
        self._ensure_bootstrap_seeded()

    def resolve_relationship_key(self, source: str, chat_id: int | None = None, metadata: dict[str, Any] | None = None) -> str | None:
        if chat_id is not None:
            return f"{source}:{chat_id}"
        if metadata and metadata.get("relationship_key"):
            return str(metadata["relationship_key"])
        return None

    async def build_turn_state(self, turn_context: TurnContext, model_name: str) -> tuple[Any, PromptInjection]:
        if not self.config.is_enabled():
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
        for annotation in annotations:
            annotation_ids.append(int(annotation.annotation_id or 0))
            memory_contributions.extend(annotation.contributions)
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
        if not self.config.should_record_annotations():
            return
        annotation = await self.cue_interpreter.consolidate_event(turn_context, assistant_response, state)
        if annotation is not None:
            self.memory_store.record_annotation(annotation)
            return
        intensity = int(max(state.drive_values.values(), default=0.0) / 10.0)
        event_type = "general_emotional_event"
        if not self.memory_store.should_record(intensity=intensity, event_type=event_type):
            return
        fallback = EmotionalAnnotation(
            annotation_id=None,
            bridge_row_type="turn",
            bridge_row_id=0,
            event_ts=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
            source=turn_context.source,
            actor_role="system",
            event_type=event_type,
            summary="Fallback emotional annotation recorded from transient turn state.",
            intensity=intensity,
            dominant_drives=list(state.dominant_drives),
            contributions=list(state.contributions),
            relationship_key=turn_context.relationship_key,
            tags=["fallback"],
            metadata={"request_id": turn_context.request_id, **turn_context.metadata},
            importance=1.0,
        )
        self.memory_store.record_annotation(fallback)

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


def build_anatta_layer(
    workspace_dir: Path,
    bridge_memory_store: Any,
    backend_manager: Any | None = None,
) -> EmotionalSelfLayer:
    config = AnattaConfig(workspace_dir)
    bridge_adapter = BridgeMemoryAdapter(bridge_memory_store)
    memory_store = AnattaMemoryStore(bridge_adapter=bridge_adapter, config=config)
    cue_interpreter = BackendLLMInterpreter(backend_manager=backend_manager)
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
