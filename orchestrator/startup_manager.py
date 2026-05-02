from __future__ import annotations

import asyncio
import logging

from orchestrator.bootstrap_logging import AnimMute

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")

INITIAL_AGENT_STARTUP_CONCURRENCY = 2


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

    async def _run_startup_banner(self, initial_agent_names, global_cfg, wa_cfg, skipped, inactive_agent_names):
        boot_state = {name: "pending" for name in initial_agent_names}
        boot_reason = {}
        startup_limit = max(1, min(INITIAL_AGENT_STARTUP_CONCURRENCY, len(initial_agent_names) or 1))
        startup_sem = asyncio.Semaphore(startup_limit)

        async def _start_initial_agent(agent_name: str):
            async with startup_sem:
                boot_state[agent_name] = "connecting"
                bridge_logger.info("%s: pending -> connecting", agent_name)
                try:
                    ok, msg = await self.kernel.start_agent(agent_name)
                except Exception as e:
                    main_logger.exception("Unexpected startup error for '%s': %s", agent_name, e)
                    boot_state[agent_name] = "failed"
                    boot_reason[agent_name] = f"{type(e).__name__}: {e}"
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
                return agent_name, (ok, msg)

        startup_tasks = [
            asyncio.create_task(_start_initial_agent(name), name=f"boot-{name}")
            for name in initial_agent_names
        ]

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
            )

        mute = AnimMute()
        self.console_handler.addFilter(mute)
        try:
            await asyncio.gather(
                asyncio.get_running_loop().run_in_executor(None, _run_banner),
                *startup_tasks,
                return_exceptions=True,
            )
        finally:
            self.console_handler.removeFilter(mute)

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
