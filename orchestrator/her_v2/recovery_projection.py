"""Bounded prompt projections for HER v2/v3 recovery and settled-checkpoint contexts.

Canonical records (``her_active_turn_recovery``, ``her_settled_checkpoints``,
``her_session_events``, ``her_provider_context_items``) keep the complete
authoritative state and stay queryable. The projections in this module exist
only for the *model-facing prompt* and never mutate those records.

Design invariants:

- Every dynamic field is hard-capped (default ``FIELD_BUDGET_CHARS``).
- The whole projection is hard-capped (default ``TOTAL_BUDGET_CHARS``).
- Truncation/omission is always recorded with a size, a SHA-256, and a
  queryable evidence reference. We never silently drop audit facts.
- Full ``tool_receipts`` arrays are never copied into the prompt; only a
  count, a bounded recent-window identity list, and an evidence reference.

These projections are deliberately not a recovery mechanism by themselves:
they make a *previously unbounded* prompt contribution bounded regardless of
how large, nested, or historically polluted the canonical state is.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

FIELD_BUDGET_CHARS: int = 24 * 1024
TOTAL_BUDGET_CHARS: int = 96 * 1024
GOAL_BUDGET_CHARS: int = 4 * 1024
RECENT_RECEIPTS_MAX: int = 8
RECENT_EXCHANGES_MAX: int = 4

_TRUNCATION_MARKER = "…[truncated]"

_PROJECTION_NOTICE = (
    "Complete canonical records remain queryable in HER audit storage. "
    "Truncated or omitted fields carry sha256 + evidence_ref."
)


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_size(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        return len(str(value or "").encode("utf-8"))


def _bounded(value: Any, *, budget_chars: int) -> Any:
    """Return ``value`` unchanged when it fits, else a truncation witness."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= budget_chars:
            return value
        return {
            "truncated": True,
            "chars": len(value),
            "sha256": _sha256_text(value),
            "excerpt": value[:budget_chars] + _TRUNCATION_MARKER,
        }
    if isinstance(value, (Mapping, Sequence)) and not isinstance(
        value, (bytes, bytearray)
    ):
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if len(serialized) <= budget_chars:
            return value
        return {
            "truncated": True,
            "chars": len(serialized),
            "sha256": _sha256_text(serialized),
            "excerpt": serialized[:budget_chars] + _TRUNCATION_MARKER,
        }
    return _bounded(str(value or ""), budget_chars=budget_chars)


