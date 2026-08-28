from __future__ import annotations

import asyncio
import gc
import importlib
import inspect
import logging
import sys

from orchestrator.bootstrap_logging import AnimMute
from orchestrator.hot_reload import (
    HotReloadError,
    discover_loaded_project_modules,
    preflight_module_sources,
)

# ``importlib.reload`` reuses this module dictionary.  Capture the class owned
# by the live kernel before the new class statement replaces the module name.
# A hot-restart coroutine already executing on that class keeps its old
# ``hot_restart`` code object, so the post-reload contract seam must be handed
# forward explicitly below.
_PRE_RELOAD_REBOOT_MANAGER_CLASS = globals().get("RebootManager")

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")
AGENT_STOP_TIMEOUT_SECONDS = 25.0
AGENT_RESTORE_TIMEOUT_SECONDS = 60.0
TARGETED_REBOOT_MODES = frozenset({"min", "number"})
BROAD_REBOOT_MODES = frozenset({"same", "max"})


def _consume_operation_task_result(task: asyncio.Future) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


def _resolve_restart_targets(kernel, restart: dict) -> tuple[str, ...]:
    """Resolve lifecycle targets without ever widening an invalid request.

    Only explicit broad modes may select more than one Agent. Targeted modes
    resolve to exactly one immutable target before source preflight, so later
    compatibility or reload work cannot promote or replace the requested
    lifecycle scope. Keep this outside ``RebootManager``: the first targeted
    reboot adopting this fix must not look like a class-interface change to a
    previously loaded manager.
    """

    mode = restart.get("mode", "same")
    requesting_agent = restart.get("agent_name")
    agent_number = restart.get("agent_number")

    if mode == "min":
        if not isinstance(requesting_agent, str) or not requesting_agent.strip():
            raise ValueError("min reboot requires a requesting agent")
        targets = (requesting_agent,)
    elif mode == "number":
        if not isinstance(agent_number, int) or isinstance(agent_number, bool):
            raise ValueError("number reboot requires an integer agent number")
        all_names = kernel.configured_agent_names()
        idx = agent_number - 1
        if not 0 <= idx < len(all_names):
            raise ValueError(
                f"agent number {agent_number} is outside 1–{len(all_names)}"
            )
        targets = (all_names[idx],)
    elif mode in BROAD_REBOOT_MODES:
        targets = tuple(dict.fromkeys(runtime.name for runtime in kernel.runtimes))
    else:
        raise ValueError(f"unknown reboot mode: {mode!r}")

    if mode in TARGETED_REBOOT_MODES and len(targets) != 1:
        raise ValueError(f"targeted reboot resolved to {len(targets)} agents")
    return targets


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
        session_store = importlib.import_module("orchestrator.session_store")
        runtime_session = importlib.import_module("orchestrator.runtime_session")
        flexible_runtime = importlib.import_module(
            "orchestrator.flexible_agent_runtime"
        )
        command_registry = importlib.import_module("orchestrator.command_registry")
        telegram_notifications = importlib.import_module(
            "orchestrator.telegram_notifications"
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
            for engine in ("her-v2", "her")
        ):
            raise HotReloadError(
                "Hot reload contract failed: a HER ID can reach a stale or retired adapter"
            )
        if not callable(getattr(runtime_pipeline, "setup_interactive_feedback", None)):
            raise HotReloadError(
                "Hot reload contract failed: runtime acknowledgement pipeline unavailable"
            )
        notification_mode = getattr(
            telegram_notifications, "notification_mode", None
        )
        set_notification_mode = getattr(
            telegram_notifications, "set_notification_mode", None
        )
        disable_notification = getattr(
            telegram_notifications, "disable_notification", None
        )
        disable_parameters = (
            inspect.signature(disable_notification).parameters
            if callable(disable_notification)
            else {}
        )
        runtime_commands = command_registry.runtime_command_map()
        if (
            not callable(notification_mode)
            or not callable(set_notification_mode)
            or "purpose" not in disable_parameters
            or "notify" not in runtime_commands
        ):
            raise HotReloadError(
                "Hot reload contract failed: Telegram notification mode or "
                "/notify command is not current"
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
        session_store_class = getattr(session_store, "SessionStore", None)
        if (
            session_store_class is None
            or getattr(runtime_session, "SessionStore", None) is not session_store_class
            or not callable(
                getattr(session_store_class, "recent_agent_exchanges", None)
            )
        ):
            raise HotReloadError(
                "Hot reload contract failed: runtime session handling retained a "
                "stale SessionStore class"
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
            "Hot reload contract verified: HER compatibility facade, runtime "
            "pipeline, and Telegram notification commands are current."
        )
        main_logger.info(contract_message)
        bridge_logger.info(contract_message)

    async def hot_restart(self, restart: dict):
        """Stop agents per restart mode, reload Python code and config, then start agents."""
        mode = restart.get("mode", "same")
        requesting_agent = restart.get("agent_name")
        agent_number = restart.get("agent_number")

        try:
            selected_targets = _resolve_restart_targets(self.kernel, restart)
        except ValueError as exc:
            main_logger.error("Hot restart scope rejected: %s", exc)
            bridge_logger.error("Hot restart scope rejected: %s", exc)
            print(
                "\033[38;5;203m  ✗ reboot rejected — invalid target scope; "
                "no agents were stopped\033[0m\n",
                flush=True,
            )
            return False

        boot_state = {name: "pending" for name in selected_targets}
        boot_reason = {}

        try:
            module_names = self.preflight_project_modules()
        except HotReloadError as exc:
            main_logger.error("%s", exc)
            bridge_logger.error("Hot restart preflight rejected: %s", exc)
            print(
                "\033[38;5;203m  ✗ reboot rejected — source preflight failed; "
                "running agents were not touched\033[0m\n",
                flush=True,
            )
            return False

        main_logger.info(
            "Hot restart: stopping %s agent(s): %s",
            len(selected_targets),
            selected_targets,
        )
        bridge_logger.warning(
            "Hot restart begin (mode=%s, requester=%s, number=%s, targets=%s)",
            mode,
            requesting_agent or "-",
            agent_number if agent_number is not None else "-",
            selected_targets,
        )
        stopped_targets = []
        for name in selected_targets:
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

        main_logger.info("Hot restart: starting agents: %s", selected_targets)
        try:
            _, agent_configs, _ = self.kernel._load_config_bundle()
            active_config_names = {cfg.name for cfg in agent_configs}
            newly_inactive = [
                name for name in selected_targets if name not in active_config_names
            ]
            start_targets = [
                name for name in selected_targets if name in active_config_names
            ]
            for name in newly_inactive:
                boot_state.pop(name, None)
            inactive_agent_names = [
                cfg.name for cfg in agent_configs if cfg.name not in start_targets
            ] + newly_inactive
        except Exception as e:
            main_logger.error("Hot restart: config reload failed: %s", e)
            start_targets = list(selected_targets)
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
                agent_names=start_targets,
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
                *[_start_agent_with_state(name) for name in start_targets],
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
            for name in start_targets
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


def _handoff_reloaded_runtime_contract(
    previous_manager_class,
    *,
    current_manager_class=RebootManager,
) -> int:
    """Move the post-reload validator onto an already-running manager class.

    The first reboot across a contract change is still executing
    ``hot_restart`` from the previous class generation.  Its subsequent
    ``self.validate_agent_runtime_contract()`` lookup must resolve to the
    validator from the source that was just reloaded.

    A failed reload can leave the module bound to a newer class generation
    while the kernel still owns an instance from an older generation.  In
    that state, handing the validator only to the module's previous class is
    insufficient.  Discover every live instance with the same class identity
    and patch that narrow seam on each generation; backend aliases and
    ordinary runtime resolution remain unchanged.
    """

    if not inspect.isclass(previous_manager_class):
        return 0

    expected_module = previous_manager_class.__module__
    expected_name = previous_manager_class.__name__
    manager_classes = {previous_manager_class}
    for candidate in gc.get_objects():
        candidate_class = type(candidate)
        if (
            candidate_class.__module__ == expected_module
            and candidate_class.__name__ == expected_name
        ):
            manager_classes.add(candidate_class)

    patched = 0
    for manager_class in manager_classes:
        if manager_class is current_manager_class:
            continue
        manager_class.validate_agent_runtime_contract = (
            current_manager_class.validate_agent_runtime_contract
        )
        patched += 1
    return patched


_HANDED_OFF_REBOOT_MANAGER_GENERATIONS = _handoff_reloaded_runtime_contract(
    _PRE_RELOAD_REBOOT_MANAGER_CLASS
)
if _HANDED_OFF_REBOOT_MANAGER_GENERATIONS:
    handoff_message = (
        "Hot reload: handed the current runtime contract validator to "
        f"{_HANDED_OFF_REBOOT_MANAGER_GENERATIONS} live RebootManager "
        "generation(s)."
    )
    main_logger.info(handoff_message)
    bridge_logger.info(handoff_message)
