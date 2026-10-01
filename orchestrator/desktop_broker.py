"""Manual desktop extension of the existing PAO capability/lease owner."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, replace
from uuid import uuid4

from orchestrator.desktop_contract import ACTIONS, CONTROL_TTL, PIN_FIELDS, DesktopError


@dataclass(frozen=True)
class ManualDesktopLease:
    lease_id: str
    capability_id: str
    capability_kind: str
    instance_id: str
    device_id: str
    user_session_id: str
    actor_id: str
    session_id: str
    issued_at: float
    expires_at: float
    actor_type: str = "user"

    # Empty agent identity is deliberate: no fabricated Agent or accepted Run.
    agent_id: str = ""
    window_id: str | None = None

    @property
    def task_id(self): return self.session_id

    @property
    def resource_key(self):
        return self.instance_id, self.device_id, self.user_session_id, None

    def to_dict(self): return {**asdict(self), "task_id": self.session_id}


class ManualDesktopBroker:
    """Uses CapabilityBroker's existing records, transport and resource leases."""

    def desktop_targets(self):
        status = self.status()
        return [{k: item[k] for k in PIN_FIELDS} for item in status["capabilities"]
                if item["capability_kind"] == "computer_control" and item["platform"].lower() == "windows"
                and ACTIONS.issubset(item["supported_actions"])]

    def _desktop_record(self, pin):
        registration, token = self._record(pin.get("capability_id", ""))
        actual = registration.to_dict()
        if any(pin.get(k) != actual.get(k) for k in PIN_FIELDS) or registration.capability_kind != "computer_control" or not ACTIONS.issubset(registration.supported_actions):
            raise DesktopError("desktop_target_changed", 409)
        return registration, token

    def _desktop_lease(self, pin, actor_id, session_id, lease_id):
        self._prune()
        lease = self._leases.get(lease_id)
        if not isinstance(lease, ManualDesktopLease) or lease.actor_id != actor_id or lease.session_id != session_id or lease.capability_id != pin["capability_id"]:
            raise DesktopError("desktop_control_expired", 409)
        return lease

    async def invoke_desktop(self, pin, *, actor_id, session_id, operation, args):
        if operation not in ACTIONS or not actor_id or not session_id:
            raise DesktopError("desktop_invalid_operation")
        with self._lock:
            registration, token = self._desktop_record(pin)
            lease = None
            arguments = dict(args)
            if operation == "desktop_control":
                mode = args.get("mode")
                if mode == "acquire":
                    self._prune()
                    resource = (registration.instance_id, registration.device_id, registration.user_session_id, None)
                    current_id = self._resource_leases.get(resource)
                    if current_id:
                        raise DesktopError("desktop_control_busy", 409)
                    lease = ManualDesktopLease("manual-"+uuid4().hex, registration.capability_id, registration.capability_kind,
                                               registration.instance_id, registration.device_id, registration.user_session_id,
                                               actor_id, session_id, time.time(), time.time()+CONTROL_TTL)
                    self._leases[lease.lease_id] = lease
                    self._resource_leases[resource] = lease.lease_id
                elif mode in {"heartbeat", "release"}:
                    lease = self._desktop_lease(pin, actor_id, session_id, args.get("lease_id", ""))
                    if mode == "heartbeat":
                        lease = replace(lease, expires_at=time.time()+CONTROL_TTL)
                        self._leases[lease.lease_id] = lease
                else:
                    raise DesktopError("desktop_invalid_control")
                arguments = {"mode": mode, "lease_id": lease.lease_id, "ttl": CONTROL_TTL}
            elif operation == "desktop_input":
                lease = self._desktop_lease(pin, actor_id, session_id, args.get("lease_id", ""))
        payload = {"protocol_version": 1, "request_id": "desktop-"+uuid4().hex,
                   "identity": {k: pin[k] for k in PIN_FIELDS if k != "worker_generation"},
                   "worker_generation": pin["worker_generation"],
                   "actor": {"type": "user", "id": actor_id}, "desktop_session_id": session_id,
                   "action": operation, "args": arguments}
        failed = False
        try:
            response = await self._transport(registration, token, payload, 5.0)
            self._verify_identity(registration, response.get("identity"))
            if response.get("worker_generation") != pin["worker_generation"]:
                raise DesktopError("desktop_target_changed", 409)
            if not response.get("ok"):
                raise DesktopError(response.get("error_code", "desktop_worker_failed"), int(response.get("status", 503)))
            result = response["result"]
            if operation not in {"desktop_frame", "desktop_info", "desktop_input"}:
                self._audit("desktop_operation", actor_type="user", actor_id=actor_id, session_id=session_id,
                            capability_id=pin["capability_id"], operation=operation)
            return result
        except DesktopError:
            failed = True
            raise
        except Exception as exc:
            failed = True
            raise DesktopError("desktop_outcome_unknown", 503) from exc
        finally:
            if lease and (failed or (operation == "desktop_control" and arguments["mode"] == "release")):
                self.release_lease(lease.lease_id, reason="desktop-released")
            if operation == "desktop_close":
                with self._lock:
                    matches = [l.lease_id for l in self._leases.values() if isinstance(l, ManualDesktopLease)
                               and l.actor_id == actor_id and l.session_id == session_id]
                for lid in matches: self.release_lease(lid, reason="desktop-closed")
