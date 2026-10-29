"""Private governed-user session binding for one exact issued action.

This is not installed as an ingress or security service. A session credential
authenticates a user session; it does not itself prove a human gesture or that
the credential and signing key are isolated from the agent. The caller must
supply a trusted runtime context and protect this object, its key, and the
authenticated frontend path before it can be enabled.
"""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from collections.abc import Mapping
from types import MappingProxyType
from datetime import datetime, timezone
import json
import math
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator.permissions.authority import (
    DecisionAction,
    DecisionReceipt,
    InvalidDecision,
    PermissionLease,
)
from orchestrator.permissions.models import ExecutionContext
from orchestrator.permissions.signed_receipts import _DOMAIN, _KEY_ID


_SESSION_TOKEN = re.compile(r"hs_sess_[A-Za-z0-9_-]{32,128}\Z")


class SessionDecisionDenied(ValueError):
    """The current user session cannot submit this decision."""


@dataclass(frozen=True, slots=True)
class SessionDecisionOutcome:
    request_id: str
    choice: str
    lease: PermissionLease | None
    replayed: bool


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class SessionDecisionGateway:
    """Private in-process integration; never expose it to a managed agent."""

    def __init__(self, identity_service, authority, *, key_id: str, signing_key,
                 owner_bindings: Mapping[str, str] | None = None) -> None:
        if (not callable(getattr(identity_service, "hold_active_session", None))
                or not all(callable(getattr(authority, name, None)) for name in (
                    "get_committed_decision_action", "get_decision_action",
                    "get_committed_decision_result", "decide",
                ))
                or not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id)
                or not isinstance(signing_key, Ed25519PrivateKey)):
            raise ValueError("trusted identity, authority, and signing key are required")
        self._identity = identity_service
        self._authority = authority
        self._key_id = key_id
        self._key = signing_key
        if owner_bindings is not None and (
            not isinstance(owner_bindings, Mapping)
            or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v.strip()
                   for k, v in owner_bindings.items())
        ):
            raise ValueError("explicit user-to-owner bindings are required")
        self._owner_bindings = None if owner_bindings is None else MappingProxyType(dict(owner_bindings))

    def uses_authority(self, authority) -> bool:
        return self._authority is authority

    def _owner_for_user(self, user) -> str:
        user_id = getattr(user, "id", None)
        if getattr(user, "status", None) != "active" or not isinstance(user_id, str) or not user_id:
            raise SessionDecisionDenied("a current governed user session is required")
        if self._owner_bindings is None:
            return "enterprise:" + user_id
        owner = self._owner_bindings.get(user_id)
        if owner is None:
            raise SessionDecisionDenied("the user is not registered as a permission owner")
        return owner

    def owner_from_session(self, session_token: str) -> str:
        if not isinstance(session_token, str) or not _SESSION_TOKEN.fullmatch(session_token):
            raise SessionDecisionDenied("a current governed user session is required")
        lookup = getattr(self._identity, "get_session_user", None)
        if not callable(lookup):
            raise SessionDecisionDenied("the user authentication service is unavailable")
        return self._owner_for_user(lookup(session_token))

    def _reference(self, action: DecisionAction, context: ExecutionContext, expiry: float) -> str:
        receipt = DecisionReceipt(
            nonce=action.nonce, request_id=action.request_id, request_revision=action.request_revision,
            requested_digest=action.requested_digest, approved_digest=action.approved_digest,
            instance_id=context.instance_id, task_id=context.task_id, run_id=context.run_id,
            execution_epoch=context.execution_epoch, operation_id=action.operation_id,
            arguments_digest=action.arguments_digest, policy_revision=context.policy_revision,
            owner_id=context.owner_id, agent_id=context.agent_id, session_id=context.session_id,
            context_generation=context.context_generation, principal_id=context.owner_id,
            choice=action.choice, lifetime=action.lifetime, duration_seconds=action.duration_seconds,
            idempotency_key=action.action_id, expires_at=expiry,
        )
        payload = json.dumps(
            asdict(receipt), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        signature = self._key.sign(_DOMAIN + self._key_id.encode("ascii") + b"\0" + payload)
        return ".".join(("hpd1", self._key_id, _b64(payload), _b64(signature)))

    def decide(self, session_token: str, context: ExecutionContext, action_id: str) -> SessionDecisionOutcome:
        # The token must come from the original authenticated server request,
        # never from model arguments, request metadata, or a normal answer.
        if (not isinstance(session_token, str) or not _SESSION_TOKEN.fullmatch(session_token)
                or not isinstance(context, ExecutionContext) or not isinstance(action_id, str)
                or not action_id):
            raise SessionDecisionDenied("a current governed user session is required")
        # Identity guard precedes the permission ledger lock. Revocation and
        # disabling serialize on the identity database until the decision ends.
        with self._identity.hold_active_session(session_token) as authenticated:
            if authenticated is None:
                raise SessionDecisionDenied("a current governed user session is required")
            user, expires_iso = authenticated
            if context.owner_id != self._owner_for_user(user):
                raise SessionDecisionDenied("the session does not own this task")
            try:
                expiration = datetime.fromisoformat(expires_iso)
                if expiration.tzinfo is None:
                    raise ValueError("session expiry must be UTC-aware")
                expires_at = expiration.astimezone(timezone.utc).timestamp()
            except (ValueError, TypeError, OverflowError, OSError) as exc:
                raise SessionDecisionDenied("the session expiry is unavailable") from exc
            if not math.isfinite(expires_at):
                raise SessionDecisionDenied("the session expiry is unavailable")
            try:
                action = self._authority.get_committed_decision_action(context, action_id)
            except InvalidDecision:
                try:
                    action = self._authority.get_decision_action(context, action_id)
                except InvalidDecision as first_error:
                    # Another trusted ingress may commit after the first read.
                    try:
                        action = self._authority.get_committed_decision_action(context, action_id)
                        lease = self._authority.get_committed_decision_result(
                            context, action_id, principal_expires_at=expires_at,
                        )
                    except InvalidDecision:
                        raise first_error
                    return SessionDecisionOutcome(action.request_id, action.choice, lease, True)
            else:
                lease = self._authority.get_committed_decision_result(
                    context, action_id, principal_expires_at=expires_at,
                )
                return SessionDecisionOutcome(action.request_id, action.choice, lease, True)
            reference = self._reference(action, context, min(action.expires_at, expires_at))
            try:
                lease = self._authority.decide(
                    reference, approved_scopes=action.approved_scopes, action_id=action.action_id,
                )
            except InvalidDecision:
                # A simultaneous retry may have committed the same action with
                # a different session expiry. Read the durable outcome instead
                # of issuing another lease or retrying the side effect.
                try:
                    committed = self._authority.get_committed_decision_action(context, action_id)
                    lease = self._authority.get_committed_decision_result(
                        context, action_id, principal_expires_at=expires_at,
                    )
                except InvalidDecision:
                    raise
                return SessionDecisionOutcome(committed.request_id, committed.choice, lease, True)
            return SessionDecisionOutcome(action.request_id, action.choice, lease, False)


# Compatibility export; no separate state or decision implementation.
EnterpriseSessionDecisionPrototype = SessionDecisionGateway
