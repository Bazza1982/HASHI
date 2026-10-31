"""Shared locale-aware permission request views; rendering never grants access."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape

from orchestrator import ui_language
from orchestrator.command_ui import card_title
from orchestrator.permissions.authority import SessionRequestPage, SessionRequestView


def _label(key: str) -> str:
    return escape(ui_language.tr("safety." + key))


def _state(view: SessionRequestView) -> str:
    request_state = _label("state." + view.request.state)
    if view.task_state != "active":
        request_state += " · " + _label("task." + view.task_state)
    if view.binding_state != "active":
        request_state += " · " + _label("execution_unavailable")
    return request_state


def management_card(outcome: dict) -> str:
    kind = outcome.get("kind")
    if kind == "narrowed":
        return request_detail_card(outcome["request"])
    if kind == "revoked":
        key = "revocation_pending" if outcome.get("cleanup_pending") else "revocation_done"
        return _label(key) + "\n<code>" + escape(outcome["task_id"]) + "</code>"
    if kind == "level_review":
        preview = outcome["preview"]
        lines = [card_title("🛡️", ui_language.tr("safety.level_review")),
                 _label("level_review_explanation"), "", _label("resources")]
        for scope in preview["baseline_scopes"]:
            lines.append("• <code>" + escape(scope["resource"]) + "</code> · "
                         + escape(", ".join(scope["actions"])))
        lines.extend(("", _label("level_review_tools"),
                      escape(", ".join(preview["native_tools"])), "", _label("level_review_restart"),
                      "<code>" + escape("/safety set-level " + outcome["level"]
                                        + " --revision " + outcome["revision"]) + "</code>"))
        return "\n".join(lines)
    if kind == "level_changed":
        key = "level_changed" if outcome.get("adopted") is True else "level_pending"
        return _label(key) + " · <code>" + escape(outcome["level"]) + "</code>"
    raise ValueError("unknown permission management outcome")


def session_summary_card(summary: dict) -> str:
    lines = [card_title("🛡️", ui_language.tr("safety.summary")), ""]
    for task in summary["tasks"]:
        lines.append("<b>" + escape(task["task_id"]) + "</b> · " + escape(task["state"]))
        for lease in task["leases"]:
            expiry = datetime.fromtimestamp(lease.expires_at, timezone.utc).isoformat(timespec="seconds")
            lines.append("• <code>" + escape(lease.lease_id) + "</code> · " + escape(lease.status)
                         + " · " + escape(lease.lifetime) + " · " + escape(expiry))
            for scope in lease.scopes:
                lines.append("  " + escape(scope.resource) + " · " + escape(", ".join(scope.actions)))
            if lease.status in {"active", "reserved"}:
                lines.append("<code>" + escape("/safety revoke " + lease.lease_id) + "</code>")
        lines.append("<code>" + escape("/safety revoke-task " + task["task_id"]) + "</code>")
        lines.append("")
    if not summary["tasks"]:
        lines.append(_label("no_tasks"))
    if summary.get("truncated") or any(task.get("leases_truncated") for task in summary["tasks"]):
        lines.append(_label("summary_truncated"))
    return "\n".join(lines)


def request_list_card(page: SessionRequestPage) -> str:
    lines = [card_title("🛡️", ui_language.tr("safety.requests")), ""]
    if not page.requests:
        lines.append(_label("no_requests"))
    for view in page.requests:
        request = view.request
        lines.extend((
            f"<b>{_state(view)}</b> · <code>{escape(request.request_id)}</code>",
            f"<code>{escape('/safety request ' + request.request_id)}</code>",
            "",
        ))
    if page.next_before is not None:
        lines.extend((_label("next_page"),
                      f"<code>{escape('/safety requests before ' + page.next_before)}</code>"))
    lines.extend(("", _label("no_grants")))
    return "\n".join(lines)


def request_prompt_card(view: SessionRequestView) -> str:
    """Compact button-first view; diagnostics and typed commands stay in detail."""
    request = view.request
    lines = [card_title("🛡️", ui_language.tr("safety.request")), "", f"<b>{_state(view)}</b>"]
    for scope in request.scopes:
        actions = ", ".join(_label("action." + action) for action in scope.actions)
        lines.append(f"{escape(scope.resource)} · {actions}")
    if request.purpose:
        lines.extend(("", escape(request.purpose)))
    return "\n".join(lines)


def request_detail_card(view: SessionRequestView) -> str:
    request = view.request
    expires = datetime.fromtimestamp(request.expires_at, timezone.utc).isoformat(timespec="seconds")
    lines = [
        card_title("🛡️", ui_language.tr("safety.request")),
        "",
        f"<b>{_state(view)}</b>",
        f"{_label('request_id')} · <code>{escape(request.request_id)}</code>",
        f"{_label('task_id')} · <code>{escape(request.task_id)}</code>",
        f"{_label('revision')} · <code>{request.revision}</code>",
        f"{_label('expires')} · <code>{escape(expires)}</code>",
        "",
        _label("resources"),
    ]
    for scope in request.scopes:
        actions = ", ".join(_label("action." + action) for action in scope.actions)
        lines.append(f"• <code>{escape(scope.resource)}</code> · {actions}")
    if request.purpose:
        lines.extend(("", _label("agent_reason"), escape(request.purpose)))
    if (request.state == "pending" and view.task_state in {"active", "paused"}
            and view.binding_state == "active"):
        command = f"/safety withdraw {request.request_id} --revision {request.revision}"
        lines.extend(("", _label("withdraw"), f"<code>{escape(command)}</code>"))
    lines.extend(("", _label("no_grants"), "<code>/safety requests</code>"))
    return "\n".join(lines)


def backend_permission_notice(status) -> str:
    """Plain text shared by settings and backend selection; no observed L2 claim."""
    from orchestrator.permissions.backend_capabilities import BackendPermissionStatus

    if not isinstance(status, BackendPermissionStatus):
        raise ValueError("a current backend permission status is required")
    if status.state == "managed":
        from orchestrator.permissions.backend_capabilities import CONTROLS

        states = {"enforced", "partial", "unmanaged", "unavailable", "unknown"}
        priority = {"unknown": 0, "unavailable": 1, "unmanaged": 2, "partial": 3, "enforced": 4}
        lines = [ui_language.tr("safety.backend_observed", backend=status.backend_id)]
        for control in CONTROLS:
            values = [report.get("controls", {}).get(control, {}).get("state", "unknown") for report in status.reports]
            value = min((item if item in states else "unknown" for item in values), key=priority.get)
            lines.append(ui_language.tr("safety.control_" + control) + " · " + ui_language.tr("safety.state_" + value))
        if status.requested_level.value == "L2":
            lines.append(ui_language.tr("safety.approval_isolation_note"))
        engines = sorted({item["engine"] for report in status.reports
                          for item in report.get("analysis", {}).get("models", [])})
        if engines:
            lines.append(ui_language.tr("safety.analysis_location", providers=", ".join(engines)))
        return "\n".join(lines)
    key = "safety.backend_unmanaged" if status.state == "unmanaged" else "safety.backend_unavailable"
    return ui_language.tr(key, backend=status.backend_id, level=status.requested_level.value)
