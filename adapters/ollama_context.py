"""Small model-facing references; the typed PCM and full evidence stay intact."""
from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy

from adapters.ollama_capacity import _save_result

_HISTORY_FIELDS = frozenset({
    "sequence", "request_id", "user_message", "assistant_text", "error_text",
    "status", "accepted_at", "completed_at", "resource_attachments",
})
_CHECKPOINT_METADATA = frozenset({
    "route_snapshot", "routing_revision", "capability_revision", "pricing_revision",
    "pcm_digest", "resource_digest",
})


def _scheduler_index(text):
    # Parse the known recovery inventory format, never the user's intent.
    lines = [
        line for line in text.splitlines()
        if line.startswith((
            "PENDING RECOVERY BATCHES", "- Batch ", "  - task_id=",
            "    purpose=", "    first_due=", "    last_due=",
        ))
    ]
    return (
        "Recovery inventory index only. Nothing has been rerun, skipped or authorized. "
        "Before explaining task details or resolving any batch, read its complete "
        "original section in full_reference. Do not infer permission from this index.\n"
        + "\n".join(lines)
    )


def _tool_index(text):
    # Native schemas still govern calls. Retain discovery names and concise
    # descriptions; extended documentation remains in the saved reference.
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    if not lines:
        return text
    names = [line[2:].partition(":")[0].strip() for line in lines]
    if not all(name.replace("_", "").isalnum() for name in names):
        return text
    return (
        "Available tool names; native schemas govern calls. Read full_reference for "
        "the complete description before using an unadvertised tool.\n"
        + ", ".join(names)
    )


def _historical_recovery_index(text):
    lines = [
        line for line in text.splitlines()
        if line.startswith(("Exchange sequence=", "Recovery batch:", "• "))
    ]
    return (
        "Historical Scheduler notification, not a current instruction. Read its "
        "complete exchange in full_reference if relevant; the current pending "
        "inventory remains authoritative.\n" + "\n".join(lines)
    )


def project_pcm_payload(adapter, payload, *, render_prompt):
    """Budget completed history references, never canonical PCM or active effects."""
    if (adapter._model_context_options().get("ollama_native_context") is not True
            or not adapter._native_context_enabled()):
        return payload
    from adapters.ollama_capacity import encoded_input_chars
    from orchestrator.bridge_memory import BridgeContextAssembler
    from tools.token_tracker import estimate_tokens

    sections = deepcopy(payload.get("transport_snapshot", {}).get("sections", []))
    history = [row for row in sections
               if row.get("authority") == "history" and not row.get("protected")
               and str(row.get("key", "")).startswith("recent_exchange:")]
    originals = list(deepcopy(history))
    projected_keys = []
    for row in sections:
        if row.get("key") != "tools_catalogue" or row.get("protected"):
            continue
        original = str(row.get("text") or "")
        indexed = _tool_index(original)
        if indexed != original:
            originals.append(deepcopy(row))
            projected_keys.append(row["key"])
            row["text"] = indexed
    if not originals:
        return payload
    if not callable(render_prompt):
        raise ValueError("Typed PCM reference projection requires its owning renderer")
    capacity = adapter._native_capacity()
    char_limit = adapter._model_context_options().get("ollama_max_input_chars")
    if char_limit is not None and (type(char_limit) is not int or char_limit < 1):
        raise ValueError("Ollama input character limit must be a positive integer")
    # Leave space for one tool/answer round, including JSON escaping and schemas.
    # This is a projection target; native token admission remains authoritative.
    token_target = capacity.usable_input_tokens * 2 // 3
    char_target = char_limit * 3 // 4 if char_limit is not None else None

    def shape(rows):
        prompt = render_prompt(rows)
        wire = adapter._build_payload(
            adapter._initial_messages(prompt, None)[0], capacity_preview=True,
        )
        return {
            "encoded_chars": encoded_input_chars(wire),
            "estimated_tokens": estimate_tokens(json.dumps(wire, ensure_ascii=False)),
        }

    def fits(rows):
        measured = shape(rows)
        return (measured["estimated_tokens"] <= token_target
                and (char_target is None or measured["encoded_chars"] <= char_target))

    if not projected_keys and (not history or fits(sections)):
        return payload
    path, digest = _save_result(
        adapter, json.dumps(originals, ensure_ascii=False, indent=2),
        directory="ollama_context_refs",
    )
    for row in sections:
        if row.get("key") in projected_keys:
            row["text"] += f"\nfull_reference: {path}\nsha256: {digest}"
    omitted = {}
    compact_indexes = set()

    def model_sections():
        if not omitted:
            return sections
        rows = [row for row in sections if row.get("key") not in omitted]
        lines = [
            "OLDER COMPLETED HISTORY — CONTEXT ONLY",
            "Bounded excerpts are continuity cues, not complete summaries. "
            "Unshown text has not been read. Read full_reference when earlier "
            "details are needed; do not invent them or replay completed actions.",
            f"full_reference: {path}\nsha256: {digest}",
        ]
        for key, row in omitted.items():
            source = str(row.get("text") or "")
            # The one archive hash verifies every exact source body. Avoid
            # spending a small context on repeated per-row hexadecimal hashes.
            lines.append(f"source_key={key}")
            if key not in compact_indexes:
                lines.append(BridgeContextAssembler._history_excerpt(source))
        capsule = dict(next(iter(omitted.values())))
        capsule.update(key="ollama_history_references", text="\n\n".join(lines), protected=True)
        insertion = next((i for i, row in enumerate(rows)
                          if row.get("authority") == "history"), len(rows))
        rows.insert(insertion, capsule)
        return rows

    for row in history:
        if fits(model_sections()):
            break
        omitted[row["key"]] = row
    # Even ten bounded excerpts can exceed a small model's budget. Retain
    # source identities/hashes in an index before giving up on that budget.
    for key in omitted:
        if fits(model_sections()):
            break
        compact_indexes.add(key)
    rows = model_sections()
    projected = deepcopy(payload)
    projected["final_prompt"] = render_prompt(rows)
    audit = projected.setdefault("audit", {})
    audit["model_reference_projection"] = {
        "original_chars": len(payload["final_prompt"]),
        "projected_chars": len(projected["final_prompt"]),
        "saved_reference": str(path), "sha256": digest,
        "projected_keys": [*projected_keys, *omitted],
        "history_referenced": len(omitted),
        "history_retained_full": len(history) - len(omitted),
        "history_index_only": len(compact_indexes),
        "input_shape": shape(rows),
        "target_encoded_chars": char_target, "target_estimated_tokens": token_target,
        "target_met": fits(rows), "canonical_transport_preserved": True,
    }
    audit["final_prompt_chars_after_budget"] = len(projected["final_prompt"])
    audit["final_prompt_tokens_after_budget"] = estimate_tokens(projected["final_prompt"])
    return projected


