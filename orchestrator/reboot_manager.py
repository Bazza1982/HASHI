from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from orchestrator.function_worker_supervisor import (
    AgentRuntimeHandle,
    FunctionWorkerClient,
    FunctionWorkerError,
    WORKER_DRAIN_TIMEOUT_SECONDS,
)

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")
TARGETED_REBOOT_MODES = frozenset({"min", "number"})
BROAD_REBOOT_MODES = frozenset({"same", "max"})


def _resolve_restart_targets(kernel, restart: Mapping[str, Any]) -> tuple[str, ...]:
    """Resolve an immutable target set; invalid input never widens scope."""

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
        names = kernel.configured_agent_names()
        index = agent_number - 1
        if not 0 <= index < len(names):
            raise ValueError(
                f"agent number {agent_number} is outside 1–{len(names)}"
            )
        targets = (names[index],)
    elif mode in BROAD_REBOOT_MODES:
        targets = tuple(dict.fromkeys(runtime.name for runtime in kernel.runtimes))
    else:
        raise ValueError(f"unknown reboot mode: {mode!r}")
    if mode in TARGETED_REBOOT_MODES and len(targets) != 1:
        raise ValueError(f"targeted reboot resolved to {len(targets)} agents")
    return targets


class RebootManager:
    """Transactional route cutover between isolated per-Agent Workers."""

    def __init__(self, kernel, console_handler):
        self.kernel = kernel
        self.console_handler = console_handler

    def reload_project_modules(self, module_names=None):
        del module_names
        raise FunctionWorkerError(
            "In-process module reload is retired; /reboot replaces Function Workers"
        )

    def _target_handles(
        self,
        names: tuple[str, ...],
    ) -> dict[str, AgentRuntimeHandle]:
        runtime_map = self.kernel._runtime_map()
        missing = [name for name in names if name not in runtime_map]
        if missing:
            raise ValueError(f"reboot target is not running: {missing}")
        handles = {name: runtime_map[name] for name in names}
        invalid = [
            name
            for name, handle in handles.items()
            if not isinstance(handle, AgentRuntimeHandle)
        ]
        if invalid:
            raise ValueError(
                f"reboot targets are not isolated Function Workers: {invalid}"
            )
        return handles

    async def _prepare_candidates(
        self,
        targets: tuple[str, ...],
    ) -> dict[str, FunctionWorkerClient]:
        generation = await asyncio.to_thread(
            self.kernel.function_workers.qualify_generation
        )
        self.kernel.function_workers.remember_generation(generation)
        bridge_logger.info(
            "Function generation verified before cutover: generation=%s "
            "modules=%s probe_pid=%s runtime=%s",
            generation.manifest.generation_id,
            len(generation.manifest.entries),
            generation.receipt.probe_pid,
            generation.receipt.runtime.runtime_id,
        )
        tasks = {
            name: asyncio.create_task(
                self.kernel.function_workers.prepare_worker(name, generation),
                name=f"prepare-function-worker:{name}",
            )
            for name in targets
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        candidates: dict[str, FunctionWorkerClient] = {}
        failures: list[str] = []
        for name, result in zip(tasks, results, strict=True):
            if isinstance(result, BaseException):
                failures.append(f"{name}: {type(result).__name__}: {result}")
            else:
                candidates[name] = result
        if failures:
            await asyncio.gather(
                *(
                    candidate.shutdown(force=True)
                    for candidate in candidates.values()
                ),
                return_exceptions=True,
            )
            raise FunctionWorkerError("; ".join(failures))
        return candidates

    @staticmethod
    async def _resume_old_workers(
        handles: Mapping[str, AgentRuntimeHandle],
        old_clients: Mapping[str, FunctionWorkerClient],
        quiesced: set[str],
    ) -> None:
        async def resume(name: str) -> None:
            client = old_clients[name]
            if name in quiesced and client.process.is_alive():
                try:
                    await client.call("worker.resume", timeout=30.0)
                except Exception as exc:
                    bridge_logger.error(
                        "Previous Worker could not resume for %s: %s",
                        name,
                        exc,
                    )
            await handles[name].abort_cutover()

        await asyncio.gather(
            *(resume(name) for name in old_clients),
            return_exceptions=True,
        )

    def _publish_generation_state(self) -> None:
        self.kernel.function_workers.publish_generation_state()

    async def hot_restart(self, restart: Mapping[str, Any]) -> bool:
        mode = str(restart.get("mode") or "same")
        try:
            selected_targets = _resolve_restart_targets(self.kernel, restart)
            if not selected_targets:
                raise ValueError("reboot selected no running agents")
            handles = self._target_handles(selected_targets)
        except ValueError as exc:
            main_logger.error("Function Worker reboot scope rejected: %s", exc)
            bridge_logger.error("Function Worker reboot scope rejected: %s", exc)
            print(
                "\033[38;5;203m  ✗ reboot rejected — invalid target scope; "
                "no Worker was touched\033[0m\n",
                flush=True,
            )
            return False

        try:
            candidates = await self._prepare_candidates(selected_targets)
        except Exception as exc:
            main_logger.error("Function Worker generation rejected: %s", exc)
            bridge_logger.error(
                "Function Worker generation rejected before cutover: %s", exc
            )
            print(
                "\033[38;5;203m  ✗ reboot rejected — candidate Workers "
                "failed verification; active Workers were not touched\033[0m\n",
                flush=True,
            )
            return False

        old_clients: dict[str, FunctionWorkerClient] = {}
        quiesced: set[str] = set()
        try:
            for name in selected_targets:
                old_clients[name] = await handles[name].begin_cutover()

            drain_results = await asyncio.gather(
                *(
                    old_clients[name].call(
                        "worker.quiesce",
                        {"timeout": WORKER_DRAIN_TIMEOUT_SECONDS},
                        timeout=WORKER_DRAIN_TIMEOUT_SECONDS + 10.0,
                    )
                    for name in selected_targets
                ),
                return_exceptions=True,
            )
            drain_failures = []
            for name, result in zip(selected_targets, drain_results, strict=True):
                if isinstance(result, BaseException):
                    drain_failures.append(
                        f"{name}: {type(result).__name__}: {result}"
                    )
                else:
                    quiesced.add(name)
            if drain_failures:
                raise FunctionWorkerError(
                    "old Worker drain failed: " + "; ".join(drain_failures)
                )

            # The source and Core contract are checked after every old Worker
            # is quiescent and immediately before candidates can receive work.
            for candidate in candidates.values():
                candidate.generation.verify(self.kernel.runtime_fingerprint)

            activation_results = await asyncio.gather(
                *(
                    self.kernel.function_workers.activate_new_worker(
                        candidates[name]
                    )
                    for name in selected_targets
                ),
                return_exceptions=True,
            )
            activation: dict[str, dict[str, Any]] = {}
            activation_failures = []
            for name, result in zip(
                selected_targets, activation_results, strict=True
            ):
                if isinstance(result, BaseException):
                    activation_failures.append(
                        f"{name}: {type(result).__name__}: {result}"
                    )
                else:
                    activation[name] = dict(result)
                    if (
                        handles[name].telegram_connected
                        and not bool(activation[name].get("telegram_connected"))
                    ):
                        activation_failures.append(
                            f"{name}: candidate lost the active Telegram transport"
                        )
            if activation_failures:
                raise FunctionWorkerError(
                    "candidate Worker activation failed: "
                    + "; ".join(activation_failures)
                )

            await self.kernel.function_workers.commit_handles_atomically(
                {
                    handles[name]: (candidates[name], activation[name])
                    for name in selected_targets
                }
            )
        except Exception as exc:
            main_logger.error("Function Worker cutover rolled back: %s", exc)
            bridge_logger.error("Function Worker cutover rolled back: %s", exc)
            await asyncio.gather(
                *(
                    candidate.shutdown(force=True)
                    for candidate in candidates.values()
                ),
                return_exceptions=True,
            )
            await self._resume_old_workers(handles, old_clients, quiesced)
            print(
                "\033[38;5;203m  ✗ reboot failed — candidate Workers were "
                "discarded; previous Workers resumed\033[0m\n",
                flush=True,
            )
            return False

        # The route-pointer exchange above is the transaction commit point.
        # Diagnostic publication cannot turn a completed commit into a false
        # rollback after active handles already reference the new Workers.
        try:
            self._publish_generation_state()
        except Exception as exc:
            main_logger.exception(
                "Function Worker generation state publication failed after commit: %s",
                exc,
            )
            bridge_logger.exception(
                "Function Worker generation state publication failed after commit: %s",
                exc,
            )
        await self.kernel.function_workers.broadcast_topology()
        await asyncio.gather(
            *(client.shutdown(force=True) for client in old_clients.values()),
            return_exceptions=True,
        )
        worker_summary = ", ".join(
            f"{name}=pid:{handles[name].worker_pid}/"
            f"{handles[name].generation_id[7:19]}"
            for name in selected_targets
        )
        main_logger.info(
            "Function Worker reboot complete: mode=%s targets=%s %s",
            mode,
            selected_targets,
            worker_summary,
        )
        bridge_logger.info(
            "Function Worker reboot committed: mode=%s targets=%s %s",
            mode,
            selected_targets,
            worker_summary,
        )
        print(
            "\033[38;5;108m  ✓ reboot complete — "
            f"{len(selected_targets)} isolated Worker(s) switched\033[0m\n",
            flush=True,
        )
        return True
