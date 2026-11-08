"""Opt-in governed human approval, opened by an ordinary frontend URL button.

The opaque invitation locates a pending request; it authenticates nobody and
grants nothing. Human credentials and session cookies stay in the trusted FC
and its existing identity/control service, never in command metadata or Workers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from html import escape
import inspect
import ipaddress
import secrets
import threading
import time
from urllib.parse import urlsplit

from aiohttp import web

from orchestrator.permissions.async_work import settled_thread
from orchestrator.permissions.authority import PermissionDenied, RevisionConflict
from orchestrator.permissions.enterprise_decisions import SessionDecisionDenied
from orchestrator.permissions.models import PermissionSubject
from orchestrator.permissions.wire import PermissionRemoteError
from orchestrator.ui_language import normalize_locale, tr

_PATH = "/api/auth/permission-approval"
_CSRF, _SESSION = "hashi_approval_csrf", "hashi_approval_session"


def approval_origin(value):
    """Only an operator-configured origin may receive governed credentials."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("a configured approval origin is required")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("invalid approval origin") from error
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or any(character.isspace() for character in value) or "\\" in value
            or port is not None and not 1 <= port <= 65535):
        raise ValueError("approval URL must be an exact origin")
    if parsed.scheme == "http":
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.casefold() == "localhost"
        if not loopback:
            raise ValueError("governed approval requires HTTPS outside loopback")
    return parsed.scheme + "://" + parsed.netloc.lower()


@dataclass
class _Invitation:
    subject: PermissionSubject
    request_id: str
    revision: int
    expires_at: float
    locale: str
    binding: str
    path: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    actions: dict = field(default_factory=dict, repr=False)
    selected: str | None = None
    outcome: object = field(default=None, repr=False)
    uncertain: bool = False
    revoked: bool = False


