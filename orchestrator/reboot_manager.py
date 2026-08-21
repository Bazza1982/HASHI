from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import sys

from orchestrator.bootstrap_logging import AnimMute
from orchestrator.hot_reload import (
    HotReloadError,
    detect_loaded_class_interface_changes,
    discover_loaded_project_modules,
    preflight_module_sources,
)

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")
AGENT_STOP_TIMEOUT_SECONDS = 25.0
AGENT_RESTORE_TIMEOUT_SECONDS = 60.0


def _consume_operation_task_result(task: asyncio.Future) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


class RebootManager:
    """Hot-restart orchestration for the live kernel."""

    def __init__(self, kernel, console_handler):
        self.kernel = kernel
        self.console_handler = console_handler

    async def _bounded_operation(self, awaitable, *, timeout_s: float, label: str):
        task = asyncio.create_task(awaitable, name=label)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout_s)
        except asyncio.CancelledError:
            task.cancel()
            if not task.done():
                task.add_done_callback(_consume_operation_task_result)
            raise
        if task not in done:
            task.cancel()
            task.add_done_callback(_consume_operation_task_result)
            return False, None, TimeoutError(
                f"{label} exceeded its {timeout_s:.1f}s deadline"
            )
        try:
            return True, task.result(), None
        except Exception as exc:
            return True, None, exc

    async def _restore_stopped_agents(self, names: list[str]) -> None:
        async def _restore(name: str) -> None:
            _completed, result, error = await self._bounded_operation(
                self.kernel.start_agent(name),
                timeout_s=AGENT_RESTORE_TIMEOUT_SECONDS,
                label=f"restore-agent-{name}",
            )
            if error is not None:
                main_logger.critical(
                    "Hot restart rollback could not restore '%s': %s",
                    name,
                    error,
                )
                bridge_logger.critical(
                    "Hot restart rollback could not restore '%s': %s",
                    name,
                    error,
                )
                return
            ok, message = result
            if not ok:
                main_logger.critical(
                    "Hot restart rollback could not restore '%s': %s",
                    name,
                    message,
                )
                bridge_logger.critical(
                    "Hot restart rollback could not restore '%s': %s",
                    name,
                    message,
                )

        if names:
            main_logger.warning(
                "Hot restart: restoring already-stopped agents without reloading code: %s",
                names,
            )
            await asyncio.gather(*[_restore(name) for name in names])

    def rebuild_hot_managers(self):
        """Transactionally rebuild hot-reloadable managers after module reload."""
        registry = importlib.import_module("orchestrator.manager_registry")
        bundle = registry.build_hot_manager_bundle(self.kernel, self.console_handler)
        registry.install_hot_manager_bundle(self.kernel, bundle)
        main_logger.info(
            "Hot reload: rebuilt skill, config, backend preflight, agent lifecycle, service, reboot, shutdown, startup, and WhatsApp managers."
        )

    def preflight_project_modules(self) -> list[str]:
        code_root = getattr(getattr(self.kernel, "paths", None), "code_root", None)
        module_names = discover_loaded_project_modules(code_root=code_root)
        if code_root is not None:
            checked = preflight_module_sources(module_names, code_root=code_root)
            main_logger.info("Hot reload preflight: compiled %s source files.", len(checked))
        return module_names

    def reload_project_modules(self, module_names: list[str] | None = None):
        """Reload project Python modules so hot restart picks up code changes."""
        to_reload = (
            module_names
            if module_names is not None
            else discover_loaded_project_modules()
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
                raise HotReloadError(
                    f"Hot reload failed after {len(reloaded)} modules while reloading {name}: "
                    f"{type(e).__name__}: {e}"
                ) from e
        if reloaded:
            main_logger.info("Hot reload: reloaded %s modules.", len(reloaded))

    def validate_agent_runtime_contract(self):
        """Verify cross-module adapter symbols before rebuilding an agent.

        Compilation alone cannot detect an import-time dependency being
        satisfied by an older in-memory module.  Keep this check small and
        focused on the protocol used by every HER acknowledgement/tool event.
        """
        stream_events = importlib.import_module("adapters.stream_events")
        backend_registry = importlib.import_module("adapters.registry")
        her_v2 = importlib.import_module("adapters.her_v2")
        runtime_pipeline = importlib.import_module("orchestrator.runtime_pipeline")
        runtime_common = importlib.import_module("orchestrator.runtime_common")
        flexible_runtime = importlib.import_module(
            "orchestrator.flexible_agent_runtime"
        )
        tool_registry = importlib.import_module("tools.registry")
        gateway_context = importlib.import_module("tools.gateway.context")

        acknowledgement_kind = getattr(stream_events, "KIND_ACKNOWLEDGEMENT", None)
        if acknowledgement_kind != "acknowledgement":
            raise HotReloadError(
                "Hot reload contract failed: adapters.stream_events does not expose "
                "KIND_ACKNOWLEDGEMENT='acknowledgement'"
            )
        resolver = getattr(backend_registry, "get_backend_class", None)
        supported_adapter = getattr(her_v2, "HERv2Adapter", None)
        if not callable(resolver) or supported_adapter is None:
            raise HotReloadError(
                "Hot reload contract failed: HER v2 registry contract unavailable"
            )
        if any(
            resolver(engine) is not supported_adapter
            for engine in ("her-v2", "her", "claw-cli")
        ):
            raise HotReloadError(
                "Hot reload contract failed: a HER ID can reach a stale or retired adapter"
            )
        if not callable(getattr(runtime_pipeline, "setup_interactive_feedback", None)):
            raise HotReloadError(
                "Hot reload contract failed: runtime acknowledgement pipeline unavailable"
            )
        queued_request = getattr(runtime_common, "QueuedRequest", None)
        enqueue_request = getattr(
            getattr(flexible_runtime, "FlexibleAgentRuntime", None),
            "enqueue_request",
            None,
        )
        queued_fields = getattr(queued_request, "__dataclass_fields__", {})
        enqueue_parameters = (
            inspect.signature(enqueue_request).parameters
            if callable(enqueue_request)
            else {}
        )
        if (
            "habit_learning_eligible" not in queued_fields
            or "habit_learning_eligible" not in enqueue_parameters
        ):
            raise HotReloadError(
                "Hot reload contract failed: habit learning request intake is incomplete"
            )
        if getattr(flexible_runtime, "QueuedRequest", None) is not queued_request:
            raise HotReloadError(
                "Hot reload contract failed: flexible runtime retained a stale "
                "QueuedRequest class"
            )
        if getattr(gateway_context, "ToolRegistry", None) is not getattr(
            tool_registry, "ToolRegistry", None
        ):
            raise HotReloadError(
                "Hot reload contract failed: tools.gateway.context retained a stale "
                "ToolRegistry class"
            )
        if not callable(
            getattr(
                getattr(tool_registry, "ToolRegistry", None),
                "execute_with_audit_context",
                None,
            )
        ):
            raise HotReloadError(
                "Hot reload contract failed: ToolRegistry scoped audit context "
                "is unavailable"
            )
        contract_message = (
            "Hot reload contract verified: HER compatibility facade and runtime pipeline are current."
        )
        main_logger.info(contract_message)
        bridge_logger.info(contract_message)

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

        try:
            module_names = self.preflight_project_modules()
            class_interface_changes = detect_loaded_class_interface_changes(
                module_names
            )
        except HotReloadError as exc:
            main_logger.error("%s", exc)
            bridge_logger.error("Hot restart preflight rejected: %s", exc)
            print(
                "\033[38;5;203m  ✗ reboot rejected — source preflight failed; "
                "running agents were not touched\033[0m\n",
                flush=True,
            )
            return False

        running_names = [runtime.name for runtime in self.kernel.runtimes]
        targeted_mode = mode in {"min", "number"}
        targets_are_running = bool(targets) and all(
            name in running_names for name in targets
        )
        if (
            targeted_mode
            and targets_are_running
            and class_interface_changes
            and targets != running_names
        ):
            original_targets = list(targets)
            targets = running_names
            change_preview = ", ".join(class_interface_changes[:8])
            if len(class_interface_changes) > 8:
                change_preview += (
                    f", +{len(class_interface_changes) - 8} more"
                )
            promotion_message = (
                "Hot restart: promoted targeted %s reboot from %s to all running "
                "agents before reload because loaded class interfaces changed: %s"
            )
            main_logger.warning(
                promotion_message,
                mode,
                original_targets,
                change_preview,
            )
            bridge_logger.warning(
                promotion_message,
                mode,
                original_targets,
                change_preview,
            )

        main_logger.info("Hot restart: stopping %s agent(s): %s", len(targets), targets)
        bridge_logger.warning(
            "Hot restart begin (mode=%s, requester=%s, number=%s, targets=%s)",
            mode,
            requesting_agent or "-",
            agent_number if agent_number is not None else "-",
            targets,
        )
        stopped_targets = []
        for name in targets:
            _completed, result, error = await self._bounded_operation(
                self.kernel.stop_agent(name, reason=f"hot-restart:{mode}"),
                timeout_s=AGENT_STOP_TIMEOUT_SECONDS,
                label=f"stop-agent-{name}",
            )
            if error is None:
                ok, message = result
                if ok:
                    stopped_targets.append(name)
                    continue
                error = RuntimeError(message)
            main_logger.error("Hot restart: failed to stop '%s': %s", name, error)
            bridge_logger.error("Hot restart failed to stop '%s': %s", name, error)
            await self._restore_stopped_agents(stopped_targets)
            print(
                "\033[38;5;203m  ✗ reboot aborted — an agent did not stop; "
                "resolve the active operation and retry /reboot\033[0m\n",
                flush=True,
            )
            return False

        reload_error: HotReloadError | None = None
        try:
            self.reload_project_modules(module_names)
            self.validate_agent_runtime_contract()
            self.rebuild_hot_managers()
        except HotReloadError as exc:
            reload_error = exc
            main_logger.critical("%s", exc)
            bridge_logger.critical("Hot restart reload failed: %s", exc)
        except Exception as exc:
            reload_error = HotReloadError(
                f"Hot manager rebuild failed: {type(exc).__name__}: {exc}"
            )
            main_logger.critical("%s", reload_error)
            bridge_logger.critical("Hot manager rebuild failed: %s", reload_error)

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
                    bridge_logger.error(
                        "Hot restart: failed to start '%s': %s",
                        name,
                        msg,
                    )
            except Exception as e:
                boot_state[name] = "failed"
                boot_reason[name] = f"{type(e).__name__}: {e}"
                main_logger.exception("Hot restart: failed to start '%s': %s", name, e)
                bridge_logger.exception(
                    "Hot restart: failed to start '%s': %s",
                    name,
                    e,
                )

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

        if reload_error is None:
            await self.kernel.service_manager.refresh_hot_services()

        if reload_error is not None:
            main_logger.error(
                "Hot restart failed; stopped agents were restored with the last usable managers. "
                "Repair the reported source/ABI mismatch and retry /reboot."
            )
            print(
                "\033[38;5;203m  ✗ reboot failed — agents restored where possible; "
                "repair the mismatch and retry /reboot\033[0m\n",
                flush=True,
            )
            return False

        failed_targets = [
            name
            for name in targets
            if boot_state.get(name) not in {"online", "local"}
        ]
        if failed_targets:
            failure_summary = "; ".join(
                f"{name}: {boot_reason.get(name) or boot_state.get(name) or 'unknown'}"
                for name in failed_targets
            )
            main_logger.error(
                "Hot restart failed for target agent(s): %s",
                failure_summary,
            )
            bridge_logger.error(
                "Hot restart failed for target agent(s): %s",
                failure_summary,
            )
            print(
                "\033[38;5;203m  ✗ reboot failed — target agent(s) did not restart: "
                f"{', '.join(failed_targets)}\033[0m\n",
                flush=True,
            )
            return False

        if self.kernel.runtimes:
            main_logger.info("Hot restart complete. %s agent(s) running.", len(self.kernel.runtimes))
            bridge_logger.warning("Hot restart complete (%s agent(s) running)", len(self.kernel.runtimes))
            print(f"\033[38;5;108m  ✓ reboot complete — {len(self.kernel.runtimes)} agent(s) online\033[0m\n", flush=True)
            return True
        else:
            main_logger.critical("Hot restart: no agents running after restart.")
            bridge_logger.critical("Hot restart failed: no agents running after restart")
            print("\033[38;5;203m  ✗ reboot failed — no agents running\033[0m\n", flush=True)
            return False
