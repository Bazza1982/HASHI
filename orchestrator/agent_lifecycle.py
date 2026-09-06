from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from orchestrator.bootstrap_logging import C_OK, C_RESET, C_WARN, C_STOP
from orchestrator.function_worker_supervisor import (
    AgentRuntimeHandle,
    WORKER_DRAIN_TIMEOUT_SECONDS,
)

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")
RUNTIME_TEARDOWN_TIMEOUT_SECONDS = WORKER_DRAIN_TIMEOUT_SECONDS + 35.0


class AgentLifecycleManager:
    """Start and stop isolated per-Agent Function Workers."""

    def __init__(self, kernel):
        self.kernel = kernel

    async def start_agent(
        self,
        agent_name: str,
        *,
        generation=None,
        generation_root: Path | None = None,
    ) -> tuple[bool, str]:
        current_task = asyncio.current_task()
        if current_task is None:
            raise RuntimeError("start_agent() must run inside an asyncio task.")

        async with self.kernel._lifecycle_lock:
            if agent_name in self.kernel._runtime_map():
                return False, f"Agent '{agent_name}' is already running."
            if agent_name in self.kernel._startup_tasks:
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
                    self.kernel.function_workers.publish_generation_state()

                if handle.telegram_connected:
                    token = str(
                        loaded_secrets.get(agent_cfg.telegram_token_key) or ""
                    )
                    try:
                        await self.kernel.function_workers.start_telegram_ingress(
                            agent_name,
                            token,
                            drop_pending_updates=True,
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
                if self.kernel._startup_tasks.get(agent_name) is current_task:
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
