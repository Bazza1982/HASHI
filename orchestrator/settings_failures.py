"""Shared FC presentation of PAO setting failures; never retries a write."""
from __future__ import annotations

import html
import logging
from dataclasses import replace
from typing import Any

from orchestrator import ui_language
from orchestrator.operation_failures import OperationFailed, OperationFailure, operation_failure


def record_settings_failure(runtime: Any, error: Exception, *, operation: str, phase: str,
                            unconfirmed: bool = False) -> OperationFailure:
    failure = error.failure if isinstance(error, OperationFailed) else operation_failure(
        error, operation=operation, phase=phase, unconfirmed=unconfirmed,
    )
    # Requested values are validated before these document boundaries.
    if not isinstance(error, OperationFailed) and isinstance(error, ValueError):
        failure = replace(failure, reason="configuration_invalid",
                          error_code=f"{operation}_{phase}_configuration_invalid")
    target = getattr(runtime, "error_logger", None) or getattr(runtime, "logger", None)
    write = getattr(target, "error", None) or getattr(target, "exception", None)
    if not callable(write):
        write = logging.getLogger("HASHI.Settings").error
    write("Settings operation failure: %s", failure.diagnostic())
    return failure


def settings_failure_summary(failure: OperationFailure, *, locale: str | None = None) -> str:
    return ui_language.tr(
        "settings.result_unconfirmed" if failure.unconfirmed else "settings.operation_failed", locale=locale,
    )


def settings_failure_text(failure: OperationFailure, *, locale: str | None = None) -> str:
    return "\n".join((
        settings_failure_summary(failure, locale=locale), failure.render(locale=locale),
        ui_language.tr("settings.failure.refresh." + failure.operation, locale=locale),
    ))


async def render_settings_callback_failure(query: Any, failure: OperationFailure, *,
                                           locale: str | None = None, current_text: str = "",
                                           keyboard: Any = None) -> None:
    alert = settings_failure_summary(failure, locale=locale)[:140] + "\n" + failure.reference
    await query.answer(alert, show_alert=True)
    message = html.escape(settings_failure_text(failure, locale=locale))
    if current_text:
        message += "\n\n" + current_text
    await query.edit_message_text(message, parse_mode="HTML", reply_markup=keyboard)
