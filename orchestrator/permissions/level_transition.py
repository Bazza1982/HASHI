"""Agent-only permission level migration through a private trusted FC owner.

The safety service never receives a generic Core client. FC quiesces and starts
only the exact authenticated Agent. No Core restart or broad reboot is exposed.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from orchestrator.config_admin import ConfigAdmin
from orchestrator.config_json import read_config_json, new_config_json, write_config_json
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.models import PermissionSettings, PermissionSubject
from orchestrator.permissions.ipc_rpc import PermissionPipeClient
from orchestrator.permissions.windows_security import require_private_path


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class FrontendLevelTransitions:
    """Constructed only by the shared FC, behind its Safety-SID-only endpoint."""

    METHODS = frozenset({"quiesce_agent", "adopt_agent"})

    def __init__(self, api, *, private_root, trusted_sid):
        self.api, self.root = api, Path(private_root)
        require_private_path(self.root, trusted_sids=(trusted_sid,))
        self.config, self._locks = ConfigAdmin(api.global_config), {}

    def _subject(self, subject):
        if not isinstance(subject, PermissionSubject) or subject.instance_id != self.api.session_store.instance_id:
            raise PermissionDenied("level transition instance changed")
        session = self.api.session_store.get_session(subject.session_id, owner_id=subject.owner_id)
        if (session["agent_id"] != subject.agent_id or str(session["context_generation"]) != subject.context_generation
                or session["status"] != "active"):
            raise PermissionDenied("level transition Session changed")

    def _record_path(self, subject, invocation_id):
        if not isinstance(invocation_id, str) or not 1 <= len(invocation_id) <= 256:
            raise PermissionDenied("a bound level-transition invocation is required")
        digest = sha256(_json([asdict(subject), invocation_id]).encode()).hexdigest()
        return self.root / ("level-" + digest + ".json")

    async def quiesce_agent(self, *, subject, invocation_id, expected_revision, requested_settings):
        self._subject(subject)
        requested = PermissionSettings.from_mapping(requested_settings)
        async with self._locks.setdefault(subject.agent_id, asyncio.Lock()):
            path = self._record_path(subject, invocation_id)
            meaning = sha256(_json([asdict(subject), expected_revision, asdict(requested)]).encode()).hexdigest()
            if path.exists():
                record = read_config_json(path)
                if record.get("meaning") != meaning:
                    raise PermissionDenied("transition invocation already has another meaning")
                if record["state"] in {"stopped", "adopted"}:
                    return {"ticket": record["ticket"], "state": record["state"]}
            else:
                snapshot = self.config.read_agent_permission_settings(subject.agent_id)
                if snapshot.revision != expected_revision:
                    raise PermissionDenied("level configuration revision changed")
                runtime = self.api._runtime_map().get(subject.agent_id)
                record = new_config_json(path, {
                    "meaning": meaning, "ticket": uuid4().hex, "state": "stopping",
                    "previous_revision": expected_revision, "requested": asdict(requested),
                    "old_pid": getattr(runtime, "worker_pid", None),
                })
                write_config_json(path, record)
            runtime = self.api._runtime_map().get(subject.agent_id)
            if runtime is not None:
                stopped = await self.api.orchestrator.stop_agent(
                    subject.agent_id, reason="permission-level-change",
                )
                if not isinstance(stopped, (tuple, list)) or stopped[0] is not True:
                    raise PermissionDenied("the exact Function Worker could not be quiesced")
            if self.api._runtime_map().get(subject.agent_id) is not None:
                raise PermissionDenied("the old Function route has not closed")
            record["state"] = "stopped"
            write_config_json(path, record)
            return {"ticket": record["ticket"], "state": "stopped"}

    async def adopt_agent(self, *, subject, invocation_id, ticket, revision, requested_settings):
        self._subject(subject)
        requested = PermissionSettings.from_mapping(requested_settings)
        async with self._locks.setdefault(subject.agent_id, asyncio.Lock()):
            path = self._record_path(subject, invocation_id)
            record = read_config_json(path)
            snapshot = self.config.read_agent_permission_settings(subject.agent_id)
            if (record.get("ticket") != ticket or record.get("state") not in {"stopped", "adopted"}
                    or record.get("requested") != asdict(requested)
                    or snapshot.revision != revision or snapshot.settings != requested):
                raise PermissionDenied("level adoption lost its exact configuration binding")
            runtime = self.api._runtime_map().get(subject.agent_id)
            if runtime is None:
                started = await self.api.orchestrator.start_agent(subject.agent_id)
                if not isinstance(started, (tuple, list)) or started[0] is not True:
                    return {"adopted": False, "state": "stopped"}
                runtime = self.api._runtime_map().get(subject.agent_id)
            # Core's normal start is the only process owner here. A new ready
            # Worker plus its actual loaded settings proves this configuration
            # was adopted; a changed file alone is never sufficient.
            cfg = getattr(runtime, "agent_config", None) or getattr(runtime, "config", None)
            actual = getattr(cfg, "permission_settings", None)
            if actual is None and isinstance(getattr(cfg, "extra", None), dict):
                actual = cfg.extra.get("permission_settings")
            if (runtime is None or getattr(runtime, "worker_pid", None) in {None, record.get("old_pid")}
                    or PermissionSettings.from_mapping(actual) != requested):
                return {"adopted": False, "state": "adoption_unverified"}
            record["state"], record["adopted_revision"] = "adopted", revision
            write_config_json(path, record)
            return {"adopted": True, "state": "adopted"}


class RemoteLevelTransition:
    def __init__(self, client, *, run_service, policies, generation, levels):
        if not isinstance(client, PermissionPipeClient):
            raise ValueError("a pinned private FC transition client is required")
        self.client, self.runs, self.policies = client, run_service, policies
        self.generation, self.levels = generation, tuple(levels)

    def review(self, subject, snapshot, requested):
        if requested.requested_level.value not in (*self.levels, "L0"):
            raise PermissionDenied("the requested level is not installed and qualified")
        preview = self.policies.preview(subject.agent_id, subject.owner_id)
        digest = sha256(_json({
            "subject": asdict(subject), "config_revision": snapshot.revision,
            "requested": asdict(requested), "preview": preview, "generation": self.generation,
        }).encode()).hexdigest()
        return {"kind": "level_review", "level": requested.requested_level.value,
                "revision": digest, "preview": preview}

    async def __call__(self, *, subject, previous, requested, commit, invocation_id):
        stopped = await self.client.call_async("quiesce_agent", {
            "subject": subject, "invocation_id": invocation_id, "expected_revision": previous.revision,
            "requested_settings": asdict(requested),
        })
        if not isinstance(stopped, dict) or stopped.get("state") not in {"stopped", "adopted"}:
            raise PermissionDenied("old Agent execution has not stopped")
        # A routing stop is not a lease or native resource stop. Independently
        # fence and retire every owned epoch before publishing the new level.
        for binding in self.runs.authority.agent_bindings(
            instance_id=subject.instance_id, owner_id=subject.owner_id, agent_id=subject.agent_id,
        ):
            context = binding.context
            self.runs.authority.fence_execution(context)
            self.runs.authority.revoke_session_access(
                PermissionSubject(context.instance_id, context.owner_id, context.agent_id,
                                  context.session_id, context.context_generation),
                context.task_id, whole_task=True,
            )
            await self.runs.close_execution(binding=binding)
        snapshot = commit()
        adopted = await self.client.call_async("adopt_agent", {
            "subject": subject, "invocation_id": invocation_id, "ticket": stopped["ticket"],
            "revision": snapshot.revision, "requested_settings": asdict(requested),
        })
        return {"kind": "level_changed", "level": requested.requested_level.value,
                "revision": snapshot.revision, "adopted": adopted.get("adopted") is True}
