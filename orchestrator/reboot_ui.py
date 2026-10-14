"""Frontend-owned reboot notification and status rendering."""

from datetime import datetime, timezone
from html import escape

from orchestrator import ui_language


def render_notice(
    record, *, starting=False, sender=None, sender_display=None, locale=None
):
    language = locale or record.get("locale")
    mode = record.get("mode", "same")
    if mode not in {"min", "number", "same", "max", "group"}:
        mode = "generic"
    scope = ui_language.tr("reboot.scope." + mode, locale=language)
    names = record.get("display_names", {})
    all_targets = list(record.get("targets", []))
    total_count = len(all_targets)
    targets = all_targets
    recovered_targets = []
    outcome = record.get("outcome")
    confidence = record.get("observation_confidence")
    if not starting and outcome in {"partial", "failed"}:
        recovered_targets = [
            name for name in targets if record.get("online", {}).get(name)
        ]
        targets = [name for name in targets if name not in recovered_targets]
    elif not starting and record["status"] == "failed" and not record.get("restored"):
        recovered_targets = [
            name for name in targets if record.get("online", {}).get(name)
        ]
        targets = [name for name in targets if name not in recovered_targets]
    shown = [escape(str(names.get(name) or name)) for name in targets[:6]]
    if len(targets) > 6:
        shown.append(
            ui_language.tr(
                "reboot.more_targets", locale=language, count=len(targets) - 6
            )
        )
    target = ", ".join(shown) or escape(
        str(names.get(record.get("source_agent")) or record.get("source_agent") or "")
    )
    lifecycle = record.get("lifecycle_state")
    partial = outcome == "partial" or bool(recovered_targets and targets)
    if starting:
        status = "starting"
    elif partial and confidence == "unconfirmed":
        status = "partial_unconfirmed"
    elif partial:
        status = "partial"
    elif outcome == "succeeded_with_warning":
        status = "succeeded_with_warning"
    elif outcome == "rolled_back":
        status = "rolled_back"
    elif outcome == "failed":
        status = "unconfirmed" if confidence == "unconfirmed" else "unavailable"
    elif outcome == "rejected":
        status = "rejected"
    elif outcome == "succeeded":
        status = "online"
    elif lifecycle == "unconfirmed" and record.get("status") == "failed":
        status = "unavailable"
    elif lifecycle in {
        "candidate_rejected",
        "committed",
        "rolled_back",
        "online",
        "rejected",
        "unconfirmed",
    }:
        status = lifecycle
    else:
        status = record["status"]
        if status == "failed":
            status = "restored" if record.get("restored") else "unavailable"
    reason = record.get("reason")
    source_update_incomplete = reason == "source_update_incomplete"
    key = "reboot.notice." + (
        "source_update_incomplete" if source_update_incomplete else status
    )
    broad = mode == "max"
    if broad and not source_update_incomplete and status in {
        "starting",
        "candidate_rejected",
        "committed",
        "rolled_back",
        "online",
        "succeeded",
        "succeeded_with_warning",
        "partial",
        "partial_unconfirmed",
        "rejected",
        "restored",
        "unavailable",
        "unconfirmed",
    }:
        key += "_broad"
    elif total_count == 1 and status in {
        "starting",
        "online",
        "succeeded",
        "succeeded_with_warning",
    }:
        key += "_single"
    adoption = (record.get("shared_replacement") or {}).get("adoption") or {}
    fallback = adoption.get("status") == "fallback"
    fallback_reason_code = str(adoption.get("reason_code") or "qualification_failed")
    if fallback_reason_code not in {"source_uncommitted", "qualification_failed"}:
        fallback_reason_code = "qualification_failed"
    if fallback and not starting and record.get("status") == "succeeded":
        key = "reboot.notice.fallback_broad" if broad else "reboot.notice.fallback"
    text = ui_language.tr(
        key,
        locale=language,
        scope=scope,
        agents=target,
        count=total_count,
        duration=(
            f"{float(record.get('duration_seconds') or 0):.1f}".rstrip("0").rstrip(".")
        ),
        online_count=len(recovered_targets),
        failed_count=len(targets),
        fallback_reason=ui_language.tr(
            "reboot.fallback_reason." + fallback_reason_code, locale=language
        ),
    )
    if fallback and not starting and record.get("status") != "succeeded":
        text += "\n" + ui_language.tr(
            "reboot.notice.fallback_detail",
            locale=language,
            fallback_reason=ui_language.tr(
                "reboot.fallback_reason." + fallback_reason_code, locale=language
            ),
        )
    if recovered_targets and not partial:
        text += "\n" + ui_language.tr(
            "reboot.restored_targets",
            locale=language,
            agents=", ".join(
                escape(str(names.get(name) or name)) for name in recovered_targets[:6]
            ),
        )
    failure_details = []
    workers = record.get("workers") or {}
    for name in targets[:6]:
        checks = list((workers.get(name) or {}).get("failed_checks") or ())
        if not checks:
            continue
        check_text = ", ".join(
            ui_language.tr("reboot.check." + check, locale=language)
            for check in checks[:3]
        )
        failure_details.append(
            ui_language.tr(
                "reboot.failed_target",
                locale=language,
                agent=escape(str(names.get(name) or name)),
                checks=check_text,
            )
        )
    if failure_details:
        text += "\n" + "\n".join(failure_details)
    if confidence == "unconfirmed" and outcome in {"partial", "failed"}:
        text += "\n" + ui_language.tr(
            "reboot.observation_unconfirmed", locale=language
        )
    warning_keys = {
        "old_worker_exit_pending",
        "topology_publish_failed",
        "generation_state_publish_failed",
    }
    for warning in record.get("warnings") or ():
        warning_key = warning if warning in warning_keys else "followup_incomplete"
        text += "\n" + ui_language.tr(
            "reboot.warning." + warning_key,
            locale=language,
        )
    if (
        reason
        and not starting
        and not source_update_incomplete
        and not (reason == "readiness" and failure_details)
    ):
        text += "\n" + ui_language.tr(
            "reboot.reason",
            locale=language,
            reason=ui_language.tr("reboot.reason." + reason, locale=language),
        )
    if reason == "target_busy" and record.get("busy_targets"):
        busy_targets = list(dict.fromkeys(record["busy_targets"]))
        busy_names = [
            escape(str(names.get(name) or name)) for name in busy_targets[:6]
        ]
        if len(busy_targets) > 6:
            busy_names.append(
                ui_language.tr(
                    "reboot.more_targets",
                    locale=language,
                    count=len(busy_targets) - 6,
                )
            )
        text += "\n" + ui_language.tr(
            "reboot.busy_targets",
            locale=language,
            agents=", ".join(busy_names),
        )
    if sender and sender != record.get("source_agent"):
        text += "\n" + ui_language.tr(
            "reboot.sent_by",
            locale=language,
            agent=escape(str(sender_display or names.get(sender) or sender)),
        )
    return text


