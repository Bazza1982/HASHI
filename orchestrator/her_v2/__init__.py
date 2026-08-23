"""Provider-neutral HASHI Engine Runtime v2.

The package deliberately keeps orchestration policy separate from provider
adapters and from HASHI-owned delivery, tools, permissions, and process
control.  The public exports form the compatibility boundary used by the
``her-v2`` backend adapter and deterministic certification tests.
"""

from .audit import AuditPersistenceError, DurableAuditLog
from .commentary import (
    CommentaryPort,
    NeutralCommentary,
    PackagedCommentary,
    PersonaCommentaryPipeline,
)
from .config import HERv2Config, ProviderProfile
from .interfaces import DreamMaintainer, HabitAdvisor, MeditationRunner
from .learning import HERv2Learning, HERv2TurnLearning, LearningRecovery
from .ledger import ExecutionLedger, LedgerInvariantError, LedgerStore
from .lifecycle import LifecycleMachine, LifecycleViolation
from .models import (
    DEFAULT_ROUTES_BY_STAGE,
    EXECUTION_ROUTES,
    ROUTE_STAGES,
    Effort,
    EFFORT_DISPLAY_LABELS,
    ExecutionDisposition,
    LifecycleState,
    ReviewOutcome,
    Route,
    Stage,
    TerminalState,
    TriageClassification,
    TurnResult,
    VerificationOutcome,
    Verifiability,
    effort_display_label,
    parse_effort,
)
from .presentation import (
    RenderedRequiredMessage,
    RequiredPersonaRenderer,
    RequiredUserMessage,
)
from .runtime import HERv2Runtime

__all__ = [
    "DEFAULT_ROUTES_BY_STAGE",
    "EXECUTION_ROUTES",
    "ROUTE_STAGES",
    "AuditPersistenceError",
    "CommentaryPort",
    "DreamMaintainer",
    "DurableAuditLog",
    "Effort",
    "EFFORT_DISPLAY_LABELS",
    "ExecutionDisposition",
    "ExecutionLedger",
    "HERv2Config",
    "HERv2Learning",
    "HERv2Runtime",
    "HERv2TurnLearning",
    "HabitAdvisor",
    "LearningRecovery",
    "LedgerInvariantError",
    "LedgerStore",
    "LifecycleMachine",
    "LifecycleState",
    "LifecycleViolation",
    "MeditationRunner",
    "NeutralCommentary",
    "PackagedCommentary",
    "PersonaCommentaryPipeline",
    "ProviderProfile",
    "RenderedRequiredMessage",
    "RequiredPersonaRenderer",
    "RequiredUserMessage",
    "ReviewOutcome",
    "Route",
    "Stage",
    "TerminalState",
    "TriageClassification",
    "TurnResult",
    "VerificationOutcome",
    "Verifiability",
    "effort_display_label",
    "parse_effort",
]