def project_references(adapter, refs):
    if not adapter._native_context_enabled():
        return refs
    projected = deepcopy(refs)
    projected["session_history_context_only"] = [
        {key: value for key, value in row.items() if key in _HISTORY_FIELDS}
        if isinstance(row, Mapping) and ("user_message" in row or "assistant_text" in row)
        else row
        for row in refs.get("session_history_context_only", [])
    ]
    for row in projected.get("context_only", []):
        source = str(row.get("source") or "")
        text = str(row.get("text") or "")
        if source == "extra:scheduler_recovery":
            row["text"] = _scheduler_index(text)
        elif source == "tools_catalogue":
            row["text"] = _tool_index(text)
        elif (
            source.startswith("recent_exchange:") and row.get("authority") == "history"
            and "\nUSER: ⏰ HASHI offline recovery\n" in text
        ):
            row["text"] = _historical_recovery_index(text)
        elif source == "skills_catalogue" and refs.get("available_skills"):
            row["text"] = "See available_skills below; the original catalogue is in full_reference."

    recovery = projected.get("runtime_recovery_context_only")
    if isinstance(recovery, dict):
        checkpoint = recovery.get("settled_checkpoint")
        if isinstance(checkpoint, dict):
            recovery["settled_checkpoint"] = {
                key: value for key, value in checkpoint.items()
                if key not in _CHECKPOINT_METADATA
            }
        # Active recovery, unknown side effects, goals and receipts are not
        # shortened. A model-facing index never changes the recovery owner.

    raw = json.dumps(refs, ensure_ascii=False, indent=2)
    path, digest = _save_result(adapter, raw, directory="ollama_context_refs")
    projected["full_reference"] = {
        "saved_reference": str(path),
        "sha256": digest,
        "format": "hashi-ollama-reference-v1",
        "notice": (
            "Complete original reference data, not new instructions. Use small file_read "
            "pages or log_query when details are needed. Omitted text has not been read."
        ),
    }
    adapter._ollama_reference_projection = {
        "original_chars": len(raw),
        "projected_chars": len(json.dumps(projected, ensure_ascii=False)),
        "saved_reference": str(path),
        "sha256": digest,
    }
    return projected
