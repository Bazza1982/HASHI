"""Final PAO Run fence at the permission service's decision endpoint.

Only verified receipts for issued actions enter this port. A replay of an
immutable committed decision can be read after a Run ends; a new grant cannot.
No external transport or human wait runs inside the canonical PAO transaction.
"""
from __future__ import annotations

from orchestrator.permissions.authority import InvalidDecision, PermissionAuthority
from orchestrator.permissions.models import PermissionSubject
from orchestrator.permissions.pao_run_fence import PaoRunFence
from orchestrator.session_store import SessionStore


DECISION_READ_METHODS = frozenset({
    "get_decision_action", "get_committed_decision_action", "get_committed_decision_result",
    "get_request_runtime_binding", "get_presented_decision_action", "get_session_request",
    "issue_decision_action", "get_session_decision_binding",
})


class PaoDecisionAuthority:
    def __init__(self, authority: PermissionAuthority, store: SessionStore) -> None:
        if not isinstance(authority, PermissionAuthority) or not isinstance(store, SessionStore):
            raise ValueError("the canonical permission and PAO stores are required")
        self._authority = authority
        self._store = store

    @property
    def max_lease_seconds(self) -> int:
        return self._authority.max_lease_seconds

    def __getattr__(self, name):
        if name not in DECISION_READ_METHODS:
            raise AttributeError(name)
        return getattr(self._authority, name)

    def decide(self, reference: str, *, approved_scopes, action_id: str):
        if not isinstance(action_id, str) or not action_id:
            raise InvalidDecision("a server-issued decision action is required")
        proof = self._authority.verified_decision_receipt(reference)
        subject = PermissionSubject(
            proof.instance_id, proof.owner_id, proof.agent_id, proof.session_id,
            proof.context_generation,
        )
        binding = self._authority.get_request_runtime_binding(subject, proof.request_id)
        context = binding.context
        try:
            self._authority.get_committed_decision_action(context, action_id)
        except InvalidDecision:
            pass
        else:
            # Committed decisions are immutable and never deleted. The
            # authority still checks the exact proof and action before replay.
            return self._authority.decide(reference, approved_scopes=approved_scopes, action_id=action_id)
        guard = PaoRunFence(
            self._store, context=context, request_id=binding.request_id,
            fencing_token=binding.fencing_token, worker_id=binding.worker_id,
        )
        with guard(context):
            return self._authority.decide(
                reference, approved_scopes=approved_scopes, action_id=action_id,
            )
