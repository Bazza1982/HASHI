from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from orchestrator.reboot_receipts import RebootReceipts, ACTIVE, MAX_DELIVERY_ATTEMPTS
from orchestrator.reboot_ui import render_notice
from orchestrator import ui_language
from orchestrator.telegram_delivery_failover import send_runtime_notice

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
            raise ValueError(f"agent number {agent_number} is outside 1–{len(names)}")
        targets = (names[index],)
    elif mode in BROAD_REBOOT_MODES:
        targets = tuple(dict.fromkeys(runtime.name for runtime in kernel.runtimes))
    elif mode == "group":
        supplied = restart.get("targets")
        if (
            not isinstance(supplied, (list, tuple))
            or not supplied
            or len(supplied) > 100
        ):
            raise ValueError("group reboot requires an explicit bounded target list")
        configured = kernel.configured_agent_names()
        if any(
            not isinstance(name, str) or name not in configured for name in supplied
        ):
            raise ValueError("group reboot contains an unknown target")
        targets = tuple(dict.fromkeys(supplied))
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
        self.receipts = RebootReceipts(
            getattr(getattr(kernel, "paths", None), "bridge_home", None)
        )
        self.active_operation = None
        self.delivery_task = None
        self.delivery_lock = asyncio.Lock()
        self.receipt_fault = False

    def _new_receipt(self, restart, targets=None):
        if targets is None:
            try:
                targets = _resolve_restart_targets(self.kernel, restart)
            except ValueError:
                targets = ()
        names = {
            handle.name: str(handle.get_display_name())[:120]
            for handle in self.kernel.runtimes
            if handle.name in targets
        }
        return self.receipts.create(
            source=str(restart.get("agent_name") or ""),
            targets=targets,
            display_names=names,
            mode=str(restart.get("mode") or "same"),
            origin=restart.get("origin"),
            locale=ui_language.normalize_locale(restart.get("locale")),
            request_key=restart.get("request_key"),
        )

    def submit(self, restart):
        """Durably acknowledge one exact request before waking the execution loop."""
        if self.receipt_fault:
            return {"accepted": False, "reason": "storage"}
        try:
            previous = self.receipts.by_request(
                restart.get("agent_name"), restart.get("request_key")
            )
        except (OSError, ValueError):
            return {"accepted": False, "reason": "storage"}
        if previous is not None:
            return {"accepted": True, "duplicate": True, "record": previous}
        if (
            self.active_operation
            or getattr(self.kernel, "_restart_request", None) is not None
            or getattr(self.kernel, "_handoff_draining", False)
            or getattr(self.kernel, "is_stopping", False)
        ):
            return {"accepted": False, "reason": "busy"}
        try:
            targets = _resolve_restart_targets(self.kernel, restart)
            self._target_handles(targets)
            if not targets:
                raise ValueError("empty reboot scope")
        except ValueError:
            return {"accepted": False, "reason": "invalid_scope"}
        try:
            record = self._new_receipt(restart, targets)
        except (OSError, ValueError):
            return {"accepted": False, "reason": "storage"}
        self.kernel._restart_request = {
            **restart,
            "operation_id": record["id"],
            "targets": record["targets"],
        }
        self.kernel.shutdown_event.set()
        bridge_logger.info(
            "Reboot accepted: operation=%s source=%s mode=%s targets=%s",
            record["id"],
            record["source_agent"],
            record["mode"],
            record["targets"],
        )
        return {"accepted": True, "record": record}

    def latest(
        self, *, actor_id=None, chat_id=None, thread_id=None, surface="telegram"
    ):
        if not actor_id or not chat_id:
            return None
        for record in reversed(self.receipts.records()):
            origin = record.get("origin", {})
            if (
                str(origin.get("actor_id")) == str(actor_id)
                and str(origin.get("chat_id")) == str(chat_id)
                and origin.get("thread_id") == thread_id
                and origin.get("surface", "telegram") == surface
            ):
                if self.receipt_fault and record["status"] in ACTIVE:
                    record.update(status="unconfirmed", reason="storage")
                return record
        return None

    async def _deliver(self, record, *, starting=False):
        origin = record.get("origin", {})
        if not origin.get("chat_id") or record["delivery"]["status"] == "not_requested":
            return {"sent": False}
        return await send_runtime_notice(
            self.kernel,
            source_agent=record["source_agent"],
            chat_id=origin["chat_id"],
            thread_id=origin.get("thread_id"),
            render_text=lambda sender, display: render_notice(
                record, starting=starting, sender=sender, sender_display=display
            ),
        )

    async def send_pending(self, *, now=None):
        if getattr(self.kernel, "_handoff_draining", False) or getattr(
            self.kernel, "is_stopping", False
        ):
            return
        async with self.delivery_lock:
            moment = time.time() if now is None else now
            for record in self.receipts.records():
                delivery = record["delivery"]
                if (
                    record["status"] in ACTIVE
                    or delivery["status"] != "pending"
                    or delivery["next_attempt_at"] > moment
                ):
                    continue
                if delivery["attempts"] >= MAX_DELIVERY_ATTEMPTS:
                    delivery["status"] = "exhausted"
                    self.receipts.update(record["id"], delivery=delivery)
                    continue
                # Reserve the attempt before transport I/O. A crash or failed
                # acknowledgement write must not reset the retry budget.
                delivery["attempts"] += 1
                delivery["next_attempt_at"] = moment + 5 * 3 ** (
                    delivery["attempts"] - 1
                )
                self.receipts.update(record["id"], delivery=delivery)
                try:
                    result = await self._deliver(record)
                except Exception as exc:
                    bridge_logger.warning(
                        "Reboot receipt delivery failed (%s)", type(exc).__name__
                    )
                    result = {"sent": False}
                if result.get("sent"):
                    delivery.update(
                        status="sent",
                        sender=result["sender"],
                        message_id=result["message_id"],
                        sent_at=moment,
                    )
                else:
                    delivery.update(
                        status="exhausted"
                        if delivery["attempts"] >= MAX_DELIVERY_ATTEMPTS
                        else "pending",
                        next_attempt_at=moment
                        + max(
                            result.get("retry_after", 5),
                            5 * 3 ** (delivery["attempts"] - 1),
                        ),
                    )
                self.receipts.update(record["id"], delivery=delivery)

    def start_delivery(self):
        if self.delivery_task is not None and not self.delivery_task.done():
            return
        try:
            self.receipts.recover()
        except (OSError, ValueError):
            self.receipt_fault = True
            bridge_logger.exception(
                "Reboot receipt recovery unavailable; new reboots will be rejected"
            )
            return

        async def watch():
            while True:
                try:
                    await self.send_pending()
                except Exception as exc:
                    bridge_logger.error(
                        "Reboot receipt watcher failed (%s)", type(exc).__name__
                    )
                await asyncio.sleep(5)

        self.delivery_task = asyncio.create_task(watch(), name="reboot-receipts")

    async def stop_delivery(self):
        if self.delivery_task:
            self.delivery_task.cancel()
            await asyncio.gather(self.delivery_task, return_exceptions=True)
            self.delivery_task = None

    def _finish(self, record, status, **fields):
        try:
            self.receipts.update(
                record["id"], status=status, phase="finished", **fields
            )
            bridge_logger.info(
                "Reboot result: operation=%s status=%s reason=%s",
                record["id"],
                status,
                fields.get("reason", ""),
            )
        except (OSError, ValueError):
            self.receipt_fault = True
            bridge_logger.exception(
                "Reboot result could not be persisted; no final notice will be sent"
            )

    @staticmethod
    async def _online(client):
        if not client.process.is_alive():
            return False
        try:
            metadata = await client.call("worker.metadata", timeout=10)
            return bool(
                metadata.get("worker_phase") == "ACTIVE"
                and metadata.get("worker_accepting")
                and metadata.get("backend_ready")
                and metadata.get("startup_success")
                and metadata.get("worker_pid") == client.pid
                and metadata.get("generation_id") == client.generation_id
            )
        except Exception:
            return False

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
        prepare_generation = getattr(
            self.kernel.function_workers,
            "prepare_generation",
            None,
        )
        if callable(prepare_generation):
            generation, generation_root = await prepare_generation(generation)
        else:
            generation_root = None
        bridge_logger.info(
            "Function generation verified before cutover: generation=%s "
            "modules=%s probe_pid=%s runtime=%s",
            generation.manifest.generation_id,
            len(generation.manifest.entries),
            generation.receipt.probe_pid,
            generation.receipt.runtime.runtime_id,
        )

        def _prepare(name: str):
            if generation_root is None:
                return self.kernel.function_workers.prepare_worker(name, generation)
            return self.kernel.function_workers.prepare_worker(
                name,
                generation,
                generation_root=generation_root,
            )

        tasks = {
            name: asyncio.create_task(
                _prepare(name),
                name=f"prepare-function-worker:{name}",
            )
            for name in targets
        }
        try:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        except asyncio.CancelledError:
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            await asyncio.gather(
                *(
                    result.shutdown(force=True)
                    for result in results
                    if not isinstance(result, BaseException)
                ),
                return_exceptions=True,
            )
            raise
        candidates: dict[str, FunctionWorkerClient] = {}
        failures: list[str] = []
        for name, result in zip(tasks, results, strict=True):
            if isinstance(result, BaseException):
                failures.append(f"{name}: {type(result).__name__}: {result}")
            else:
                candidates[name] = result
        if failures:
            await asyncio.gather(
                *(candidate.shutdown(force=True) for candidate in candidates.values()),
                return_exceptions=True,
            )
            raise FunctionWorkerError("; ".join(failures))
        return candidates

    @staticmethod
    async def _resume_old_workers(
        handles: Mapping[str, AgentRuntimeHandle],
        old_clients: Mapping[str, FunctionWorkerClient],
        quiesced: set[str],
    ) -> dict[str, bool]:
        async def resume(name: str) -> None:
            client = old_clients[name]
            if client.process.is_alive():
                try:
                    state = await client.call("worker.metadata", timeout=10)
                    if name in quiesced or state.get("worker_phase") in {
                        "QUIESCED",
                        "DRAINING",
                    }:
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
        states = await asyncio.gather(
            *(RebootManager._online(client) for client in old_clients.values())
        )
        return dict(zip(old_clients, states, strict=True))

    def _publish_generation_state(self) -> None:
        self.kernel.function_workers.publish_generation_state()

    async def hot_restart(self, restart: Mapping[str, Any]) -> bool:
        try:
            record = self.receipts.get(
                restart.get("operation_id")
            ) or self._new_receipt(restart)
        except (OSError, ValueError):
            bridge_logger.exception(
                "Reboot rejected before execution: receipt storage unavailable"
            )
            return False
        if record["status"] not in ACTIVE:
            return record["status"] == "succeeded"
        self.active_operation = record["id"]
        try:
            self.receipts.update(record["id"], status="running", phase="preparing")
            try:
                await self._deliver(record, starting=True)
            except Exception as exc:
                bridge_logger.warning(
                    "Reboot start notification failed (%s)", type(exc).__name__
                )
            return await self._perform_restart(restart, record)
        except asyncio.CancelledError:
            current = self.receipts.get(record["id"])
            if current["status"] in ACTIVE:
                self._finish(record, "unconfirmed", reason="interrupted")
            raise
        except Exception:
            self._finish(record, "unconfirmed", reason="unexpected")
            bridge_logger.exception("Reboot result could not be confirmed")
            return False
        finally:
            self.active_operation = None
            try:
                await self.send_pending()
            except Exception as exc:
                bridge_logger.error(
                    "Reboot result is retained; notification pending (%s)",
                    type(exc).__name__,
                )

    async def _perform_restart(self, restart, record) -> bool:
        mode = str(restart.get("mode") or "same")
        try:
            selected_targets = tuple(record["targets"])
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
            self._finish(record, "rejected", reason="invalid_scope")
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
            self._finish(record, "rejected", reason="candidate_rejected")
            return False

        old_clients: dict[str, FunctionWorkerClient] = {}
        quiesced: set[str] = set()
        try:
            self.receipts.update(record["id"], phase="switching")
            for name in selected_targets:
                old_clients[name] = await asyncio.wait_for(
                    handles[name].begin_cutover(),
                    timeout=WORKER_DRAIN_TIMEOUT_SECONDS,
                )

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
                    drain_failures.append(f"{name}: {type(result).__name__}: {result}")
                else:
                    quiesced.add(name)
            if drain_failures:
                raise FunctionWorkerError(
                    "old Worker drain failed: " + "; ".join(drain_failures)
                )

            # The source and Core contract are checked after every old Worker
            # is quiescent and immediately before candidates can receive work.
            generation = next(iter(candidates.values())).generation
            verify = getattr(
                generation,
                "verify_qualified_source",
                generation.verify,
            )
            await asyncio.to_thread(verify, self.kernel.runtime_fingerprint)

            activation_results = await asyncio.gather(
                *(
                    self.kernel.function_workers.activate_new_worker(candidates[name])
                    for name in selected_targets
                ),
                return_exceptions=True,
            )
            activation: dict[str, dict[str, Any]] = {}
            activation_failures = []
            for name, result in zip(selected_targets, activation_results, strict=True):
                if isinstance(result, BaseException):
                    activation_failures.append(
                        f"{name}: {type(result).__name__}: {result}"
                    )
                else:
                    activation[name] = dict(result)
                    if handles[name].telegram_connected and not bool(
                        activation[name].get("telegram_connected")
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
        except (Exception, asyncio.CancelledError) as exc:
            main_logger.error("Function Worker cutover rolled back: %s", exc)
            bridge_logger.error("Function Worker cutover rolled back: %s", exc)
            await asyncio.gather(
                *(candidate.shutdown(force=True) for candidate in candidates.values()),
                return_exceptions=True,
            )
            restored_states = await self._resume_old_workers(
                handles, old_clients, quiesced
            )
            untouched = {
                name: await self._online(handle.client)
                for name, handle in handles.items()
                if name not in old_clients
            }
            restored_states.update(untouched)
            restored = bool(restored_states) and all(restored_states.values())
            self._finish(
                record,
                "failed",
                restored=restored,
                online=restored_states,
                reason="switch_failed" if restored else "restore_failed",
            )
            print(
                "\033[38;5;203m  ✗ reboot failed — candidate Workers were "
                f"discarded; previous Workers restored={restored}\033[0m\n",
                flush=True,
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            return False

        committed = {
            "committed": True,
            "generations": {
                name: handles[name].generation_id for name in selected_targets
            },
        }
        try:
            self.receipts.update(record["id"], phase="committed", **committed)
        except (OSError, ValueError):
            # Routes have changed. Continue retirement/readiness; this is never
            # a reason to destroy the new Workers or claim a rollback.
            bridge_logger.exception("Reboot commit receipt write failed")

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
        try:
            await self.kernel.function_workers.broadcast_topology()
        except Exception:
            bridge_logger.exception("Topology notification failed after reboot commit")
        await asyncio.gather(
            *(client.shutdown(force=True) for client in old_clients.values()),
            return_exceptions=True,
        )
        worker_summary = ", ".join(
            f"{name}=pid:{handles[name].worker_pid}/{handles[name].generation_id[7:19]}"
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
        states = dict(
            zip(
                selected_targets,
                await asyncio.gather(
                    *(self._online(handles[name].client) for name in selected_targets)
                ),
                strict=True,
            )
        )
        self._finish(
            record,
            "succeeded" if all(states.values()) else "unconfirmed",
            online=states,
            **committed,
            reason="" if all(states.values()) else "readiness",
        )
        return True
