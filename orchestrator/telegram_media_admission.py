"""Trusted Telegram downloads admitted through PAO's Session attachment store.

Only Telegram's download and completed /long paths call this adapter. A media
path supplied to an Engine or an API is not an attachment admission capability.
"""
from __future__ import annotations

import json
from typing import Any

from orchestrator.multimodal_contract import (
    _read_authorized_media,
    attachment_manifest,
    canonical_request_content,
    normalize_request_content,
)


def admit_telegram_media(
    runtime: Any,
    *,
    chat_id: Any,
    prompt: str,
    request_content: dict[str, Any],
    request_metadata: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Bind downloaded bytes and persisted Message references to one Session."""
    content = normalize_request_content(request_content)
    metadata = dict(request_metadata)
    store = getattr(runtime, "session_store", None)
    if store is None or content is None or not any(
        part["type"] == "media" for part in content["parts"]
    ):
        # Embedded/legacy callers without PAO retain their local-path contract.
        return prompt, request_content, metadata

    from orchestrator import runtime_session

    session, owner, surface, channel = runtime_session.resolve_request_session(
        runtime, source="telegram", chat_id=chat_id, metadata=metadata,
    )
    scope = {"session_id": session["session_id"], "owner_id": owner}
    metadata.update({
        **scope,
        "session_surface": surface,
        "session_channel_key": channel,
        "session_context_generation": session["context_generation"],
    })
    parts = []
    replacements = {}
    for part in content["parts"]:
        if part["type"] != "media":
            parts.append(dict(part))
            continue
        # Use only this Agent's Telegram download directory. Integrity is
        # checked against the intake receipt before any bytes are committed.
        staged = store.stage_attachment(
            **scope,
            filename=part["filename"],
            media_type=part["mime_type"],
            size_bytes=part["size_bytes"],
            sha256=part["sha256"],
            semantic_role=part.get("semantic_role", ""),
            duration_ms=part.get("duration_ms"),
        )
        payload = _read_authorized_media(
            part, authorized_roots=(runtime.media_dir,),
        )
        store.upload_attachment_bytes(
            **scope, attachment_id=staged["attachment_id"], payload=payload,
        )
        store.commit_attachment(**scope, attachment_id=staged["attachment_id"])
        canonical = store.attachment_canonical_part(
            **scope,
            attachment_id=staged["attachment_id"],
            item_index=part["item_index"],
            caption=part.get("caption", ""),
            detail=part.get("detail", ""),
        )
        canonical["kind"] = part["kind"]
        canonical["transport"] = dict(part.get("transport") or {})
        replacements[part["local_ref"]] = canonical["local_ref"]
        parts.append(canonical)

    def rewrite_paths(text: str) -> str:
        for old, new in replacements.items():
            text = text.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1])
            text = text.replace(old, new)
        return text

    prompt = rewrite_paths(prompt)
    blocks = []
    for part in parts:
        if part["type"] == "text":
            part["text"] = rewrite_paths(part["text"])
            blocks.append({"type": "text", "text": part["text"]})
        else:
            blocks.append({
                "type": "attachment", "attachment_id": part["attachment_id"],
                "caption": part.get("caption", ""),
                **({"semantic_role": part["semantic_role"]}
                   if part.get("semantic_role") else {}),
            })
    content = canonical_request_content(parts)
    metadata.update({
        "canonical_content_version": content["version"],
        "attachment_manifest": [dict(item) for item in attachment_manifest(content)],
        "session_message_content": blocks,
    })
    return prompt, content, metadata