class HumanApproval:
    def __init__(self, control, session_store, origin):
        self.control, self.store = control, session_store
        self.origin = approval_origin(origin)
        self._invitations = {}
        self._lock = threading.RLock()

    def invite(self, subject, request_id, revision, *, locale, connection_binding):
        if not isinstance(subject, PermissionSubject) or not isinstance(connection_binding, str) or not connection_binding:
            raise ValueError("an original frontend subject is required")
        view = self.control.get_session_request(subject, request_id)
        if (view.request.revision != revision or view.request.state != "pending"
                or view.task_state not in {"active", "paused"} or view.binding_state != "active"
                or view.request.expires_at <= time.time()):
            raise RevisionConflict("the pending request changed")
        ticket = secrets.token_urlsafe(32)
        invitation = _Invitation(subject, request_id, revision, view.request.expires_at,
                                 normalize_locale(locale), connection_binding)
        with self._lock:
            self._invitations = {key: item for key, item in self._invitations.items()
                                 if item.expires_at > time.time()}
            self._invitations[ticket] = invitation
        return self.origin + _PATH + "/" + ticket

    def _session(self, invitation):
        subject = invitation.subject
        session = self.store.get_session(subject.session_id, owner_id=subject.owner_id, include_deleted=False)
        if (session["agent_id"] != subject.agent_id or session["status"] != "active"
                or str(session["context_generation"]) != subject.context_generation
                or self.store.instance_id != subject.instance_id):
            raise PermissionDenied("the original Session changed")
        return self.control.get_session_request(subject, invitation.request_id)

    async def _owner(self, invitation, token):
        user = await settled_thread(self.control.session_user, session_token=token)
        if user.get("owner_id") != invitation.subject.owner_id:
            raise SessionDecisionDenied("this human does not own the request")

    def _response(self, invitation, body, *, status=200, csrf=None):
        locale = invitation.locale
        title = escape(tr("safety.human_title", locale=locale))
        response = web.Response(
            text='<!doctype html><html lang="' + escape(locale, quote=True) + '"><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width, initial-scale=1">'
                 '<title>' + title + '</title><main><h1>' + title + '</h1>' + body + '</main></html>',
            content_type="text/html", status=status,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin",
                     "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                     "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"},
        )
        if csrf:
            response.set_cookie(_CSRF, csrf, path=invitation.path, httponly=True,
                                secure=self.origin.startswith("https:"), samesite="Strict")
        return response

    def _form(self, invitation, csrf, operation, contents):
        hidden = '<input type="hidden" name="csrf" value="' + escape(csrf, quote=True) + '">'
        hidden += '<input type="hidden" name="operation" value="' + operation + '">'
        return '<form method="post" action="' + invitation.path + '">' + hidden + contents + '</form>'

    def _login(self, invitation, csrf, *, status=200):
        locale = invitation.locale
        fields = ""
        for name, input_type in (("org_id", "text"), ("email", "email"), ("password", "password")):
            label = escape(tr("safety.human_" + name, locale=locale))
            fields += '<p><label>' + label + ' <input required name="' + name + '" type="' + input_type + '"'
            fields += ' autocomplete="' + ("current-password" if name == "password" else "username" if name == "email" else "organization") + '"></label></p>'
        fields += '<button type="submit">' + escape(tr("safety.human_login", locale=locale)) + '</button>'
        body = '<p>' + escape(tr("safety.human_login_note", locale=locale)) + '</p>'
        if status != 200:
            body += '<p role="alert">' + escape(tr("safety.human_login_failed", locale=locale)) + '</p>'
        return self._response(invitation, body + self._form(invitation, csrf, "login", fields), status=status, csrf=csrf)

    def _button(self, invitation, csrf, operation, label, *, action_id=None):
        value = '<input type="hidden" name="action_id" value="' + escape(action_id, quote=True) + '">' if action_id else ""
        return self._form(invitation, csrf, operation, value + '<button type="submit">' + escape(label) + '</button>')

    async def handle(self, request):
        ticket = request.match_info["ticket"]
        with self._lock:
            invitation = self._invitations.get(ticket)
        if invitation is None or invitation.expires_at <= time.time():
            raise web.HTTPGone(text="This approval link has expired. Open the current request again.")
        # Never trust Host or forwarded headers to choose a credential target.
        if request.scheme + "://" + request.host.lower() != self.origin:
            raise web.HTTPForbidden(text="Approval origin does not match.")
        invitation.path = _PATH + "/" + ticket
        csrf = request.cookies.get(_CSRF, "")
        token = request.cookies.get(_SESSION, "")
        if request.method == "GET" and not csrf:
            csrf = secrets.token_urlsafe(32)
        try:
            async with invitation.lock:
                if request.method == "POST":
                    if request.headers.get("Origin") != self.origin:
                        raise web.HTTPForbidden(text="A same-origin human form is required.")
                    if request.content_length is None or request.content_length > (4096 + 320 + 128) * 4 + 2048:
                        raise web.HTTPBadRequest(text="Invalid human form.")
                    form = await request.post()
                    submitted = form.get("csrf", "")
                    if (not csrf or not isinstance(submitted, str)
                            or not secrets.compare_digest(csrf, submitted)
                            or any(len(form.getall(key)) != 1 for key in form)):
                        raise web.HTTPForbidden(text="The human form changed.")
                    operation = form.get("operation")
                    if operation == "login":
                        if set(form) != {"csrf", "operation", "org_id", "email", "password"}:
                            raise web.HTTPBadRequest(text="Invalid human login form.")
                        login = await settled_thread(self.control.login, **{key: form[key] for key in ("org_id", "email", "password")})
                        new_token = login["session"]["token"]
                        try:
                            await self._owner(invitation, new_token)
                        except BaseException:
                            await settled_thread(self.control.logout, session_token=new_token)
                            raise

                        session_expiry = datetime.fromisoformat(login["session"]["expires_at"]).timestamp()
                        response = web.Response(status=303, headers={
                            "Location": invitation.path, "Cache-Control": "no-store", "Referrer-Policy": "same-origin",
                        })
                        response.set_cookie(_SESSION, new_token, path=invitation.path, httponly=True,
                                            secure=self.origin.startswith("https:"), samesite="Strict",
                                            max_age=max(0, int(min(invitation.expires_at, session_expiry) - time.time())))
                        return response
                    if operation == "logout":
                        if set(form) != {"csrf", "operation"}:
                            raise web.HTTPBadRequest(text="Invalid human form.")
                        await settled_thread(self.control.logout, session_token=token)
                        response = self._login(invitation, secrets.token_urlsafe(32))
                        response.del_cookie(_SESSION, path=invitation.path)
                        return response
                    await self._owner(invitation, token)
                    if invitation.uncertain:
                        return self._response(invitation, '<p role="alert">' + escape(tr("safety.human_uncertain", locale=invitation.locale)) + '</p>', status=503)
                    if operation == "decide":
                        action_id = form.get("action_id", "")
                        actions = invitation.actions.get(hashlib.sha256(csrf.encode()).hexdigest(), {})
                        if (set(form) != {"csrf", "operation", "action_id"} or action_id not in actions
                                or invitation.selected is not None and invitation.selected != action_id):
                            raise web.HTTPConflict(text="The displayed decision changed.")
                        view = await settled_thread(self._session, invitation)
                        if view.request.revision != invitation.revision:
                            raise RevisionConflict("the displayed request changed")
                        invitation.selected = action_id
                        try:
                            invitation.outcome = await settled_thread(
                                self.control.submit_session_action, invitation.subject, action_id, session_token=token,
                            )
                        except BaseException:
                            invitation.uncertain = True
                            raise
                    elif operation == "revoke":
                        if (set(form) != {"csrf", "operation"} or invitation.outcome is None
                                or invitation.outcome.lease is None or invitation.revoked):
                            raise web.HTTPConflict(text="No displayed access can be revoked.")
                        try:
                            result = await settled_thread(
                                self.control.manage_session, subject=invitation.subject,
                                arguments=["revoke", invitation.outcome.lease.lease_id], session_token=token,
                                invocation_id="human-revoke-" + hashlib.sha256(ticket.encode()).hexdigest(),
                            )
                            if inspect.isawaitable(result):
                                result = await result
                            invitation.revoked = True
                        except BaseException:
                            invitation.uncertain = True
                            raise
                    else:
                        raise web.HTTPBadRequest(text="Unknown human operation.")
                elif not token:
                    return self._login(invitation, csrf)
                await self._owner(invitation, token)
                view = await settled_thread(self._session, invitation)
                if view.request.revision != invitation.revision:
                    raise RevisionConflict("the displayed request changed")
                locale = invitation.locale
                body = '<p>' + escape(tr("safety.human_review", locale=locale)) + '</p>'
                body += '<dl><dt>' + escape(tr("safety.human_agent", locale=locale)) + '</dt><dd>' + escape(invitation.subject.agent_id) + '</dd>'
                body += '<dt>' + escape(tr("safety.task_id", locale=locale)) + '</dt><dd>' + escape(view.request.task_id) + '</dd>'
                body += '<dt>' + escape(tr("safety.request_id", locale=locale)) + '</dt><dd>' + escape(view.request.request_id) + '</dd>'
                expiry = datetime.fromtimestamp(view.request.expires_at, timezone.utc).isoformat()
                body += '<dt>' + escape(tr("safety.expires", locale=locale)) + '</dt><dd>' + escape(expiry) + '</dd>'
                body += '<dt>' + escape(tr("safety.agent_reason", locale=locale)) + '</dt><dd>' + escape(view.request.purpose) + '</dd></dl><ul>'
                for scope in view.request.scopes:
                    body += '<li>' + escape(scope.kind + ": " + scope.resource + " — " + ", ".join(scope.actions)) + '</li>'
                body += '</ul>'
                if invitation.uncertain:
                    body += '<p role="alert">' + escape(tr("safety.human_uncertain", locale=locale)) + '</p>'
                elif invitation.outcome is not None:
                    body += '<p role="status">' + escape(tr("safety.human_recorded", locale=locale)) + '</p>'
                    if invitation.outcome.lease is not None:

                        expiry = datetime.fromtimestamp(invitation.outcome.lease.expires_at, timezone.utc).isoformat()
                        body += '<p>' + escape(tr("safety.decision_approved", locale=locale, expiry=expiry)) + '</p>'
                        if not invitation.revoked:
                            body += self._button(invitation, csrf, "revoke", tr("safety.human_revoke", locale=locale))
                    if invitation.revoked:
                        body += '<p role="status">' + escape(tr("safety.human_revoked", locale=locale)) + '</p>'
                elif (view.request.state == "pending" and view.task_state in {"active", "paused"}
                      and view.binding_state == "active"):
                    actions = await settled_thread(
                        self.control.issue_session_actions, invitation.subject, invitation.request_id, invitation.revision,
                        session_token=token, presentation_id=hashlib.sha256((ticket + csrf + invitation.binding).encode()).hexdigest(),
                    )
                    invitation.actions[hashlib.sha256(csrf.encode()).hexdigest()] = {action.action_id: action for action in actions}
                    if actions:
                        seconds = actions[0].duration_seconds
                        divisor, unit = (3600, "hours") if seconds % 3600 == 0 else ((60, "minutes") if seconds % 60 == 0 else (1, "seconds"))
                        body += '<p>' + escape(tr("safety.maximum_" + unit, locale=locale, value=seconds // divisor)) + '</p>'
                    for action in actions:
                        label = tr("safety.choose_" + (action.lifetime if action.choice == "approve" else "deny"), locale=locale)
                        if action.choice == "approve" and action.lifetime == "ttl":
                            seconds = action.duration_seconds
                            divisor, unit = (3600, "hours") if seconds % 3600 == 0 else ((60, "minutes") if seconds % 60 == 0 else (1, "seconds"))
                            label = tr("safety.approve_" + unit, locale=locale, value=seconds // divisor)
                        body += self._button(invitation, csrf, "decide", label, action_id=action.action_id)
                else:
                    body += '<p>' + escape(tr("safety.human_changed", locale=locale)) + '</p>'
                body += self._button(invitation, csrf, "logout", tr("safety.human_logout", locale=locale))
                return self._response(invitation, body, csrf=csrf)
        except web.HTTPException:
            raise
        except (PermissionDenied, SessionDecisionDenied):
            if invitation.uncertain:
                return self._response(invitation, escape(tr("safety.human_uncertain", locale=invitation.locale)), status=503)
            response = self._login(invitation, csrf, status=401)
            response.del_cookie(_SESSION, path=invitation.path)
            return response
        except RevisionConflict:
            return self._response(invitation, escape(tr("safety.human_changed", locale=invitation.locale)), status=409)
        except PermissionRemoteError as error:
            if error.code in {"SessionDecisionDenied", "PermissionDenied"} and not invitation.uncertain:
                return self._login(invitation, csrf, status=401)
            message = "safety.human_uncertain" if invitation.uncertain else "safety.human_unavailable"
            return self._response(invitation, escape(tr(message, locale=invitation.locale)), status=503)
        except (OSError, ValueError, TypeError, KeyError):
            message = "safety.human_uncertain" if invitation.uncertain else "safety.human_unavailable"
            return self._response(invitation, escape(tr(message, locale=invitation.locale)), status=503)


def human_approval(api):
    raw = getattr(api.global_config, "permission_service", None)
    control = getattr(api, "permission_control", None)
    if control is None or not isinstance(raw, dict) or "approval_origin" not in raw:
        return None
    origin = approval_origin(raw["approval_origin"])
    key = (id(control), id(api.session_store), origin)
    current = getattr(api, "_human_approval", None)
    if current is None or current[0] != key:
        current = (key, HumanApproval(control, api.session_store, origin))
        api._human_approval = current
    return current[1]


def register_human_approval(api):
    async def handle(request):
        manager = human_approval(api)
        if manager is None:
            raise web.HTTPNotFound()
        return await manager.handle(request)
    api.app.router.add_get(_PATH + "/{ticket}", handle)
    api.app.router.add_post(_PATH + "/{ticket}", handle)
# HASHI-Fingerprint: hfp_4c4332fe860847f7a49ea65285107c19