def _as_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _receipt_identity(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Minimal, bounded identity for one tool receipt / side effect."""
    identity: dict[str, Any] = {}
    for key in (
        "operation_id",
        "invocation_id",
        "tool_name",
        "tool",
        "name",
        "status",
        "state",
        "read_only",
    ):
        value = receipt.get(key)
        if value not in (None, "", [], {}):
            identity[key] = _bounded(value, budget_chars=256)
    if not identity:
        return {"summary": _bounded(dict(receipt), budget_chars=512)}
    return identity


def _project_tool_receipts(
    value: Any, *, session_id: str, turn_id: str, budget_chars: int
) -> dict[str, Any]:
    items = _as_mappings(value)
    total = len(items)
    if total == 0:
        return {"total": 0}
    recent = [_receipt_identity(item) for item in items[-RECENT_RECEIPTS_MAX:]]
    return _bounded(
        {
            "total": total,
            "recent": recent,
            "omitted": total - len(recent),
            "evidence_ref": (
                f"her_active_turn_recovery:tool_receipts:{session_id}:{turn_id}"
            ),
        },
        budget_chars=budget_chars,
    )


def _project_side_effects(
    value: Any, *, session_id: str, turn_id: str, budget_chars: int
) -> dict[str, Any]:
    items = _as_mappings(value)
    total = len(items)
    if total == 0:
        return {"total": 0}
    unresolved = [
        item for item in items if item.get("state") in {"pending", "unknown", None}
    ]
    recent = [
        _receipt_identity(item) for item in unresolved[-RECENT_RECEIPTS_MAX:]
    ]
    return _bounded(
        {
            "total": total,
            "unresolved_total": len(unresolved),
            "unresolved": recent,
            "omitted": len(unresolved) - len(recent),
            "evidence_ref": (
                f"her_active_turn_recovery:side_effects:{session_id}:{turn_id}"
            ),
        },
        budget_chars=budget_chars,
    )


def _project_remaining_work(
    value: Any, *, session_id: str, turn_id: str, budget_chars: int
) -> dict[str, Any]:
    del session_id, turn_id
    if not isinstance(value, Mapping):
        return _bounded(value, budget_chars=budget_chars)
    projected: dict[str, Any] = {}
    for key in ("status", "current_stage", "recovery_source_turn_id"):
        if value.get(key) not in (None, ""):
            projected[key] = value[key]
    goal = value.get("goal")
    if isinstance(goal, str) and goal:
        projected["goal"] = _bounded(goal, budget_chars=GOAL_BUDGET_CHARS)
    elif goal not in (None, ""):
        projected["goal"] = _bounded(goal, budget_chars=GOAL_BUDGET_CHARS)
    # ``inherited_recovery`` re-embeds a prior turn's remaining_work and is a
    # recursion amplifier; it stays in the canonical record but never in the
    # prompt projection.
    return _bounded(projected, budget_chars=budget_chars)


def _project_settled_exchanges(
    value: Any, *, session_id: str, turn_id: str, budget_chars: int
) -> dict[str, Any]:
    del turn_id
    items = _as_mappings(value)
    total = len(items)
    if total == 0:
        return {"total": 0}
    recent: list[dict[str, Any]] = []
    for item in items[-RECENT_EXCHANGES_MAX:]:
        row: dict[str, Any] = {}
        for key in ("sequence", "turn_id", "routing_revision"):
            if item.get(key) is not None:
                row[key] = item[key]
        for key in ("user", "assistant"):
            if item.get(key):
                row[key] = _bounded(item[key], budget_chars=1024)
        recent.append(row)
    return _bounded(
        {
            "total": total,
            "recent": recent,
            "omitted": total - len(recent),
            "evidence_ref": f"her_turns:settled:{session_id}",
        },
        budget_chars=budget_chars,
    )


def _project_provider_context(
    value: Any, *, session_id: str, turn_id: str, budget_chars: int
) -> Any:
    del budget_chars
    if not isinstance(value, Mapping) or not value:
        return value
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return {
        "present": True,
        "chars": len(serialized),
        "sha256": _sha256_text(serialized),
        "digest": value.get("digest") or value.get("provider_context_digest") or None,
        "evidence_ref": (
            f"her_provider_context_items:{session_id}:{turn_id}"
        ),
    }


def bounded_goal(
    goal: Any, *, budget_chars: int = GOAL_BUDGET_CHARS
) -> dict[str, Any]:
    """Bound a recovery goal string; keep a SHA-256 for full audit traceability.

    The complete authoritative request stays in the canonical audit stream
    (``her_session_events`` request_received, ``her_turns.user_message``); this
    summary is the only form allowed in ``remaining_work.goal`` so a previously
    expanded provider prompt can never be re-fed on recovery.
    """
    text = str(goal or "")
    if not text:
        return {}
    if len(text) <= budget_chars:
        return {"goal": text}
    return {
        "goal": text[:budget_chars] + _TRUNCATION_MARKER,
        "goal_truncated": True,
        "goal_chars": len(text),
        "goal_sha256": _sha256_text(text),
    }


def bounded_prompt_projection(
    context: Mapping[str, Any],
    *,
    field_budget_chars: int = FIELD_BUDGET_CHARS,
    total_budget_chars: int = TOTAL_BUDGET_CHARS,
) -> dict[str, Any]:
    """Project a canonical recovery/checkpoint context into bounded prompt form.

    Only for model-facing prompt injection. The source canonical records are
    never modified.
    """
    if not isinstance(context, Mapping):
        return {}
    session_id = str(context.get("session_id") or "")
    turn_id = str(
        context.get("interrupted_turn_id")
        or context.get("settled_through_turn_id")
        or ""
    )

    projected: dict[str, Any] = {}
    for key, value in context.items():
        if key == "tool_receipts":
            projected[key] = _project_tool_receipts(
                value,
                session_id=session_id,
                turn_id=turn_id,
                budget_chars=field_budget_chars,
            )
        elif key == "side_effects":
            projected[key] = _project_side_effects(
                value,
                session_id=session_id,
                turn_id=turn_id,
                budget_chars=field_budget_chars,
            )
        elif key == "remaining_work":
            projected[key] = _project_remaining_work(
                value,
                session_id=session_id,
                turn_id=turn_id,
                budget_chars=field_budget_chars,
            )
        elif key == "recent_settled_exchanges":
            projected[key] = _project_settled_exchanges(
                value,
                session_id=session_id,
                turn_id=turn_id,
                budget_chars=field_budget_chars,
            )
        elif key == "provider_context_checkpoint":
            projected[key] = _project_provider_context(
                value,
                session_id=session_id,
                turn_id=turn_id,
                budget_chars=field_budget_chars,
            )
        elif key == "limitations":
            projected[key] = _bounded(value, budget_chars=field_budget_chars)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            projected[key] = value
        elif isinstance(value, (Mapping, Sequence)) and not isinstance(
            value, (bytes, bytearray)
        ):
            projected[key] = _bounded(value, budget_chars=field_budget_chars)
        else:
            projected[key] = _bounded(str(value or ""), budget_chars=field_budget_chars)

    projected["projection_limits"] = {
        "field_budget_chars": int(field_budget_chars),
        "total_budget_chars": int(total_budget_chars),
        "authority": "quoted_recovery_context",
        "notice": _PROJECTION_NOTICE,
    }

    # Final hard ceiling: even with per-field budgets a pathological input must
    # not be able to exceed the whole-projection budget.
    if _json_size(projected) > total_budget_chars:
        serialized = json.dumps(
            projected,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return {
            "truncated": True,
            "chars": len(serialized),
            "sha256": _sha256_text(serialized),
            "excerpt": serialized[:total_budget_chars],
        }
    return projected


__all__ = [
    "FIELD_BUDGET_CHARS",
    "GOAL_BUDGET_CHARS",
    "TOTAL_BUDGET_CHARS",
    "bounded_goal",
    "bounded_prompt_projection",
]
# HASHI-Fingerprint: hfp_71071e3f156a4ae4a25249b6ec0e080e
