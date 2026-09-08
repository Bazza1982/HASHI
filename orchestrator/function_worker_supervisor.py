"""Stable Core supervisor for isolated, per-Agent Function Workers."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import multiprocessing
import os
import shutil
import stat
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from orchestrator.function_generation import (
    CandidateProbeReceipt,
    FUNCTION_GENERATION_SCHEMA_VERSION,
    SourceManifest,
    VerifiedFunctionGeneration,
    probe_function_generation,
    configured_observers_are_qualified,
    verify_qualified_manifest_bytes,
)
from orchestrator.function_worker_bootstrap import run_function_worker_process
from orchestrator.function_worker_features import WORKER_LOG_RELAY_FEATURE
from orchestrator.function_worker_protocol import (
    FUNCTION_WORKER_PROTOCOL_VERSION,
    FunctionWorkerDisconnected,
    FunctionWorkerProtocolError,
    JsonConnectionPeer,
)
from orchestrator.telegram_ingress import CoreTelegramIngress

logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")

WORKER_READY_TIMEOUT_SECONDS = 240.0
WORKER_REQUEST_TIMEOUT_SECONDS = 180.0
WORKER_SHUTDOWN_TIMEOUT_SECONDS = 30.0
WORKER_DRAIN_TIMEOUT_SECONDS = 120.0
WORKER_RECOVERY_ATTEMPTS = 3
QUALIFIED_GENERATION_CACHE_SCHEMA_VERSION = 1
TELEGRAM_STATUS_WARNING_DEBOUNCE_SECONDS = 0.5
TELEGRAM_STATUS_WARNING_COOLDOWN_SECONDS = 30.0


class FunctionWorkerError(RuntimeError):
    """A Function Worker could not be prepared, switched, or recovered."""


def _manifest_receipt_from_dict(value: Mapping[str, Any]) -> CandidateProbeReceipt:
    runtime_value = value["runtime"]
    from orchestrator.runtime_contract import RuntimeFingerprint

    return CandidateProbeReceipt(
        generation_id=str(value["generation_id"]),
        module_names=tuple(str(item) for item in value["module_names"]),
        runtime=RuntimeFingerprint.from_mapping(runtime_value),
        probe_pid=int(value["probe_pid"]),
    )


def generation_from_dict(value: Mapping[str, Any]) -> VerifiedFunctionGeneration:
    return VerifiedFunctionGeneration(
        code_root=Path(str(value["code_root"])).resolve(),
        manifest=SourceManifest.from_mapping(value["manifest"]),
        receipt=_manifest_receipt_from_dict(value["receipt"]),
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_manifest_path(root: Path) -> Path:
    return root / "function-generation.json"


def verify_generation_artifact(
    root: Path,
    generation: VerifiedFunctionGeneration,
) -> None:
    root = Path(root).resolve()
    metadata_path = _artifact_manifest_path(root)
    if not metadata_path.is_file():
        raise FunctionWorkerError(f"Function generation artifact has no manifest: {root}")
    try:
        stored = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FunctionWorkerError(
            f"Function generation artifact manifest is unreadable: {root}"
        ) from exc
    if stored.get("schema_version") != FUNCTION_GENERATION_SCHEMA_VERSION:
        raise FunctionWorkerError(
            f"Function generation artifact schema mismatch: {root}"
        )
    if stored.get("manifest") != generation.manifest.to_dict():
        raise FunctionWorkerError(
            f"Function generation artifact manifest mismatch: {root}"
        )
    verify_qualified_manifest_bytes(generation.manifest, code_root=root)


def materialize_generation_artifact(
    bridge_home: Path,
    generation: VerifiedFunctionGeneration,
) -> Path:
    """Copy verified function bytes into a content-addressed immutable tree."""

    state_root = Path(bridge_home).resolve() / "state" / "function_generations"
    state_root.mkdir(parents=True, exist_ok=True)
    slug = generation.manifest.generation_id.removeprefix("sha256:")
    destination = state_root / slug
    if destination.exists():
        verify_generation_artifact(destination, generation)
        return destination

    temporary = state_root / f".{slug}.staging-{os.getpid()}-{uuid4().hex[:8]}"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        for entry in generation.manifest.entries:
            source = generation.code_root / entry.relative_path
            if _file_sha256(source) != entry.sha256:
                raise FunctionWorkerError(
                    f"Function source changed while building artifact: {entry.module}"
                )
            target = temporary / entry.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        for asset in generation.manifest.assets:
            source = generation.code_root / asset.relative_path
            if _file_sha256(source) != asset.sha256:
                raise FunctionWorkerError(
                    "Function asset changed while building artifact: "
                    f"{asset.relative_path}"
                )
            target = temporary / asset.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if asset.executable:
                target.chmod(target.stat().st_mode | stat.S_IXUSR)

        metadata = {
            "schema_version": FUNCTION_GENERATION_SCHEMA_VERSION,
            "generation_id": generation.manifest.generation_id,
            "created_at": datetime.now().astimezone().isoformat(),
            "manifest": generation.manifest.to_dict(),
            "probe": {
                "pid": generation.receipt.probe_pid,
                "runtime_id": generation.receipt.runtime.runtime_id,
            },
        }
        _artifact_manifest_path(temporary).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verify_generation_artifact(temporary, generation)
        temporary.replace(destination)
        if os.name != "nt":
            for path in destination.rglob("*"):
                if path.is_file():
                    try:
                        path.chmod(
                            path.stat().st_mode
                            & ~stat.S_IWUSR
                            & ~stat.S_IWGRP
                            & ~stat.S_IWOTH
                        )
                    except OSError:
                        pass
        return destination
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _qualified_generation_cache_path(bridge_home: Path) -> Path:
    return (
        Path(bridge_home).resolve()
        / "state"
        / "function_generations"
        / "qualified-generation.json"
    )


def persist_qualified_generation_cache(
    bridge_home: Path,
    generation: VerifiedFunctionGeneration,
    artifact: Path,
) -> None:
    """Persist the isolated-probe receipt for unchanged cold restarts."""

    cache_path = _qualified_generation_cache_path(bridge_home)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": QUALIFIED_GENERATION_CACHE_SCHEMA_VERSION,
        "saved_at": datetime.now().astimezone().isoformat(),
        "artifact_slug": Path(artifact).resolve().name,
        "generation": generation.to_dict(),
    }
    temporary = cache_path.with_name(
        f".{cache_path.name}.staging-{os.getpid()}-{uuid4().hex[:8]}"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(cache_path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_qualified_generation_cache(
    bridge_home: Path,
    code_root: Path,
    expected_runtime: Any,
) -> tuple[VerifiedFunctionGeneration, Path] | None:
    """Load a prior probe receipt only when runtime and every byte still match."""

    cache_path = _qualified_generation_cache_path(bridge_home)
    if not cache_path.is_file():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            int(payload.get("schema_version", -1))
            != QUALIFIED_GENERATION_CACHE_SCHEMA_VERSION
        ):
            return None
        generation_value = dict(payload["generation"])
        # A repository may be moved or promoted to another instance without
        # changing the qualified bytes.  The current source root is authority.
        generation_value["code_root"] = str(Path(code_root).resolve())
        generation = generation_from_dict(generation_value)
        slug = generation.manifest.generation_id.removeprefix("sha256:")
        if str(payload.get("artifact_slug") or "") != slug:
            return None
        artifact = cache_path.parent / slug
        generation.verify_qualified_source(expected_runtime)
        verify_generation_artifact(artifact, generation)
        return generation, artifact
    except Exception as exc:
        bridge_logger.info(
            "Qualified generation cache rejected; running isolated probe: %s: %s",
            type(exc).__name__,
            exc,
        )
        return None


class _RemoteVoiceManagerView:
    def __init__(self, handle: AgentRuntimeHandle) -> None:
        self.handle = handle

    def native_audio_enabled(self, terminal: str | None = None) -> bool:
        key = str(terminal or "workbench")
        return bool(self.handle.metadata.get("native_audio", {}).get(key, False))

    def native_policy_for_terminal(self, terminal: str) -> dict[str, Any]:
        del terminal
        return dict(self.handle.metadata.get("native_voice_policy") or {})

    @property
    def native_policy(self) -> dict[str, Any]:
        return dict(self.handle.metadata.get("native_voice_policy") or {})


class FunctionWorkerClient:
    """One candidate or active worker process and its private IPC peer."""

    def __init__(
        self,
        *,
        supervisor: FunctionWorkerSupervisor,
        agent_name: str,
        generation: VerifiedFunctionGeneration,
        generation_root: Path,
        nonce: str,
        process: multiprocessing.Process,
        connection: Any,
    ) -> None:
        self.supervisor = supervisor
        self.agent_name = agent_name
        self.generation = generation
        self.generation_root = Path(generation_root)
        self.nonce = nonce
        self.process = process
        self.metadata: dict[str, Any] = {}
        self.ready = asyncio.get_running_loop().create_future()
        self.peer = JsonConnectionPeer(
            connection,
            label=f"core:{agent_name}:{process.pid}",
            request_handler=self._handle_request,
            event_handler=self._handle_event,
        )
        self.monitor_task: asyncio.Task[None] | None = None
        self.closed = False
        self.expected_exit = False

    @property
    def pid(self) -> int:
        return int(self.process.pid or 0)

    @property
    def generation_id(self) -> str:
        return self.generation.manifest.generation_id

    def start(self) -> None:
        self.peer.start()
        self.monitor_task = asyncio.create_task(
            self._monitor_process(),
            name=f"function-worker-monitor:{self.agent_name}:{self.pid}",
        )

    async def wait_ready(self, timeout: float = WORKER_READY_TIMEOUT_SECONDS) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(asyncio.shield(self.ready), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise FunctionWorkerError(
                f"Function Worker {self.agent_name!r} did not become READY within {timeout:.1f}s"
            ) from exc

    async def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = WORKER_REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        if self.closed or not self.process.is_alive():
            raise FunctionWorkerDisconnected(
                f"Function Worker {self.agent_name!r} pid={self.pid} is not alive"
            )
        return await self.peer.request(method, params, timeout=timeout)

    async def _handle_request(self, method: str, params: dict[str, Any]) -> Any:
        return await self.supervisor.handle_worker_request(self, method, params)

    async def _handle_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "worker.ready":
            try:
                if (
                    int(payload.get("worker_protocol", -1))
                    != FUNCTION_WORKER_PROTOCOL_VERSION
                ):
                    raise FunctionWorkerProtocolError(
                        f"Worker protocol mismatch for {self.agent_name!r}"
                    )
                if (
                    str(payload.get("name") or payload.get("id") or "")
                    != self.agent_name
                ):
                    raise FunctionWorkerProtocolError(
                        "Worker READY identity mismatch"
                    )
                if str(payload.get("generation_id") or "") != self.generation_id:
                    raise FunctionWorkerProtocolError(
                        "Worker READY generation mismatch"
                    )
                if int(payload.get("worker_pid") or 0) != self.pid:
                    raise FunctionWorkerProtocolError("Worker READY pid mismatch")
                if str(payload.get("worker_nonce") or "") != self.nonce:
                    raise FunctionWorkerProtocolError("Worker READY nonce mismatch")
            except FunctionWorkerProtocolError as exc:
                if not self.ready.done():
                    self.ready.set_exception(exc)
                raise
            self.metadata = dict(payload)
            if not self.ready.done():
                self.ready.set_result(dict(payload))
            return
        if event == "worker.failed":
            error = FunctionWorkerError(
                f"Function Worker {self.agent_name!r} rejected generation "
                f"{self.generation_id}: {payload.get('error_type')}: {payload.get('error')}"
            )
            if not self.ready.done():
                self.ready.set_exception(error)
            return
        if event == "worker.metadata":
            self.metadata = dict(payload)
            self.supervisor.update_active_metadata(self, payload)
            return
        await self.supervisor.handle_worker_event(self, event, payload)

    async def _monitor_process(self) -> None:
        while self.process.is_alive() and not self.closed:
            await asyncio.sleep(0.2)
        try:
            await asyncio.to_thread(self.process.join, 0.2)
        except (AssertionError, RuntimeError):
            pass
        if not self.ready.done():
            self.ready.set_exception(
                FunctionWorkerError(
                    f"Function Worker {self.agent_name!r} exited before READY "
                    f"(pid={self.pid}, exit={self.process.exitcode})"
                )
            )
        if not self.closed and not self.expected_exit:
            await self.supervisor.handle_worker_exit(self)

    async def shutdown(self, *, force: bool = True) -> None:
        if self.closed:
            return
        self.expected_exit = True
        try:
            if self.process.is_alive() and not self.peer.is_closed:
                await self.call(
                    "worker.shutdown",
                    timeout=WORKER_SHUTDOWN_TIMEOUT_SECONDS,
                )
        except Exception:
            pass
        deadline = asyncio.get_running_loop().time() + WORKER_SHUTDOWN_TIMEOUT_SECONDS
        while self.process.is_alive() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        if force and self.process.is_alive():
            self.process.terminate()
            await asyncio.to_thread(self.process.join, 5.0)
            if self.process.is_alive() and hasattr(self.process, "kill"):
                self.process.kill()
                await asyncio.to_thread(self.process.join, 5.0)
        self.closed = True
        await self.peer.close()
        monitor = self.monitor_task
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)


class AgentRuntimeHandle:
    """Stable Core object whose active Worker target changes atomically."""

    is_function_worker_proxy = True

    def __init__(
        self,
        kernel: Any,
        client: FunctionWorkerClient,
        metadata: Mapping[str, Any],
    ) -> None:
        self.kernel = kernel
        self.name = client.agent_name
        self._client = client
        self.metadata = dict(metadata)
        self._condition = asyncio.Condition()
        self._cutover = False
        self._offline_error: str | None = None
        self._route_inflight = 0
        self._request_listeners: dict[str, list[Any]] = {}
        self._pending_request_results: dict[str, dict[str, Any]] = {}
        self._outstanding_request_ids: set[str] = set()
        self.logger = logging.getLogger(f"FunctionWorkerProxy.{self.name}")
        self.voice_manager = _RemoteVoiceManagerView(self)
        self._native_voice_transcripts: dict[str, Any] = {}
        self.process_task = None

    @property
    def client(self) -> FunctionWorkerClient:
        return self._client

    @property
    def worker_pid(self) -> int:
        return self._client.pid

    @property
    def generation_id(self) -> str:
        return self._client.generation_id

    @property
    def startup_success(self) -> bool:
        return bool(self.metadata.get("startup_success", True))

    @property
    def backend_ready(self) -> bool:
        return bool(self.metadata.get("backend_ready", self.metadata.get("online", True)))

    @property
    def telegram_connected(self) -> bool:
        return bool(self.metadata.get("telegram_connected", False))

    @property
    def workspace_dir(self) -> Path:
        return Path(str(self.metadata.get("workspace_dir") or "."))

    @property
    def media_dir(self) -> Path:
        return Path(str(self.metadata.get("media_dir") or self.workspace_dir / "media"))

    @property
    def transcript_log_path(self) -> str:
        return str(
            self.metadata.get("transcript_log_path")
            or self.metadata.get("transcript_path")
            or self.workspace_dir / "transcript.jsonl"
        )

    @property
    def session_id_dt(self) -> str:
        return str(self.metadata.get("session_id") or "")

    @property
    def core_transcript_log_path(self) -> str:
        return str(
            self.metadata.get("core_transcript_log_path")
            or self.workspace_dir / "core_transcript.jsonl"
        )

    @property
    def global_config(self) -> Any:
        return self.kernel.global_cfg

    @property
    def skill_manager(self) -> Any:
        return self.kernel.skill_manager

    @property
    def current_request_meta(self) -> dict[str, Any]:
        return dict(self.metadata.get("current_request_meta") or {})

    @property
    def _safevoice_enabled(self) -> bool:
        return bool(self.metadata.get("safe_voice_enabled", False))

    @property
    def runtime_id(self) -> str:
        return self.kernel.runtime_fingerprint.runtime_id

    @property
    def python(self) -> str:
        return self.kernel.runtime_fingerprint.python

    @property
    def platform_abi(self) -> str:
        return self.kernel.runtime_fingerprint.platform_abi

    @property
    def core_api(self) -> int:
        return self.kernel.runtime_fingerprint.core_api

    @property
    def function_api(self) -> int:
        return self.kernel.runtime_fingerprint.function_api

    @property
    def dependency_digest(self) -> str:
        return self.kernel.runtime_fingerprint.dependency_digest

    @property
    def core_source_digest(self) -> str:
        return self.kernel.runtime_fingerprint.core_source_digest

    def get_runtime_metadata(self) -> dict[str, Any]:
        return dict(self.metadata)

    def get_display_name(self) -> str:
        return str(self.metadata.get("display_name") or self.name)

    def get_agent_emoji(self) -> str:
        return str(self.metadata.get("emoji") or "🤖")

    def get_current_model(self) -> str:
        return str(self.metadata.get("model") or "unknown")

    def get_current_provider(self) -> str:
        return str(self.metadata.get("provider") or "unknown")

    @property
    def config(self) -> Any:
        return SimpleNamespace(
            active_backend=str(self.metadata.get("active_backend") or "unknown"),
            type=str(self.metadata.get("type") or "assistant"),
        )

    @property
    def backend_manager(self) -> Any:
        return SimpleNamespace(active_backend=self.config.active_backend)

    @property
    def org_id(self) -> str | None:
        value = self.metadata.get("org_id")
        return None if value is None else str(value)

    def _primary_chat_id(self) -> int:
        return int(self.metadata.get("primary_chat_id") or 0)

    def has_active_transfer(self) -> bool:
        return bool(self.metadata.get("active_transfer", False))

    def supported_commands(self) -> list[str]:
        return [str(item) for item in self.metadata.get("supported_commands", ())]

    def native_audio_ready(self, terminal: str | None = None) -> bool:
        return self.voice_manager.native_audio_enabled(terminal)

    async def begin_cutover(self) -> FunctionWorkerClient:
        async with self._condition:
            if self._cutover:
                raise FunctionWorkerError(f"Agent {self.name!r} is already cutting over")
            self._cutover = True
            try:
                while self._route_inflight:
                    await self._condition.wait()
                return self._client
            except BaseException:
                self._cutover = False
                self._condition.notify_all()
                raise

    async def commit_cutover(
        self,
        client: FunctionWorkerClient,
        metadata: Mapping[str, Any],
    ) -> None:
        async with self._condition:
            if not self._cutover:
                raise FunctionWorkerError(f"Agent {self.name!r} has no active cutover")
            self._client = client
            self.metadata = dict(metadata)
            self._offline_error = None
            self._cutover = False
            self._condition.notify_all()

    async def abort_cutover(self) -> None:
        async with self._condition:
            self._cutover = False
            self._condition.notify_all()

    async def close_route(self, message: str) -> None:
        async with self._condition:
            self._offline_error = str(message)
            self._cutover = False
            self._condition.notify_all()

    async def _route(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = WORKER_REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        async with self._condition:
            while self._cutover:
                await self._condition.wait()
            if self._offline_error is not None:
                raise FunctionWorkerDisconnected(self._offline_error)
            client = self._client
            self._route_inflight += 1
        try:
            return await client.call(method, params, timeout=timeout)
        finally:
            async with self._condition:
                self._route_inflight -= 1
                if self._route_inflight == 0:
                    self._condition.notify_all()

    async def enqueue_request(
        self,
        chat_id: int,
        prompt: str,
        source: str,
        summary: str,
        silent: bool = False,
        is_retry: bool = False,
        deliver_to_telegram: bool = True,
        skip_memory_injection: bool = False,
        habit_learning_eligible: bool = True,
        skill_id: str | None = None,
        scheduler_context: Mapping[str, str] | None = None,
        request_metadata: Mapping[str, Any] | None = None,
        request_content: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        result = await self._route(
            "runtime.enqueue_request",
            {
                "chat_id": int(chat_id),
                "prompt": str(prompt),
                "source": str(source),
                "summary": str(summary),
                "silent": bool(silent),
                "is_retry": bool(is_retry),
                "deliver_to_telegram": bool(deliver_to_telegram),
                "skip_memory_injection": bool(skip_memory_injection),
                "habit_learning_eligible": bool(habit_learning_eligible),
                "skill_id": skill_id,
                "scheduler_context": dict(scheduler_context or {}),
                "request_metadata": dict(request_metadata or {}),
                "request_content": dict(request_content or {}) if request_content else None,
                "idempotency_key": idempotency_key,
            },
        )
        if result:
            self._outstanding_request_ids.add(str(result))
        return None if result is None else str(result)

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
        result = await self._route(
            "runtime.enqueue_api_text",
            {
                "text": str(text),
                "source": str(source),
                "deliver_to_telegram": bool(deliver_to_telegram),
                "chat_id": chat_id,
                "request_metadata": dict(request_metadata or {}),
                "idempotency_key": idempotency_key,
            },
        )
        if result:
            self._outstanding_request_ids.add(str(result))
        return None if result is None else str(result)

    async def enqueue_startup_bootstrap(self, chat_id: int) -> bool:
        return bool(
            await self._route(
                "runtime.enqueue_startup_bootstrap",
                {"chat_id": int(chat_id)},
            )
        )

    async def deliver_telegram_update(self, update: Mapping[str, Any]) -> bool:
        return bool(
            await self._route(
                "runtime.telegram_update",
                {"update": dict(update)},
            )
        )

    async def enqueue_api_media(
        self,
        local_path: Path,
        media_kind: str,
        filename: str,
        caption: str = "",
        emoji: str = "",
        source: str = "api",
        deliver_to_telegram: bool = True,
        request_metadata: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> str | None:
        result = await self._route(
            "runtime.enqueue_api_media",
            {
                "local_path": str(local_path),
                "media_kind": str(media_kind),
                "filename": str(filename),
                "caption": str(caption),
                "emoji": str(emoji),
                "source": str(source),
                "deliver_to_telegram": bool(deliver_to_telegram),
                "request_metadata": dict(request_metadata or {}),
                "idempotency_key": idempotency_key,
            },
        )
        if result:
            self._outstanding_request_ids.add(str(result))
        return None if result is None else str(result)

    async def _send_text(self, chat_id: int, text: str, **kwargs: Any) -> bool:
        return bool(
            await self._route(
                "runtime.send_text",
                {"chat_id": int(chat_id), "text": str(text), "kwargs": kwargs},
            )
        )

    async def send_long_message(
        self,
        chat_id: int,
        text: str,
        request_id: str | None = None,
        purpose: str = "response",
        parse_mode: str | None = None,
    ) -> tuple[float, int]:
        result = await self._route(
            "runtime.send_long_message",
            {
                "chat_id": int(chat_id),
                "text": str(text),
                "request_id": request_id,
                "purpose": str(purpose),
                "parse_mode": parse_mode,
            },
        )
        values = list(result or (0.0, 0))
        return float(values[0]), int(values[1])

    async def _run_job_now(
        self,
        job: dict[str, Any],
        *,
        kind: str | None = None,
    ) -> tuple[bool, str]:
        result = await self._route(
            "runtime.run_job_now",
            {"job": dict(job), "kind": kind},
        )
        return bool(result[0]), str(result[1])

    async def invoke_scheduler_skill(
        self,
        skill_id: str,
        args: str,
        task_id: str,
        *,
        scheduler_context: Mapping[str, str] | None = None,
    ) -> tuple[bool, str | None]:
        result = await self._route(
            "runtime.invoke_scheduler_skill",
            {
                "skill_id": skill_id,
                "args": args,
                "task_id": task_id,
                "scheduler_context": dict(scheduler_context or {}),
            },
        )
        return bool(result[0]), result[1]

    async def invoke_scheduler_automation(
        self,
        automation_id: str,
        args: str,
        task_id: str,
    ) -> tuple[bool, str | None]:
        result = await self._route(
            "runtime.invoke_scheduler_automation",
            {"automation_id": automation_id, "args": args, "task_id": task_id},
        )
        return bool(result[0]), result[1]

    async def invoke_her_dream(
        self,
        *,
        task_id: str,
        scheduled_for: str | None = None,
    ) -> tuple[bool, str | None]:
        result = await self._route(
            "runtime.invoke_her_dream",
            {"task_id": task_id, "scheduled_for": scheduled_for},
        )
        return bool(result[0]), result[1]

    def export_daily_transcript(self, cutoff_dt: datetime) -> bool:
        # Scheduler calls this synchronous compatibility method.  The worker's
        # transcript is already a shared durable file, so export locally using
        # the stable path while execution remains isolated.
        from orchestrator.transcript_export import export_daily_transcript

        return export_daily_transcript(
            Path(self.transcript_log_path),
            self.workspace_dir / "journals",
            cutoff_dt,
        )

    async def cos_query(self, question: str) -> dict[str, Any]:
        return dict(
            await self._route(
                "runtime.cos_query",
                {"question": str(question)},
            )
            or {}
        )

    async def execute_slash_command(
        self,
        text: str,
        *,
        source_channel: str,
        chat_id: int | str | None = None,
        session_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        result = await self._route(
            "runtime.slash",
            {
                "text": text,
                "source_channel": source_channel,
                "chat_id": chat_id,
                "session_metadata": dict(session_metadata or {}),
            },
        )
        return None if result is None else dict(result)

    async def poll_request_activity(
        self,
        request_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return dict(
            await self._route(
                "runtime.activity.poll",
                {
                    "request_id": request_id,
                    "after_sequence": int(after_sequence),
                    "limit": int(limit),
                },
            )
            or {}
        )

    async def decide_native_voice_transcript(
        self,
        *,
        request_id: str,
        transcript_id: str,
        decision: str,
    ) -> bool:
        return bool(
            await self._route(
                "runtime.native_transcript_decide",
                {
                    "request_id": request_id,
                    "transcript_id": transcript_id,
                    "decision": decision,
                },
            )
        )

    async def set_command_menu(self, *, chat_id: int, locale: str) -> bool:
        return bool(
            await self._route(
                "runtime.set_command_menu",
                {"chat_id": int(chat_id), "locale": locale},
            )
        )

    def register_request_listener(self, request_id: str, callback: Any) -> None:
        key = str(request_id)
        self._request_listeners.setdefault(key, []).append(callback)
        pending = self._pending_request_results.pop(key, None)
        if pending is not None:
            result = callback(pending)
            if inspect.isawaitable(result):
                asyncio.create_task(result)

    def unregister_request_listener(self, request_id: str, callback: Any) -> None:
        key = str(request_id)
        callbacks = self._request_listeners.get(key)
        if not callbacks:
            return
        self._request_listeners[key] = [item for item in callbacks if item is not callback]
        if not self._request_listeners[key]:
            self._request_listeners.pop(key, None)

    async def notify_request_completed(
        self,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        key = str(request_id)
        self._outstanding_request_ids.discard(key)
        callbacks = self._request_listeners.pop(key, [])
        result_payload = dict(payload)
        if not callbacks:
            self._pending_request_results[key] = result_payload
            return
        for callback in callbacks:
            result = callback(result_payload)
            if inspect.isawaitable(result):
                await result

    async def fail_outstanding(self, message: str) -> None:
        for request_id in tuple(self._outstanding_request_ids):
            await self.notify_request_completed(
                request_id,
                {
                    "request_id": request_id,
                    "success": False,
                    "text": None,
                    "error": message,
                    "source": "function-worker",
                    "summary": "Worker interrupted",
                },
            )

    def update_metadata(self, client: FunctionWorkerClient, value: Mapping[str, Any]) -> None:
        if client is self._client:
            self.metadata = dict(value)


class FunctionWorkerSupervisor:
    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self._candidates: set[FunctionWorkerClient] = set()
        self._shutting_down = False
        self._recovery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._qualification_lock = asyncio.Lock()
        self._artifact_lock = asyncio.Lock()
        self._cached_generation: VerifiedFunctionGeneration | None = None
        self._cached_artifact: tuple[str, Path] | None = None
        self._telegram_ingress: dict[str, CoreTelegramIngress] = {}
        self._telegram_status_failures: dict[str, tuple[str, str]] = {}
        self._telegram_status_warning_task: asyncio.Task[Any] | None = None
        self._last_telegram_status_warning: (
            tuple[tuple[tuple[str, str], ...], float] | None
        ) = None

    def qualify_generation(self) -> VerifiedFunctionGeneration:
        return probe_function_generation(self.kernel)

    async def qualified_generation(self) -> VerifiedFunctionGeneration:
        async with self._qualification_lock:
            cached = self._cached_generation
            if cached is not None:
                try:
                    cached.verify_qualified_source(self.kernel.runtime_fingerprint)
                    if not configured_observers_are_qualified(self.kernel, cached):
                        raise ValueError("Enabled observers require a new Function generation")
                except Exception:
                    self._cached_generation = None
                    self._cached_artifact = None
                else:
                    return cached
            disk_cached = await asyncio.to_thread(
                load_qualified_generation_cache,
                self.kernel.paths.bridge_home,
                self.kernel.paths.code_root,
                self.kernel.runtime_fingerprint,
            )
            if disk_cached is not None and configured_observers_are_qualified(self.kernel, disk_cached[0]):
                generation, artifact = disk_cached
                self._cached_generation = generation
                self._cached_artifact = (
                    generation.manifest.generation_id,
                    artifact,
                )
                bridge_logger.info(
                    "Reused qualified Function generation: generation=%s "
                    "modules=%s artifact=%s",
                    generation.manifest.generation_id,
                    len(generation.manifest.entries),
                    artifact,
                )
                return generation
            generation = await asyncio.to_thread(self.qualify_generation)
            self._cached_generation = generation
            self._cached_artifact = None
            return generation

    def remember_generation(self, generation: VerifiedFunctionGeneration) -> None:
        if (
            self._cached_generation is None
            or self._cached_generation.manifest.generation_id
            != generation.manifest.generation_id
        ):
            self._cached_artifact = None
        self._cached_generation = generation

    async def prepare_generation(
        self,
        generation: VerifiedFunctionGeneration | None = None,
    ) -> tuple[VerifiedFunctionGeneration, Path]:
        """Qualify and materialize one generation for any number of Workers."""

        if generation is None:
            generation = await self.qualified_generation()
        else:
            await asyncio.to_thread(
                generation.verify_qualified_source,
                self.kernel.runtime_fingerprint,
            )
        generation_id = generation.manifest.generation_id
        async with self._artifact_lock:
            cached = self._cached_artifact
            if (
                cached is not None
                and cached[0] == generation_id
                and cached[1].exists()
            ):
                artifact = cached[1]
            else:
                artifact = await asyncio.to_thread(
                    materialize_generation_artifact,
                    self.kernel.paths.bridge_home,
                    generation,
                )
                self._cached_artifact = (generation_id, artifact)
            await asyncio.to_thread(
                persist_qualified_generation_cache,
                self.kernel.paths.bridge_home,
                generation,
                artifact,
            )
        return generation, artifact

    async def reconcile_interrupted_session_runs(
        self,
        agent_name: str,
        *,
        lifecycle_reason: str,
    ) -> list[dict[str, Any]]:
        """Fence durable Runs after an Agent executor disappears.

        The replacement Worker repeats this check before activation.  Doing it
        here as well gives Workbench/API clients an immediate terminal record
        while crash recovery is still preparing.  During early boot the
        Workbench store may not exist yet; in that case activation remains the
        fail-closed reconciliation boundary.
        """

        workbench = getattr(self.kernel, "workbench_api", None)
        store = getattr(workbench, "session_store", None)
        reconcile = getattr(store, "reconcile_incomplete_runs", None)
        if not callable(reconcile):
            return []
        reconciled = await asyncio.to_thread(
            reconcile,
            agent_id=str(agent_name),
        )
        if reconciled:
            bridge_logger.warning(
                "%s: reconciled %s interrupted Session Run(s) at %s",
                agent_name,
                len(reconciled),
                lifecycle_reason,
            )
        return list(reconciled)

    def publish_generation_state(self) -> None:
        per_agent = {
            handle.name: {
                "generation_id": handle.generation_id,
                "worker_pid": handle.worker_pid,
                "module_count": int(
                    handle.metadata.get("generation_module_count") or 0
                ),
                "runtime_id": str(handle.metadata.get("runtime_id") or ""),
                "phase": str(handle.metadata.get("worker_phase") or ""),
                "accepting": bool(handle.metadata.get("worker_accepting", False)),
            }
            for handle in self.kernel.runtimes
            if isinstance(handle, AgentRuntimeHandle)
        }
        generations = {value["generation_id"] for value in per_agent.values()}
        aggregate = (
            next(iter(generations))
            if len(generations) == 1
            else "mixed"
            if generations
            else "none"
        )
        self.kernel.function_generation = {
            "generation_id": aggregate,
            "worker_model": "per-agent-process",
            "worker_protocol": FUNCTION_WORKER_PROTOCOL_VERSION,
            "per_agent": per_agent,
            "runtime_id": self.kernel.runtime_fingerprint.runtime_id,
        }
        from orchestrator.runtime_handoff import persist
        persist(self.kernel)

    def topology_snapshot(self, *, agent_name: str | None = None) -> dict[str, Any]:
        runtimes = [runtime.get_runtime_metadata() for runtime in self.kernel.runtimes]
        scheduler_snapshot: dict[str, Any] = {}
        scheduler = getattr(self.kernel, "scheduler", None)
        if scheduler is not None and agent_name:
            delayed_reader = getattr(scheduler, "list_delayed_messages_now", None)
            recovery_builder = getattr(scheduler, "build_recovery_context", None)
            if callable(delayed_reader):
                scheduler_snapshot["delayed_messages"] = list(
                    delayed_reader(agent_name) or ()
                )
            if callable(recovery_builder):
                scheduler_snapshot["recovery_context"] = str(
                    recovery_builder(agent_name) or ""
                )
        service_manager = getattr(self.kernel, "service_manager", None)
        api_gateway: dict[str, Any] = {}
        if service_manager is not None:
            snapshot = getattr(service_manager, "api_gateway_state_snapshot", None)
            if callable(snapshot):
                api_gateway = dict(snapshot())
        endpoint_registry = getattr(self.kernel, "endpoint_registry", None)
        service_endpoints = (
            endpoint_registry.snapshot() if endpoint_registry is not None else {}
        )
        capability_broker = getattr(self.kernel, "capability_broker", None)
        capabilities = (
            capability_broker.status() if capability_broker is not None else {}
        )
        return {
            "runtimes": runtimes,
            "starting": sorted(getattr(self.kernel, "_startup_tasks", {})),
            "whatsapp_connected": bool(getattr(self.kernel, "whatsapp", None)),
            "scheduler": scheduler_snapshot,
            "services": {"api_gateway": api_gateway},
            "service_endpoints": service_endpoints,
            "capabilities": capabilities,
        }

    async def start_telegram_ingress(
        self,
        agent_name: str,
        token: str,
        *,
        drop_pending_updates: bool,
    ) -> bool:
        name = str(agent_name)
        if not str(token or "").strip():
            return False
        existing = self._telegram_ingress.get(name)
        if existing is not None and existing.is_running:
            return True
        if existing is not None:
            await existing.stop()

        ingress = CoreTelegramIngress(
            agent_name=name,
            token=str(token),
            handle_lookup=lambda target: self.kernel._runtime_map().get(target),
            status_callback=lambda connected: self.set_worker_telegram_status(name, connected),
            checkpoint_callback=lambda offset: self._checkpoint_telegram_offset(name, offset),
        )
        offsets = getattr(self.kernel, "_handoff_offsets", {})
        if name in offsets:
            ingress.offset = offsets[name]
            drop_pending_updates = False
        ingress.drop_pending_on_start = drop_pending_updates
        if not getattr(self.kernel, "_handoff_draining", False):
            await ingress.start(drop_pending_updates=drop_pending_updates)
        self._telegram_ingress[name] = ingress
        return True

    def _checkpoint_telegram_offset(self, name: str, offset: int) -> None:
        if getattr(self.kernel, "_shared_committed", False):
            from orchestrator.runtime_handoff import checkpoint_offset
            checkpoint_offset(self.kernel.paths.bridge_home, name, offset)

    async def stop_telegram_ingress(self, agent_name: str) -> None:
        ingress = self._telegram_ingress.pop(str(agent_name), None)
        if ingress is not None:
            await ingress.stop(notify_status=False)

    def telegram_ingress_running(self, agent_name: str) -> bool:
        ingress = self._telegram_ingress.get(str(agent_name))
        return bool(ingress is not None and ingress.is_running)

    def telegram_ingress_snapshot(self, agent_name: str) -> dict[str, Any]:
        ingress = self._telegram_ingress.get(str(agent_name))
        return {
            "configured": ingress is not None,
            "running": bool(ingress is not None and ingress.is_running),
            "connected": bool(ingress is not None and ingress.connected),
            "offset": None if ingress is None else ingress.offset,
        }

    def _queue_telegram_status_warning(self, agent_name: str, exc: Exception) -> None:
        name = str(agent_name)
        self._telegram_status_failures[name] = (type(exc).__name__, str(exc))
        bridge_logger.debug(
            "Function Worker Telegram status propagation failed: agent=%s error=%s: %s",
            name,
            type(exc).__name__,
            exc,
        )
        task = self._telegram_status_warning_task
        if task is None or task.done():
            self._telegram_status_warning_task = asyncio.create_task(
                self._publish_telegram_status_warning(),
                name="telegram-status-warning",
            )

    async def _publish_telegram_status_warning(self) -> None:
        await asyncio.sleep(TELEGRAM_STATUS_WARNING_DEBOUNCE_SECONDS)
        if self._shutting_down or bool(getattr(self.kernel, "is_stopping", False)):
            self._telegram_status_failures.clear()
            return
        failures = dict(self._telegram_status_failures)
        self._telegram_status_failures.clear()
        if not failures:
            return
        names = tuple(sorted(failures))
        signature = tuple(
            sorted((name, details[0]) for name, details in failures.items())
        )
        now = asyncio.get_running_loop().time()
        previous = self._last_telegram_status_warning
        if (
            previous is not None
            and previous[0] == signature
            and now - previous[1] < TELEGRAM_STATUS_WARNING_COOLDOWN_SECONDS
        ):
            return
        self._last_telegram_status_warning = (signature, now)
        global_cfg = getattr(self.kernel, "global_cfg", None)
        instance_id = str(
            getattr(global_cfg, "instance_id", None)
            or getattr(getattr(self.kernel, "paths", None), "instance_id", None)
            or "HASHI"
        ).upper()
        agents = ", ".join(names)
        error_types = ", ".join(
            sorted({details[0] for details in failures.values()})
        )
        message = (
            f"{instance_id} Telegram status synchronisation was interrupted for "
            f"{len(names)} Function Worker(s): {agents}.\n"
            "Cause: Core could not deliver the Telegram connection-state update "
            f"to those Worker IPC channels ({error_types}).\n"
            "Impact: Worker Telegram availability metadata may be temporarily stale. "
            "This status update failure does not itself stop an active task; a disconnected Worker may still affect its own task.\n"
            "System response: Core Telegram ingress keeps its normal retry loop active and will resynchronise status on the next connection transition; Worker recovery is handled separately.\n"
            "Action: no immediate action is required. If this persists, check the function_workers and telegram_ingress fields in /api/health.\n"
            "Diagnostic code: telegram_status_channel_disconnected"
        )
        logger.warning(message)
        bridge_logger.warning(message)

    async def set_worker_telegram_status(
        self,
        agent_name: str,
        connected: bool,
    ) -> None:
        name = str(agent_name)
        handle = self.kernel._runtime_map().get(name)
        if not isinstance(handle, AgentRuntimeHandle):
            return
        if self._shutting_down or bool(getattr(self.kernel, "is_stopping", False)):
            handle.metadata["telegram_connected"] = bool(connected)
            bridge_logger.debug(
                "Skipped Function Worker Telegram status propagation during shutdown: agent=%s connected=%s",
                name,
                bool(connected),
            )
            return
        try:
            client = handle.client
            result = await client.call(
                "worker.telegram_status",
                {"connected": bool(connected)},
                timeout=30.0,
            )
        except Exception as exc:
            self._queue_telegram_status_warning(name, exc)
        else:
            self._telegram_status_failures.pop(name, None)
            metadata = dict(result.get("metadata") or {})
            if metadata:
                handle.update_metadata(client, metadata)
        # Ingress truth still changes when Worker IPC cannot refresh metadata.
        startup = getattr(self.kernel, "startup_manager", None)
        reconcile = getattr(startup, "reconcile_connector_status", None)
        if (callable(reconcile) and getattr(self.kernel, "_shared_committed", False)
                and not getattr(self.kernel, "_handoff_draining", False)
                and not getattr(self.kernel, "_connector_activation_pending", False)):
            reconcile()

    async def prepare_worker(
        self,
        agent_name: str,
        generation: VerifiedFunctionGeneration | None = None,
        *,
        generation_root: Path | None = None,
    ) -> FunctionWorkerClient:
        if generation_root is None:
            generation, artifact = await self.prepare_generation(generation)
        else:
            if generation is None:
                raise FunctionWorkerError(
                    "A reused Function Worker artifact requires its generation receipt"
                )
            artifact = generation_root
        context = multiprocessing.get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=True)
        nonce = uuid4().hex
        bootstrap = {
            "entrypoint": "orchestrator.function_worker_host:run_function_worker",
            "protocol": FUNCTION_WORKER_PROTOCOL_VERSION,
            "protocol_features": [WORKER_LOG_RELAY_FEATURE],
            "nonce": nonce,
            "agent_name": str(agent_name),
            "code_root": str(self.kernel.paths.code_root),
            "bridge_home": str(self.kernel.paths.bridge_home),
            "generation_root": str(artifact),
            "runtime": self.kernel.runtime_fingerprint.to_dict(),
            "manifest": generation.manifest.to_dict(),
            "topology": self.topology_snapshot(agent_name=agent_name),
        }
        process = context.Process(
            target=run_function_worker_process,
            args=(child_connection, bootstrap),
            name=f"hashi-function-{agent_name}-{generation.manifest.generation_id[7:19]}",
            daemon=False,
        )
        process.start()
        child_connection.close()
        client = FunctionWorkerClient(
            supervisor=self,
            agent_name=agent_name,
            generation=generation,
            generation_root=artifact,
            nonce=nonce,
            process=process,
            connection=parent_connection,
        )
        self._candidates.add(client)
        client.start()
        try:
            await client.wait_ready()
        except (Exception, asyncio.CancelledError):
            await client.shutdown(force=True)
            self._candidates.discard(client)
            raise
        return client

    async def activate_new_worker(
        self,
        client: FunctionWorkerClient,
    ) -> dict[str, Any]:
        result = await client.call("worker.activate")
        metadata = dict(result.get("metadata") or {})
        if metadata.get("worker_phase") != "ACTIVE":
            raise FunctionWorkerError(
                f"Worker {client.agent_name!r} activation returned no ACTIVE receipt"
            )
        client.metadata = metadata
        self._candidates.discard(client)
        return metadata

    async def create_active_handle(
        self,
        agent_name: str,
        generation: VerifiedFunctionGeneration | None = None,
        *,
        generation_root: Path | None = None,
    ) -> AgentRuntimeHandle:
        client = await self.prepare_worker(
            agent_name,
            generation,
            generation_root=generation_root,
        )
        try:
            metadata = await self.activate_new_worker(client)
        except Exception:
            await client.shutdown(force=True)
            raise
        return AgentRuntimeHandle(self.kernel, client, metadata)

    async def cutover_handle(
        self,
        handle: AgentRuntimeHandle,
        candidate: FunctionWorkerClient,
        *,
        drain_timeout: float = WORKER_DRAIN_TIMEOUT_SECONDS,
    ) -> tuple[FunctionWorkerClient, dict[str, Any]]:
        old = await handle.begin_cutover()
        old_quiesced = False
        try:
            await old.call(
                "worker.quiesce",
                {"timeout": float(drain_timeout)},
                timeout=float(drain_timeout) + 10.0,
            )
            old_quiesced = True
            await asyncio.to_thread(
                candidate.generation.verify_qualified_source,
                self.kernel.runtime_fingerprint,
            )
            metadata = await self.activate_new_worker(candidate)
            await handle.commit_cutover(candidate, metadata)
            return old, metadata
        except Exception:
            try:
                if old_quiesced and old.process.is_alive():
                    await old.call("worker.resume", timeout=30.0)
            finally:
                await handle.abort_cutover()
                await candidate.shutdown(force=True)
                self._candidates.discard(candidate)
            raise

    async def commit_handles_atomically(
        self,
        assignments: Mapping[
            AgentRuntimeHandle,
            tuple[FunctionWorkerClient, Mapping[str, Any]],
        ],
    ) -> None:
        """Publish every new route before waking any waiting request."""

        prepared = [
            (handle, client, dict(metadata))
            for handle, (client, metadata) in assignments.items()
        ]
        prepared.sort(key=lambda item: item[0].name)
        acquired: list[asyncio.Condition] = []
        try:
            # Acquire every route gate before changing the first pointer.  A
            # cancellation while acquiring therefore leaves every generation
            # untouched.  Once all gates are held, the commit and notifications
            # contain no await point and cannot be observed half-applied.
            for handle, _client, _metadata in prepared:
                await handle._condition.acquire()
                acquired.append(handle._condition)
            for handle, _client, _metadata in prepared:
                if not handle._cutover:
                    raise FunctionWorkerError(
                        f"Agent {handle.name!r} has no active cutover"
                    )

            previous = [
                (
                    handle,
                    handle._client,
                    handle.metadata,
                    handle._offline_error,
                    handle._cutover,
                )
                for handle, _client, _metadata in prepared
            ]
            try:
                for handle, client, metadata in prepared:
                    handle._client = client
                    handle.metadata = metadata
                    handle._offline_error = None
                    handle._cutover = False
            except BaseException:
                for handle, client, metadata, offline_error, cutover in previous:
                    handle._client = client
                    handle.metadata = metadata
                    handle._offline_error = offline_error
                    handle._cutover = cutover
                raise
            for handle, _client, _metadata in prepared:
                handle._condition.notify_all()
        finally:
            for condition in reversed(acquired):
                condition.release()

    async def broadcast_topology(self) -> None:
        calls = []
        for handle in list(self.kernel.runtimes):
            if not isinstance(handle, AgentRuntimeHandle):
                continue
            snapshot = self.topology_snapshot(agent_name=handle.name)
            calls.append(
                handle.client.call(
                    "worker.topology",
                    snapshot,
                    timeout=30.0,
                )
            )
        if calls:
            results = await asyncio.gather(*calls, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    bridge_logger.warning("Function Worker topology refresh failed: %s", result)

    def update_active_metadata(
        self,
        client: FunctionWorkerClient,
        metadata: Mapping[str, Any],
    ) -> None:
        handle = self.kernel._runtime_map().get(client.agent_name)
        if isinstance(handle, AgentRuntimeHandle):
            handle.update_metadata(client, metadata)

    async def handle_worker_event(
        self,
        client: FunctionWorkerClient,
        event: str,
        payload: dict[str, Any],
    ) -> None:
        if event == "worker.log":
            from orchestrator.bootstrap_logging import receive_worker_log

            receive_worker_log(payload)
            return
        if event == "runtime.request_completed":
            handle = self.kernel._runtime_map().get(client.agent_name)
            if isinstance(handle, AgentRuntimeHandle):
                await handle.notify_request_completed(
                    str(payload.get("request_id") or ""),
                    dict(payload.get("result") or {}),
                )
            return
        if event == "core.restart_requested":
            self.kernel.request_restart(
                mode=str(payload.get("mode") or "same"),
                agent_name=str(payload.get("agent_name") or client.agent_name),
                agent_number=(
                    int(payload["agent_number"])
                    if payload.get("agent_number") is not None
                    else None
                ),
            )
            return
        if event == "core.config_changed":
            directory = getattr(self.kernel, "agent_directory", None)
            if directory is not None:
                directory.refresh()
            await self.broadcast_topology()
            return
        raise FunctionWorkerProtocolError(
            f"Unknown event from Worker {client.agent_name!r}: {event}"
        )

    async def handle_worker_request(
        self,
        client: FunctionWorkerClient,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        if method in {"core.reboot.submit", "core.reboot.status"}:
            handle = self.kernel._runtime_map().get(client.agent_name)
            if handle is None or handle.client is not client:
                raise FunctionWorkerProtocolError("Reboot request came from an inactive Worker")
            if method == "core.reboot.status":
                return self.kernel.reboot_manager.latest(actor_id=params.get("actor_id"), chat_id=params.get("chat_id"), thread_id=params.get("thread_id"), surface=params.get("surface") or "telegram")
            allowed = {key: params[key] for key in ("mode", "agent_number", "targets", "origin", "locale", "request_key") if key in params}
            return self.kernel.reboot_manager.submit({**allowed, "agent_name": client.agent_name})
        if method == "core.start_agent":
            return list(await self.kernel.start_agent(str(params["agent_name"])))
        if method == "core.stop_agent":
            return list(
                await self.kernel.stop_agent(
                    str(params["agent_name"]),
                    reason=str(params.get("reason") or "worker-command"),
                )
            )
        if method == "core.whatsapp.start":
            return list(
                await self.kernel.start_whatsapp_transport(
                    persist_enabled=bool(params.get("persist_enabled", True))
                )
            )
        if method == "core.whatsapp.stop":
            return list(
                await self.kernel.stop_whatsapp_transport(
                    persist_enabled=bool(params.get("persist_enabled", True))
                )
            )
        if method == "core.whatsapp.send":
            return list(
                await self.kernel.send_whatsapp_text(
                    str(params["phone_number"]),
                    str(params["text"]),
                )
            )
        if method == "core.cos.query":
            return await self._chief_of_staff_query(client, params)
        if method.startswith("core.service."):
            return await self._service_request(method, params)
        if method.startswith("core.background_jobs."):
            return await self._background_job_request(method, params)
        if method.startswith("core.route."):
            return await self._route_worker_request(method, params)
        if method.startswith("core.scheduler."):
            return await self._scheduler_request(method, params)
        if method.startswith("core.capability."):
            return await self._capability_request(client, method, params)
        raise FunctionWorkerProtocolError(
            f"Unknown Core request from Worker {client.agent_name!r}: {method}"
        )

    async def _capability_request(
        self,
        client: FunctionWorkerClient,
        method: str,
        params: Mapping[str, Any],
    ) -> Any:
        broker = getattr(self.kernel, "capability_broker", None)
        if broker is None:
            raise FunctionWorkerError("Core capability broker is unavailable")
        if method == "core.capability.status":
            return broker.status()
        task_id = str(params.get("task_id") or params.get("request_id") or "").strip()
        if not task_id:
            raise ValueError("capability request task_id is required")
        if method == "core.capability.invoke":
            capability_args = dict(params.get("args") or {})
            authority_roots = getattr(
                self.kernel,
                "agent_authority_roots",
                {},
            )
            authority_root = str(
                authority_roots.get(client.agent_name) or ""
                if isinstance(authority_roots, Mapping)
                else ""
            ).strip()
            if not authority_root:
                handle = self.kernel._runtime_map().get(client.agent_name)
                authority_root = str(
                    getattr(handle, "workspace_dir", "") or ""
                ).strip()
            if not authority_root:
                raise FunctionWorkerError(
                    "Core filesystem authority is unavailable for capability request"
                )
            # A Function Worker may request a path, but it may not widen its
            # own root.  Core replaces any caller-supplied authority metadata.
            capability_args["_authorized_roots"] = [authority_root]
            return await broker.invoke(
                str(params.get("capability_kind") or ""),
                str(params.get("action") or ""),
                capability_args,
                agent_id=client.agent_name,
                task_id=task_id,
                request_id=(
                    str(params["request_id"])
                    if params.get("request_id") is not None
                    else None
                ),
                authorization=str(
                    params.get("authorization") or "tool_registry"
                ),
                timeout_seconds=float(params.get("timeout_seconds") or 60.0),
                lease_id=(
                    str(params["lease_id"])
                    if params.get("lease_id") is not None
                    else None
                ),
            )
        if method == "core.capability.acquire":
            lease = broker.acquire_for_task(
                str(params.get("capability_kind") or ""),
                action=(
                    str(params["action"])
                    if params.get("action") is not None
                    else None
                ),
                agent_id=client.agent_name,
                task_id=task_id,
                window_id=(
                    str(params["window_id"])
                    if params.get("window_id") is not None
                    else None
                ),
                ttl_seconds=float(params.get("ttl_seconds") or 30.0),
            )
            return lease.to_dict()
        if method == "core.capability.handoff":
            lease = broker.handoff(
                str(params.get("source_lease_id") or ""),
                target_capability_kind=str(
                    params.get("target_capability_kind") or ""
                ),
                agent_id=client.agent_name,
                task_id=task_id,
                window_id=(
                    str(params["window_id"])
                    if params.get("window_id") is not None
                    else None
                ),
            )
            return lease.to_dict()
        if method == "core.capability.cancel":
            return broker.cancel_task(
                agent_id=client.agent_name,
                task_id=task_id,
                reason="worker-cancelled",
            )
        if method == "core.capability.release":
            return broker.release_task_lease(
                str(params.get("lease_id") or ""),
                agent_id=client.agent_name,
                task_id=task_id,
                reason="worker-released",
            )
        raise FunctionWorkerProtocolError(f"Unknown Core capability method: {method}")

    async def _service_request(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        manager = getattr(self.kernel, "service_manager", None)
        if manager is None:
            raise FunctionWorkerError("Core service manager is unavailable")
        if method == "core.service.api_gateway.start":
            ok, message = await manager.start_api_gateway_runtime()
        elif method == "core.service.api_gateway.stop":
            ok, message = await manager.stop_api_gateway_runtime()
        elif method == "core.service.api_gateway.model":
            result = manager.set_api_gateway_default_model(str(params.get("model") or ""))
            if inspect.isawaitable(result):
                result = await result
            ok, message = result
        else:
            raise FunctionWorkerProtocolError(f"Unknown Core service method: {method}")
        snapshot = dict(manager.api_gateway_state_snapshot())
        await self.broadcast_topology()
        return {"ok": bool(ok), "message": str(message), "snapshot": snapshot}

    async def _background_job_request(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> Any:
        manager = getattr(self.kernel, "background_job_manager", None)
        if manager is None:
            raise FunctionWorkerError("Core BackgroundJobManager is unavailable")
        if method == "core.background_jobs.start":
            allowed = {
                "agent",
                "cwd",
                "argv",
                "command",
                "shell",
                "origin",
                "notify_on_complete",
                "notify_on_failure",
                "trigger_agent_on_complete",
                "trigger_agent_on_failure",
                "max_stdout_bytes",
                "max_stderr_bytes",
            }
            kwargs = {key: value for key, value in params.items() if key in allowed}
            record = await manager.start_job(**kwargs)
            return record.to_dict()
        if method == "core.background_jobs.list":
            states = params.get("states")
            records = manager.list(
                agent=(str(params["agent"]) if params.get("agent") else None),
                states=(set(str(item) for item in states) if states else None),
                limit=max(1, min(int(params.get("limit") or 50), 1000)),
            )
            return [record.to_dict() for record in records]
        job_id = str(params.get("job_id") or "")
        if not job_id:
            raise ValueError("background job id is required")
        if method == "core.background_jobs.get":
            record = manager.get(job_id)
            return None if record is None else record.to_dict()
        if method == "core.background_jobs.tail":
            return manager.tail(
                job_id,
                stream=str(params.get("stream") or "stdout"),
                lines=max(1, min(int(params.get("lines") or 80), 1000)),
            )
        if method == "core.background_jobs.cancel":
            record = await manager.cancel(
                job_id,
                grace_seconds=max(0.0, float(params.get("grace_seconds") or 2.0)),
            )
            return record.to_dict()
        raise FunctionWorkerProtocolError(
            f"Unknown Core background-job method: {method}"
        )

    async def _chief_of_staff_query(
        self,
        client: FunctionWorkerClient,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        source_agent = str(params.get("source_agent") or client.agent_name)
        question = str(params.get("question") or "").strip()
        if not question:
            return {"answered": False, "response": None, "reason": "empty_question"}
        target = self.kernel._runtime_map().get("lily")
        if not isinstance(target, AgentRuntimeHandle) or not target.startup_success:
            return {"answered": False, "response": None, "reason": "lily_offline"}
        from uuid import uuid4

        cos_id = f"cos-{uuid4().hex[:12]}"
        prompt = (
            f"[cos query from {source_agent}] (ID: {cos_id})\n"
            "An agent needs a decision. Search your memory for precedent.\n"
            "If you find clear precedent, reply with: COS_APPROVED: <your recommendation>\n"
            "If no clear precedent exists, reply with: COS_DECLINED: <reason>\n\n"
            f"Question: {question}"
        )
        request_id = await target.enqueue_api_text(
            prompt,
            source=f"cos-query:{source_agent}",
            deliver_to_telegram=True,
        )
        if not request_id:
            return {"answered": False, "response": None, "reason": "enqueue_failed"}
        future = asyncio.get_running_loop().create_future()

        def completed(payload: Mapping[str, Any]) -> None:
            if not future.done():
                future.set_result(dict(payload))

        target.register_request_listener(request_id, completed)
        try:
            result = await asyncio.wait_for(future, timeout=540.0)
        except asyncio.TimeoutError:
            return {"answered": False, "response": None, "reason": "timeout"}
        finally:
            target.unregister_request_listener(request_id, completed)
        response = str(result.get("text") or "").strip()
        if not result.get("success"):
            return {
                "answered": False,
                "response": response or None,
                "reason": "execution_failed",
            }
        if response.startswith("COS_DECLINED"):
            return {"answered": False, "response": response, "reason": "declined"}
        return {
            "answered": True,
            "response": response,
            "reason": "approved",
            "cos_id": cos_id,
        }

    async def _route_worker_request(self, method: str, params: dict[str, Any]) -> Any:
        target = self.kernel._runtime_map().get(str(params["agent_name"]))
        if target is None:
            raise FunctionWorkerError(f"Target Agent is offline: {params['agent_name']}")
        if method == "core.route.enqueue_api_text":
            return await target.enqueue_api_text(
                str(params.get("text") or ""),
                source=str(params.get("source") or "api"),
                deliver_to_telegram=bool(params.get("deliver_to_telegram", True)),
                chat_id=params.get("chat_id"),
                request_metadata=params.get("request_metadata"),
                idempotency_key=params.get("idempotency_key"),
            )
        if method == "core.route.send_long_message":
            return list(
                await target.send_long_message(
                    int(params["chat_id"]),
                    str(params.get("text") or ""),
                    request_id=params.get("request_id"),
                    purpose=str(params.get("purpose") or "response"),
                    parse_mode=params.get("parse_mode"),
                )
            )
        if method == "core.route.set_command_menu":
            return await target.set_command_menu(
                chat_id=int(params["chat_id"]),
                locale=str(params["locale"]),
            )
        raise FunctionWorkerProtocolError(f"Unknown Agent route method: {method}")

    async def _scheduler_request(self, method: str, params: dict[str, Any]) -> Any:
        scheduler = getattr(self.kernel, "scheduler", None)
        if scheduler is None:
            if method == "core.scheduler.handle_recovery_reply":
                return None
            return []
        agent_name = str(params.get("agent_name") or "")
        if method == "core.scheduler.list_delayed_messages":
            call = getattr(scheduler, "list_delayed_messages", None)
            if not callable(call):
                return []
            result = call(agent_name)
            return await result if inspect.isawaitable(result) else result
        if method == "core.scheduler.cancel_delayed_messages":
            return await scheduler.cancel_delayed_messages(
                agent_name,
                delay_ids=set(str(item) for item in params.get("delay_ids", ())),
            )
        if method == "core.scheduler.handle_recovery_reply":
            return await scheduler.handle_recovery_reply(
                agent_name=agent_name,
                text=str(params.get("text") or ""),
                runtime_map=self.kernel._runtime_map(),
            )
        raise FunctionWorkerProtocolError(f"Unknown scheduler method: {method}")

    async def handle_worker_exit(self, client: FunctionWorkerClient) -> None:
        self._candidates.discard(client)
        capability_broker = getattr(self.kernel, "capability_broker", None)
        if capability_broker is not None:
            capability_broker.cancel_agent(
                client.agent_name,
                reason="function-worker-exited",
            )
        if client.closed or self._shutting_down:
            return
        handle = self.kernel._runtime_map().get(client.agent_name)
        if not isinstance(handle, AgentRuntimeHandle) or handle.client is not client:
            return
        message = (
            f"Function Worker {client.agent_name!r} exited unexpectedly "
            f"(pid={client.pid}, exit={client.process.exitcode})"
        )
        logger.error(message)
        bridge_logger.error(message)
        await handle.fail_outstanding(message)
        try:
            await self.reconcile_interrupted_session_runs(
                client.agent_name,
                lifecycle_reason="function-worker-exit",
            )
        except Exception as exc:
            # Recovery activation retries reconciliation before the new Worker
            # can accept traffic.  Keep recovery alive while surfacing the
            # immediate durable-store failure.
            bridge_logger.exception(
                "%s: Session Run reconciliation failed after Worker exit: %s",
                client.agent_name,
                exc,
            )
        if client.agent_name in self._recovery_tasks:
            return
        task = asyncio.create_task(
            self._recover_active_worker(handle, client),
            name=f"function-worker-recover:{client.agent_name}",
        )
        self._recovery_tasks[client.agent_name] = task
        task.add_done_callback(
            lambda _task, name=client.agent_name: self._recovery_tasks.pop(name, None)
        )

    async def _recover_active_worker(
        self,
        handle: AgentRuntimeHandle,
        failed: FunctionWorkerClient,
    ) -> None:
        try:
            await handle.begin_cutover()
        except Exception as exc:
            bridge_logger.error(
                "Function Worker recovery could not close the route gate for %s: %s",
                handle.name,
                exc,
            )
            return
        for attempt in range(1, WORKER_RECOVERY_ATTEMPTS + 1):
            if self._shutting_down:
                await handle.abort_cutover()
                return
            try:
                candidate = await self.prepare_worker(
                    handle.name,
                    failed.generation,
                    generation_root=failed.generation_root,
                )
                metadata = await self.activate_new_worker(candidate)
                await handle.commit_cutover(candidate, metadata)
                await self.broadcast_topology()
                bridge_logger.info(
                    "Function Worker recovered: agent=%s old_pid=%s new_pid=%s generation=%s",
                    handle.name,
                    failed.pid,
                    candidate.pid,
                    candidate.generation_id,
                )
                return
            except Exception as exc:
                bridge_logger.error(
                    "Function Worker recovery %s/%s failed for %s: %s",
                    attempt,
                    WORKER_RECOVERY_ATTEMPTS,
                    handle.name,
                    exc,
                )
                if attempt < WORKER_RECOVERY_ATTEMPTS:
                    await asyncio.sleep(float(attempt))
        handle.metadata["startup_success"] = False
        handle.metadata["online"] = False
        handle.metadata["worker_phase"] = "FAILED"
        await handle.close_route(
            f"Function Worker {handle.name!r} could not be recovered after "
            f"{WORKER_RECOVERY_ATTEMPTS} attempts"
        )
        self.publish_generation_state()

    async def shutdown_all(self) -> None:
        self._shutting_down = True
        warning_task = self._telegram_status_warning_task
        self._telegram_status_warning_task = None
        if warning_task is not None:
            warning_task.cancel()
            await asyncio.gather(warning_task, return_exceptions=True)
        self._telegram_status_failures.clear()
        ingresses = list(self._telegram_ingress.values())
        self._telegram_ingress.clear()
        if ingresses:
            await asyncio.gather(
                *(ingress.stop(notify_status=False) for ingress in ingresses),
                return_exceptions=True,
            )
        recovery_tasks = list(self._recovery_tasks.values())
        for task in recovery_tasks:
            task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)
        clients = {
            handle.client
            for handle in self.kernel.runtimes
            if isinstance(handle, AgentRuntimeHandle)
        }
        await asyncio.gather(
            *(
                handle.close_route(
                    f"Function Worker {handle.name!r} is stopping with HASHI Core"
                )
                for handle in self.kernel.runtimes
                if isinstance(handle, AgentRuntimeHandle)
            ),
            return_exceptions=True,
        )
        clients.update(self._candidates)
        await asyncio.gather(
            *(client.shutdown(force=True) for client in clients),
            return_exceptions=True,
        )
        self._candidates.clear()
