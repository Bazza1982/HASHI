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
    return (
        "Tool index; native schemas govern calls. Read full_reference for complete "
        "descriptions before using a tool whose schema is not advertised.\n"
        + "\n".join(line[:180] for line in lines)
    )


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
