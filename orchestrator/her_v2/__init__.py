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
from .cognitive_control import (
    COGNITIVE_DECISION_TOOL,
    CognitiveInterrupt,
    StageCognitiveController,
)
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
    ReplanningOutcome,
    ReviewOutcome,
    Route,
    Stage,
    StrategyDecision,
    TerminalState,
    TriageClassification,
    TurnResult,
    effort_display_label,
    parse_effort,
)
from .checkpoint import (
    CHECKPOINT_ELAPSED_THRESHOLD_S,
    CHECKPOINT_RESULT_THRESHOLD,
    CompulsoryReplanCoordinator,
    ReplanDirective,
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
    "CHECKPOINT_ELAPSED_THRESHOLD_S",
    "CHECKPOINT_RESULT_THRESHOLD",
    "CompulsoryReplanCoordinator",
    "COGNITIVE_DECISION_TOOL",
    "CognitiveInterrupt",
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
    "ReplanDirective",
    "ReplanningOutcome",
    "RenderedRequiredMessage",
    "RequiredPersonaRenderer",
    "RequiredUserMessage",
    "ReviewOutcome",
    "Route",
    "Stage",
    "StageCognitiveController",
    "StrategyDecision",
    "TerminalState",
    "TriageClassification",
    "TurnResult",
    "effort_display_label",
    "parse_effort",
]
