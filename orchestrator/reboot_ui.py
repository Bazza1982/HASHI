"""Frontend-owned reboot notification and status rendering."""

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
    targets = record.get("targets", [])
    recovered_targets = []
    if not starting and record["status"] == "failed" and not record.get("restored"):
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
    status = "starting" if starting else record["status"]
    if status == "failed":
        status = "restored" if record.get("restored") else "unavailable"
    key = "reboot.notice." + status
    text = ui_language.tr(key, locale=language, scope=scope, agents=target)
    if recovered_targets:
        text += "\n" + ui_language.tr(
            "reboot.restored_targets",
            locale=language,
            agents=", ".join(
                escape(str(names.get(name) or name)) for name in recovered_targets[:6]
            ),
        )
    reason = record.get("reason")
    if reason and not starting:
        text += "\n" + ui_language.tr(
            "reboot.reason",
            locale=language,
            reason=ui_language.tr("reboot.reason." + reason, locale=language),
        )
    if record.get("recovered") and not starting:
        text = ui_language.tr("reboot.delayed_notice", locale=language) + "\n" + text
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
        record, starting=record["status"] in {"accepted", "running"}, locale=locale
    )
    text += "\n" + ui_language.tr(
        "reboot.delivery_status",
        locale=locale,
        status=ui_language.tr(
            "reboot.delivery." + record["delivery"]["status"], locale=locale
        ),
    )
    return text
