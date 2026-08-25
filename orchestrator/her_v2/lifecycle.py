"""Strict authoritative lifecycle graph for HER v2."""

from __future__ import annotations

from dataclasses import dataclass

from .models import TERMINAL_STATES, LifecycleState


class LifecycleViolation(RuntimeError):
    def __init__(self, current: LifecycleState, requested: LifecycleState):
        super().__init__(
            f"invalid HER v2 lifecycle transition: {current.value} -> {requested.value}"
        )
        self.current = current
        self.requested = requested


_PRINCIPAL_EDGES: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.RECEIVED: frozenset({LifecycleState.TRIAGED}),
    LifecycleState.TRIAGED: frozenset(
        {
            LifecycleState.PLANNED,
            LifecycleState.EXECUTING,
            LifecycleState.FINALISING,
            LifecycleState.PENDING_USER_INPUT,
        }
    ),
    LifecycleState.PLANNED: frozenset({LifecycleState.EXECUTING}),
    LifecycleState.EXECUTING: frozenset(
        {
            LifecycleState.REPLANNING,
            LifecycleState.EXECUTION_COMPLETED,
        }
    ),
    LifecycleState.REPLANNING: frozenset(
        {
            LifecycleState.EXECUTING,
            LifecycleState.EXECUTION_COMPLETED,
        }
    ),
    LifecycleState.EXECUTION_COMPLETED: frozenset(
        {
            LifecycleState.REVIEWING,
            LifecycleState.VERIFYING,
            LifecycleState.FINALISING,
            LifecycleState.COMPLETED,
            LifecycleState.ERROR,
        }
    ),
    LifecycleState.REVIEWING: frozenset(
        {
            LifecycleState.REPLANNING,
            LifecycleState.VERIFYING,
            LifecycleState.FINALISING,
        }
    ),
    LifecycleState.VERIFYING: frozenset(
        {LifecycleState.REPLANNING, LifecycleState.FINALISING}
    ),
    LifecycleState.FINALISING: frozenset(
        {
            LifecycleState.COMPLETED,
            LifecycleState.COMPLETED_WITH_LIMITATIONS,
            LifecycleState.FAILED,
            LifecycleState.ERROR,
            LifecycleState.STOPPED,
            LifecycleState.PENDING_USER_INPUT,
        }
    ),
}


@dataclass
class LifecycleMachine:
    """Validate transitions without inventing missing predecessor states.

    An invalid transition changes the authoritative state directly to ``ERROR``
    and raises :class:`LifecycleViolation`.  It never walks through or
    synthesises intermediate states.
    """

    state: LifecycleState = LifecycleState.RECEIVED

    def can_transition(self, requested: LifecycleState) -> bool:
        if self.state in TERMINAL_STATES:
            return False
        if requested in {LifecycleState.ERROR, LifecycleState.STOPPED}:
            return True
        return requested in _PRINCIPAL_EDGES.get(self.state, frozenset())

    def transition(self, requested: LifecycleState) -> LifecycleState:
        current = self.state
        if not self.can_transition(requested):
            if current not in TERMINAL_STATES:
                self.state = LifecycleState.ERROR
            raise LifecycleViolation(current, requested)
        self.state = requested
        return self.state

    @staticmethod
    def allowed_edges() -> frozenset[tuple[LifecycleState, LifecycleState]]:
        edges = {
            (source, target)
            for source, targets in _PRINCIPAL_EDGES.items()
            for target in targets
        }
        for source in LifecycleState:
            if source not in TERMINAL_STATES:
                edges.add((source, LifecycleState.ERROR))
                edges.add((source, LifecycleState.STOPPED))
        return frozenset(edges)
