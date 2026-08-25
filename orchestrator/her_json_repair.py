"""Shared, data-only input contract for the HER v2 JSON Repair specialist."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

MAX_REJECTED_OUTPUT_CHARS = 200_000
MAX_VALIDATION_ERROR_CHARS = 4_000


def _bounded_text(value: Any, *, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit]


def render_json_repair_input(
    *,
    rejected_output: str,
    required_schema: Mapping[str, Any],
    validation_error: str,
) -> str:
    """Render the sole user-message envelope accepted by JSON Repair.

    The original task, attachments, tools, Persona, and workflow context are
    deliberately absent. Values remain quoted data beneath the isolated
    ``system_json_repair`` contract.
    """

    return json.dumps(
        {
            "rejected_output": _bounded_text(
                rejected_output,
                limit=MAX_REJECTED_OUTPUT_CHARS,
            ),
            "required_schema": dict(required_schema),
            "validation_error": _bounded_text(
                validation_error,
                limit=MAX_VALIDATION_ERROR_CHARS,
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
