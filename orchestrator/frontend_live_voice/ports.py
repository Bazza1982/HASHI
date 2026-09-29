"""NEW integration contracts, not claims about existing SessionStore methods.

Implement in Functions against the current canonical owners. No in-memory
production substitute, browser transcript archive, direct shell execution or
HTTP loopback-to-self is permitted. See integration/HASHI_PORTS.md.
"""
from __future__ import annotations
from collections.abc import Mapping
from typing import Any, Protocol
from .protocol import CallBinding, Fragment
from .delegation import Proposal

class DurableVoicePort(Protocol):
    async def append_fragment_once(self, binding: CallBinding, fragment: Fragment) -> Mapping[str, Any]:
        """In one SessionStore transaction deduplicate, append canonical event and assign sequence."""
        ...

    async def register_delegation_once(self, binding: CallBinding, delegation_id: str, offset_ms: int) -> bool:
        """Persist unique delegation and proposal-work outbox atomically. Return newly created."""
        ...

    async def schedule_proposal(self, binding: CallBinding, delegation_id: str, offset_ms: int) -> None:
        """Durable bounded work owned by Function lifetime, with late-fragment grace. No untracked task."""
        ...

    async def read_proposal(self, binding: CallBinding, delegation_id: str) -> Proposal:
        """Return immutable source proposal; new-admission expiry is checked transactionally."""
        ...

class AdmissionPort(Protocol):
    async def find_admission(self, binding: CallBinding, *, idempotency_key: str,
                            request_digest: str) -> Mapping[str, Any] | None:
        """Return matching prior receipt; conflicting key/digest raises 409, even after expiry."""
        ...

    async def admit_delegation(self, binding: CallBinding, proposal: Proposal, *,
                               idempotency_key: str, request_digest: str) -> Mapping[str, Any]:
        """Revalidate current authority/scope/expiry and atomically admit once.

        Client delegation uses canonical PAO Session/Message/Run and the Agent's
        normal permission, delivery-freezing and dispatch/outbox behaviour.
        Replays return the prior receipt even if response was lost; never submit
        a second Run on a timeout.
        """
        ...

class LiveApplicationPort(Protocol):
    """Methods consumed by routes.py. Implement all before qualification."""
    available: bool
    async def invoke(self, operation: str, authority: Any, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """Dispatch only routes.py's declared operations; inspect existing authenticated owner.

        Implement: context, start, cancel_start, attempt, snapshot, events and
        control. start/cancel_start share a durable attempt ledger;
        every call-bound operation checks stored CallBinding. Response projection
        contains no secret/internal provider errors. No client-supplied principal.
        """
        ...
