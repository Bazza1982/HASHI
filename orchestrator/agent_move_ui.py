"""Frontend rendering for durable Agent transfer completion notices."""

from __future__ import annotations

import html
from collections.abc import Mapping

from orchestrator import ui_language
from orchestrator.agent_move.package import PERSONAL_CLONE_HISTORY_NOT_INCLUDED_WARNING


def render_clone_warnings(result: Mapping, *, locale: str | None = None) -> str:
    """Show transfer notes consistently on interactive and background cards."""
    raw = result.get("warnings")
    if not isinstance(raw, (list, tuple)):
        return ""
    warnings = list(dict.fromkeys(str(item) for item in raw if str(item).strip()))
    if not warnings:
        return ""
    lines = [ui_language.tr("remote.move.review_notes", locale=locale)]
    for warning in warnings:
        if warning == PERSONAL_CLONE_HISTORY_NOT_INCLUDED_WARNING:
            warning = ui_language.tr("remote.clone.history.other_source_retained", locale=locale)
        lines.append(f"  • {html.escape(warning)}")
    return "\n\n" + "\n".join(lines)


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
        message = ui_language.tr(
            "remote.clone.complete_inactive" if result.get("desired_target_active") is False
            else "remote.clone.complete",
            locale=locale,
            source_agent=html.escape(str(result.get("agent_id") or "")),
            target_agent=html.escape(
                str(result.get("target_agent_id") or result.get("agent_id") or "")
            ),
            target=html.escape(str(result.get("target_instance") or "")),
        )
        if result.get("retain_telegram_backup"):
            key = (
                "remote.clone.backup_confirmed" if result.get("telegram_backup_confirmed") is True
                else "remote.clone.backup_absent" if result.get("telegram_backup_present") is False
                else "remote.clone.backup_unconfirmed"
            )
            message += "\n\n" + ui_language.tr(key, locale=locale)
        return message + render_clone_warnings(result, locale=locale)
    message = ui_language.tr(
        "remote.move.lifecycle_complete",
        locale=locale,
        agent=html.escape(str(result.get("agent_id") or "")),
        target_agent=html.escape(
            str(result.get("target_agent_id") or result.get("agent_id") or "")
        ),
        target=html.escape(str(result.get("target_instance") or "")),
    )
    pending_count = result.get("source_retained_pending_count") or 0
    if isinstance(pending_count, int) and pending_count > 0:
        message += "\n\n" + ui_language.tr(
            "remote.move.source_pending_retained", locale=locale, count=pending_count,
        )
    retained = result.get("retained_source_workspace")
    if retained:
        message += "\n\n" + ui_language.tr(
            "remote.move.source_workspace_retained", locale=locale,
            path=html.escape(str(retained)),
        )
    elif (result.get("source_workspace_retention") or {}).get("status") == "missing":
        message += "\n\n" + ui_language.tr("remote.move.source_workspace_missing", locale=locale)
    return message
# HASHI-Fingerprint: hfp_600d5d12128e4ae890916649c7af9703
