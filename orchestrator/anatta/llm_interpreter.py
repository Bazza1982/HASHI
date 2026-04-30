from __future__ import annotations

from typing import Any

from .models import DriveContribution, EmotionalAnnotation, EmergentTurnState, TurnContext


class BackendLLMInterpreter:
    """Backend-native LLM interpreter placeholder.

    This initial implementation deliberately avoids introducing a second API path.
    Integration with the agent's active backend will happen through runtime-owned
    execution hooks in a later patch. For now this class defines the contract and
    returns an empty contribution set so the package is safe to import.
    """

    def __init__(self, backend_manager: Any | None = None):
        self.backend_manager = backend_manager

    async def interpret_turn(self, turn_context: TurnContext) -> list[DriveContribution]:
        return []

    async def consolidate_event(
        self,
        turn_context: TurnContext,
        assistant_response: str,
        state: EmergentTurnState,
    ) -> EmotionalAnnotation | None:
        return None
