"""One trusted control owner, shared by L1 Functions and the L2 safety service."""
from __future__ import annotations

from orchestrator.permissions.decision_commands import PermissionDecisionController
from orchestrator.permissions.decision_gate import PaoDecisionAuthority
from orchestrator.permissions.enterprise_decisions import SessionDecisionGateway


class PermissionControlPlane:
    """Keep this object, identity store and signing key out of execution hosts."""

    def __init__(self, authority, run_store, identity_service, *, key_id, signing_key,
                 owner_bindings=None, settings=None):
        self._views = authority
        port = PaoDecisionAuthority(authority, run_store)
        gateway = SessionDecisionGateway(
            identity_service, port, key_id=key_id, signing_key=signing_key,
            owner_bindings=owner_bindings,
        )
        self._decisions = PermissionDecisionController(port, gateway, settings=settings)

    def get_session_request(self, subject, request_id):
        return self._views.get_session_request(subject, request_id)

    def list_session_requests(self, subject, *, before=None, limit=10):
        return self._views.list_session_requests(subject, before=before, limit=limit)

    def withdraw_session_request(self, subject, request_id, expected_revision):
        return self._views.withdraw_session_request(subject, request_id, expected_revision)

    def issue_session_actions(self, subject, request_id, revision, *, session_token, presentation_id):
        return self._decisions.issue_session_actions(
            subject, request_id, revision, session_token=session_token, presentation_id=presentation_id,
        )

    def submit_session_action(self, subject, action_id, *, session_token):
        return self._decisions.submit_session_action(subject, action_id, session_token=session_token)

    def submit_session_decision(self, subject, arguments, *, session_token,
                                invocation_id, connection_binding):
        return self._decisions.submit_session_decision(
            subject, arguments, session_token=session_token,
            invocation_id=invocation_id, connection_binding=connection_binding,
        )
