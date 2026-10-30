"""Formal management intentions through the original authenticated FC session."""
from __future__ import annotations

from dataclasses import replace
import re

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.decision_commands import parse_decision_intent, _selected_scopes
from orchestrator.permissions.models import PermissionLevel, PermissionSubject


class PermissionManagement:
    def __init__(self, authority, identity, *, owner_bindings, config_admin, run_service, level_transition):
        self.authority, self.identity = authority, identity
        self.owners, self.config = dict(owner_bindings), config_admin
        self.runs, self.transition = run_service, level_transition

    def _authenticated(self, subject, authenticated):
        if (not isinstance(subject, PermissionSubject) or authenticated is None
                or self.owners.get(authenticated[0].id) != subject.owner_id):
            raise PermissionDenied("an active owner session is required")
        session = self.runs.store.get_session(subject.session_id, owner_id=subject.owner_id)
        if (session["agent_id"] != subject.agent_id or session["status"] != "active"
                or str(session["context_generation"]) != subject.context_generation
                or self.runs.store.instance_id != subject.instance_id):
            raise PermissionDenied("the management Session changed")

    async def manage(self, subject, arguments, *, session_token, invocation_id):
        if (not isinstance(arguments, (list, tuple)) or not arguments or len(arguments) > 64
                or any(not isinstance(item, str) or not 1 <= len(item) <= 4096 for item in arguments)
                or not isinstance(invocation_id, str) or not 1 <= len(invocation_id) <= 256):
            raise ValueError("an exact formal management command is required")
        verb = arguments[0]
        with self.identity.hold_active_session(session_token) as authenticated:
            self._authenticated(subject, authenticated)
            if verb == "narrow":
                if len(arguments) < 6 or any(word in {"--for"} for word in arguments):
                    raise ValueError("select narrower resources/actions and an exact request revision")
                intent = parse_decision_intent(["approve", arguments[1], "task", *arguments[2:]])
                binding = self.authority.get_request_runtime_binding(subject, intent.request_id)
                request = self.authority.get_session_request(subject, intent.request_id).request
                narrowed = _selected_scopes(intent, request.scopes)
                result = self.authority.narrow_request(
                    binding.context, request.request_id, intent.revision, narrowed,
                )
                return {"kind": "narrowed", "request": self.authority.get_session_request(subject, result.request_id)}
            if verb in {"revoke", "revoke-task"} and len(arguments) == 2:
                result = self.authority.revoke_session_access(
                    subject, arguments[1], whole_task=verb == "revoke-task",
                )
            elif verb == "set-level" and len(arguments) in {2, 4}:
                try:
                    level = PermissionLevel(arguments[1])
                except ValueError as error:
                    raise ValueError("select L0, L1 or L2") from error
                snapshot = self.config.read_agent_permission_settings(subject.agent_id)
                if not callable(self.transition) or not callable(getattr(self.transition, "review", None)):
                    raise PermissionDenied("verified Agent transition owner is unavailable")
                new_settings = replace(snapshot.settings, requested_level=level)
                review = self.transition.review(subject, snapshot, new_settings)
                if len(arguments) == 2:
                    return review
                if (arguments[2] != "--revision" or re.fullmatch(r"[0-9a-f]{64}", arguments[3]) is None
                        or arguments[3] != review["revision"]):
                    raise PermissionDenied("the reviewed policy or configuration changed")
                result = None
            else:
                raise ValueError("unknown or incomplete permission management command")
        if verb in {"revoke", "revoke-task"}:
            # The ledger was fenced under the session guard. Cleanup is an
            # independent observation and cannot be fabricated by that commit.
            for binding in tuple(self.runs._active.values()):
                if binding.context.task_id == result["task_id"]:
                    executor = self.runs.execution_registry(binding)
                    stopper = getattr(executor, "stop_revoked_resources", None)
                    if callable(stopper):
                        await stopper()
            current = self.authority.session_summary(subject)
            task = next(row for row in current["tasks"] if row["task_id"] == result["task_id"])
            result.update({"kind": "revoked", "task": task})
            return result
        # The operational transition owns routing fences and verified resource
        # retirement. It receives a short authenticated CAS writer, never an
        # impersonation credential, and must not restart Core.
        def commit():
            with self.identity.hold_active_session(session_token) as authenticated:
                self._authenticated(subject, authenticated)
                return self.config.write_agent_permission_settings(
                    subject.agent_id, new_settings, expected_revision=snapshot.revision,
                )
        return await self.transition(
            subject=subject, previous=snapshot, requested=new_settings, commit=commit,
            invocation_id=invocation_id,
        )
