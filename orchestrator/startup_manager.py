from __future__ import annotations

import asyncio
import importlib
import logging
import time

from orchestrator.bootstrap_logging import AnimMute

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")

# Worker startup is dominated by isolated imports and backend I/O.  A limit of
# two serialized six configured Agents into three avoidable waves.  Eight is a
# bounded default for larger installations while allowing common 4-8 Agent
# instances to prepare in one wave.
INITIAL_AGENT_STARTUP_CONCURRENCY = 8
STARTUP_PROGRESS_HEARTBEAT_SECONDS = 5.0


class StartupManager:
    """Initial agent selection, backend preflight, and startup banner orchestration."""

    def __init__(self, kernel, console_handler):
        self.kernel = kernel
        self.console_handler = console_handler

    async def start_initial_agents(self, global_cfg, agent_configs, secrets) -> tuple[bool, dict]:
        selected_configs = [
            cfg for cfg in agent_configs
            if (self.kernel.selected_agents is not None and cfg.name in self.kernel.selected_agents)
            or (self.kernel.selected_agents is None and cfg.is_active)
        ]

        inactive_agent_names = [
            cfg.name for cfg in agent_configs
            if cfg.name not in [selected.name for selected in selected_configs]
        ]

        if not selected_configs:
            print("\n" + "!" * 64)
            print("  CRITICAL ERROR: No active agents found.")
            print("  Please ensure at least one agent is set to 'is_active: true'")
            print("  in your 'agents.json' file.")
            print("!" * 64 + "\n")
            main_logger.critical("Aborting launch: No active agents configured.")
            return False, {}

        engine_status = self.kernel._check_backend_availability(global_cfg, selected_configs, secrets)
        startable_configs, skipped = self.kernel._partition_agents_by_availability(selected_configs, engine_status)
        initial_agent_names = [cfg.name for cfg in startable_configs]
        bridge_logger.info("Agents to start: %s", initial_agent_names)
        if skipped:
            for name, reason in skipped:
                bridge_logger.warning("Skipping agent '%s': %s", name, reason)

        try:
            _, wa_cfg = self.kernel._load_whatsapp_cfg()
        except Exception:
            wa_cfg = {}

        await self._ensure_remote_lifecycle()

        if not initial_agent_names:
            print("\n" + "=" * 64)
            print("  CRITICAL ERROR: No agents can start.")
            print("  Reason: All backend engines (Gemini, Claude, etc.) are unavailable.")
            print("  Please check your 'secrets.json' for API keys and ensure")
            print("  CLI tools are installed as per the README.")
            print("=" * 64 + "\n")
            main_logger.critical("No agents can start - all backends are unavailable.")
            return False, wa_cfg

        await self._run_startup_banner(
            initial_agent_names,
            global_cfg,
            wa_cfg,
            skipped,
            inactive_agent_names,
        )

        if not self.kernel.runtimes:
            print("\n" + "*" * 64)
            print("  CRITICAL ERROR: All agents failed to connect to Telegram.")
            print("  Please check that:")
            print("  1. Your Bot Tokens in 'secrets.json' are correct.")
            print("  2. Your internet connection is active.")
            print("  3. The Telegram API is not being blocked.")
            print("*" * 64 + "\n")
            main_logger.critical("All agents failed to start. Exiting.")
            return False, wa_cfg

        return True, wa_cfg

    async def _ensure_remote_lifecycle(self) -> None:
        root = getattr(getattr(self.kernel, "global_config", None), "project_root", None)
        try:
            remote_lifecycle = importlib.import_module(
                "orchestrator.remote_lifecycle"
            )
            result = await remote_lifecycle.ensure_remote_started(root)
        except Exception as exc:
            bridge_logger.warning("Hashi Remote lifecycle check failed: %s: %s", type(exc).__name__, exc)
            return
        action = result.get("action")
        settings = result.get("settings")
        port = getattr(settings, "port", "?")
        if result.get("ok"):
            bridge_logger.info("Hashi Remote lifecycle: %s on port %s", action, port)
            process = result.get("process")
            if process is not None:
                setattr(self.kernel, "_remote_lifecycle_process", process)
            return
        bridge_logger.info("Hashi Remote lifecycle: %s (%s)", action, result.get("reason") or "no detail")

    async def _run_startup_banner(self, initial_agent_names, global_cfg, wa_cfg, skipped, inactive_agent_names):
        boot_state = {name: "pending" for name in initial_agent_names}
        boot_reason = {}
        startup_progress = {
            "phase": "qualifying_generation",
            "ready": False,
            "services_ready": False,
            "generation_id": None,
        }
        started = getattr(self.kernel, "_startup_started_monotonic", time.monotonic())
        total = len(initial_agent_names)

        def _publish_progress(*, log: bool = True) -> None:
            ready_agents = sum(
                state in {"online", "local"} for state in boot_state.values()
            )
            failed_agents = sum(
                state == "failed" for state in boot_state.values()
            )
            completed = ready_agents + failed_agents
            agent_percent = round((completed / total) * 100) if total else 100
            generation_ready = bool(startup_progress.get("generation_id"))
            overall_percent = min(
                90,
                (15 if generation_ready else 5)
                + round(75 * (completed / total if total else 1.0)),
            )
            snapshot = {
                **startup_progress,
                "completed": completed,
                "ready_agents": ready_agents,
                "failed_agents": failed_agents,
                "connecting_agents": sum(
                    state == "connecting" for state in boot_state.values()
                ),
                "pending_agents": sum(
                    state == "pending" for state in boot_state.values()
                ),
                "total": total,
                "agent_percent": agent_percent,
                "percent": overall_percent,
                "elapsed_seconds": round(time.monotonic() - started, 1),
            }
            startup_progress.update(snapshot)
            self.kernel.startup_status = dict(snapshot)
            if log:
                bridge_logger.info(
                    "Startup progress: phase=%s overall=%s%% agents=%s/%s (%s%%) "
                    "ready=%s failed=%s connecting=%s pending=%s elapsed=%.1fs",
                    snapshot["phase"],
                    snapshot["percent"],
                    completed,
                    total,
                    agent_percent,
                    ready_agents,
                    failed_agents,
                    snapshot["connecting_agents"],
                    snapshot["pending_agents"],
                    snapshot["elapsed_seconds"],
                )

        async def _prepare_initial_generation():
            _publish_progress()
            prepare = getattr(self.kernel.function_workers, "prepare_generation", None)
            if not callable(prepare):
                startup_progress["phase"] = "starting_workers"
                _publish_progress()
                return None, None
            generation, generation_root = await prepare()
            startup_progress.update(
                {
                    "phase": "starting_workers",
                    "generation_id": generation.manifest.generation_id,
                }
            )
            _publish_progress()
            return generation, generation_root

        prepared_generation_task = asyncio.create_task(
            _prepare_initial_generation(),
            name="boot-function-generation",
        )
        startup_limit = max(1, min(INITIAL_AGENT_STARTUP_CONCURRENCY, len(initial_agent_names) or 1))
        startup_sem = asyncio.Semaphore(startup_limit)

        async def _start_initial_agent(agent_name: str):
            try:
                generation, generation_root = await asyncio.shield(
                    prepared_generation_task
                )
            except Exception as e:
                boot_state[agent_name] = "failed"
                boot_reason[agent_name] = f"{type(e).__name__}: {e}"
                _publish_progress()
                bridge_logger.error(
                    "%s: pending -> failed (generation: %s)",
                    agent_name,
                    e,
                )
                return agent_name, (False, str(e))
            async with startup_sem:
                boot_state[agent_name] = "connecting"
                bridge_logger.info("%s: pending -> connecting", agent_name)
                _publish_progress()
                try:
                    if generation is None:
                        ok, msg = await self.kernel.start_agent(agent_name)
                    else:
                        ok, msg = await self.kernel.start_agent(
                            agent_name,
                            generation=generation,
                            generation_root=generation_root,
                        )
                except Exception as e:
                    main_logger.exception("Unexpected startup error for '%s': %s", agent_name, e)
                    boot_state[agent_name] = "failed"
                    boot_reason[agent_name] = f"{type(e).__name__}: {e}"
                    _publish_progress()
                    bridge_logger.error("%s: connecting -> failed (exception: %s)", agent_name, e)
                    return agent_name, (False, str(e))
                if ok:
                    new_state = "local" if "LOCAL MODE" in msg.upper() else "online"
                    boot_state[agent_name] = new_state
                    if new_state == "local":
                        boot_reason[agent_name] = "Telegram unavailable"
                    bridge_logger.info("%s: connecting -> %s", agent_name, new_state)
                else:
                    boot_state[agent_name] = "failed"
                    boot_reason[agent_name] = msg
                    bridge_logger.error("%s: connecting -> failed (%s)", agent_name, msg)
                _publish_progress()
                return agent_name, (ok, msg)

        async def _progress_heartbeat() -> None:
            while True:
                await asyncio.sleep(STARTUP_PROGRESS_HEARTBEAT_SECONDS)
                if all(
                    state in {"online", "local", "failed"}
                    for state in boot_state.values()
                ):
                    return
                _publish_progress()

        startup_tasks = [
            asyncio.create_task(_start_initial_agent(name), name=f"boot-{name}")
            for name in initial_agent_names
        ]
        progress_heartbeat = asyncio.create_task(
            _progress_heartbeat(),
            name="boot-progress-heartbeat",
        )

        from orchestrator.banner import show_startup_banner

        def _run_banner():
            show_startup_banner(
                agent_names=initial_agent_names,
                boot_state=boot_state,
                workbench_port=global_cfg.workbench_port,
                wa_enabled=bool(wa_cfg.get("enabled")),
                api_gateway_enabled=self.kernel.enable_api_gateway,
                skipped_agents=skipped,
                inactive_agents=inactive_agent_names,
                boot_reason=boot_reason,
                startup_progress=startup_progress,
            )

        mute = AnimMute()
        if self.console_handler is not None:
            self.console_handler.addFilter(mute)
        try:
            await asyncio.gather(
                asyncio.get_running_loop().run_in_executor(None, _run_banner),
                *startup_tasks,
                return_exceptions=True,
            )
        finally:
            progress_heartbeat.cancel()
            await asyncio.gather(progress_heartbeat, return_exceptions=True)
            if self.console_handler is not None:
                self.console_handler.removeFilter(mute)

        startup_progress["phase"] = (
            "agents_ready"
            if all(state in {"online", "local"} for state in boot_state.values())
            else "agents_degraded"
        )
        _publish_progress()

        if skipped:
            for name, reason in skipped:
                main_logger.warning("Skipping agent '%s': %s", name, reason)

        for task in startup_tasks:
            if task.cancelled():
                continue
            try:
                _agent_name, (ok, message) = task.result()
                if not ok:
                    main_logger.error(message)
            except Exception as e:
                main_logger.error("Unexpected error reading startup task result: %s", e)
