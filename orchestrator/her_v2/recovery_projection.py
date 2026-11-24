"""Bounded, evidence-linked model views of canonical HER recovery state.

The SQLite recovery, checkpoint, turn and audit records remain authoritative.
These limits apply only to serialized prompt projections, not to stored work.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

# Historical names are retained for existing callers; units are UTF-8 JSON bytes.
FIELD_BUDGET_CHARS = 24 * 1024
TOTAL_BUDGET_CHARS = 96 * 1024
GOAL_BUDGET_CHARS = 4 * 1024
RECENT_RECEIPTS_MAX = 8
RECENT_EXCHANGES_MAX = 4
_TRUNCATION_MARKER = "…[truncated]"
_PROJECTION_NOTICE = (
    "Quoted recovery context only. Consult the referenced canonical HER records "
    "for omitted details; never replay an uncertain side effect automatically."
)


def _serialized(value: Any) -> str:
    # The non-compact form also bounds callers that serialize with JSON spaces.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_size(value: Any) -> int:
    return len(_serialized(value).encode("utf-8"))


def _sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_ref(context: Mapping[str, Any], field: str) -> str:
    session = str(context.get("session_id") or "")
    settled = bool(context.get("settled_through_turn_id")) or str(
        context.get("format") or ""
    ).startswith("her-settled-")
    turn = str(context.get("interrupted_turn_id") or context.get("settled_through_turn_id") or "")
    source = "her_settled_checkpoints" if settled else "her_active_turn_recovery"
    return f"{source}:{field}:{session}:{turn}"


def _bounded(value: Any, *, budget_chars: int, evidence_ref: str = "") -> Any:
    """Bound one complete JSON value; only scalar text gets an excerpt."""
    if _json_size(value) <= budget_chars:
        return value
    raw = value if isinstance(value, str) else _serialized(value)
    witness: dict[str, Any] = {
        "truncated": True,
        "bytes": len(raw.encode("utf-8")),
        "sha256": _sha256_text(raw),
        "evidence_ref": evidence_ref,
    }
    if isinstance(value, str):
        lo, hi = 0, len(value)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if _json_size({**witness, "excerpt": value[:mid] + _TRUNCATION_MARKER}) <= budget_chars:
                lo = mid
            else:
                hi = mid - 1
        if lo:
            witness["excerpt"] = value[:lo] + _TRUNCATION_MARKER
    return witness


def _as_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _receipt_identity(receipt: Mapping[str, Any], evidence_ref: str) -> dict[str, Any]:
    identity: dict[str, Any] = {}
    for key in ("operation_id", "invocation_id", "tool_name", "tool", "name", "status", "state", "read_only"):
        value = receipt.get(key)
        if value not in (None, "", [], {}):
            identity[key] = _bounded(value, budget_chars=256, evidence_ref=evidence_ref)
    return identity or {"evidence_ref": evidence_ref}


def _fit_summary(summary: dict[str, Any], *, budget_chars: int, list_key: str) -> dict[str, Any]:
    # Keep counts, reference and safety facts even when individual identities
    # must be omitted. Never turn the complete JSON document into an excerpt.
    while _json_size(summary) > budget_chars and summary.get(list_key):
        summary[list_key].pop(0)
        summary["omitted"] = int(summary.get("omitted") or 0) + 1
    return summary


def _project_tool_receipts(value: Any, *, evidence_ref: str, budget_chars: int) -> dict[str, Any]:
    if isinstance(value, Mapping) and "total" in value:
        total = int(value.get("total") or 0)
        recent = _as_mappings(value.get("recent"))
        omitted = int(value.get("omitted") or max(0, total - len(recent)))
        ref = str(value.get("evidence_ref") or evidence_ref)
    else:
        items = _as_mappings(value)
        total = len(items)
        ref = evidence_ref
        recent = [_receipt_identity(item, ref) for item in items[-RECENT_RECEIPTS_MAX:]]
        omitted = total - len(recent)
    result = {"total": total, "recent": recent, "omitted": omitted, "evidence_ref": ref}
    return _fit_summary(result, budget_chars=budget_chars, list_key="recent")


def _project_side_effects(value: Any, *, evidence_ref: str, budget_chars: int) -> dict[str, Any]:
    if isinstance(value, Mapping) and "total" in value:
        total = int(value.get("total") or 0)
        unresolved_total = int(value.get("unresolved_total") or 0)
        recent = _as_mappings(value.get("unresolved"))
        omitted = int(value.get("omitted") or max(0, unresolved_total - len(recent)))
        ref = str(value.get("evidence_ref") or evidence_ref)
    else:
        items = _as_mappings(value)
        unresolved = [item for item in items if item.get("state") in {"pending", "unknown", None}]
        total, unresolved_total = len(items), len(unresolved)
        ref = evidence_ref
        recent = [_receipt_identity(item, ref) for item in unresolved[-RECENT_RECEIPTS_MAX:]]
        omitted = unresolved_total - len(recent)
    result = {
        "total": total, "unresolved_total": unresolved_total, "unresolved": recent,
        "omitted": omitted, "evidence_ref": ref,
    }
    return _fit_summary(result, budget_chars=budget_chars, list_key="unresolved")


def _project_remaining_work(value: Any, *, evidence_ref: str, budget_chars: int) -> Any:
    if not isinstance(value, Mapping):
        return _bounded(value, budget_chars=budget_chars, evidence_ref=evidence_ref)
    result: dict[str, Any] = {}
    for key in (
        "status", "current_stage", "recovery_source_turn_id", "recovery_origin_turn_id", "goal",
        "goal_truncated", "goal_chars", "goal_sha256", "goal_evidence_ref",
    ):
        if value.get(key) not in (None, ""):
            field_ref = str(value.get("goal_evidence_ref") or evidence_ref) if key == "goal" else evidence_ref
            result[key] = _bounded(
                value[key], budget_chars=min(GOAL_BUDGET_CHARS, budget_chars // 2),
                evidence_ref=field_ref,
            )
    omitted = [key for key in value if key not in result]
    if omitted:
        result["omitted_fields"] = omitted
        result["evidence_ref"] = evidence_ref
    return _bounded(result, budget_chars=budget_chars, evidence_ref=evidence_ref)


def _project_settled_exchanges(value: Any, *, evidence_ref: str, budget_chars: int) -> dict[str, Any]:
    if isinstance(value, Mapping) and "total" in value:
        result = dict(value)
        result.setdefault("evidence_ref", evidence_ref)
        return _fit_summary(result, budget_chars=budget_chars, list_key="recent")
    items = _as_mappings(value)
    recent = []
    for item in items[-RECENT_EXCHANGES_MAX:]:
        row = {key: item[key] for key in ("sequence", "turn_id", "routing_revision") if item.get(key) is not None}
        for key in ("user", "assistant"):
            if item.get(key):
                row[key] = _bounded(item[key], budget_chars=1024, evidence_ref=evidence_ref)
        recent.append(row)
    return _fit_summary(
        {"total": len(items), "recent": recent, "omitted": len(items) - len(recent), "evidence_ref": evidence_ref},
        budget_chars=budget_chars, list_key="recent",
    )


def bounded_goal(goal: Any, *, budget_chars: int = GOAL_BUDGET_CHARS) -> dict[str, Any]:
    """Bound a goal projection; its full user request remains in her_turns."""
    text = str(goal or "")
    if not text:
        return {}
    if _json_size({"goal": text}) <= budget_chars:
        return {"goal": text}
    result: dict[str, Any] = {
        "goal_truncated": True, "goal_chars": len(text), "goal_sha256": _sha256_text(text),
    }
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _json_size({**result, "goal": text[:mid] + _TRUNCATION_MARKER}) <= budget_chars:
            lo = mid
        else:
            hi = mid - 1
    result["goal"] = text[:lo] + _TRUNCATION_MARKER
    return result


def bounded_prompt_projection(
    context: Mapping[str, Any], *,
    field_budget_chars: int = FIELD_BUDGET_CHARS,
    total_budget_chars: int = TOTAL_BUDGET_CHARS,
) -> dict[str, Any]:
    """Return an evidence-linked UTF-8 JSON view, preserving safety state."""
    if not isinstance(context, Mapping):
        return {}
    field_budget_chars = min(FIELD_BUDGET_CHARS, int(field_budget_chars))
    total_budget_chars = min(TOTAL_BUDGET_CHARS, int(total_budget_chars))
    result: dict[str, Any] = {}
    for key, value in context.items():
        if key == "projection_limits":
            continue
        ref = _source_ref(context, str(key))
        # Include the enclosing JSON field name, quotes, colon and whitespace.
        value_budget = field_budget_chars - (_json_size({key: None}) - _json_size(None))
        if value_budget < 512:
            raise ValueError("recovery field name exceeds projection budget")
        if key == "tool_receipts":
            result[key] = _project_tool_receipts(value, evidence_ref=ref, budget_chars=value_budget)
        elif key == "side_effects":
            result[key] = _project_side_effects(value, evidence_ref=ref, budget_chars=value_budget)
        elif key == "remaining_work":
            result[key] = _project_remaining_work(value, evidence_ref=ref, budget_chars=value_budget)
        elif key == "recent_settled_exchanges":
            result[key] = _project_settled_exchanges(value, evidence_ref=ref, budget_chars=value_budget)
        elif key == "provider_context_checkpoint" and isinstance(value, Mapping) and value:
            if "available" not in value and ("present" in value or value.get("truncated") is True):
                # This is already a model view, not fresh checkpoint metadata.
                # Keep its original evidence and never promote unknown or
                # unavailable state to a present private checkpoint.
                projected = dict(value)
                if projected.get("present") is not True:
                    projected["present"] = False
                    projected.pop("items_evidence_ref", None)
                result[key] = _bounded(projected, budget_chars=value_budget, evidence_ref=ref)
            else:
                encoded = _serialized(value)
                available = bool(value.get("available")) if "available" in value else bool(value)
                result[key] = {
                    "present": available, "bytes": len(encoded.encode("utf-8")),
                    "sha256": _sha256_text(encoded),
                    "digest": _bounded(
                        value.get("digest") or value.get("provider_context_digest") or None,
                        budget_chars=512, evidence_ref=_source_ref(context, "provider_context"),
                    ),
                    "evidence_ref": _source_ref(context, "provider_context"),
                }
                if available and (value.get("message_count") or value.get("reasoning_count")):
                    session = str(context.get("session_id") or "")
                    turn = str(context.get("interrupted_turn_id") or "")
                    result[key]["items_evidence_ref"] = f"her_provider_context_items:{session}:{turn}"
        else:
            result[key] = _bounded(value, budget_chars=value_budget, evidence_ref=ref)
        # Settled checkpoints are replaced on the next completed Turn. Keep a
        # stable reference to the underlying per-Turn evidence as well.
        if (
            context.get("settled_through_turn_id")
            and isinstance(result[key], dict)
            and (key in {"tool_receipts", "side_effects", "recent_settled_exchanges"}
                 or result[key].get("truncated"))
        ):
            session = str(context.get("session_id") or "")
            turn = str(context.get("settled_through_turn_id") or "")
            if key in {"tool_receipts", "side_effects", "strategy", "plan"}:
                fallback_ref = f"her_active_turn_recovery:{key}:{session}:{turn}"
            elif key == "recent_settled_exchanges":
                fallback_ref = f"her_turns:settled:{session}"
            elif key == "route_snapshot":
                fallback_ref = f"her_turns:route_snapshot:{session}:{turn}"
            else:
                fallback_ref = ""
            if fallback_ref:
                result[key] = {**result[key], "canonical_evidence_ref": fallback_ref}
                if _json_size(result[key]) > value_budget:
                    result[key].pop("excerpt", None)
                if key in {"tool_receipts", "side_effects", "recent_settled_exchanges"}:
                    list_key = "unresolved" if key == "side_effects" else "recent"
                    result[key] = _fit_summary(result[key], budget_chars=value_budget, list_key=list_key)
    result["projection_limits"] = {
        "field_budget_bytes": field_budget_chars, "total_budget_bytes": total_budget_chars,
        "authority": "quoted_recovery_context", "notice": _PROJECTION_NOTICE,
    }
    essential = {
        "session_id", "interrupted_turn_id", "settled_through_turn_id",
        "safe_to_resume", "recovery_disposition", "tool_receipts", "side_effects",
        "projection_limits",
    }
    removed: dict[str, Any] = {}
    while _json_size(result) > total_budget_chars:
        candidates = [key for key in result if key not in essential and key != "omitted_fields"]
        if not candidates:
            for key, list_key in (("tool_receipts", "recent"), ("side_effects", "unresolved")):
                item = result.get(key)
                if isinstance(item, dict) and item.get(list_key):
                    item[list_key].pop(0)
                    item["omitted"] = int(item.get("omitted") or 0) + 1
                    break
            else:
                raise ValueError("recovery safety metadata exceeds projection budget")
            continue
        key = max(candidates, key=lambda name: _json_size(result[name]))
        result.pop(key)
        # The witness covers the source fields, not an already truncated view.
        removed[key] = context.get(key)
        result["omitted_fields"] = {
            "count": len(removed), "sha256": _sha256_text(_serialized(removed)),
            "evidence_ref": _source_ref(context, "*"),
        }
    return result


__all__ = ["FIELD_BUDGET_CHARS", "GOAL_BUDGET_CHARS", "TOTAL_BUDGET_CHARS", "bounded_goal", "bounded_prompt_projection"]
# HASHI-Fingerprint: hfp_71071e3f156a4ae4a25249b6ec0e080e
