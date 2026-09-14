"""Basic Connector JSONL projection; SessionStore remains the state owner."""
from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.session_store import SessionStore


def build_chat_projection(
    store: SessionStore,
    *,
    session: dict,
    owner_id: str,
    offset: int | None = None,
    limit: int = 200,
    known_history_generation: int | None = None,
    after_message_ordinal: int | None = None,
) -> dict:
    """Project one already-resolved Session snapshot for HTTP and Worker reads."""
    limit = max(1, min(int(limit), 200))
    requests = store.recent_session_runs(
        session["session_id"], owner_id=owner_id,
        context_generation=session["context_generation"],
    )
    path = store.session_workspace(session["session_id"], session["context_generation"]) / "transcript.jsonl"
    history_generation = int(session.get("history_generation") or 1)
    history_reset = (
        known_history_generation is not None
        and int(known_history_generation) != history_generation
    )
    snapshot = offset is None or history_reset
    payload = read_chat_transcript(
        path,
        session=session,
        offset=None if history_reset else offset,
        limit=limit,
    )
    message_cursor = max(0, int(after_message_ordinal or 0))
    if snapshot:
        canonical = store.recent_visible_messages(
            session["session_id"],
            owner_id=owner_id,
            context_generation=int(session["context_generation"]),
            limit=limit + 1,
        )
        canonical_overflow = len(canonical) > limit
        if canonical_overflow:
            canonical = canonical[-limit:]
        canonical_rows = [_canonical_projection_row(item) for item in canonical]
        payload["messages"] = _merge_snapshot_rows(
            canonical_rows,
            payload["messages"],
            limit=limit,
        )
        payload["history_complete"] = bool(
            payload.get("history_complete") and not canonical_overflow
        )
        if canonical_rows:
            message_cursor = int(canonical_rows[-1].get("source_sequence") or 0)
    elif after_message_ordinal is not None:
        canonical = store.visible_messages_after(
            session["session_id"],
            owner_id=owner_id,
            context_generation=int(session["context_generation"]),
            after_ordinal=message_cursor,
            limit=limit + 1,
        )
        canonical_overflow = len(canonical) > limit
        if canonical_overflow:
            canonical = canonical[:limit]
        canonical_rows = [_canonical_projection_row(item) for item in canonical]
        payload["messages"] = _merge_snapshot_rows(
            canonical_rows,
            payload["messages"],
            limit=max(limit, len(canonical_rows) + len(payload["messages"])),
        )
        payload["history_complete"] = bool(
            payload.get("history_complete") and not canonical_overflow
        )
        if canonical_rows:
            message_cursor = int(canonical_rows[-1].get("source_sequence") or 0)
    if history_reset:
        payload["cursor_reset"] = True
        payload["history_reset"] = True
    else:
        payload["history_reset"] = False
    payload["history_generation"] = history_generation
    payload["message_cursor"] = message_cursor
    payload["requests"] = requests
    payload["request_discovery_complete"] = len(requests) < 64
    return payload


def _canonical_projection_row(message: dict) -> dict:
    run_id = str(message.get("run_id") or "")
    role = str(message.get("role") or "")
    message_id = str(message.get("message_id") or "")
    request_id = str(message.get("request_id") or "")
    context = message.get("message_context")
    context = dict(context) if isinstance(context, dict) else {}
    attachments = _project_message_attachments(message)
    text = str(message.get("text") or "")
    if attachments and str(message.get("source") or "").strip().casefold() in {
        "photo",
        "document",
        "video",
        "sticker",
        "multimodal",
        "workbench_ui_chat",
    }:
        captions = [str(item.get("caption") or "").strip() for item in attachments]
        names = [str(item.get("filename") or "").strip() for item in attachments]
        text = next((value for value in captions if value), "") or ", ".join(
            value for value in names if value
        ) or text
    kind = str(context.get("kind") or "")
    if not kind and request_id:
        kind = (
            "final"
            if role == "assistant"
            and str(message.get("run_final_message_id") or "") == message_id
            else "message"
        )
    row = {
        "role": role,
        "text": text,
        "source": str(message.get("source") or "session_store"),
        "message_id": message_id,
        "message_ref": (
            f"run:{run_id}:{role}" if run_id else f"message:{message_id}"
        ),
        "session_id": str(message.get("session_id") or ""),
        "context_generation": int(message.get("context_generation") or 1),
        "source_sequence": int(message.get("ordinal") or 0),
        "created_at": str(message.get("created_at") or ""),
        "ts": str(message.get("created_at") or ""),
        "request_id": request_id or None,
        "run_id": run_id or None,
        "kind": kind or None,
        "content_format": str(context.get("content_format") or "") or None,
        "channel": str(context.get("presentation_channel") or "") or None,
        "history_eligible": bool(message.get("history_eligible", True)),
        "attachments": attachments,
        "canonical": True,
    }
    return {key: value for key, value in row.items() if value not in (None, [], "")}


