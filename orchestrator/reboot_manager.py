from __future__ import annotations

import asyncio
import importlib
import logging
import sys

from orchestrator.bootstrap_logging import AnimMute

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")


class RebootManager:
    """Hot-restart orchestration for the live kernel."""

    def __init__(self, kernel, console_handler):
        self.kernel = kernel
        self.console_handler = console_handler

    def rebuild_hot_managers(self):
        """Transactionally rebuild hot-reloadable managers after module reload."""
        config_cls = sys.modules["orchestrator.config_admin"].ConfigAdmin
        preflight_cls = sys.modules["orchestrator.backend_preflight"].BackendPreflight
        lifecycle_cls = sys.modules["orchestrator.agent_lifecycle"].AgentLifecycleManager
        service_cls = sys.modules["orchestrator.service_manager"].ServiceManager
        reboot_cls = sys.modules["orchestrator.reboot_manager"].RebootManager
        whatsapp_cls = sys.modules["orchestrator.whatsapp_manager"].WhatsAppManager

        new_config_admin = config_cls(self.kernel.paths)
        new_backend_preflight = preflight_cls()
        new_agent_lifecycle = lifecycle_cls(self.kernel)
        new_service_manager = service_cls(self.kernel)
        new_reboot_manager = reboot_cls(self.kernel, self.console_handler)
        new_whatsapp_manager = whatsapp_cls(self.kernel)

        self.kernel.config_admin = new_config_admin
        self.kernel.backend_preflight = new_backend_preflight
        self.kernel.agent_lifecycle = new_agent_lifecycle
        self.kernel.service_manager = new_service_manager
        self.kernel.reboot_manager = new_reboot_manager
        self.kernel.whatsapp_manager = new_whatsapp_manager
        main_logger.info(
            "Hot reload: rebuilt config, backend preflight, agent lifecycle, service, reboot, and WhatsApp managers."
        )

    def reload_project_modules(self):
        """Reload project Python modules so hot restart picks up code changes."""
        prefixes = ("adapters.", "orchestrator.")

        def _reload_key(name: str):
            if name.startswith("adapters."):
                return (0, name)
            if "_runtime" in name:
                return (2, name)
            return (1, name)

        to_reload = sorted(
            (name for name in list(sys.modules) if any(name.startswith(prefix) for prefix in prefixes)),
            key=_reload_key,
        )
        reloaded = []
        for name in to_reload:
            module = sys.modules.get(name)
            if module is None:
                continue
            try:
                importlib.reload(module)
                reloaded.append(name)
            except Exception as e:
                main_logger.warning("Hot reload failed for %s: %s", name, e)
        if reloaded:
            main_logger.info("Hot reload: reloaded %s modules.", len(reloaded))

    async def hot_restart(self, restart: dict):
        """Stop agents per restart mode, reload Python code and config, then start agents."""
        mode = restart.get("mode", "same")
        requesting_agent = restart.get("agent_name")
        agent_number = restart.get("agent_number")

        if mode == "min" and requesting_agent:
            targets = [requesting_agent]
        elif mode == "number" and agent_number is not None:
            all_names = self.kernel.configured_agent_names()
            idx = agent_number - 1
            if 0 <= idx < len(all_names):
                targets = [all_names[idx]]
            else:
                main_logger.warning("Restart: invalid agent number %s, restarting all.", agent_number)
                targets = [rt.name for rt in self.kernel.runtimes]
        elif mode == "max":
            targets = [rt.name for rt in self.kernel.runtimes]
        else:
            targets = [rt.name for rt in self.kernel.runtimes]

        boot_state = {name: "pending" for name in targets}
        boot_reason = {}

        main_logger.info("Hot restart: stopping %s agent(s): %s", len(targets), targets)
        bridge_logger.warning(
            "Hot restart begin (mode=%s, requester=%s, number=%s, targets=%s)",
            mode,
            requesting_agent or "-",
            agent_number if agent_number is not None else "-",
            targets,
        )
        for name in targets:
            try:
                await self.kernel.stop_agent(name, reason=f"hot-restart:{mode}")
            except Exception as e:
                main_logger.warning("Hot restart: failed to stop '%s': %s", name, e)
                bridge_logger.warning("Hot restart failed to stop '%s': %s: %s", name, type(e).__name__, e)

        self.reload_project_modules()
        self.rebuild_hot_managers()

        main_logger.info("Hot restart: starting agents: %s", targets)
        try:
            _, agent_configs, _ = self.kernel._load_config_bundle()
            active_config_names = {cfg.name for cfg in agent_configs}
            newly_inactive = [name for name in targets if name not in active_config_names]
            targets = [name for name in targets if name in active_config_names]
            for name in newly_inactive:
                boot_state.pop(name, None)
            inactive_agent_names = [cfg.name for cfg in agent_configs if cfg.name not in targets] + newly_inactive
        except Exception as e:
            main_logger.error("Hot restart: config reload failed: %s", e)
            inactive_agent_names = []

        from orchestrator.banner import show_startup_banner

        loop = asyncio.get_running_loop()
        wa_enabled = self.kernel.whatsapp is not None
        workbench_port = getattr(self.kernel.global_cfg, "workbench_port", None) if self.kernel.global_cfg else None
        api_gw = self.kernel.api_gateway is not None

        async def _start_agent_with_state(name):
            boot_state[name] = "connecting"
            try:
                ok, msg = await self.kernel.start_agent(name)
                if ok:
                    new_state = "local" if "LOCAL MODE" in msg.upper() else "online"
                    boot_state[name] = new_state
                    if new_state == "local":
                        boot_reason[name] = "Telegram unavailable"
                else:
                    boot_state[name] = "failed"
                    boot_reason[name] = msg
                    main_logger.error("Hot restart: %s", msg)
            except Exception as e:
                boot_state[name] = "failed"
                boot_reason[name] = f"{type(e).__name__}: {e}"
                main_logger.error("Hot restart: failed to start '%s': %s", name, e)

        def _run_banner():
            show_startup_banner(
                agent_names=targets,
                boot_state=boot_state,
                workbench_port=workbench_port,
                wa_enabled=wa_enabled,
                api_gateway_enabled=api_gw,
                inactive_agents=inactive_agent_names,
                boot_reason=boot_reason,
            )

        mute = AnimMute()
        self.console_handler.addFilter(mute)
        try:
            await asyncio.gather(
                loop.run_in_executor(None, _run_banner),
                *[_start_agent_with_state(name) for name in targets],
                return_exceptions=True,
            )
        finally:
            self.console_handler.removeFilter(mute)

        await self.kernel.service_manager.restart_scheduler()

        if self.kernel.runtimes:
            main_logger.info("Hot restart complete. %s agent(s) running.", len(self.kernel.runtimes))
            bridge_logger.warning("Hot restart complete (%s agent(s) running)", len(self.kernel.runtimes))
            print(f"\033[38;5;108m  ✓ reboot complete — {len(self.kernel.runtimes)} agent(s) online\033[0m\n", flush=True)
        else:
            main_logger.critical("Hot restart: no agents running after restart.")
            bridge_logger.critical("Hot restart failed: no agents running after restart")
            print("\033[38;5;203m  ✗ reboot failed — no agents running\033[0m\n", flush=True)
