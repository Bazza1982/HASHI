"""Operational host for one isolated HASHI Agent Function Worker.

This module is imported only inside a spawned worker process.  The stable Core
communicates with it through ``function_worker_protocol`` and never imports an
Agent runtime class into the live Core module space.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orchestrator.kernel_artifact import GenerationModuleFinder
from orchestrator.function_generation import (
    FUNCTION_GENERATION_SCHEMA_VERSION,
    SourceManifest,
    candidate_import_guard,
    verify_qualified_manifest_bytes,
)
from orchestrator.function_worker_protocol import (
    FUNCTION_WORKER_PROTOCOL_VERSION,
    JsonConnectionPeer,
    json_value,
)
from orchestrator.function_contract import validate_function_contract
from orchestrator.pathing import build_bridge_paths
from orchestrator.runtime_contract import (
    RuntimeFingerprint,
    compare_runtime_fingerprints,
    enforce_runtime_contract,
)
from orchestrator.service_endpoints import endpoint_from_snapshot

logger = logging.getLogger("BridgeU.FunctionWorker")

WORKER_PREPARE_TIMEOUT_SECONDS = 180.0
WORKER_DRAIN_TIMEOUT_SECONDS = 120.0
LOCAL_MODE_TOKEN = "WORKBENCH_ONLY_NO_TOKEN"


class FunctionWorkerStateError(RuntimeError):
    """An operation is invalid for the worker's current lifecycle phase."""


class _GenerationModuleFinder(GenerationModuleFinder):
    def __init__(self, *, generation_root, code_root, manifest):
        super().__init__(generation_root=generation_root, code_root=code_root,
                         manifest=manifest.to_dict())


class _WhatsAppMarker:
    def __init__(self, connected: bool) -> None:
        self._client = object() if connected else None


class WorkerSchedulerFacade:
    """Cached reads plus Core RPC for scheduler state owned outside the Worker."""

    def __init__(self, peer: JsonConnectionPeer, agent_name: str) -> None:
        self.peer = peer
        self.agent_name = agent_name
        self.snapshot: dict[str, Any] = {}

    def update(self, value: Mapping[str, Any] | None) -> None:
        self.snapshot = dict(value or {})

    def count_delayed_messages(self, agent_name: str) -> int:
        records = self.list_delayed_messages_now(agent_name)
        return len(records)

    def list_delayed_messages_now(self, agent_name: str) -> list[dict[str, Any]]:
        if str(agent_name) != self.agent_name:
            return []
        return [dict(item) for item in self.snapshot.get("delayed_messages", ())]

    async def list_delayed_messages(self, agent_name: str) -> list[dict[str, Any]]:
        result = await self.peer.request(
            "core.scheduler.list_delayed_messages",
            {"agent_name": str(agent_name)},
        )
        return [dict(item) for item in result or ()]

    async def schedule_delayed_message(
        self,
        *,
        agent_name: str,
        chat_id: int,
        prompt: str,
        delay_minutes: int,
        idempotency_key: str | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        deliver_to_telegram: bool = True,
    ) -> dict[str, Any]:
        result = await self.peer.request(
            "core.scheduler.schedule_delayed_message",
            {
                "agent_name": agent_name,
                "chat_id": chat_id,
                "prompt": prompt,
                "delay_minutes": delay_minutes,
                "idempotency_key": idempotency_key,
                "request_metadata": (
                    dict(request_metadata) if request_metadata is not None else None
                ),
                "deliver_to_telegram": deliver_to_telegram,
            },
        )
        return dict(result or {})

    async def cancel_delayed_messages(
        self,
        agent_name: str,
        *,
        delay_ids: set[str] | list[str] | tuple[str, ...],
    ) -> list[dict[str, Any]]:
        result = await self.peer.request(
            "core.scheduler.cancel_delayed_messages",
            {
                "agent_name": str(agent_name),
                "delay_ids": sorted(str(item) for item in delay_ids),
            },
        )
        return [dict(item) for item in result or ()]

    def build_recovery_context(self, agent_name: str) -> str:
        if str(agent_name) != self.agent_name:
            return ""
        return str(self.snapshot.get("recovery_context") or "")

    async def handle_recovery_reply(
        self,
        *,
        agent_name: str,
        text: str,
        runtime_map: Mapping[str, Any] | None = None,
    ) -> str | None:
        del runtime_map
        result = await self.peer.request(
            "core.scheduler.handle_recovery_reply",
            {"agent_name": str(agent_name), "text": str(text)},
        )
        return None if result is None else str(result)


class WorkerServiceManagerFacade:
    """Worker control surface for shared Functions; legacy wire names stay stable."""

    def __init__(self, peer: JsonConnectionPeer) -> None:
        self.peer = peer
        self._api_gateway: dict[str, Any] = {
            "enabled": False,
            "running": False,
            "default_model": "",
            "available_models": [],
            "base_url": None,
            "port": None,
        }

    def update(self, value: Mapping[str, Any] | None) -> None:
        self._api_gateway = dict((value or {}).get("api_gateway") or {})

    def api_gateway_state_snapshot(self) -> dict[str, Any]:
        return dict(self._api_gateway)

    def api_gateway_status(self) -> dict[str, Any]:
        snapshot = self.api_gateway_state_snapshot()
        return {
            **snapshot,
            "enabled_flag": bool(snapshot.get("enabled")),
            "bind_host": (
                str(snapshot.get("base_url") or "")
                .removeprefix("http://")
                .split(":", 1)[0]
                or None
            ),
        }

    async def start_api_gateway_runtime(self) -> tuple[bool, str]:
        result = await self.peer.request("core.service.api_gateway.start", {})
        self.update({"api_gateway": result.get("snapshot") or {}})
        return bool(result["ok"]), str(result["message"])

    async def stop_api_gateway_runtime(self) -> tuple[bool, str]:
        result = await self.peer.request("core.service.api_gateway.stop", {})
        self.update({"api_gateway": result.get("snapshot") or {}})
        return bool(result["ok"]), str(result["message"])

    async def set_api_gateway_enabled(self, enabled: bool) -> tuple[bool, str]:
        if enabled:
            return await self.start_api_gateway_runtime()
        return await self.stop_api_gateway_runtime()

    async def set_api_gateway_default_model(self, model: str) -> tuple[bool, str]:
        result = await self.peer.request(
            "core.service.api_gateway.model",
            {"model": str(model)},
        )
        self.update({"api_gateway": result.get("snapshot") or {}})
        return bool(result["ok"]), str(result["message"])


