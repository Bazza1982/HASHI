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
from .ledger import ExecutionLedger, LedgerInvariantError, LedgerStore
from .learning import HERv2Learning, HERv2TurnLearning, LearningRecovery
from .lifecycle import LifecycleMachine, LifecycleViolation
from .models import (
    Effort,
    ExecutionDisposition,
    LifecycleState,
    ReviewOutcome,
    Stage,
    TerminalState,
    TriageClassification,
    TurnResult,
)
from .runtime import HERv2Runtime

__all__ = [
    "AuditPersistenceError",
    "CommentaryPort",
    "DurableAuditLog",
    "DreamMaintainer",
    "Effort",
    "ExecutionDisposition",
    "ExecutionLedger",
    "HERv2Config",
    "HERv2Runtime",
    "HERv2Learning",
    "HERv2TurnLearning",
    "HabitAdvisor",
    "LedgerInvariantError",
    "LedgerStore",
    "LifecycleMachine",
    "LifecycleState",
    "LifecycleViolation",
    "LearningRecovery",
    "MeditationRunner",
    "NeutralCommentary",
    "PackagedCommentary",
    "PersonaCommentaryPipeline",
    "ProviderProfile",
    "ReviewOutcome",
    "Stage",
    "TerminalState",
    "TriageClassification",
    "TurnResult",
]
