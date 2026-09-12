"""Basic Connector JSONL projection; SessionStore remains the state owner."""
from __future__ import annotations

from collections import deque
import hashlib
import json
from pathlib import Path


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
