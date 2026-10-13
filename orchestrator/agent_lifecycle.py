from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from orchestrator.bootstrap_logging import C_OK, C_RESET, C_WARN, C_STOP
from orchestrator.function_worker_supervisor import (
    AgentRuntimeHandle,
    WORKER_DRAIN_TIMEOUT_SECONDS,
    WORKER_READY_TIMEOUT_SECONDS,
)

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")
RUNTIME_TEARDOWN_TIMEOUT_SECONDS = WORKER_DRAIN_TIMEOUT_SECONDS + 35.0
# Allow native Worker readiness and transport overhead on lifecycle API calls.
AGENT_LIFECYCLE_REQUEST_TIMEOUT_SECONDS = max(
    WORKER_READY_TIMEOUT_SECONDS, RUNTIME_TEARDOWN_TIMEOUT_SECONDS
) + 60.0


class AgentLifecycleManager:
    """Start and stop isolated per-Agent Function Workers."""

    def __init__(self, kernel):
        self.kernel = kernel
        self.manually_stopped_agents: set[str] = set()

    async def request_start_agent(
        self,
        agent_name: str,
        *,
        deactivate_on_failure_revision: str | None = None,
    ) -> dict:
        """Admit one start without making an HTTP client wait for qualification."""
        async with self.kernel._lifecycle_lock:
            if getattr(self.kernel, "_handoff_draining", False):
                return {"ok": False, "status": "unavailable", "message": "Shared Functions are draining."}
            if agent_name in self.kernel._runtime_map():
                return {"ok": True, "status": "running", "message": f"Agent '{agent_name}' is running."}
            if agent_name not in self.kernel._startup_tasks:
                task = asyncio.create_task(
                    self._complete_requested_start(
                        agent_name,
                        deactivate_on_failure_revision=deactivate_on_failure_revision,
                    ),
                    name=f"agent-start:{agent_name}",
                )
                self.kernel._startup_tasks[agent_name] = task
                self._project_requested_start(agent_name, "starting", "Starting Function Worker.")
            return {"ok": True, "status": "starting", "message": f"Agent '{agent_name}' is starting."}

    def _project_requested_start(self, name: str, state: str, message: str) -> None:
        # Existing startup status is a projection; _startup_tasks and the Worker
        # registry remain the sole owners of pending and active lifecycles.
        status = dict(getattr(self.kernel, "startup_status", {}) or {})
        states = dict(status.get("agent_states") or {})
        reasons = dict(status.get("agent_reasons") or {})
        states[name] = state
        if state == "online":
            reasons.pop(name, None)
        else:
            reasons[name] = message
        status.update(agent_states=states, agent_reasons=reasons)
        self.kernel.startup_status = status

    async def _complete_requested_start(
        self,
        agent_name: str,
        *,
        deactivate_on_failure_revision: str | None = None,
    ) -> None:
        cancelled: asyncio.CancelledError | None = None
        try:
            ok, message = await self.start_agent(
                agent_name,
                _retain_startup_task=True,
            )
        except asyncio.CancelledError as exc:
            ok, message = False, "Agent startup was interrupted."
            cancelled = exc
        except Exception as exc:
            ok, message = False, f"Agent startup failed: {type(exc).__name__}: {exc}"
            main_logger.exception("Requested Agent startup failed: %s", agent_name)
        if not ok and deactivate_on_failure_revision is not None:
            try:
                self.kernel.config_admin.set_agent_active(
                    agent_name,
                    False,
                    expected_revision=deactivate_on_failure_revision,
                    allow_last_active_deactivation=True,
                )
            except Exception as exc:
                main_logger.error(
                    "Could not roll back failed newly-created Agent %s: %s: %s",
                    agent_name,
                    type(exc).__name__,
                    exc,
                )
                message = f"{message} Inactive-state publication needs reconciliation."
        try:
            self._project_requested_start(agent_name, "online" if ok else "failed", message)
            startup = getattr(self.kernel, "startup_manager", None)
            reconcile = getattr(startup, "reconcile_connector_status", None)
            if callable(reconcile):
                reconcile()
        finally:
            async with self.kernel._lifecycle_lock:
                if self.kernel._startup_tasks.get(agent_name) is asyncio.current_task():
                    self.kernel._startup_tasks.pop(agent_name, None)
        if cancelled is not None:
            raise cancelled

    async def start_agent(
        self,
        agent_name: str,
        *,
        generation=None,
        generation_root: Path | None = None,
        _retain_startup_task: bool = False,
    ) -> tuple[bool, str]:
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("start_agent() must run inside an asyncio task.")

        async with self.kernel._lifecycle_lock:
            if agent_name in self.kernel._runtime_map():
                return False, f"Agent '{agent_name}' is already running."
            if agent_name in self.kernel._startup_tasks and self.kernel._startup_tasks[agent_name] is not current_task:
                return False, f"Agent '{agent_name}' is already starting."
            self.kernel._startup_tasks[agent_name] = current_task

        agent_lock = self.kernel._agent_lock(agent_name)
        try:
            async with agent_lock:
                async with self.kernel._lifecycle_lock:
                    if agent_name in self.kernel._runtime_map():
                        return False, f"Agent '{agent_name}' is already running."
                    try:
                        global_cfg, agent_configs, loaded_secrets = (
                            self.kernel._load_config_bundle()
                        )
                    except Exception as exc:
                        return False, f"Failed to load configuration: {exc}"
                    agent_cfg = next(
                        (cfg for cfg in agent_configs if cfg.name == agent_name),
                        None,
                    )
                    if agent_cfg is None:
                        return False, f"Agent '{agent_name}' is not configured."

                try:
                    if generation is None and generation_root is None:
                        handle = await self.kernel.function_workers.create_active_handle(
                            agent_name
                        )
                    else:
                        handle = await self.kernel.function_workers.create_active_handle(
                            agent_name,
                            generation,
                            generation_root=generation_root,
                        )
                except Exception as exc:
                    message = (
                        f"Failed to initialize Function Worker for '{agent_name}': "
                        f"{type(exc).__name__}: {exc}"
                    )
                    main_logger.exception(message)
                    bridge_logger.exception(message)
                    return False, message

                async with self.kernel._lifecycle_lock:
                    if agent_name in self.kernel._runtime_map():
                        await handle.client.shutdown(force=True)
                        return False, f"Agent '{agent_name}' started concurrently."
                    self.kernel.runtimes.append(handle)
                    self.manually_stopped_agents.discard(agent_name)
                    self.kernel.function_workers.publish_generation_state()

                token = str(loaded_secrets.get(agent_cfg.telegram_token_key) or "").strip()
                if token:
                    try:
                        await self.kernel.function_workers.start_telegram_ingress(
                            agent_name,
                            token,
                            drop_pending_updates=False,
                        )
                    except Exception as exc:
                        bridge_logger.warning(
                            "Core Telegram ingress failed for %s: %s",
                            agent_name,
                            exc,
                        )
                        await self.kernel.function_workers.set_worker_telegram_status(
                            agent_name,
                            False,
                        )
                await self.kernel.function_workers.broadcast_topology()

                if handle.telegram_connected and (
                    self.kernel.function_workers.telegram_ingress_running(
                        agent_name
                    )
                ):
                    try:
                        await handle.enqueue_startup_bootstrap(
                            global_cfg.authorized_id
                        )
                    except Exception as exc:
                        bridge_logger.warning(
                            "Startup bootstrap failed for %s: %s",
                            agent_name,
                            exc,
                        )
                    message = f"Started agent '{agent_name}'."
                    bridge_logger.info(
                        "%s: ONLINE in Function Worker pid=%s generation=%s",
                        agent_name,
                        handle.worker_pid,
                        handle.generation_id,
                    )
                else:
                    if self.kernel.whatsapp is not None:
                        await self.kernel._send_whatsapp_startup_notification(handle)
                    message = (
                        f"Started '{agent_name}' in LOCAL MODE "
                        "(Workbench + WhatsApp only)."
                    )
                    bridge_logger.info(
                        "%s: LOCAL MODE in Function Worker pid=%s generation=%s",
                        agent_name,
                        handle.worker_pid,
                        handle.generation_id,
                    )
                return True, message
        finally:
            async with self.kernel._lifecycle_lock:
                if (
                    not _retain_startup_task
                    and self.kernel._startup_tasks.get(agent_name) is current_task
                ):
                    self.kernel._startup_tasks.pop(agent_name, None)

    async def stop_agent(
        self,
        agent_name: str,
        reason: str = "manual-stop",
    ) -> tuple[bool, str]:
        agent_lock = self.kernel._agent_lock(agent_name)
        async with agent_lock:
            async with self.kernel._lifecycle_lock:
                if agent_name in self.kernel._startup_tasks:
                    return False, f"Agent '{agent_name}' is still starting."
                runtime = self.kernel._runtime_map().get(agent_name)
            if runtime is None:
                return False, f"Agent '{agent_name}' is not running."
            if not isinstance(runtime, AgentRuntimeHandle):
                return False, (
                    f"Agent '{agent_name}' is not running in an isolated Function Worker."
                )

            bridge_logger.info(
                "Stopping Function Worker agent=%s pid=%s reason=%s",
                agent_name,
                runtime.worker_pid,
                reason,
            )
            try:
                client = await runtime.begin_cutover()
                try:
                    await client.call(
                        "worker.quiesce",
                        {"timeout": WORKER_DRAIN_TIMEOUT_SECONDS},
                        timeout=WORKER_DRAIN_TIMEOUT_SECONDS + 10.0,
                    )
                except Exception:
                    await runtime.abort_cutover()
                    raise
                await self.kernel.function_workers.stop_telegram_ingress(
                    agent_name
                )
            except Exception as exc:
                message = (
                    f"Agent '{agent_name}' did not quiesce; it remains registered: "
                    f"{type(exc).__name__}: {exc}"
                )
                main_logger.error(message)
                bridge_logger.error(message)
                return False, message

            async with self.kernel._lifecycle_lock:
                self.kernel.runtimes[:] = [
                    item for item in self.kernel.runtimes if item is not runtime
                ]
                self.kernel.function_workers.publish_generation_state()
            await runtime.close_route(f"Agent {agent_name!r} was stopped")
            await self.kernel.function_workers.broadcast_topology()
            await client.shutdown(force=True)
            if reason in {"manual-stop", "worker-command"}:
                self.manually_stopped_agents.add(agent_name)
            from orchestrator import runtime_handoff

            try:
                runtime_handoff.persist(self.kernel)
            except Exception:
                bridge_logger.exception(
                    "Could not checkpoint manually stopped agent %s", agent_name
                )
            main_logger.info("Agent '%s' stopped.", agent_name)
            bridge_logger.info(
                "Agent '%s' Function Worker stopped (reason=%s)",
                agent_name,
                reason,
            )
            print(
                f"{C_STOP}[system] Agent '{agent_name}' stopped{C_RESET}",
                flush=True,
            )
            return True, f"Stopped agent '{agent_name}'."

    async def teardown_runtime(
        self,
        runtime: AgentRuntimeHandle,
        timeout: float | None = None,
    ) -> bool:
        timeout = float(timeout or RUNTIME_TEARDOWN_TIMEOUT_SECONDS)
        if not isinstance(runtime, AgentRuntimeHandle):
            return False
        try:
            client = await runtime.begin_cutover()
            try:
                await client.call(
                    "worker.quiesce",
                    {"timeout": max(0.1, timeout - 35.0)},
                    timeout=max(1.0, timeout - 25.0),
                )
            except Exception:
                await runtime.abort_cutover()
                raise
            await self.kernel.function_workers.stop_telegram_ingress(runtime.name)
            await client.shutdown(force=True)
            await runtime.close_route(f"Agent {runtime.name!r} was stopped")
            return not client.process.is_alive()
        except Exception as exc:
            main_logger.warning(
                "Function Worker shutdown warning for '%s': %s",
                runtime.name,
                exc,
            )
            bridge_logger.warning(
                "Function Worker shutdown warning for '%s': %s: %s",
                runtime.name,
                type(exc).__name__,
                exc,
            )
            return False

    async def shutdown_all_agents(self, timeout: float = 180.0):
        agents = list(self.kernel.runtimes)
        if not agents:
            self.kernel.last_shutdown_summary = {
                "requested_agents": 0,
                "stopped_agents": 0,
                "complete": True,
            }
            return
        main_logger.info(
            "Shutting down %s isolated Function Workers...", len(agents)
        )
        bridge_logger.info(
            "Shutting down %s isolated Function Workers", len(agents)
        )
        shutdown_complete = True
        try:
            await asyncio.wait_for(
                self.kernel.function_workers.shutdown_all(),
                timeout=max(1.0, float(timeout)),
            )
        except asyncio.TimeoutError:
            shutdown_complete = False
            main_logger.error(
                "Function Worker shutdown exceeded %.1fs", float(timeout)
            )
        except Exception as exc:
            shutdown_complete = False
            main_logger.error(
                "Function Worker shutdown failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            bridge_logger.exception("Function Worker shutdown failed")
        for runtime in agents:
            await runtime.close_route(
                f"Agent {runtime.name!r} is stopping with HASHI Core"
            )
        stopped_agents = sum(
            not runtime.client.process.is_alive()
            for runtime in agents
            if isinstance(runtime, AgentRuntimeHandle)
        )
        shutdown_complete = bool(
            shutdown_complete and stopped_agents == len(agents)
        )
        summary = {
            "requested_agents": len(agents),
            "stopped_agents": stopped_agents,
            "complete": shutdown_complete,
        }
        self.kernel.last_shutdown_summary = summary
        self.kernel.runtimes.clear()
        self.kernel.function_workers.publish_generation_state()
        global_cfg = getattr(self.kernel, "global_cfg", None)
        instance_id = str(
            getattr(global_cfg, "instance_id", None)
            or getattr(getattr(self.kernel, "paths", None), "instance_id", None)
            or "HASHI"
        ).upper()
        if shutdown_complete:
            print(
                f"{C_OK}[system] {instance_id} shut down normally · "
                f"{stopped_agents}/{len(agents)} agents stopped.{C_RESET}",
                flush=True,
            )
        else:
            print(
                f"{C_WARN}[system] {instance_id} shutdown incomplete · "
                f"{stopped_agents}/{len(agents)} agents stopped. "
                f"Review the preceding shutdown errors.{C_RESET}",
                flush=True,
            )