class WorkerBackgroundJobManagerFacade:
    """Async JSON facade for the process-owned BackgroundJobManager."""

    def __init__(self, peer: JsonConnectionPeer) -> None:
        self.peer = peer

    @staticmethod
    def _record(value: Mapping[str, Any] | None):
        if value is None:
            return None
        from orchestrator.background_jobs import BackgroundJobRecord

        return BackgroundJobRecord(**dict(value))

    async def start_job(self, **kwargs: Any):
        result = await self.peer.request("core.background_jobs.start", kwargs)
        return self._record(result)

    async def get(self, job_id: str):
        result = await self.peer.request(
            "core.background_jobs.get",
            {"job_id": str(job_id)},
        )
        return self._record(result)

    async def list(
        self,
        *,
        agent: str | None = None,
        states: set[str] | None = None,
        limit: int = 50,
    ) -> list[Any]:
        result = await self.peer.request(
            "core.background_jobs.list",
            {
                "agent": agent,
                "states": sorted(states) if states else None,
                "limit": int(limit),
            },
        )
        return [self._record(item) for item in result or ()]

    async def tail(
        self,
        job_id: str,
        *,
        stream: str = "stdout",
        lines: int = 80,
    ) -> str:
        return str(
            await self.peer.request(
                "core.background_jobs.tail",
                {
                    "job_id": str(job_id),
                    "stream": str(stream),
                    "lines": int(lines),
                },
            )
        )

    async def cancel(
        self,
        job_id: str,
        *,
        grace_seconds: float = 2.0,
    ):
        result = await self.peer.request(
            "core.background_jobs.cancel",
            {
                "job_id": str(job_id),
                "grace_seconds": float(grace_seconds),
            },
        )
        return self._record(result)


