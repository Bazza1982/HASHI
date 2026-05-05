from __future__ import annotations

from .memory import AnattaMemoryStore
from .models import DriveContribution, TurnContext


class RelationshipInterpreter:
    def __init__(self, memory_store: AnattaMemoryStore):
        self.memory_store = memory_store

    async def interpret_relationship(self, turn_context: TurnContext) -> list[DriveContribution]:
        summary = self.memory_store.compute_relationship_summary(turn_context.relationship_key)
        if not summary:
            return []
        net_trust = float(summary.get("net_trust_shift", 0.0))
        care_signal = float(summary.get("care_signal", 0.0))
        repair_signal = float(summary.get("repair_signal", 0.0))
        rupture_count = int(summary.get("rupture_count", 0))
        delta: dict[str, float] = {}
        rationale_parts: list[str] = []
        if care_signal > 0:
            delta["CARE"] = min(care_signal / 5.0, 0.25)
            rationale_parts.append("relationship history shows care and validation salience")
        if repair_signal > 0 and net_trust < 0:
            delta["CARE"] = max(delta.get("CARE", 0.0), min(repair_signal / 12.0, 0.10))
            rationale_parts.append("relationship history includes attempted repair under strain")
        if net_trust < 0:
            delta["FEAR"] = min(abs(net_trust) / 5.0, 0.25)
            delta["PANIC_GRIEF"] = min(abs(net_trust) / 6.0, 0.20)
            rationale_parts.append("relationship history shows trust strain")
        if rupture_count > 0 and net_trust >= 0:
            delta["CARE"] = max(delta.get("CARE", 0.0), min(rupture_count / 10.0, 0.15))
            rationale_parts.append("relationship carries remembered rupture context")
        if not delta:
            return []
        return [
            DriveContribution(
                source="relationship_interpreter",
                drive_delta=delta,
                weight=1.0,
                rationale="; ".join(rationale_parts) or "relationship history contribution",
                metadata={"relationship_summary": summary},
            )
        ]
