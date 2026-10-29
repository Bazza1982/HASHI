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
    key = "safety.backend_unmanaged" if status.state == "unmanaged" else "safety.backend_unavailable"
    return ui_language.tr(key, backend=status.backend_id, level=status.requested_level.value)
