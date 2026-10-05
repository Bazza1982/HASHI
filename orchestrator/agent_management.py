"""Typed PAO owner for Frontend Connector Agent-management actions.

Frontend Connectors authenticate and translate their wire format into the
typed action below.  This manager coordinates the existing configuration,
creation, deletion, and Function Worker lifecycle owners; Connectors never
write ``agents.json`` or mutate a runtime projection themselves.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Mapping

from orchestrator.agent_creation import AgentCreationService, AgentCreationSpec
from orchestrator.agent_deletion import AgentDeletionService
from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError
from orchestrator.frontend_connector_registry import (
    get_compatibility_adapter,
    require_connector_operation,
)
from orchestrator.frontend_command_admission import (
    reserve_frontend_command_in_store,
)
from orchestrator.pathing import BridgePaths
from orchestrator.session_store import IdempotencyConflict, SessionConflict, SessionNotFound

logger = logging.getLogger("BridgeU.AgentManagement")

_ACTION_OPERATIONS = {
    "agent.create",
    "metadata.update",
    "deletion.preview",
    "deletion.commit",
    "deletion.status",
}
_CONTROL_OPERATIONS = {"lifecycle.set_active"}


class AgentManagementError(Exception):
    def __init__(self, message: str, *, error_code: str, status_code: int):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(frozen=True)
class AgentManagementAdmission:
    """Durable source Session and transport identity for one control."""

    session_id: str
    context_generation: int
    request_id: str
    client_id: str
    source_agent_id: str
    actor_id: str
    endpoint_id: str

    def __post_init__(self) -> None:
        for field in (
            "session_id",
            "request_id",
            "client_id",
            "source_agent_id",
            "actor_id",
            "endpoint_id",
        ):
            value = str(getattr(self, field) or "").strip()
            if not value:
                raise ValueError(f"Agent management {field} is required")
            object.__setattr__(self, field, value)
        if type(self.context_generation) is not int or self.context_generation < 1:
            raise ValueError("Agent management context generation is invalid")


@dataclass(frozen=True)
class AgentManagementAction:
    """One authenticated, Connector-neutral Agent management mutation."""

    kind: Literal["action", "control"]
    operation: str
    owner_id: str
    connector_id: str
    agent_id: str | None
    payload: Mapping[str, Any]
    admission: AgentManagementAdmission | None = None

    def __post_init__(self) -> None:
        expected = _ACTION_OPERATIONS if self.kind == "action" else _CONTROL_OPERATIONS
        if self.operation not in expected:
            raise ValueError("unsupported Agent management operation")
        if not str(self.owner_id or "").strip():
            raise ValueError("authenticated owner is required")
        connector = str(self.connector_id or "").strip().casefold()
        adapter = get_compatibility_adapter(
            f"{connector}.agent_management_{self.kind}"
        )
        if (
            adapter["connector_id"] != connector
            or adapter["direction"] != "ingress"
            or adapter["operation"] != self.kind
            or adapter["route"] != "standard_fc"
        ):
            raise ValueError("invalid Agent management compatibility adapter")
        require_connector_operation(connector, "ingress", self.kind)
        if self.operation != "deletion.status" and not str(self.agent_id or "").strip():
            raise ValueError("Agent ID is required")
        payload = dict(self.payload or {})
        if "owner_id" in payload or "actor_id" in payload:
            raise ValueError("Agent management identity cannot come from payload")
        if self.operation == "lifecycle.set_active":
            if set(payload) - {"is_active", "reason", "source"}:
                raise ValueError("unknown Agent lifecycle control field")
            if not isinstance(payload.get("is_active"), bool):
                raise ValueError("Agent lifecycle is_active must be boolean")
            if self.admission is None:
                raise ValueError("Agent lifecycle control admission is required")
        elif self.operation == "metadata.update":
            if set(payload) - {"display_name", "emoji"}:
                raise ValueError("unknown Agent metadata action field")
            display_name = payload.get("display_name")
            emoji = payload.get("emoji")
            if display_name is None and emoji is None:
                raise ValueError("Agent metadata action is empty")
            if display_name is not None and (
                not isinstance(display_name, str)
                or not display_name.strip()
                or len(display_name.strip()) > 160
            ):
                raise ValueError("Agent display name is invalid")
            if emoji is not None and (
                not isinstance(emoji, str)
                or not emoji.strip()
                or len(emoji.strip()) > 32
            ):
                raise ValueError("Agent emoji is invalid")
        elif self.operation == "agent.create":
            if set(payload) - {
                "display_name",
                "backend",
                "model",
                "provider",
                "effort",
                "is_active",
                "restricted",
            }:
                raise ValueError("unknown Agent creation action field")
            if not isinstance(payload.get("backend"), str) or not str(
                payload.get("backend")
            ).strip():
                raise ValueError("Agent creation backend is required")
            if not isinstance(payload.get("is_active", False), bool):
                raise ValueError("Agent creation is_active must be boolean")
            if not isinstance(payload.get("restricted", False), bool):
                raise ValueError("Agent creation restricted must be boolean")
            for field in ("display_name", "model", "provider", "effort"):
                value = payload.get(field)
                if value is not None and not isinstance(value, str):
                    raise ValueError(
                        f"Agent creation {field} must be a string or null"
                    )
        elif self.operation == "deletion.commit":
            if set(payload) - {
                "preview_token",
                "confirmed_agent_id",
                "idempotency_key",
            }:
                raise ValueError("unknown Agent deletion action field")
            if not str(payload.get("preview_token") or "").strip() or not str(
                payload.get("confirmed_agent_id") or ""
            ).strip():
                raise ValueError("Agent deletion confirmation is required")
        elif self.operation == "deletion.status":
            if set(payload) != {"operation_id"} or not str(
                payload.get("operation_id") or ""
            ).strip():
                raise ValueError("Agent deletion operation ID is required")
        object.__setattr__(self, "connector_id", connector)
        object.__setattr__(self, "owner_id", str(self.owner_id).strip())
        object.__setattr__(self, "agent_id", str(self.agent_id or "").strip() or None)
        object.__setattr__(self, "payload", payload)


class AgentManagementManager:
    """Coordinate typed FC management actions through existing PAO owners."""

    def __init__(self, kernel):
        self.kernel = kernel

    @classmethod
    def standalone(
        cls,
        paths: BridgePaths,
        *,
        global_config: Any,
        orchestrator: Any = None,
    ) -> "AgentManagementManager":
        kernel = orchestrator or SimpleNamespace()
        kernel.paths = paths
        kernel.global_cfg = global_config
        kernel.config_admin = getattr(kernel, "config_admin", None) or ConfigAdmin(paths)
        kernel.runtimes = getattr(kernel, "runtimes", [])
        kernel._startup_tasks = getattr(kernel, "_startup_tasks", {})
        return cls(kernel)

    @property
    def _admin(self) -> ConfigAdmin:
        return self.kernel.config_admin

    def _runtime_map(self) -> dict[str, Any]:
        resolver = getattr(self.kernel, "_runtime_map", None)
        if callable(resolver):
            runtimes = resolver()
        else:
            runtimes = {
                str(runtime.name).strip(): runtime
                for runtime in getattr(self.kernel, "runtimes", [])
            }
        return {
            str(name).strip().casefold(): runtime
            for name, runtime in dict(runtimes or {}).items()
        }

    def _deletion(self, session_store) -> AgentDeletionService:
        return AgentDeletionService(
            self.kernel.paths,
            orchestrator=self.kernel,
            session_store=session_store,
        )

    def _creation(self) -> AgentCreationService:
        return AgentCreationService(
            self.kernel.paths,
            global_config=getattr(self.kernel, "global_cfg", None),
            admin=self._admin,
        )

    def creation_catalogue(self) -> dict:
        return self._creation().creation_catalogue()

    def _agent_row(self, agent_id: str) -> dict[str, Any] | None:
        raw = self._admin.load_raw_config()
        return next(
            (
                dict(row)
                for row in raw.get("agents", [])
                if isinstance(row, dict) and row.get("name") == agent_id
            ),
            None,
        )

    @staticmethod
    def _target_generation(agent_id: str, row: Mapping[str, Any] | None) -> str:
        """Return the immutable PAO identity fence for a lifecycle target."""

        lifecycle_id = str((row or {}).get("agent_lifecycle_id") or "").strip()
        return lifecycle_id or f"legacy:{agent_id.casefold()}"

    async def dispatch(self, action: AgentManagementAction, *, session_store=None) -> dict:
        logger.info(
            "agent_management.accepted operation=%s agent=%s owner=%s connector=%s",
            action.operation,
            action.agent_id or "-",
            action.owner_id,
            action.connector_id,
        )
        if action.operation == "metadata.update":
            return self._update_metadata(action)
        if action.operation == "lifecycle.set_active":
            return await self._dispatch_lifecycle(action, session_store=session_store)
        if action.operation == "agent.create":
            return await self._create(action)
        if action.operation == "deletion.preview":
            preview = self._deletion(session_store).preview(action.agent_id or "")
            return {"preview": preview.to_dict(), "status": 200}
        if action.operation == "deletion.commit":
            result = self._deletion(session_store).delete(
                action.agent_id or "",
                preview_token=str(action.payload.get("preview_token") or ""),
                confirmed_agent_id=str(action.payload.get("confirmed_agent_id") or ""),
                idempotency_key=(
                    str(action.payload.get("idempotency_key") or "").strip() or None
                ),
            )
            return {"result": result, "status": 200}
        if action.operation == "deletion.status":
            operation_id = str(action.payload.get("operation_id") or "").strip()
            receipt = self._deletion(session_store).get_receipt(operation_id)
            if receipt is None:
                raise AgentManagementError(
                    "operation not found", error_code="not_found", status_code=404
                )
            return {"receipt": receipt, "status": 200}
        raise AssertionError(action.operation)

    async def _dispatch_lifecycle(self, action, *, session_store) -> dict:
        admission = action.admission
        if admission is None or session_store is None:
            raise AgentManagementError(
                "Agent lifecycle admission is unavailable",
                error_code="lifecycle_admission_unavailable",
                status_code=503,
            )
        actor_digest = "sha256:" + hashlib.sha256(
            admission.actor_id.encode("utf-8")
        ).hexdigest()
        target_id = action.agent_id or ""
        target_generation = self._target_generation(
            target_id, self._agent_row(target_id)
        )
        invocation = {
            "type": "hashi.frontend-command",
            "version": 2,
            "invocation_id": f"agentctl_{admission.request_id}",
            "request_id": admission.request_id,
            "connector_id": action.connector_id,
            "endpoint_id": admission.endpoint_id,
            "session_id": admission.session_id,
            "context_generation": admission.context_generation,
            "command": "agent-lifecycle",
            "issued_action_id": None,
            "revision": None,
            "arguments": [
                action.operation,
                target_id,
                target_generation,
                "active" if action.payload["is_active"] else "inactive",
            ],
            "actor_digest": actor_digest,
            "idempotency_digest": "sha256:"
            + hashlib.sha256(admission.request_id.encode("utf-8")).hexdigest(),
            "authorization": {"decision": "allowed", "scope": "agent-lifecycle"},
        }
        try:
            reservation = reserve_frontend_command_in_store(
                session_store,
                session_id=admission.session_id,
                owner_id=action.owner_id,
                client_id=admission.client_id,
                request_id=admission.request_id,
                context_generation=admission.context_generation,
                payload={
                    "operation": action.operation,
                    "source_agent_id": admission.source_agent_id,
                    "target_agent_id": target_id,
                    "target_generation": target_generation,
                    "is_active": action.payload["is_active"],
                    "reason": str(action.payload.get("reason") or ""),
                    "source": str(action.payload.get("source") or ""),
                },
                invocation=invocation,
            )
        except IdempotencyConflict as exc:
            raise AgentManagementError(
                str(exc), error_code="idempotency_conflict", status_code=409
            ) from exc
        except (SessionConflict, SessionNotFound) as exc:
            raise AgentManagementError(
                str(exc), error_code="lifecycle_scope_changed", status_code=409
            ) from exc
        if reservation.state == "completed" and reservation.response is not None:
            replay = dict(reservation.response)
            replay["replayed"] = True
            replay["event_id"] = reservation.event_id
            replay["operation_id"] = admission.request_id
            return replay
        if reservation.state == "pending":
            return {
                "ok": True,
                "state": "accepted",
                "status": 202,
                "replayed": True,
                "operation_id": admission.request_id,
                "message": "Agent lifecycle control is already pending.",
                "lifecycle": {"ok": True, "status": "starting"},
                "agent_row": self._agent_row(action.agent_id or "") or {},
            }
        current_row = self._agent_row(target_id)
        if current_row is None or self._target_generation(
            target_id, current_row
        ) != target_generation:
            outcome = {
                "ok": False,
                "state": "failed",
                "status": 409,
                "error": "Agent lifecycle target changed before control execution.",
                "error_code": "lifecycle_target_changed",
                "message": "Agent lifecycle target changed before control execution.",
                "replayed": False,
                "operation_id": admission.request_id,
            }
            try:
                completed = reservation.complete(outcome)
            except Exception as completion_exc:
                logger.exception(
                    "Agent lifecycle target-change result could not be committed target=%s",
                    target_id,
                )
                return {
                    "ok": False,
                    "state": "unknown",
                    "status": 503,
                    "error": "Agent lifecycle result persistence is unknown.",
                    "error_code": "lifecycle_result_unknown",
                    "message": (
                        f"{type(completion_exc).__name__}: result persistence is unknown"
                    ),
                    "replayed": False,
                    "operation_id": admission.request_id,
                }
            outcome["event_id"] = completed.get("event_id")
            return outcome
        try:
            outcome = await self._set_active(action)
        except AgentManagementError as exc:
            outcome = {
                "ok": False,
                "state": "failed",
                "status": exc.status_code,
                "error": str(exc),
                "error_code": exc.error_code,
                "message": str(exc),
                "replayed": False,
                "operation_id": admission.request_id,
            }
            try:
                completed = reservation.complete(outcome)
            except Exception as completion_exc:
                logger.exception(
                    "Agent lifecycle failure result could not be committed target=%s",
                    action.agent_id,
                )
                return {
                    "ok": False,
                    "state": "unknown",
                    "status": 503,
                    "error": "Agent lifecycle result persistence is unknown.",
                    "error_code": "lifecycle_result_unknown",
                    "message": f"{type(completion_exc).__name__}: result persistence is unknown",
                    "replayed": False,
                    "operation_id": admission.request_id,
                }
            outcome["event_id"] = completed.get("event_id")
            return outcome
        except Exception as exc:
            logger.exception(
                "Agent lifecycle control outcome is unknown target=%s", action.agent_id
            )
            return {
                "ok": False,
                "state": "unknown",
                "status": 503,
                "error": "Agent lifecycle outcome is unknown.",
                "error_code": "lifecycle_outcome_unknown",
                "message": f"{type(exc).__name__}: lifecycle outcome is unknown",
                "replayed": False,
                "operation_id": admission.request_id,
            }
        outcome = dict(outcome)
        lifecycle_status = str((outcome.get("lifecycle") or {}).get("status") or "")
        outcome.update(
            ok=True,
            state="starting" if lifecycle_status == "starting" else "succeeded",
            replayed=False,
            operation_id=admission.request_id,
            message=str((outcome.get("lifecycle") or {}).get("message") or ""),
        )
        try:
            completed = reservation.complete(outcome)
        except Exception as completion_exc:
            logger.exception(
                "Agent lifecycle success result could not be committed target=%s",
                action.agent_id,
            )
            return {
                "ok": False,
                "state": "unknown",
                "status": 503,
                "error": "Agent lifecycle result persistence is unknown.",
                "error_code": "lifecycle_result_unknown",
                "message": f"{type(completion_exc).__name__}: result persistence is unknown",
                "replayed": False,
                "operation_id": admission.request_id,
            }
        outcome["event_id"] = completed.get("event_id")
        return outcome

    def _update_metadata(self, action: AgentManagementAction) -> dict:
        display_name = action.payload.get("display_name")
        emoji = action.payload.get("emoji")
        try:
            row = self._admin.update_agent_metadata(
                action.agent_id or "",
                display_name=str(display_name) if display_name is not None else None,
                emoji=str(emoji) if emoji is not None else None,
            )
        except ConfigConflictError as exc:
            raise AgentManagementError(
                "Agent configuration changed before metadata publication.",
                error_code="config_conflict",
                status_code=409,
            ) from exc
        except ConfigDurabilityError as exc:
            raise AgentManagementError(
                "Agent metadata was committed but directory durability is unconfirmed.",
                error_code="config_durability_unconfirmed",
                status_code=503,
            ) from exc
        if row is None:
            raise AgentManagementError(
                "agent not found", error_code="agent_not_found", status_code=404
            )
        runtime = self._runtime_map().get((action.agent_id or "").casefold())
        if runtime is not None and getattr(runtime, "config", None) is not None:
            extra = dict(getattr(runtime.config, "extra", None) or {})
            if display_name is not None:
                extra["display_name"] = row["display_name"]
            if emoji is not None:
                extra["emoji"] = row["emoji"]
            runtime.config.extra = extra
        return {"agent_row": row, "status": 200}

    async def _set_active(self, action: AgentManagementAction) -> dict:
        name = action.agent_id or ""
        desired = bool(action.payload["is_active"])
        raw = self._admin.load_raw_config()
        row = next(
            (
                item
                for item in raw.get("agents", [])
                if isinstance(item, dict) and item.get("name") == name
            ),
            None,
        )
        if row is None:
            raise AgentManagementError(
                "agent not found", error_code="agent_not_found", status_code=404
            )
        previous = row.get("is_active", True) is not False
        runtime = self._runtime_map().get(name.casefold())
        lifecycle = {
            "ok": True,
            "status": "online" if runtime is not None else "inactive",
            "message": "No lifecycle change was needed.",
        }

        if desired:
            if not previous:
                if not self._admin.set_agent_active(name, True):
                    raise AgentManagementError(
                        "agent not found", error_code="agent_not_found", status_code=404
                    )
            if runtime is None:
                manager = getattr(self.kernel, "agent_lifecycle", None)
                request_start = getattr(manager, "request_start_agent", None)
                if callable(request_start):
                    lifecycle = await request_start(name)
                else:
                    start = getattr(self.kernel, "start_agent", None)
                    if not callable(start):
                        lifecycle = {
                            "ok": False,
                            "status": "unavailable",
                            "message": "Agent lifecycle service is unavailable.",
                        }
                    else:
                        ok, message = await start(name)
                        already_starting = (
                            not ok and "already" in str(message).casefold()
                        )
                        lifecycle = {
                            "ok": bool(ok or already_starting),
                            "status": "starting" if already_starting else "online" if ok else "failed",
                            "message": str(message),
                        }
                if not lifecycle.get("ok"):
                    if not previous:
                        self._admin.set_agent_active(name, False)
                    raise AgentManagementError(
                        str(lifecycle.get("message") or "Agent startup failed."),
                        error_code="agent_start_failed",
                        status_code=400,
                    )
        else:
            if name in getattr(self.kernel, "_startup_tasks", {}):
                raise AgentManagementError(
                    "Agent is still starting.",
                    error_code="agent_starting",
                    status_code=409,
                )
            if previous:
                active_count = sum(
                    1
                    for item in raw.get("agents", [])
                    if isinstance(item, dict)
                    and item.get("is_active", True) is not False
                )
                if active_count <= 1:
                    raise AgentManagementError(
                        "cannot deactivate the last active Agent; create or clone another Agent first",
                        error_code="last_active_agent",
                        status_code=409,
                    )
            if runtime is not None:
                stop = getattr(self.kernel, "stop_agent", None)
                if not callable(stop):
                    raise AgentManagementError(
                        "Agent lifecycle service is unavailable.",
                        error_code="lifecycle_unavailable",
                        status_code=503,
                    )
                reason = str(
                    action.payload.get("reason")
                    or action.payload.get("source")
                    or "frontend lifecycle control"
                )
                ok, message = await stop(name, reason=reason)
                lifecycle = {
                    "ok": bool(ok),
                    "status": "inactive" if ok else "failed",
                    "message": str(message),
                }
                if not ok and "not running" not in str(message).casefold():
                    raise AgentManagementError(
                        str(message), error_code="agent_stop_failed", status_code=400
                    )
            if previous and not self._admin.set_agent_active(name, False):
                raise AgentManagementError(
                    "Agent state changed before it could be disabled.",
                    error_code="config_conflict",
                    status_code=409,
                )
            lifecycle.update(ok=True, status="inactive")

        fresh = self._agent_row(name)
        if fresh is None:
            raise AgentManagementError(
                "agent not found", error_code="agent_not_found", status_code=404
            )
        pending = desired and self._runtime_map().get(name.casefold()) is None
        lifecycle["status"] = "starting" if pending else "online" if desired else "inactive"
        return {
            "agent_row": fresh,
            "lifecycle": lifecycle,
            "status": 202 if pending else 200,
        }

    async def _create(self, action: AgentManagementAction) -> dict:
        payload = action.payload
        spec = AgentCreationSpec(
            name=action.agent_id or "",
            backend=str(payload.get("backend") or ""),
            display_name=(
                str(payload["display_name"])
                if payload.get("display_name") is not None
                else None
            ),
            model=str(payload["model"]) if payload.get("model") is not None else None,
            provider=str(payload["provider"]) if payload.get("provider") is not None else None,
            effort=str(payload["effort"]) if payload.get("effort") is not None else None,
            is_active=bool(payload.get("is_active", False)),
            restricted=bool(payload.get("restricted", False)),
        )
        result = self._creation().create(spec)
        lifecycle = {
            "ok": True,
            "status": "inactive",
            "message": "Agent was created inactive; no start was requested.",
        }
        status = 201
        if result.is_active:
            manager = getattr(self.kernel, "agent_lifecycle", None)
            request_start = getattr(manager, "request_start_agent", None)
            if callable(request_start):
                lifecycle = await request_start(
                    result.name,
                    deactivate_on_failure_revision=result.config_revision,
                )
                if lifecycle.get("ok"):
                    status = 202
                else:
                    configuration_warning = None
                    try:
                        self._admin.set_agent_active(
                            result.name,
                            False,
                            expected_revision=result.config_revision,
                            allow_last_active_deactivation=True,
                        )
                    except ConfigConflictError:
                        configuration_warning = (
                            "Agent configuration changed while startup was failing"
                        )
                    except ConfigDurabilityError:
                        configuration_warning = (
                            "inactive state was committed but directory durability "
                            "could not be confirmed"
                        )
                    return {
                        "creation": result,
                        "agent_row": self._agent_row(result.name),
                        "lifecycle": lifecycle,
                        "status": 503,
                        "error": str(
                            lifecycle.get("message") or "Agent startup was rejected."
                        ),
                        "error_code": "agent_start_failed",
                        **(
                            {"configuration_warning": configuration_warning}
                            if configuration_warning
                            else {}
                        ),
                    }
            else:
                start = getattr(self.kernel, "start_agent", None)
                if callable(start):
                    ok, message = await start(result.name)
                    lifecycle = {
                        "ok": bool(ok),
                        "message": str(message),
                    }
                else:
                    lifecycle = {
                        "ok": False,
                        "message": "Agent lifecycle service is unavailable.",
                    }
                if not lifecycle["ok"]:
                    configuration_warning = None
                    try:
                        self._admin.set_agent_active(
                            result.name,
                            False,
                            expected_revision=result.config_revision,
                            allow_last_active_deactivation=True,
                        )
                    except ConfigConflictError:
                        configuration_warning = (
                            "Agent configuration changed while startup was failing"
                        )
                        logger.exception(
                            "Could not confirm inactive state after Agent creation failure: %s",
                            result.name,
                        )
                    except ConfigDurabilityError:
                        configuration_warning = (
                            "inactive state was committed but directory durability "
                            "could not be confirmed"
                        )
                    return {
                        "creation": result,
                        "agent_row": self._agent_row(result.name),
                        "lifecycle": lifecycle,
                        "status": 503,
                        "error": lifecycle["message"],
                        "error_code": "agent_start_failed",
                        **(
                            {"configuration_warning": configuration_warning}
                            if configuration_warning
                            else {}
                        ),
                    }
        return {
            "creation": result,
            "agent_row": self._agent_row(result.name),
            "lifecycle": lifecycle,
            "status": status,
        }


__all__ = [
    "AgentManagementAction",
    "AgentManagementAdmission",
    "AgentManagementError",
    "AgentManagementManager",
]
