"""Produce typed Tool media at the producing Tool boundary.

This is not a text classifier: callers must already own actual image bytes
from a screenshot operation. Plain browser output, source code and logs never
pass through this converter.
"""
from __future__ import annotations

import base64

from orchestrator.multimodal_contract import (
    HASHI_API_MAX_IMAGE_BYTES,
    MultimodalContractError,
    _validate_signature,
)


def screenshot_tool_result(
    image: bytes,
    description: str,
    *,
    mime_type: str = "image/png",
):
    """Return a native Tool content block without embedding bytes in stdout."""

    from tools.registry import StructuredToolOutput

    if not isinstance(image, bytes) or not image:
        raise MultimodalContractError(
            "screenshot returned no image bytes", code="MEDIA_UNAVAILABLE",
        )
    if len(image) > HASHI_API_MAX_IMAGE_BYTES:
        raise MultimodalContractError(
            "screenshot exceeds the media byte limit",
            code="MEDIA_LIMIT_EXCEEDED",
        )
    _validate_signature(image[:64], mime_type, "tool-screenshot")
    return StructuredToolOutput(
        output=str(description),
        content=[
            {"type": "text", "text": str(description)},
            {
                "type": "image",
                "mimeType": mime_type,
                "data": base64.b64encode(image).decode("ascii"),
            },
        ],
    )