class RemoteRuntimeReference:
    """Worker-side view of another Agent routed by the stable Core."""

    def __init__(
        self,
        peer: JsonConnectionPeer,
        metadata: Mapping[str, Any],
    ) -> None:
        self.peer = peer
        self.update(metadata)

    def update(self, metadata: Mapping[str, Any]) -> None:
        self.metadata = dict(metadata)
        self.name = str(self.metadata.get("name") or self.metadata.get("id") or "")
        self.startup_success = bool(self.metadata.get("startup_success", True))
        self.backend_ready = bool(self.metadata.get("online", True))
        self.telegram_connected = bool(self.metadata.get("telegram_connected", False))
        self.workspace_dir = Path(str(self.metadata.get("workspace_dir") or "."))
        self.session_id_dt = str(self.metadata.get("session_id") or "")
        active_backend = str(self.metadata.get("active_backend") or "unknown")
        self.config = SimpleNamespace(active_backend=active_backend)
        self.backend_manager = SimpleNamespace(active_backend=active_backend)
        org_id = self.metadata.get("org_id")
        self.org_id = None if org_id is None else str(org_id)

    def get_runtime_metadata(self) -> dict[str, Any]:
        return dict(self.metadata)

    def get_display_name(self) -> str:
        return str(self.metadata.get("display_name") or self.name)

    def get_agent_emoji(self) -> str:
        return str(self.metadata.get("emoji") or "🤖")

    def get_current_model(self) -> str:
        return str(self.metadata.get("model") or "unknown")

    def _primary_chat_id(self) -> int:
        return int(self.metadata.get("primary_chat_id") or 0)

    async def enqueue_api_text(
        self,
        text: str,
        source: str = "api",
        deliver_to_telegram: bool = True,
        *,
        chat_id: Any | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        result = await self.peer.request(
            "core.route.enqueue_api_text",
            {
                "agent_name": self.name,
                "text": text,
                "source": source,
                "deliver_to_telegram": bool(deliver_to_telegram),
                "chat_id": chat_id,
                "request_metadata": dict(request_metadata or {}),
                "idempotency_key": idempotency_key,
            },
        )
        return None if result is None else str(result)

    async def send_long_message(
        self,
        chat_id: int,
        text: str,
        request_id: str | None = None,
        purpose: str = "response",
        parse_mode: str | None = None,
    ) -> tuple[float, int]:
        result = await self.peer.request(
            "core.route.send_long_message",
            {
                "agent_name": self.name,
                "chat_id": int(chat_id),
                "text": text,
                "request_id": request_id,
                "purpose": purpose,
                "parse_mode": parse_mode,
            },
        )
        values = list(result or (0.0, 0))
        return float(values[0]), int(values[1])

    async def set_command_menu(self, *, chat_id: int, locale: str) -> bool:
        return bool(
            await self.peer.request(
                "core.route.set_command_menu",
                {
                    "agent_name": self.name,
                    "chat_id": int(chat_id),
                    "locale": str(locale),
                },
            )
        )


class WorkerKernelFacade:
    """Narrow Core capability surface visible to functional Agent code."""

    is_function_worker_facade = True

    def __init__(
        self,
        *,
        peer: JsonConnectionPeer,
        paths: Any,
        global_cfg: Any,
        skill_manager: Any,
        agent_name: str,
        topology: Mapping[str, Any] | None = None,
    ) -> None:
        from orchestrator.config_admin import ConfigAdmin

        self.peer = peer
        self.paths = paths
        self.global_cfg = global_cfg
        self.global_config = global_cfg
        self.skill_manager = skill_manager
        self.agent_name = agent_name
        self.config_admin = ConfigAdmin(paths)
        self.scheduler = WorkerSchedulerFacade(peer, agent_name)
        self.service_manager = WorkerServiceManagerFacade(peer)
        self.background_job_manager = WorkerBackgroundJobManagerFacade(peer)
        self.runtimes: list[Any] = []
        self._runtime: Any | None = None
        self._remote_runtimes: dict[str, RemoteRuntimeReference] = {}
        self._startup_tasks: dict[str, object] = {}
        self._service_endpoints: dict[str, Any] = {}
        self._capabilities: dict[str, Any] = {}
        self.whatsapp: Any | None = None
        self.agent_directory: Any | None = None
        self._topology = dict(topology or {})

    def attach_runtime(self, runtime: Any) -> None:
        from orchestrator.agent_directory import AgentDirectory

        self._runtime = runtime
        self.agent_directory = AgentDirectory(
            self.paths.config_path,
            self.paths.bridge_home / "agent_capabilities.json",
            self.runtimes,
        )
        self.update_topology(self._topology)

    def update_topology(self, topology: Mapping[str, Any]) -> None:
        self._topology = dict(topology or {})
        self._service_endpoints = dict(
            self._topology.get("service_endpoints") or {}
        )
        self._capabilities = dict(self._topology.get("capabilities") or {})
        rows = {
            str(item.get("name") or item.get("id")): dict(item)
            for item in self._topology.get("runtimes", ())
            if isinstance(item, Mapping) and (item.get("name") or item.get("id"))
        }
        remote: dict[str, RemoteRuntimeReference] = {}
        for name, metadata in rows.items():
            if name == self.agent_name:
                continue
            reference = self._remote_runtimes.get(name)
            if reference is None:
                reference = RemoteRuntimeReference(self.peer, metadata)
            else:
                reference.update(metadata)
            remote[name] = reference
        self._remote_runtimes = remote
        views: list[Any] = []
        for name in rows:
            if name == self.agent_name and self._runtime is not None:
                views.append(self._runtime)
            elif name in remote:
                views.append(remote[name])
        if self._runtime is not None and self.agent_name not in rows:
            views.insert(0, self._runtime)
        self.runtimes[:] = views
        self._startup_tasks = {
            str(name): object()
            for name in self._topology.get("starting", ())
        }
        self.whatsapp = (
            _WhatsAppMarker(True)
            if bool(self._topology.get("whatsapp_connected"))
            else None
        )
        self.scheduler.update(self._topology.get("scheduler"))
        self.service_manager.update(self._topology.get("services"))
        if self.agent_directory is not None:
            self.agent_directory.runtimes = self.runtimes
            self.agent_directory.refresh()

    def resolve_service_endpoint(
        self,
        service: str,
        *,
        expected_instance: str | None = None,
    ) -> dict[str, Any]:
        endpoint = endpoint_from_snapshot(
            self._service_endpoints,
            service,
            expected_instance=(
                expected_instance
                or getattr(self.global_cfg, "instance_id", None)
                or "HASHI"
            ),
        )
        return endpoint.to_dict()

    def capability_status(self) -> dict[str, Any]:
        return dict(self._capabilities)

    async def refresh_capability_status(self) -> dict[str, Any]:
        result = await self.peer.request("core.capability.status", {})
        self._capabilities = dict(result or {})
        return dict(self._capabilities)

    async def invoke_capability(
        self,
        capability_kind: str,
        action: str,
        args: Mapping[str, Any] | None,
        *,
        task_id: str,
        request_id: str | None = None,
        authorization: str = "tool_registry",
        timeout_seconds: float = 60.0,
        lease_id: str | None = None,
    ) -> Any:
        return await self.peer.request(
            "core.capability.invoke",
            {
                "capability_kind": str(capability_kind),
                "action": str(action),
                "args": dict(args or {}),
                "task_id": str(task_id),
                "request_id": request_id,
                "authorization": str(authorization),
                "timeout_seconds": float(timeout_seconds),
                "lease_id": lease_id,
            },
            timeout=max(1.0, float(timeout_seconds) + 10.0),
        )

    async def acquire_capability_lease(
        self,
        capability_kind: str,
        *,
        action: str | None,
        task_id: str,
        window_id: str | None = None,
        ttl_seconds: float = 30.0,
    ) -> dict[str, Any]:
        result = await self.peer.request(
            "core.capability.acquire",
            {
                "capability_kind": str(capability_kind),
                "action": action,
                "task_id": str(task_id),
                "window_id": window_id,
                "ttl_seconds": float(ttl_seconds),
            },
        )
        return dict(result or {})

    async def handoff_capability_lease(
        self,
        source_lease_id: str,
        *,
        target_capability_kind: str,
        task_id: str,
        window_id: str | None = None,
    ) -> dict[str, Any]:
        result = await self.peer.request(
            "core.capability.handoff",
            {
                "source_lease_id": str(source_lease_id),
                "target_capability_kind": str(target_capability_kind),
                "task_id": str(task_id),
                "window_id": window_id,
            },
        )
        return dict(result or {})

    async def cancel_capability_task(self, task_id: str) -> int:
        return int(
            await self.peer.request(
                "core.capability.cancel",
                {"task_id": str(task_id)},
            )
        )

    async def release_capability_lease(
        self,
        lease_id: str,
        *,
        task_id: str,
    ) -> bool:
        return bool(
            await self.peer.request(
                "core.capability.release",
                {"lease_id": str(lease_id), "task_id": str(task_id)},
            )
        )

    def _runtime_map(self) -> dict[str, Any]:
        return {runtime.name: runtime for runtime in self.runtimes}

    def _notify_config_changed(self) -> None:
        try:
            asyncio.get_running_loop().create_task(
                self.peer.emit("core.config_changed", {"agent_name": self.agent_name})
            )
        except RuntimeError:
            pass

    def get_all_agents_raw(self) -> list[dict[str, Any]]:
        return self.config_admin.get_all_agents_raw()

    def configured_agent_names(self) -> list[str]:
        return self.config_admin.configured_agent_names()

    def get_startable_agent_names(self, exclude_name: str | None = None) -> list[str]:
        return self.config_admin.get_startable_agent_names(
            running=set(self._runtime_map()),
            starting=set(self._startup_tasks),
            exclude_name=exclude_name,
        )

    def set_agent_active(self, agent_name: str, active: bool) -> bool:
        result = self.config_admin.set_agent_active(agent_name, active)
        if result:
            self._notify_config_changed()
        return result

    def delete_agent_from_config(self, agent_name: str) -> bool:
        result = self.config_admin.delete_agent_from_config(agent_name)
        if result:
            self._notify_config_changed()
        return result

    def add_agent_to_config(
        self,
        agent_name: str,
        agent_cfg: dict | str | None = None,
        token: str | None = None,
    ) -> Any:
        result = self.config_admin.add_agent_to_config(agent_name, agent_cfg, token)
        if result is True or (isinstance(result, tuple) and result[0]):
            self._notify_config_changed()
        return result

    async def start_agent(self, agent_name: str) -> tuple[bool, str]:
        result = await self.peer.request(
            "core.start_agent",
            {"agent_name": str(agent_name), "requester": self.agent_name},
        )
        return bool(result[0]), str(result[1])

    async def stop_agent(
        self,
        agent_name: str,
        reason: str = "worker-command",
    ) -> tuple[bool, str]:
        result = await self.peer.request(
            "core.stop_agent",
            {
                "agent_name": str(agent_name),
                "reason": str(reason),
                "requester": self.agent_name,
            },
        )
        return bool(result[0]), str(result[1])

    def request_restart(
        self,
        mode: str = "same",
        agent_name: str | None = None,
        agent_number: int | None = None,
    ) -> None:
        asyncio.get_running_loop().create_task(
            self.peer.emit(
                "core.restart_requested",
                {
                    "mode": str(mode),
                    "agent_name": agent_name or self.agent_name,
                    "agent_number": agent_number,
                },
            )
        )

    async def request_reboot(self, **request):
        return await self.peer.request("core.reboot.submit", request)

    async def reboot_status(self, **origin):
        return await self.peer.request("core.reboot.status", origin)

    async def start_whatsapp_transport(
        self,
        persist_enabled: bool = True,
    ) -> tuple[bool, str]:
        result = await self.peer.request(
            "core.whatsapp.start",
            {"persist_enabled": bool(persist_enabled)},
        )
        return bool(result[0]), str(result[1])

    async def stop_whatsapp_transport(
        self,
        persist_enabled: bool = True,
    ) -> tuple[bool, str]:
        result = await self.peer.request(
            "core.whatsapp.stop",
            {"persist_enabled": bool(persist_enabled)},
        )
        return bool(result[0]), str(result[1])

    async def send_whatsapp_text(
        self,
        phone_number: str,
        text: str,
    ) -> tuple[bool, str]:
        result = await self.peer.request(
            "core.whatsapp.send",
            {"phone_number": str(phone_number), "text": str(text)},
        )
        return bool(result[0]), str(result[1])

    async def query_chief_of_staff(
        self,
        source_agent: str,
        question: str,
    ) -> dict[str, Any]:
        return dict(
            await self.peer.request(
                "core.cos.query",
                {
                    "source_agent": str(source_agent),
                    "question": str(question),
                },
                timeout=600.0,
            )
            or {}
        )


class FunctionWorkerHost:
    def __init__(
        self,
        connection: Any,
        bootstrap: Mapping[str, Any],
    ) -> None:
        self.bootstrap = dict(bootstrap)
        self.agent_name = str(self.bootstrap.get("agent_name") or "")
        self.nonce = str(self.bootstrap.get("nonce") or "")
        self.code_root = Path(str(self.bootstrap["code_root"])).resolve()
        self.bridge_home = Path(str(self.bootstrap["bridge_home"])).resolve()
        self.generation_root = Path(
            str(self.bootstrap["generation_root"])
        ).resolve()
        self.expected_runtime = RuntimeFingerprint.from_mapping(
            self.bootstrap["runtime"]
        )
        self.manifest = SourceManifest.from_mapping(self.bootstrap["manifest"])
        self.peer = JsonConnectionPeer(
            connection,
            label=f"worker:{self.agent_name}:{os.getpid()}",
            request_handler=self.handle_request,
            event_handler=self.handle_event,
        )
        self.paths: Any | None = None
        self.runtime: Any | None = None
        self.facade: WorkerKernelFacade | None = None
        self.phase = "BOOTING"
        self.accepting = False
        self.was_telegram_connected = False
        self.stop_event = asyncio.Event()
        self.audio_transcript_tasks: set[asyncio.Task[Any]] = set()
        self.generation_finder: _GenerationModuleFinder | None = None

    async def prepare(self) -> None:
        if not self.agent_name or not self.nonce:
            raise FunctionWorkerStateError("worker bootstrap identity is incomplete")
        if (
            int(self.bootstrap.get("protocol", -1))
            != FUNCTION_WORKER_PROTOCOL_VERSION
        ):
            raise FunctionWorkerStateError(
                "Function Worker bootstrap protocol does not match Core"
            )
        actual_runtime = enforce_runtime_contract(self.code_root)
        compare_runtime_fingerprints(self.expected_runtime, actual_runtime)
        if actual_runtime.worker_protocol != FUNCTION_WORKER_PROTOCOL_VERSION:
            raise FunctionWorkerStateError(
                "Function Worker protocol implementation does not match Core policy"
            )
        if actual_runtime.generation_schema != FUNCTION_GENERATION_SCHEMA_VERSION:
            raise FunctionWorkerStateError(
                "Function generation schema implementation does not match Core policy"
            )
        self._install_generation_import_paths()
        verify_qualified_manifest_bytes(
            self.manifest,
            code_root=self.generation_root,
        )

        with candidate_import_guard():
            for entry in self.manifest.entries:
                importlib.import_module(entry.module)
            validate_function_contract()
        verify_qualified_manifest_bytes(
            self.manifest,
            code_root=self.generation_root,
        )

        from orchestrator.config import ConfigManager
        from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
        from orchestrator.skill_manager import SkillManager

        self.paths = build_bridge_paths(self.code_root, bridge_home=self.bridge_home, canonical_home=True)
        manager = ConfigManager(
            self.paths.config_path,
            self.paths.secrets_path,
            bridge_home=self.paths.bridge_home,
            code_root=self.paths.code_root,
        )
        global_cfg, agent_configs, secrets = manager.load()
        agent_cfg = next(
            (item for item in agent_configs if item.name == self.agent_name),
            None,
        )
        if agent_cfg is None:
            raise FunctionWorkerStateError(
                f"Agent {self.agent_name!r} is not configured"
            )
        token = secrets.get(agent_cfg.telegram_token_key) or LOCAL_MODE_TOKEN
        skill_manager = SkillManager(self.paths.code_root, self.paths.tasks_path)
        runtime = FlexibleAgentRuntime(
            agent_cfg,
            global_cfg,
            token,
            secrets,
            skill_manager,
        )
        facade = WorkerKernelFacade(
            peer=self.peer,
            paths=self.paths,
            global_cfg=global_cfg,
            skill_manager=skill_manager,
            agent_name=self.agent_name,
            topology=self.bootstrap.get("topology") or {},
        )
        runtime.orchestrator = facade
        facade.attach_runtime(runtime)
        runtime.agent_directory = facade.agent_directory
        runtime.bind_handlers()

        initialized = await runtime.initialize()
        if not initialized:
            raise FunctionWorkerStateError(
                f"Backend initialization returned false for {self.agent_name!r}"
            )
        runtime.backend_ready = True
        self.runtime = runtime
        self.facade = facade
        self._bridge_request_completions(runtime)
        self.phase = "READY"
        await self.peer.emit("worker.ready", self.metadata())

    def _install_generation_import_paths(self) -> None:
        """Install exact manifest routing without shadowing protected Core."""

        finder = _GenerationModuleFinder(
            generation_root=self.generation_root,
            code_root=self.code_root,
            manifest=self.manifest,
        )
        sys.meta_path.insert(0, finder)
        self.generation_finder = finder

    def _bridge_request_completions(self, runtime: Any) -> None:
        original = runtime._notify_request_listeners

        async def notify(request_id: str, payload: dict) -> None:
            await original(request_id, payload)
            await self.peer.emit(
                "runtime.request_completed",
                {
                    "request_id": str(request_id),
                    "result": json_value(payload),
                },
            )
            await self.emit_metadata()

        runtime._notify_request_listeners = notify

    def _require_runtime(self) -> Any:
        if self.runtime is None:
            raise FunctionWorkerStateError("Agent runtime is not prepared")
        return self.runtime

    def metadata(self) -> dict[str, Any]:
        runtime = self._require_runtime()
        result = dict(runtime.get_runtime_metadata())
        command_registry_notices: list[dict[str, Any]] = []
        try:
            from orchestrator.admin_local_testing import supported_commands

            commands = supported_commands(runtime)
        except Exception:
            commands = []
        try:
            from orchestrator.command_registry import runtime_registry_notices

            command_registry_notices = runtime_registry_notices()
        except Exception:
            command_registry_notices = []
        native_audio: dict[str, bool] = {}
        manager = getattr(runtime, "voice_manager", None)
        native_enabled = getattr(manager, "native_audio_enabled", None)
        for terminal in ("session-api", "workbench", "browser", "telegram"):
            try:
                enabled = bool(native_enabled(terminal))
            except (TypeError, AttributeError):
                enabled = False
            if enabled:
                try:
                    from orchestrator.runtime_media import (
                        _backend_supports_native_audio_chat,
                    )

                    backend = getattr(
                        getattr(runtime, "backend_manager", None),
                        "current_backend",
                        None,
                    )
                    enabled = bool(
                        _backend_supports_native_audio_chat(
                            runtime,
                            backend,
                            terminal=terminal,
                        )
                    )
                except Exception:
                    enabled = False
            native_audio[terminal] = enabled
        result.update(
            {
                "startup_success": bool(runtime.startup_success),
                "backend_ready": bool(runtime.backend_ready),
                "telegram_connected": bool(runtime.telegram_connected),
                "primary_chat_id": int(runtime._primary_chat_id()),
                "media_dir": str(runtime.media_dir),
                "transcript_log_path": str(runtime.transcript_log_path),
                "core_transcript_log_path": str(runtime.core_transcript_log_path),
                "session_id": str(runtime.session_id_dt),
                "worker_pid": os.getpid(),
                "worker_protocol": FUNCTION_WORKER_PROTOCOL_VERSION,
                "worker_nonce": self.nonce,
                "worker_phase": self.phase,
                "worker_accepting": self.accepting,
                "generation_id": self.manifest.generation_id,
                "generation_module_count": len(self.manifest.entries),
                "runtime_id": self.expected_runtime.runtime_id,
                "safe_voice_enabled": bool(
                    getattr(runtime, "_safevoice_enabled", False)
                ),
                "native_audio": native_audio,
                "native_voice_policy": dict(
                    getattr(manager, "native_policy", {}) or {}
                ),
                "supported_commands": commands,
                "command_registry_notices": command_registry_notices,
                "active_transfer": bool(runtime.has_active_transfer()),
                "is_generating": bool(runtime.is_generating),
                "queue_depth": int(runtime.queue.qsize()),
                "current_request_meta": self._current_request_metadata(runtime),
                "org_id": getattr(runtime, "org_id", None),
            }
        )
        return json_value(result)

    @staticmethod
    def _current_request_metadata(runtime: Any) -> dict[str, Any]:
        raw = getattr(runtime, "current_request_meta", None)
        if not isinstance(raw, Mapping):
            return {}
        allowed = {
            "request_id",
            "chat_id",
            "source",
            "summary",
            "session_id",
            "run_id",
        }
        return {
            str(key): value
            for key, value in raw.items()
            if key in allowed
            and (value is None or isinstance(value, (str, int, float, bool)))
        }

    async def emit_metadata(self) -> None:
        await self.peer.emit("worker.metadata", self.metadata())

    async def _prepare_telegram_application(self) -> bool:
        """Validate and start handlers; Core alone owns long polling."""

        runtime = self._require_runtime()
        if runtime.token == LOCAL_MODE_TOKEN:
            runtime.telegram_connected = False
            return False
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                await runtime.app.initialize()
                await runtime.app.start()
                runtime.telegram_connected = True
                from orchestrator.runtime_command_binding import (
                    register_flexible_bot_commands,
                )

                await register_flexible_bot_commands(runtime)
                return True
            except Exception as exc:
                last_error = exc
                try:
                    if getattr(runtime.app, "running", False):
                        await runtime.app.stop()
                    await runtime.app.shutdown()
                except Exception:
                    pass
                if attempt < 3:
                    await asyncio.sleep(5)
        runtime.telegram_connected = False
        logger.warning(
            "Telegram application unavailable for %s; entering local mode: %s",
            self.agent_name,
            last_error,
        )
        return False

    async def _reconcile_interrupted_session_runs(self, runtime: Any) -> list[dict]:
        """Fence only this Worker's Agent Runs before accepting new work."""

        # This Worker is the sole executor for its Agent.  Before it accepts a
        # new request, fence durable queued/running Runs left by the previous
        # Worker process.  The Agent filter is essential: another Agent's live
        # Worker may be serving the same Session database concurrently.
        from orchestrator import runtime_session

        session_store = runtime_session.ensure_store(runtime)
        reconciled = await asyncio.to_thread(
            session_store.reconcile_incomplete_runs,
            agent_id=self.agent_name,
        )
        if reconciled:
            logger.warning(
                "Reconciled %s interrupted Session Run(s) before activating "
                "Function Worker %s pid=%s",
                len(reconciled),
                self.agent_name,
                os.getpid(),
            )
        return list(reconciled)

    async def activate(self) -> dict[str, Any]:
        if self.phase != "READY":
            raise FunctionWorkerStateError(
                f"activate requires READY, current phase={self.phase}"
            )
        runtime = self._require_runtime()
        self.phase = "ACTIVATING"
        await self._reconcile_interrupted_session_runs(runtime)
        telegram_connected = await self._prepare_telegram_application()
        runtime.startup_success = True
        if hasattr(runtime, "prepare_post_start_state"):
            runtime.prepare_post_start_state()
        runtime.process_task = asyncio.create_task(
            runtime.process_queue(),
            name=f"queue-{runtime.name}",
        )
        self.accepting = True
        self.phase = "ACTIVE"
        await self.emit_metadata()
        return {
            "ok": True,
            "local_mode": not telegram_connected,
            "metadata": self.metadata(),
        }

    async def quiesce(self, timeout: float) -> dict[str, Any]:
        if self.phase != "ACTIVE":
            raise FunctionWorkerStateError(
                f"quiesce requires ACTIVE, current phase={self.phase}"
            )
        runtime = self._require_runtime()
        self.phase = "DRAINING"
        self.accepting = False
        self.was_telegram_connected = bool(runtime.telegram_connected)
        runtime.telegram_connected = False
        deadline = time.monotonic() + max(0.1, float(timeout))
        while True:
            active_background = any(
                not task.done()
                for task in (
                    list(getattr(runtime, "_background_tasks", ()))
                    + list(getattr(runtime, "_persona_background_status_tasks", ()))
                )
            )
            if (
                runtime.queue.empty()
                and not bool(runtime.is_generating)
                and not active_background
            ):
                break
            if time.monotonic() >= deadline:
                await self.resume()
                raise TimeoutError(
                    f"Agent {self.agent_name!r} did not drain within {timeout:.1f}s"
                )
            await asyncio.sleep(0.05)
        persist = getattr(runtime, "_persist_transfer_state", None)
        if callable(persist):
            persist()
        self.phase = "QUIESCED"
        await self.emit_metadata()
        return {"ok": True, "metadata": self.metadata()}

    async def resume(self) -> dict[str, Any]:
        if self.phase not in {"DRAINING", "QUIESCED"}:
            raise FunctionWorkerStateError(
                f"resume requires DRAINING or QUIESCED, current phase={self.phase}"
            )
        runtime = self._require_runtime()
        if self.was_telegram_connected:
            runtime.telegram_connected = True
        self.accepting = True
        self.phase = "ACTIVE"
        await self.emit_metadata()
        return {"ok": True, "metadata": self.metadata()}

    async def shutdown(self) -> dict[str, Any]:
        if self.phase in {"STOPPING", "STOPPED"}:
            return {"ok": True}
        self.phase = "STOPPING"
        self.accepting = False
        runtime = self.runtime
        if runtime is not None:
            process_task = getattr(runtime, "process_task", None)
            if process_task is not None:
                process_task.cancel()
            try:
                await asyncio.wait_for(runtime.shutdown(), timeout=20.0)
            except asyncio.TimeoutError:
                logger.warning("Worker shutdown timed out for %s", self.agent_name)
            except Exception as exc:
                logger.warning(
                    "Worker shutdown warning for %s: %s: %s",
                    self.agent_name,
                    type(exc).__name__,
                    exc,
                )
        for task in tuple(self.audio_transcript_tasks):
            task.cancel()
        if self.audio_transcript_tasks:
            await asyncio.gather(*self.audio_transcript_tasks, return_exceptions=True)
        self.phase = "STOPPED"
        asyncio.get_running_loop().call_later(0.1, self.stop_event.set)
        return {"ok": True}

    async def handle_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "core.topology":
            if self.facade is not None:
                self.facade.update_topology(payload)
            return
        raise FunctionWorkerStateError(f"unknown Core event: {event}")

    async def handle_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "worker.ping":
            return {
                "pid": os.getpid(),
                "phase": self.phase,
                "protocol": FUNCTION_WORKER_PROTOCOL_VERSION,
            }
        if method == "worker.metadata":
            return self.metadata()
        if method == "worker.topology":
            if self.facade is not None:
                self.facade.update_topology(params)
            return self.metadata()
        if method == "worker.activate":
            return await self.activate()
        if method == "worker.quiesce":
            return await self.quiesce(
                float(params.get("timeout") or WORKER_DRAIN_TIMEOUT_SECONDS)
            )
        if method == "worker.resume":
            return await self.resume()
        if method == "worker.shutdown":
            return await self.shutdown()
        if method == "worker.telegram_status":
            runtime = self._require_runtime()
            runtime.telegram_connected = bool(params.get("connected"))
            await self.emit_metadata()
            return {"ok": True, "metadata": self.metadata()}

        runtime = self._require_runtime()
        if self.phase != "ACTIVE" or not self.accepting:
            raise FunctionWorkerStateError(
                f"Agent {self.agent_name!r} is not accepting requests (phase={self.phase})"
            )
        if method == "runtime.enqueue_request":
            content = params.get("request_content")
            request_metadata = params.get("request_metadata")
            metadata = (
                request_metadata if isinstance(request_metadata, Mapping) else {}
            )
            transcript_state = await self._begin_native_voice_transcription(
                content,
                terminal=str(
                    metadata.get("session_surface")
                    or params.get("source")
                    or ""
                ),
            )
            request_id = await runtime.enqueue_request(
                int(params["chat_id"]),
                str(params.get("prompt") or ""),
                str(params.get("source") or "api"),
                str(params.get("summary") or ""),
                silent=bool(params.get("silent", False)),
                is_retry=bool(params.get("is_retry", False)),
                deliver_to_telegram=bool(params.get("deliver_to_telegram", True)),
                skip_memory_injection=bool(params.get("skip_memory_injection", False)),
                habit_learning_eligible=bool(
                    params.get("habit_learning_eligible", True)
                ),
                skill_id=params.get("skill_id"),
                scheduler_context=params.get("scheduler_context"),
                request_metadata=request_metadata,
                request_content=content,
                idempotency_key=params.get("idempotency_key"),
            )
            if request_id and transcript_state is not None:
                self._bind_native_voice_transcription(
                    transcript_state,
                    str(request_id),
                )
            await self.emit_metadata()
            return request_id
        if method == "runtime.telegram_update":
            from telegram import Update

            update_payload = params.get("update")
            if not isinstance(update_payload, Mapping):
                raise FunctionWorkerStateError(
                    "runtime.telegram_update requires an update object"
                )
            update = Update.de_json(dict(update_payload), runtime.app.bot)
            await runtime.app.process_update(update)
            await self.emit_metadata()
            return True
        if method == "runtime.enqueue_api_text":
            result = await runtime.enqueue_api_text(
                str(params.get("text") or ""),
                source=str(params.get("source") or "api"),
                deliver_to_telegram=bool(params.get("deliver_to_telegram", True)),
                chat_id=params.get("chat_id"),
                request_metadata=params.get("request_metadata"),
                idempotency_key=params.get("idempotency_key"),
            )
            await self.emit_metadata()
            return result
        if method == "runtime.enqueue_startup_bootstrap":
            await runtime.enqueue_startup_bootstrap(int(params["chat_id"]))
            return True
        if method == "runtime.enqueue_api_media":
            result = await runtime.enqueue_api_media(
                local_path=Path(str(params["local_path"])),
                media_kind=str(params["media_kind"]),
                filename=str(params["filename"]),
                caption=str(params.get("caption") or ""),
                emoji=str(params.get("emoji") or ""),
                source=str(params.get("source") or "api"),
                deliver_to_telegram=bool(params.get("deliver_to_telegram", True)),
                request_metadata=params.get("request_metadata"),
                idempotency_key=params.get("idempotency_key"),
            )
            await self.emit_metadata()
            return result
        if method == "runtime.send_text":
            await runtime._send_text(
                int(params["chat_id"]),
                str(params.get("text") or ""),
                **dict(params.get("kwargs") or {}),
            )
            return True
        if method == "runtime.send_long_message":
            result = await runtime.send_long_message(
                int(params["chat_id"]),
                str(params.get("text") or ""),
                request_id=params.get("request_id"),
                purpose=str(params.get("purpose") or "response"),
                parse_mode=params.get("parse_mode"),
            )
            return list(result)
        if method == "runtime.run_job_now":
            result = await runtime._run_job_now(
                dict(params.get("job") or {}),
                kind=params.get("kind"),
            )
            return list(result)
        if method == "runtime.invoke_scheduler_skill":
            result = await runtime.invoke_scheduler_skill(
                str(params["skill_id"]),
                str(params.get("args") or ""),
                str(params["task_id"]),
                scheduler_context=params.get("scheduler_context"),
            )
            return list(result)
        if method == "runtime.invoke_scheduler_automation":
            result = await runtime.invoke_scheduler_automation(
                str(params["automation_id"]),
                str(params.get("args") or ""),
                str(params["task_id"]),
            )
            return list(result)
        if method == "runtime.invoke_her_dream":
            result = await runtime.invoke_her_dream(
                task_id=str(params["task_id"]),
                scheduled_for=params.get("scheduled_for"),
            )
            return list(result)
        if method == "runtime.export_daily_transcript":
            return bool(
                runtime.export_daily_transcript(
                    datetime.fromisoformat(str(params["cutoff_dt"]))
                )
            )
        if method == "runtime.cos_query":
            return await runtime.cos_query(str(params.get("question") or ""))
        if method == "runtime.slash":
            from orchestrator.admin_local_testing import (
                try_execute_slash_command_text,
            )

            return await try_execute_slash_command_text(
                runtime,
                str(params.get("text") or ""),
                source_channel=str(params.get("source_channel") or "api_chat"),
                chat_id=params.get("chat_id"),
                session_metadata=params.get("session_metadata"),
            )
        if method == "runtime.activity.poll":
            return runtime.request_activity.poll(
                str(params["request_id"]),
                after_sequence=int(params.get("after_sequence") or 0),
                limit=int(params.get("limit") or 100),
            )
        if method == "runtime.has_active_transfer":
            return bool(runtime.has_active_transfer())
        if method == "runtime.set_command_menu":
            from telegram import BotCommandScopeChat

            await runtime.app.bot.set_my_commands(
                runtime.get_bot_commands(locale=str(params["locale"])),
                scope=BotCommandScopeChat(chat_id=int(params["chat_id"])),
            )
            return True
        if method == "runtime.native_transcript_decide":
            return self._decide_native_transcript(params)
        raise FunctionWorkerStateError(f"unknown Worker request: {method}")

    async def _begin_native_voice_transcription(
        self,
        canonical_content: Any,
        *,
        terminal: str | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(canonical_content, Mapping):
            return None
        runtime = self._require_runtime()
        voice_parts = [
            dict(part)
            for part in canonical_content.get("parts", ())
            if isinstance(part, Mapping)
            and part.get("type") == "media"
            and part.get("modality") == "audio"
            and part.get("semantic_role") == "voice_message"
        ]
        if not voice_parts:
            return None
        registry = runtime._native_voice_transcripts
        attachment_ids = [str(part["attachment_id"]) for part in voice_parts]
        existing = [registry.get(item) for item in attachment_ids]
        if existing and all(
            isinstance(item, dict) and item is existing[0] for item in existing
        ):
            return existing[0]

        async def transcribe_part(part: dict[str, Any]) -> dict[str, str]:
            from orchestrator.voice_transcriber import get_transcriber

            result = str(
                await get_transcriber().transcribe(Path(str(part["local_ref"])))
            )
            return {
                "attachment_id": str(part["attachment_id"]),
                "text": "" if result.startswith("[Transcription error]") else result.strip(),
                "error": result if result.startswith("[Transcription error]") else "",
            }

        async def transcribe_all() -> list[dict[str, str]]:
            return list(
                await asyncio.gather(*(transcribe_part(part) for part in voice_parts))
            )

        task = asyncio.create_task(transcribe_all())
        manager = getattr(runtime, "voice_manager", None)
        native_enabled = getattr(manager, "native_audio_enabled", None)
        native_authorized = False
        if callable(native_enabled):
            try:
                native_authorized = bool(native_enabled(terminal))
            except TypeError:
                native_authorized = bool(native_enabled())

        state: dict[str, Any] = {
            "task": task,
            "ready_event": asyncio.Event(),
            "release_event": asyncio.Event(),
            "gate_lock": asyncio.Lock(),
            "status": "pending",
            "attachment_id": attachment_ids[0],
            "attachment_ids": attachment_ids,
            "transcript_decisions": {},
            "safe_voice": bool(
                runtime._safevoice_enabled and not native_authorized
            ),
            "confirmation_requested": False,
            "confirmation_presented": False,
            "native_audio_completed": False,
            "text": "",
        }
        for attachment_id in attachment_ids:
            registry[attachment_id] = state
        return state

    def _bind_native_voice_transcription(
        self,
        state: dict[str, Any],
        request_id: str,
    ) -> None:
        from orchestrator.voice_transcript_gate import state_after_transcription

        runtime = self._require_runtime()
        registry = runtime._native_voice_transcripts
        state["request_id"] = request_id
        registry[request_id] = state

        async def request_confirmation() -> None:
            recorded = runtime.session_store.require_voice_transcript_confirmation(
                request_id=request_id
            )
            state["status"] = "pending_confirmation"
            decisions = state["transcript_decisions"]
            for transcript_id, decision in tuple(decisions.items()):
                if decision == "ready":
                    decisions[transcript_id] = "pending_confirmation"
            decisions[str(recorded["transcript_id"])] = "pending_confirmation"

        async def auto_release() -> None:
            recorded = runtime.session_store.release_ready_voice_transcript(
                request_id=request_id
            )
            decisions = state["transcript_decisions"]
            for transcript_id, decision in tuple(decisions.items()):
                if decision == "ready":
                    decisions[transcript_id] = "released"
            decisions[str(recorded["transcript_id"])] = "released"

        state["request_confirmation"] = request_confirmation
        state["auto_release"] = auto_release

        async def finalize() -> None:
            try:
                results = list(await state["task"])
                unavailable = any(not str(item.get("text") or "") for item in results)
                state["text"] = "\n".join(
                    str(item.get("text") or "").strip()
                    for item in results
                    if str(item.get("text") or "").strip()
                )
                async with state["gate_lock"]:
                    successful_state = state_after_transcription(state)
                    state["status"] = "unavailable" if unavailable else successful_state
                for result in results:
                    text = str(result.get("text") or "").strip()
                    part_state = "unavailable" if not text else successful_state
                    recorded = runtime.session_store.record_voice_transcript(
                        request_id=request_id,
                        attachment_id=str(result["attachment_id"]),
                        text=text,
                        provenance="local_stt",
                        safe_voice_state=part_state,
                    )
                    state["transcript_decisions"][str(recorded["transcript_id"])] = part_state
            except asyncio.CancelledError:
                raise
            except Exception:
                state["text"] = ""
                state["status"] = "unavailable"
            finally:
                state["ready_event"].set()
                if state["status"] in {"released", "discarded", "unavailable"}:
                    state["release_event"].set()

        task = asyncio.create_task(
            finalize(),
            name=f"worker-native-transcript:{request_id}",
        )
        self.audio_transcript_tasks.add(task)
        task.add_done_callback(self.audio_transcript_tasks.discard)

    def _decide_native_transcript(self, params: Mapping[str, Any]) -> bool:
        runtime = self._require_runtime()
        request_id = str(params.get("request_id") or "")
        transcript_id = str(params.get("transcript_id") or "")
        decision = str(params.get("decision") or "")
        state = runtime._native_voice_transcripts.get(request_id)
        if not isinstance(state, dict):
            return False
        decisions = state.get("transcript_decisions")
        if isinstance(decisions, dict):
            decisions[transcript_id] = (
                "released" if decision == "confirm" else "discarded"
            )
            if decision == "discard":
                state["status"] = "discarded"
            elif decisions and all(value == "released" for value in decisions.values()):
                state["status"] = "released"
        if state.get("status") in {"released", "discarded", "unavailable"}:
            release = state.get("release_event")
            if isinstance(release, asyncio.Event):
                release.set()
        return True


async def run_function_worker(connection: Any, bootstrap: Mapping[str, Any]) -> None:
    host = FunctionWorkerHost(connection, bootstrap)
    host.peer.start()
    from orchestrator.bootstrap_logging import setup_worker_logging
    from orchestrator.function_worker_features import worker_log_relay_enabled

    log_relay = setup_worker_logging(
        host.bridge_home,
        host.peer,
        relay_enabled=worker_log_relay_enabled(bootstrap),
    )
    try:
        await asyncio.wait_for(host.prepare(), timeout=WORKER_PREPARE_TIMEOUT_SECONDS)
    except Exception as exc:
        try:
            await host.peer.emit(
                "worker.failed",
                {
                    "agent_name": host.agent_name,
                    "nonce": host.nonce,
                    "pid": os.getpid(),
                    "phase": host.phase,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:4000],
                },
            )
        except Exception:
            pass
        await log_relay.drain()
        await host.peer.close()
        return

    stop_wait = asyncio.create_task(host.stop_event.wait())
    closed_wait = asyncio.create_task(host.peer.wait_closed())
    done, pending = await asyncio.wait(
        {stop_wait, closed_wait},
        return_when=asyncio.FIRST_COMPLETED,
    )
    del done
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if host.phase != "STOPPED":
        await host.shutdown()
    await log_relay.drain()
    await host.peer.close()