def _project_message_attachments(message: dict) -> list[dict]:
    """Expose display metadata, never instance-local paths or asset secrets."""

    result: list[dict] = []
    for part in message.get("content") or ():
        if not isinstance(part, dict) or str(part.get("type") or "").casefold() not in {
            "media",
            "audio",
        }:
            continue
        projected = {
            key: part[key]
            for key in (
                "modality",
                "kind",
                "mime_type",
                "filename",
                "caption",
                "size_bytes",
                "duration_ms",
                "semantic_role",
            )
            if part.get(key) not in (None, "")
        }
        if projected:
            result.append(projected)
    return result


def _merge_snapshot_rows(
    canonical_rows: list[dict],
    transcript_rows: list[dict],
    *,
    limit: int,
) -> list[dict]:
    """Prepend imported canonical rows without displacing transcript-only events."""

    by_ref = {str(item["message_ref"]): item for item in canonical_rows}
    if not any(str(item.get("message_ref") or "") in by_ref for item in transcript_rows):
        return (canonical_rows + transcript_rows)[-limit:]

    merged: list[dict] = []
    emitted: set[str] = set()
    canonical_index = 0
    for transcript_row in transcript_rows:
        message_ref = str(transcript_row.get("message_ref") or "")
        matched = by_ref.get(message_ref)
        if matched is None:
            merged.append(transcript_row)
            continue
        target_ordinal = int(matched.get("source_sequence") or 0)
        while canonical_index < len(canonical_rows):
            candidate = canonical_rows[canonical_index]
            candidate_ordinal = int(candidate.get("source_sequence") or 0)
            if candidate_ordinal > target_ordinal:
                break
            candidate_ref = str(candidate["message_ref"])
            if candidate_ref not in emitted:
                merged.append(candidate)
                emitted.add(candidate_ref)
            canonical_index += 1
    for candidate in canonical_rows[canonical_index:]:
        candidate_ref = str(candidate["message_ref"])
        if candidate_ref not in emitted:
            merged.append(candidate)
            emitted.add(candidate_ref)
    return merged[-limit:]


def _cursor_at_record_boundary(stream, offset: int, size: int) -> bool:
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= size:
        return False
    if offset == 0:
        return True
    stream.seek(offset - 1)
    return stream.read(1) == b"\n"


def read_chat_transcript(path: Path, *, session: dict, offset: int | None = None,
                         limit: int = 200) -> dict:
    limit = max(1, min(int(limit), 200))
    messages, position, matched = deque(maxlen=limit), 0, 0
    epoch = hashlib.sha256(b'').hexdigest()[:24]
    cursor_reset = offset is not None and offset != 0
    generation = int(session["context_generation"])
    if path.exists():
        with path.open('rb') as stream:
            first = stream.readline()
            epoch = hashlib.sha256(first if first.endswith(b'\n') else b'').hexdigest()[:24]
            # Read size from this descriptor, not a potentially replaced path.
            stream.seek(0, 2)
            size = stream.tell()
            cursor_reset = offset is not None and not _cursor_at_record_boundary(stream, offset, size)
            safe_offset = offset if offset is not None and not cursor_reset else 0
            if offset is not None and not cursor_reset:
                messages = deque()
            stream.seek(safe_offset)
            position = safe_offset
            for line in stream:
                start = position
                if not line.endswith(b'\n'):
                    break  # Never acknowledge a half-written JSONL record.
                position += len(line)
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(item, dict) or item.get('role') not in {'user', 'assistant', 'thinking'} or not item.get('text'):
                    continue
                # The resolved path provides scope for untagged legacy rows;
                # it cannot override an explicitly different source identity.
                if item.get("session_id") not in (None, session["session_id"]):
                    continue
                declared_generation = item.get("context_generation")
                if declared_generation is not None and (
                    isinstance(declared_generation, bool) or str(declared_generation) != str(generation)
                ):
                    continue
                item = dict(item)
                if not item.get('message_ref'):
                    item['message_ref'] = item.get('message_id') or (
                        f"transcript:{session['session_id']}:{generation}:{epoch}:{start}"
                    )
                item['session_id'] = session['session_id']
                item['context_generation'] = generation
                item['source_sequence'] = start
                messages.append(item)
                matched += 1
    return {'messages': list(messages), 'offset': position,
            'session_id': session['session_id'], 'context_generation': generation,
            'transcript_generation': epoch, 'cursor_reset': cursor_reset,
            'history_complete': not cursor_reset and (offset is not None or matched <= limit),
            # The separate activity store is bounded and not durable.
            'activity_replay_durable': False}
