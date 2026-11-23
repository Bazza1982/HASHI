"""Conservative text-token projection of typed multimodal Provider requests.

The provider wire body is never altered. Native media bytes are excluded only
from text token estimation; their actual byte limits remain provider-owned.
Ordinary user/tool text is *never* classified by keywords or regex.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


_MEDIA_PART_KINDS = frozenset({
    "image_url", "input_image", "image", "input_audio", "audio",
    "input_video", "video", "input_file", "file",
})


def _text_only(value: Any) -> Any:
    if isinstance(value, Mapping):
        kind = str(value.get("type") or "").strip().casefold()
        if kind in _MEDIA_PART_KINDS:
            # Type comes from a trusted content part, never the contents of a
            # text field. Keep a small marker for per-part structural overhead.
            return {"type": kind, "media_payload": "[typed media]"}
        return {str(key): _text_only(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_text_only(item) for item in value]
    return value


def provider_text_capacity_json(payload: Mapping[str, Any]) -> str:
    """Return text and Tool schemas for conservative capacity preflight.

    Media payload byte limits and native visual/audio token accounting are
    separate. This view must never be transmitted or stored as the request.
    """

    projection = {
        key: (_text_only(payload.get(key)) if key == "messages" else payload.get(key))
        for key in ("messages", "tools")
        if key in payload
    }
    return json.dumps(
        projection, ensure_ascii=False, separators=(",", ":"), default=str,
    )