def render_status(record, *, locale=None):
    if not record:
        return ui_language.tr("reboot.no_recent", locale=locale)
    text = render_notice(
        record,
        starting=(
            record["status"] in {"accepted", "running"}
            and record.get("lifecycle_state", "accepted") == "accepted"
        ),
        locale=locale,
    )
    text += "\n" + ui_language.tr(
        "reboot.delivery_status",
        locale=locale,
        status=ui_language.tr(
            "reboot.delivery." + record["delivery"]["status"], locale=locale
        ),
    )
    return text


def _receipt_time(value):
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (OverflowError, TypeError, ValueError):
        return None


def _menu_target(record, *, locale=None):
    mode = str(record.get("mode") or "generic")
    if mode not in {"min", "number", "same", "max", "group"}:
        mode = "generic"
    scope = ui_language.tr("reboot.scope." + mode, locale=locale)
    targets = list(record.get("targets") or ())
    if mode == "max" or not targets:
        return ui_language.tr(
            "reboot.menu.target_count",
            locale=locale,
            scope=escape(scope),
            count=len(targets),
        )
    names = dict(record.get("display_names") or {})
    shown = [escape(str(names.get(name) or name)) for name in targets[:6]]
    if len(targets) > 6:
        shown.append(
            escape(
                ui_language.tr(
                    "reboot.more_targets", locale=locale, count=len(targets) - 6
                )
            )
        )
    return ui_language.tr(
        "reboot.menu.target_agents",
        locale=locale,
        scope=escape(scope),
        agents=", ".join(shown),
    )


def render_menu_status(record, *, locale=None):
    """Label a menu projection without changing proactive receipt notices."""

    if not record:
        return render_status(record, locale=locale)
    active = record.get("status") in {"accepted", "running"}
    heading_key = "reboot.menu.current" if active else "reboot.menu.previous"
    time_key = "reboot.menu.started_at" if active else "reboot.menu.completed_at"
    timestamp = _receipt_time(
        record.get("created_at") if active else record.get("finished_at")
    )
    lines = [f"<b>{escape(ui_language.tr(heading_key, locale=locale))}</b>"]
    if timestamp:
        lines.append(
            f"{escape(ui_language.tr(time_key, locale=locale))} · "
            f"<code>{escape(timestamp)}</code>"
        )
    lines.extend(
        [
            f"{escape(ui_language.tr('common.target', locale=locale))} · "
            f"{_menu_target(record, locale=locale)}",
            "",
            render_status(record, locale=locale),
        ]
    )
    return "\n".join(lines)
