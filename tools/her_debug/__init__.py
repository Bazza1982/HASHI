"""Isolated HER certification lab primitives.

The package deliberately avoids eager imports so ``python -m`` fixture entrypoints
do not load their own modules before execution.
"""

__all__ = [
    "CleanupGuard",
    "EvidenceCollector",
    "HerDebugLab",
    "RunLayout",
    "restart_receipt_ready",
    "runtime_start_marker",
    "ScriptedProvider",
    "SequentialStepState",
    "StepProtocolError",
    "UnsafeCleanupTarget",
]
