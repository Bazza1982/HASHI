"""Bind an in-process permission executor to a current PAO Run attempt.

This adapter does not authenticate its constructor or provide an OS boundary.
Only a trusted PAO integration may construct it from canonical Run facts.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.models import ExecutionContext
from orchestrator.session_store import SessionConflict, SessionStore


class PaoRunFence:
    """Hold the PAO Run fence while one short synchronous effect is dispatched."""

    def __init__(
        self, store: SessionStore, *, context: ExecutionContext, request_id: str,
        fencing_token: int, worker_id: str,
    ) -> None:
        if not isinstance(store, SessionStore) or not isinstance(context, ExecutionContext):
            raise ValueError("a trusted PAO store and execution context are required")
        if (store.instance_id != context.instance_id.upper()
                or not isinstance(request_id, str) or not request_id.strip()
                or not isinstance(worker_id, str) or not worker_id.strip()
                or type(fencing_token) is not int or fencing_token <= 0):
            raise ValueError("invalid PAO Run binding")
        try:
            generation = int(context.context_generation)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid PAO context generation") from exc
        if generation <= 0 or str(generation) != context.context_generation:
            raise ValueError("invalid PAO context generation")
        self._store = store
        self._context = context
        self._request_id = request_id
        self._fencing_token = fencing_token
        self._worker_id = worker_id
        self._generation = generation

    @contextmanager
    def __call__(self, context: ExecutionContext) -> Iterator[None]:
        if context != self._context:
            raise PermissionDenied("PAO Run binding changed")
        try:
            with self._store.hold_active_run(
                run_id=context.run_id, owner_id=context.owner_id, agent_id=context.agent_id,
                session_id=context.session_id, request_id=self._request_id,
                context_generation=self._generation, fencing_token=self._fencing_token,
                worker_id=self._worker_id,
            ):
                yield
        except SessionConflict as exc:
            raise PermissionDenied("PAO Run is no longer current") from exc
