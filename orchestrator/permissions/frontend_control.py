"""Trusted shared FC control context, kept outside public envelopes and Workers."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from orchestrator.permissions.enterprise_decisions import SessionDecisionDenied
from orchestrator.permissions.models import PermissionSubject


@runtime_checkable
class PermissionViewPort(Protocol):
    def get_session_request(self, subject, request_id): ...
    def list_session_requests(self, subject, *, before=None, limit=10): ...
    def withdraw_session_request(self, subject, request_id, expected_revision): ...


@dataclass(frozen=True, slots=True)
class PermissionIngress:
    """Created only in the authenticated FC process; never serialize this."""
    subject: PermissionSubject
    controller: object = field(repr=False, compare=False)
    session_token: str = field(repr=False, compare=False)
    connection_binding: str

    def __post_init__(self):
        if (not isinstance(self.subject, PermissionSubject)
                or not callable(getattr(self.controller, "submit_session_decision", None))
                or not isinstance(self.session_token, str)
                or not isinstance(self.connection_binding, str) or not self.connection_binding):
            raise ValueError("original authenticated frontend context is required")

    def issue_actions(self, subject, request_id, revision, *, presentation_id):
        if subject != self.subject:
            raise SessionDecisionDenied("the command Session changed")
        return self.controller.issue_session_actions(
            subject, request_id, revision, session_token=self.session_token,
            presentation_id=presentation_id,
        )

    def submit_action(self, subject, action_id):
        if subject != self.subject:
            raise SessionDecisionDenied("the command Session changed")
        return self.controller.submit_session_action(
            subject, action_id, session_token=self.session_token,
        )

    def decide(self, subject, arguments, *, invocation_id):
        if subject != self.subject:
            raise SessionDecisionDenied("the command no longer targets the authenticated Session")
        return self.controller.submit_session_decision(
            subject, arguments, session_token=self.session_token,
            invocation_id=invocation_id, connection_binding=self.connection_binding,
        )


_CURRENT: ContextVar[PermissionIngress | None] = ContextVar("hashi_private_permission_ingress", default=None)


@contextmanager
def bind_permission_ingress(ingress: PermissionIngress | None):
    if ingress is not None and not isinstance(ingress, PermissionIngress):
        raise ValueError("a private frontend context is required")
    token = _CURRENT.set(ingress)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_permission_ingress() -> PermissionIngress:
    value = _CURRENT.get()
    if value is None:
        raise SessionDecisionDenied("use an authenticated permission command connection")
    return value


class PermissionCommandProjection:
    """FC-only command renderer, with no engine, generic RPC, or model tools."""

    def __init__(self, runtime, *, session_store, views):
        if not isinstance(views, PermissionViewPort):
            raise ValueError("the canonical permission view service is required")
        self.name, self.global_config = runtime.name, runtime.global_config
        self.session_store, self.permission_authority = session_store, views
        self.agent_config = getattr(runtime, "agent_config", None)
        self._policy = getattr(runtime, "_is_command_allowed", None)
        self._actor = getattr(runtime, "_is_authorized_user", None)
        self.backend_manager = getattr(runtime, "backend_manager", None)

    def _is_authorized_user(self, actor):
        return (type(actor) is int and actor == self.global_config.authorized_id
                and (not callable(self._actor) or bool(self._actor(actor))))

    def _is_command_allowed(self, name):
        return name == "safety" and (not callable(self._policy) or bool(self._policy(name)))

    async def _reply_text(self, update, text, **kwargs):
        query = getattr(update, "callback_query", None)
        if query is not None:
            return await query.edit_message_text(text, **kwargs)
        return await update.message.reply_text(text, **kwargs)
