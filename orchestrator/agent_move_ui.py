"""Frontend rendering for durable Agent transfer completion notices."""

from __future__ import annotations

import html
from collections.abc import Mapping

from orchestrator import ui_language


def render_background_notice(
    record: Mapping,
    *,
    sender: str | None = None,
    sender_display: str | None = None,
) -> str:
    del sender, sender_display
    locale = str(record.get("locale") or "") or None
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    operation = str(record.get("operation") or result.get("operation") or "move")
    if record.get("status") != "completed":
        return (
            ui_language.tr(
                "remote.move.failed",
                locale=locale,
                error=html.escape(str(record.get("last_error") or "unknown error")),
            )
            + "\n\n"
            + ui_language.tr("remote.move.recovery_hint", locale=locale)
        )
    if operation == "clone":
        return ui_language.tr(
            "remote.clone.complete",
            locale=locale,
            source_agent=html.escape(str(result.get("agent_id") or "")),
            target_agent=html.escape(
                str(result.get("target_agent_id") or result.get("agent_id") or "")
            ),
            target=html.escape(str(result.get("target_instance") or "")),
        )
    return ui_language.tr(
        "remote.move.lifecycle_complete",
        locale=locale,
        agent=html.escape(str(result.get("agent_id") or "")),
        target_agent=html.escape(
            str(result.get("target_agent_id") or result.get("agent_id") or "")
        ),
        target=html.escape(str(result.get("target_instance") or "")),
    )
