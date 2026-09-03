from __future__ import annotations

import asyncio
import logging

from orchestrator.bootstrap_logging import AnimMute
from orchestrator.function_generation import (
    FunctionGenerationError,
    PreparedFunctionGeneration,
    prepare_function_generation,
)
from orchestrator.hot_reload import (
    HotReloadError,
    discover_loaded_project_modules,
    validate_function_contract,
)

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


def _validate_generation_cutover_scope(
    kernel,
    restart: dict,
    selected_targets: tuple[str, ...],
) -> None:
    """Fail closed until each Agent owns an isolated Function Worker.

    The HASHI3 pilot commits Python module bindings at process scope. Leaving
    another runtime active across that commit could mix an old object graph
    with a future lazy import from the new canonical generation. A targeted
    lifecycle restart is therefore safe only when it is the sole live Agent.
    Broad modes already quiesce every live runtime.
    """

    mode = restart.get("mode", "same")
    if mode not in TARGETED_REBOOT_MODES:
        return
    selected = set(selected_targets)
    unselected = tuple(
        runtime.name for runtime in kernel.runtimes if runtime.name not in selected
    )
    if unselected:
        raise ValueError(
            "targeted generation cutover requires per-Agent Function Worker "
            f"isolation; still-running agents={unselected}. Use /reboot max "
            "for this process until worker isolation is installed"
        )


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

    def preflight_project_modules(self) -> list[str]:
        """Return the complete active function surface for candidate staging."""
        code_root = getattr(getattr(self.kernel, "paths", None), "code_root", None)
        return discover_loaded_project_modules(code_root=code_root)

    def reload_project_modules(self, module_names: list[str] | None = None):
        """Reject the retired in-place module mutation path.

        Keeping an explicit failure here protects older callers and plugins
        from silently reintroducing ``importlib.reload`` into the Core process.
        """

        del module_names
        raise HotReloadError(
            "In-place module reload is retired; use a verified function generation"
        )

    def prepare_candidate_generation(self) -> PreparedFunctionGeneration:
        module_names = self.preflight_project_modules()
        return prepare_function_generation(
            self.kernel,
            self.console_handler,
            module_names=module_names,
        )

    def validate_agent_runtime_contract(self):
        validate_function_contract()
        contract_message = (
            "Function generation contract verified: fixed/flex configuration, "
            "HER compatibility facade, runtime pipeline, and Telegram "
            "notification commands are current."
        )
        main_logger.info(contract_message)
        bridge_logger.info(contract_message)

    async def hot_restart(self, restart: dict):
        """Atomically adopt a verified function generation for selected Agents."""
        mode = restart.get("mode", "same")
        requesting_agent = restart.get("agent_name")
        agent_number = restart.get("agent_number")

        try:
            selected_targets = _resolve_restart_targets(self.kernel, restart)
            _validate_generation_cutover_scope(
                self.kernel,
                restart,
                selected_targets,
            )
        except ValueError as exc:
            main_logger.error("Hot restart scope rejected: %s", exc)
            bridge_logger.error("Hot restart scope rejected: %s", exc)
            print(
                "\033[38;5;203m  ✗ reboot rejected — invalid or unsafe target scope; "
                "no agents were stopped\033[0m\n",
                flush=True,
            )
            return False

        try:
            candidate = self.prepare_candidate_generation()
        except (FunctionGenerationError, HotReloadError) as exc:
            main_logger.error("%s", exc)
            bridge_logger.error("Function generation rejected before cutover: %s", exc)
            print(
                "\033[38;5;203m  ✗ reboot rejected — candidate generation "
                "failed verification; running agents were not touched\033[0m\n",
                flush=True,
            )
            return False

        boot_state = {name: "pending" for name in selected_targets}
        boot_reason = {}
        main_logger.info(
            "Function generation ready: %s (%s modules, probe_pid=%s)",
            candidate.manifest.generation_id,
            len(candidate.manifest.entries),
            candidate.receipt.probe_pid,
        )
        bridge_logger.info(
            "Function generation staged and verified: generation=%s modules=%s "
            "runtime=%s probe_pid=%s",
            candidate.manifest.generation_id,
            len(candidate.manifest.entries),
            candidate.receipt.runtime.runtime_id,
            candidate.receipt.probe_pid,
        )

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

        try:
            candidate.activate(self.kernel)
        except FunctionGenerationError as exc:
            main_logger.error("Function generation commit rejected: %s", exc)
            bridge_logger.error("Function generation commit rejected: %s", exc)
            await self._restore_stopped_agents(stopped_targets)
            print(
                "\033[38;5;203m  ✗ reboot rejected — candidate changed during "
                "cutover; previous generation restored\033[0m\n",
                flush=True,
            )
            return False

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

        started_new_targets: list[str] = []

        async def _start_agent_with_state(name):
            boot_state[name] = "connecting"
            try:
                ok, msg = await self.kernel.start_agent(name)
                if ok:
                    started_new_targets.append(name)
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
        if self.console_handler is not None:
            self.console_handler.addFilter(mute)
        try:
            await asyncio.gather(
                loop.run_in_executor(None, _run_banner),
                *[_start_agent_with_state(name) for name in start_targets],
                return_exceptions=True,
            )
        finally:
            if self.console_handler is not None:
                self.console_handler.removeFilter(mute)

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
            for name in list(started_new_targets):
                await self.kernel.stop_agent(
                    name,
                    reason=f"generation-rollback:{mode}",
                )
            candidate.rollback(self.kernel)
            await self._restore_stopped_agents(stopped_targets)
            print(
                "\033[38;5;203m  ✗ reboot failed — target agent(s) did not restart: "
                f"{', '.join(failed_targets)}; previous generation restored\033[0m\n",
                flush=True,
            )
            return False

        try:
            await self.kernel.service_manager.refresh_hot_services(
                expected_whatsapp=wa_enabled,
            )
        except Exception as exc:
            main_logger.exception("Function service cutover failed: %s", exc)
            bridge_logger.exception("Function service cutover failed: %s", exc)
            for name in list(started_new_targets):
                await self.kernel.stop_agent(
                    name,
                    reason=f"generation-rollback:{mode}",
                )
            candidate.rollback(self.kernel)
            await self._restore_stopped_agents(stopped_targets)
            try:
                await self.kernel.service_manager.refresh_hot_services(
                    expected_whatsapp=wa_enabled,
                )
            except Exception as restore_exc:
                main_logger.error(
                    "Previous-generation service refresh needs operator retry: %s",
                    restore_exc,
                )
                bridge_logger.error(
                    "Previous-generation service refresh needs operator retry: %s",
                    restore_exc,
                )
            print(
                "\033[38;5;203m  ✗ reboot failed — warm service cutover was "
                "rolled back to the previous function generation\033[0m\n",
                flush=True,
            )
            return False

        if self.kernel.runtimes:
            main_logger.info(
                "Hot restart complete. generation=%s; %s agent(s) running.",
                candidate.manifest.generation_id,
                len(self.kernel.runtimes),
            )
            bridge_logger.warning(
                "Hot restart complete (generation=%s, %s agent(s) running)",
                candidate.manifest.generation_id,
                len(self.kernel.runtimes),
            )
            print(
                f"\033[38;5;108m  ✓ reboot complete — generation "
                f"{candidate.manifest.generation_id[7:19]} · "
                f"{len(self.kernel.runtimes)} agent(s) online\033[0m\n",
                flush=True,
            )
            return True
        else:
            main_logger.critical("Hot restart: no agents running after restart.")
            bridge_logger.critical("Hot restart failed: no agents running after restart")
            print("\033[38;5;203m  ✗ reboot failed — no agents running\033[0m\n", flush=True)
            return False
