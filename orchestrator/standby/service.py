"""Standby control plane: no implicit capture, inference or Agent side effects."""

from __future__ import annotations

import asyncio
import time

from .contract import LIVE_OPERATIONS, StandbyError, binding, identifier, keys

# This is a qualification boundary, not a model/provider catalogue. A future
# stage must implement and qualify these contracts before making them available.
QUALIFICATION_BLOCKERS = (
    "standby_media_unqualified", "standby_judge_unqualified",
    "standby_engine_retention_unqualified", "standby_content_disposal_unqualified",
)


def capabilities():
    return {
        "version": 1, "control_available": True, "enabled": False,
        "media_available": False, "judge_available": False, "armed_available": False,
        "recording_available": False, "capture_started": False,
        "blockers": list(QUALIFICATION_BLOCKERS),
        "templates": {name: {"available": False, "reason": "standby_capability_unqualified"}
                      for name in ("quiet", "meeting", "paper_slip", "timer", "posture", "baby", "visitor")},
        "defaults": {"mode": "SHADOW", "automatic_memory": False,
                     "persistent_environment_content": False},
    }


class StandbyService:
    def __init__(self, store, profiles, ports, *, now=time.time):
        self.store, self.profiles, self.ports, self.now = store, profiles, ports, now

    async def invoke(self, owner, body):
        if not owner or not isinstance(body, dict):
            raise StandbyError("standby_request_invalid")
        operation = body.get("operation")
        if not isinstance(operation, str):
            raise StandbyError("standby_request_invalid")
        if operation == "capabilities":
            keys(body, required=("operation",))
            return {"ok": True, **capabilities()}
        # Nothing claiming to capture/route content is enabled by control API
        # existence, a profile flag, a model label or a caller-supplied capability.
        if operation in LIVE_OPERATIONS:
            raise StandbyError("standby_capability_unqualified", 501)
        scope = binding(body.get("binding"))
        if operation in {"pause", "end", "delete_data"}:
            session = await asyncio.to_thread(
                self.store.standby_control, owner_id=owner, body=body, now=self.now())
            return {"ok": True, "session": session}
        if operation == "operation_status":
            keys(body, required=("operation", "binding", "operation_id"))
            result = await asyncio.to_thread(self.store.standby_operation_status,
                owner_id=owner, agent_id=scope["agent_id"], operation_id=body["operation_id"])
            # Receipts cannot be queried through a different Conversation.
            saved = result.get("session")
            if saved and any(saved[key] != value for key, value in scope.items()):
                raise StandbyError("standby_scope_changed", 404)
            return {"ok": True, **result}
        if operation == "status":
            keys(body, required=("operation", "binding", "standby_id"))
            session = await asyncio.to_thread(self.store.standby_snapshot,
                owner_id=owner, binding=scope, standby_id=body["standby_id"])
            return {"ok": True, "session": session}
        if operation == "recent":
            keys(body, required=("operation", "binding"))
            rows = await asyncio.to_thread(self.store.standby_recent,
                owner_id=owner, binding=scope, now=self.now())
            return {"ok": True, "sessions": rows}
        if operation not in {"profiles", "save_profile", "preflight", "configure"}:
            raise StandbyError("standby_operation_unsupported")
        self.ports.validate(owner, scope)
        if operation == "profiles":
            keys(body, required=("operation", "binding"))
            return {"ok": True, **await asyncio.to_thread(self.profiles.list, owner, scope["agent_id"])}
        if operation == "save_profile":
            keys(body, required=("operation", "binding", "profile_id", "profile", "expected_revision"))
            result = await asyncio.to_thread(self.profiles.save, owner, scope["agent_id"],
                body["profile_id"], body["profile"], expected_revision=body["expected_revision"])
            return {"ok": True, **result}
        if operation == "preflight":
            keys(body, required=("operation", "binding", "profile_id"))
            profile = await asyncio.to_thread(self.profiles.get, owner, scope["agent_id"], body["profile_id"])
            privacy = self.ports.privacy_level(scope["agent_id"])
            blockers = list(QUALIFICATION_BLOCKERS)
            if type(privacy) is not int or privacy not in (0, 1, 2):
                blockers.append("standby_privacy_unavailable")
            return {"ok": True, "ready": False, "capture_started": False,
                    "privacy_level": privacy, "blockers": blockers, **profile,
                    "data_flow": {"device_capture": "disabled", "host_processing": "disabled",
                                  "external_processing": "disabled", "delivery": "disabled"}}
        # Configure saves only an immutable Profile reference. It cannot acquire
        # media or turn SHADOW/ARMED on. Unknown mutations are queried by their ID.
        if operation == "configure":
            saved = await asyncio.to_thread(self.profiles.get, owner, scope["agent_id"], body.get("profile_id"))
            if saved["revision"] != body.get("profile_revision"):
                raise StandbyError("standby_revision_conflict", 409)
            session = await asyncio.to_thread(self.store.standby_control,
                owner_id=owner, body=body, now=self.now())
            return {"ok": True, "session": session}
