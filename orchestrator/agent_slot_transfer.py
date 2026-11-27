"""Deterministic slot placement for cross-Agent configuration transfer.

Both ``/workzone copy|import`` and ``/sys copy|import`` share one rule set:

* the source slot's own id is preferred when the target still has it free;
* otherwise the next free slot in the canonical order receives the entry;
* an occupied target slot is never overwritten;
* entries whose value already exists on the target are skipped, and a value is
  only transferred once even when the source repeats it.

The module is intentionally free of runtime, Telegram and storage imports so
the placement rules can be tested directly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceSlot:
    """One configured source slot offered for transfer."""

    slot: str
    value: str
    enabled: bool = True


@dataclass(frozen=True)
class SlotTransfer:
    """One planned placement decision."""

    source_slot: str
    target_slot: str | None
    value: str
    enabled: bool
    outcome: str  # "copied" | "duplicate" | "no_free_slot"


def normalize_slot_value(value: str, *, as_path: bool) -> str:
    """Return the comparison key used for idempotent de-duplication."""

    text = str(value or "").strip()
    if not text:
        return ""
    if not as_path:
        return text
    try:
        text = str(Path(text).expanduser().resolve())
    except (OSError, RuntimeError):
        text = text
    return os.path.normcase(text)


def source_slots_from_records(
    records: Sequence[Mapping[str, Any]],
    *,
    slot_key: str,
    value_key: str,
    enabled_key: str | None = None,
) -> list[SourceSlot]:
    """Project storage rows into transfer entries, dropping empty slots."""

    entries: list[SourceSlot] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        slot = str(record.get(slot_key) or "").strip()
        value = str(record.get(value_key) or "")
        if not slot or not value.strip():
            continue
        enabled = bool(record.get(enabled_key)) if enabled_key else True
        entries.append(SourceSlot(slot=slot, value=value, enabled=enabled))
    return entries


def plan_slot_transfer(
    *,
    source_slots: Sequence[SourceSlot],
    target_slot_ids: Sequence[str],
    target_values: Sequence[str],
    order: Sequence[str],
    slot: str | None = None,
    as_path: bool = False,
) -> list[SlotTransfer]:
    """Place source slots on free target slots without overwriting anything."""

    positions = {str(name): index for index, name in enumerate(order)}
    occupied = {str(name) for name in target_slot_ids}
    seen: set[str] = {
        key for key in (normalize_slot_value(value, as_path=as_path) for value in target_values) if key
    }
    plan: list[SlotTransfer] = []
    for entry in source_slots:
        if slot is not None and entry.slot != slot:
            continue
        key = normalize_slot_value(entry.value, as_path=as_path)
        if not key:
            continue
        if key in seen:
            plan.append(
                SlotTransfer(
                    source_slot=entry.slot,
                    target_slot=None,
                    value=entry.value,
                    enabled=entry.enabled,
                    outcome="duplicate",
                )
            )
            continue
        seen.add(key)
        target = _choose_target(entry.slot, order, positions, occupied)
        if target is None:
            plan.append(
                SlotTransfer(
                    source_slot=entry.slot,
                    target_slot=None,
                    value=entry.value,
                    enabled=entry.enabled,
                    outcome="no_free_slot",
                )
            )
            continue
        occupied.add(target)
        plan.append(
            SlotTransfer(
                source_slot=entry.slot,
                target_slot=target,
                value=entry.value,
                enabled=entry.enabled,
                outcome="copied",
            )
        )
    return plan


def _choose_target(
    source_slot: str,
    order: Sequence[str],
    positions: Mapping[str, int],
    occupied: set[str],
) -> str | None:
    if not order:
        return None
    start = positions.get(str(source_slot), 0)
    for offset in range(len(order)):
        candidate = order[(start + offset) % len(order)]
        if candidate not in occupied:
            return candidate
    return None


def describe_outcomes(plan: Sequence[SlotTransfer]) -> dict[str, int]:
    counts = {"copied": 0, "duplicate": 0, "no_free_slot": 0}
    for item in plan:
        if item.outcome in counts:
            counts[item.outcome] += 1
    return counts


__all__ = [
    "SlotTransfer",
    "SourceSlot",
    "describe_outcomes",
    "normalize_slot_value",
    "plan_slot_transfer",
    "source_slots_from_records",
]
# HASHI-Fingerprint: hfp_e23460e33c1342029762346a7eb183b0
