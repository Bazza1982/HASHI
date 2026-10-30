"""Bind an in-process permission executor to a current PAO Run attempt.

This adapter does not authenticate its constructor or provide an OS boundary.
Only a trusted PAO integration may construct it from canonical Run facts.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.models import ExecutionContext, RuntimeExecutionBinding
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

    @property
    def binding(self) -> RuntimeExecutionBinding:
        return RuntimeExecutionBinding(
            self._context, self._request_id, self._fencing_token, self._worker_id,
        )

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


class PaoTerminalRunFence:
    """Short canonical terminal barrier, including a stop-advanced PAO fence."""

    def __init__(self, store, binding):
        if not isinstance(store, SessionStore) or not isinstance(binding, RuntimeExecutionBinding):
            raise ValueError("canonical PAO terminal binding is required")
        self.store, self.binding = store, binding

    @contextmanager
    def __call__(self, context):
        import hashlib
        import json
        from dataclasses import asdict
        from orchestrator.session_store import TERMINAL_RUN_STATES

        binding = self.binding
        if context != binding.context:
            raise PermissionDenied("terminal binding changed")
        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT r.*,s.owner_id FROM runs r JOIN sessions s ON s.session_id=r.session_id WHERE r.run_id=?",
                (context.run_id,),
            ).fetchone()
            if (run is None or run["state"] not in TERMINAL_RUN_STATES
                    or run["owner_id"] != context.owner_id or run["agent_id"] != context.agent_id
                    or run["session_id"] != context.session_id or run["request_id"] != binding.request_id
                    or str(run["context_generation"]) != context.context_generation):
                raise PermissionDenied("the canonical Run is not terminal for this binding")
            route = connection.execute(
                "SELECT * FROM permission_run_routes WHERE run_id=?", (context.run_id,),
            ).fetchone()
            raw = json.dumps(asdict(binding), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if (route is None or route["task_id"] != context.task_id
                    or route["execution_binding_json"] != raw
                    or route["binding_digest"] != hashlib.sha256(raw.encode()).hexdigest()):
                raise PermissionDenied("a newer execution owns this terminal Run")
            active = connection.execute(
                "SELECT 1 FROM permission_run_routes p JOIN runs r ON r.run_id=p.run_id "
                "WHERE p.task_id=? AND r.state IN ('queued','running') "
                "UNION ALL SELECT 1 FROM runs child WHERE child.parent_run_id IN "
                "(SELECT run_id FROM permission_run_routes WHERE task_id=?) "
                "AND child.state IN ('queued','running') AND child.session_id=? "
                "AND child.agent_id=? AND child.context_generation=? LIMIT 1",
                (context.task_id, context.task_id, context.session_id, context.agent_id,
                 int(context.context_generation)),
            ).fetchone()
            yield {"run": dict(run), "task_has_active_work": active is not None}
