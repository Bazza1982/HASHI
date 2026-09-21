from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestrator.reboot_receipts import RebootReceipts, ACTIVE, MAX_DELIVERY_ATTEMPTS
from orchestrator.reboot_ui import render_notice
from orchestrator import remote_lifecycle, runtime_session, ui_language
from orchestrator.telegram_delivery_failover import send_runtime_notice
from orchestrator.kernel_process import instance_runtime_dir, write_record
from orchestrator.reboot_adoption_bridge import legacy_handoff_markers

from orchestrator.function_worker_supervisor import (
    AgentRuntimeHandle,
    FunctionWorkerClient,
    FunctionWorkerError,
)
from orchestrator.function_generation import UncommittedFunctionSourceError

main_logger = logging.getLogger("BridgeU.Orchestrator")
bridge_logger = logging.getLogger("BridgeU.Bridge")
# Interactive recovery must not wait two minutes for a stuck task.
# This Functions policy does not alter generic Worker lifecycle timeouts.
REBOOT_DRAIN_TIMEOUT_SECONDS = 10.0
# Qualification (240s), drain (180s), activation (360s), and commit (120s)
# have independent bounded stages. Leave headroom before declaring a missing
# Core receipt unconfirmed; this timeout never changes Core's own transaction.
SHARED_REPLACEMENT_TIMEOUT_SECONDS = 1200.0

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
    """Adopt Function generations without replacing the Core process."""

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
        surface = origin.get("surface", "telegram")
        if record["delivery"]["status"] == "not_requested":
            return {"sent": False}
        if surface == "telegram" and not origin.get("chat_id"):
            return {"sent": False}
        rendered = {}

        def render(sender, display):
            text = render_notice(
                record, starting=starting, sender=sender, sender_display=display
            )
            rendered["text"] = text
            return text

        idempotency_key = f"reboot:{record['id']}:{'starting' if starting else 'final'}"

        if surface == "telegram":
            result = await send_runtime_notice(
                self.kernel,
                source_agent=record["source_agent"],
                chat_id=origin["chat_id"],
                thread_id=origin.get("thread_id"),
                render_text=render,
            )
            if result.get("sent") and rendered.get("text"):
                runtime_session.record_kernel_presentation_notice(
                    self.kernel,
                    agent_id=record["source_agent"],
                    text=rendered["text"],
                    idempotency_key=idempotency_key,
                )
            return result

        # Workbench (and other shared-primary frontends) are delivered through
        # the session store that survives Worker cutover; no Telegram send.
        text = render(record["source_agent"], None)
        message = runtime_session.record_kernel_presentation_notice(
            self.kernel,
            agent_id=record["source_agent"],
            text=text,
            idempotency_key=idempotency_key,
        )
        if message:
            return {
                "sent": True,
                "sender": "workbench",
                "message_id": message.get("message_id"),
            }
        return {"sent": False}

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
                        status=(
                            "exhausted"
                            if delivery["attempts"] >= MAX_DELIVERY_ATTEMPTS
                            else "pending"
                        ),
                        next_attempt_at=moment
                        + max(
                            result.get("retry_after", 5),
                            5 * 3 ** (delivery["attempts"] - 1),
                        ),
                    )
                self.receipts.update(record["id"], delivery=delivery)

    def _runtime_state_dir(self) -> Path:
        return instance_runtime_dir(self.kernel.paths.bridge_home)

    def _stage_shared_replacement(self, record: dict[str, Any]) -> dict[str, Any]:
        """Persist the whole-Function request before publishing it to Core."""

        request_id = record["id"]
        workers = {
            handle.name: {
                "old_pid": handle.worker_pid,
                "generation_id": handle.generation_id,
            }
            for handle in self.kernel.runtimes
            if handle.name in record["targets"]
        }
        shared = {
            "status": "requested",
            "request_id": request_id,
            "requested_at": time.time(),
            "old_shared_pid": os.getpid(),
            "generation_id": None,
            "remote": {},
        }
        self.receipts.update(
            record["id"],
            status="running",
            phase="shared_replacement_requested",
            lifecycle_state="accepted",
            workers=workers,
            shared_replacement=shared,
        )
        return shared

    def _publish_shared_replacement(self, shared: Mapping[str, Any]) -> None:
        request_id = str(shared.get("request_id") or "")
        if len(request_id) != 32 or any(
            char not in "0123456789abcdef" for char in request_id
        ):
            raise ValueError("invalid shared Function replacement request id")
        write_record(
            self._runtime_state_dir() / "kernel-requests" / f"{request_id}.json",
            {"id": request_id},
        )
        bridge_logger.info(
            "Whole-Function replacement submitted: operation=%s", request_id
        )

    @staticmethod
    def _remote_pid(result: Mapping[str, Any] | None) -> int | None:
        health = (result or {}).get("health") or {}
        instance = health.get("instance") or {}
        claim = instance.get("runtime_claim") or {}
        try:
            pid = int(claim.get("pid") or 0)
        except (TypeError, ValueError):
            return None
        return pid or None

    @staticmethod
    def _remote_evidence(result: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "status": "adopted" if result.get("ok") else "failed",
            "action": str(result.get("action") or "unknown"),
            "old_pid": result.get("old_pid"),
            "new_pid": result.get("new_pid") or RebootManager._remote_pid(result),
            "ready": bool(result.get("ok")),
            "reason": str(result.get("reason") or ""),
        }

    async def _replacement_worker_evidence(
        self,
        handle: AgentRuntimeHandle,
        previous: Mapping[str, Any],
        *,
        expected_generation: str,
    ) -> dict[str, Any]:
        evidence = await self._online_evidence(handle.client)
        old_pid = previous.get("old_pid")
        new_pid = evidence.get("new_pid")
        evidence.update(
            old_pid=old_pid,
            pid_changed=old_pid is None or new_pid != old_pid,
            old_exited=old_pid is None or new_pid != old_pid,
        )
        evidence["online"] = bool(
            evidence.get("online")
            and evidence["pid_changed"]
            and evidence.get("generation_id") == expected_generation
            and evidence.get("observed_generation_id") == expected_generation
        )
        return evidence

    async def reconcile_shared_replacements(self, *, now: float | None = None) -> None:
        """Complete broad reboot receipts from the successor shared process."""

        if getattr(self.kernel, "_handoff_draining", False) or getattr(
            self.kernel, "is_stopping", False
        ):
            return
        moment = time.time() if now is None else now
        state_dir = self._runtime_state_dir()
        self._promote_legacy_shared_replacements(state_dir)
        for record in self.receipts.records():
            shared = record.get("shared_replacement") or {}
            if (
                record.get("status") not in ACTIVE
                or shared.get("status") != "requested"
            ):
                continue
            request_id = str(shared.get("request_id") or "")
            receipt_path = state_dir / f"replacement-{request_id}.json"
            if not receipt_path.exists():
                requested_at = float(shared.get("requested_at") or 0.0)
                request_path = state_dir / "kernel-requests" / f"{request_id}.json"
                if (
                    requested_at
                    and moment - requested_at > SHARED_REPLACEMENT_TIMEOUT_SECONDS
                    and not request_path.exists()
                ):
                    failed = {
                        **shared,
                        "status": "unconfirmed",
                    }
                    self._finish(
                        record,
                        "unconfirmed",
                        reason="shared_replacement_unconfirmed",
                        shared_replacement=failed,
                    )
                continue
            try:
                if receipt_path.stat().st_size > 4096:
                    raise ValueError("oversized shared replacement receipt")
                replacement = json.loads(receipt_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(replacement, dict)
                    or type(replacement.get("ok")) is not bool
                ):
                    raise ValueError("invalid shared replacement receipt")
            except (OSError, ValueError, TypeError) as exc:
                failed = {**shared, "status": "unconfirmed"}
                self._finish(
                    record,
                    "unconfirmed",
                    reason="shared_replacement_unconfirmed",
                    shared_replacement=failed,
                )
                bridge_logger.error(
                    "Shared replacement receipt is invalid: operation=%s error=%s",
                    request_id,
                    exc,
                )
                continue
            generation_id = str(replacement.get("generation_id") or "")
            if not replacement["ok"]:
                rolled_back = {
                    **shared,
                    "status": "rolled_back",
                    "generation_id": generation_id or None,
                }
                self._finish(
                    record,
                    "failed",
                    lifecycle_state="rolled_back",
                    restored=True,
                    reason="shared_replacement_failed",
                    shared_replacement=rolled_back,
                )
                continue
            current_generation = str(
                getattr(self.kernel, "shared_generation_id", "") or ""
            )
            if not generation_id or generation_id != current_generation:
                unconfirmed = {
                    **shared,
                    "status": "unconfirmed",
                    "generation_id": generation_id or None,
                }
                self._finish(
                    record,
                    "unconfirmed",
                    committed=True,
                    reason="shared_generation_mismatch",
                    shared_replacement=unconfirmed,
                )
                continue

            try:
                remote_result = await remote_lifecycle.reload_remote_for_reboot(
                    self.kernel.paths.bridge_home
                )
            except Exception as exc:
                bridge_logger.exception(
                    "Enabled Remote adoption failed unexpectedly: operation=%s",
                    request_id,
                )
                remote_result = {
                    "ok": False,
                    "action": "remote_reload_exception",
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            remote = self._remote_evidence(remote_result)
            shared_result = {
                **shared,
                "status": "committed" if remote_result.get("ok") else "unconfirmed",
                "generation_id": generation_id,
                "remote": remote,
            }
            if not remote_result.get("ok"):
                self._finish(
                    record,
                    "unconfirmed",
                    committed=True,
                    reason="remote_reload_failed",
                    shared_replacement={**shared_result, "status": "unconfirmed"},
                )
                continue
            # Persist the one-shot Remote adoption before Worker verification.
            # A successor crash now becomes unconfirmed on recovery instead of
            # repeating a destructive Remote stop/start loop.
            self.receipts.update(
                record["id"],
                phase="verifying",
                lifecycle_state="committed",
                committed=True,
                shared_replacement=shared_result,
            )
            handles = self.kernel._runtime_map()
            missing = [name for name in record["targets"] if name not in handles]
            evidence = {}
            try:
                if not missing:
                    evidence = dict(
                        zip(
                            record["targets"],
                            await asyncio.gather(
                                *(
                                    self._replacement_worker_evidence(
                                        handles[name],
                                        record.get("workers", {}).get(name, {}),
                                        expected_generation=generation_id,
                                    )
                                    for name in record["targets"]
                                )
                            ),
                            strict=True,
                        )
                    )
            except Exception:
                bridge_logger.exception(
                    "Replacement Worker evidence failed: operation=%s",
                    request_id,
                )
                missing = list(record["targets"])
            online = {name: bool(item.get("online")) for name, item in evidence.items()}
            adopted = (
                not missing
                and len(evidence) == len(record["targets"])
                and all(online.values())
                and bool(remote_result.get("ok"))
                and os.getpid() != shared.get("old_shared_pid")
            )
            if adopted:
                self._finish(
                    record,
                    "succeeded",
                    lifecycle_state="online",
                    committed=True,
                    reason="",
                    online=online,
                    workers=evidence,
                    generations={name: generation_id for name in record["targets"]},
                    shared_replacement=shared_result,
                )
            else:
                self._finish(
                    record,
                    "unconfirmed",
                    committed=True,
                    reason="readiness",
                    online=online,
                    workers=evidence,
                    shared_replacement={**shared_result, "status": "unconfirmed"},
                )

    def _promote_legacy_shared_replacements(self, state_dir: Path) -> None:
        """Adopt a legacy broad receipt only after Core committed its handoff."""

        current_generation = str(
            getattr(self.kernel, "shared_generation_id", "") or ""
        )
        for marker_path, marker in legacy_handoff_markers(
            self.kernel.paths.bridge_home
        ):
            operation_id = marker["operation_id"]
            record = self.receipts.get(operation_id)
            if record is None:
                continue
            shared = record.get("shared_replacement") or {}
            if (
                record.get("mode") not in BROAD_REBOOT_MODES
                or record.get("status") != "succeeded"
                or shared.get("status") != "not_requested"
                or marker.get("old_shared_pid") == os.getpid()
            ):
                continue
            replacement_path = state_dir / f"replacement-{operation_id}.json"
            if not replacement_path.exists():
                continue
            try:
                if replacement_path.stat().st_size > 4096:
                    raise ValueError("oversized shared replacement receipt")
                replacement = json.loads(
                    replacement_path.read_text(encoding="utf-8")
                )
                if (
                    not isinstance(replacement, dict)
                    or replacement.get("ok") is not True
                    or replacement.get("generation_id") != current_generation
                ):
                    continue
                targets = list(record.get("targets") or ())
                workers = record.get("workers") or {}
                generations = record.get("generations") or {}
                worker_generation = str(marker.get("expected_generation_id") or "")
                if (
                    not targets
                    or min(targets) != marker.get("leader_agent")
                    or any(
                        generations.get(name) != worker_generation for name in targets
                    )
                ):
                    raise ValueError("legacy broad receipt generation mismatch")
                baselines = {}
                for name in targets:
                    evidence = workers.get(name) or {}
                    pid = evidence.get("new_pid")
                    if (
                        isinstance(pid, bool)
                        or not isinstance(pid, int)
                        or pid <= 0
                        or evidence.get("generation_id") != worker_generation
                        or evidence.get("online") is not True
                    ):
                        raise ValueError("legacy broad receipt evidence mismatch")
                    baselines[name] = {
                        "old_pid": pid,
                        # The legacy PID is the pre-handoff baseline. The
                        # successor Worker must run from the committed full
                        # shared generation, not the legacy Agent-only closure.
                        "generation_id": current_generation,
                    }
                leader = baselines.get(str(marker.get("leader_agent"))) or {}
                if leader.get("old_pid") != marker.get("worker_pid"):
                    raise ValueError("legacy bridge leader evidence mismatch")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                bridge_logger.error(
                    "Legacy broad reboot promotion rejected: operation=%s error=%s",
                    operation_id,
                    exc,
                )
                continue

            promoted = {
                "status": "requested",
                "request_id": operation_id,
                "requested_at": marker["requested_at"],
                "old_shared_pid": marker["old_shared_pid"],
                "generation_id": None,
                "remote": {},
            }
            self.receipts.update(
                operation_id,
                status="running",
                phase="shared_replacement_requested",
                lifecycle_state="accepted",
                committed=False,
                reason="",
                workers=baselines,
                shared_replacement=promoted,
            )
            try:
                marker_path.unlink()
            except OSError:
                bridge_logger.warning(
                    "Legacy reboot marker cleanup failed: operation=%s",
                    operation_id,
                )
            bridge_logger.info(
                "Legacy broad reboot receipt promoted after Core handoff: operation=%s",
                operation_id,
            )

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
                    await self.reconcile_shared_replacements()
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
        lifecycle_state = fields.pop("lifecycle_state", None)
        if lifecycle_state is None:
            if status == "succeeded":
                lifecycle_state = "online"
            elif status == "failed" and fields.get("restored"):
                lifecycle_state = "rolled_back"
            elif status == "rejected" and fields.get("reason") == "candidate_rejected":
                lifecycle_state = "candidate_rejected"
            elif status == "rejected":
                lifecycle_state = "rejected"
            else:
                lifecycle_state = "unconfirmed"
        try:
            self.receipts.update(
                record["id"],
                status=status,
                phase="finished",
                lifecycle_state=lifecycle_state,
                **fields,
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
    async def _online_evidence(client, *, old_client=None):
        generation = getattr(client, "generation", None)
        receipt = getattr(generation, "receipt", None)
        expected_runtime = str(
            getattr(getattr(receipt, "runtime", None), "runtime_id", "") or ""
        )
        source_commit = str(getattr(receipt, "source_commit", "") or "")
        old_pid = getattr(old_client, "pid", None)
        new_pid = getattr(client, "pid", None)
        process_alive = bool(client.process.is_alive())
        old_exited = old_client is None or not old_client.process.is_alive()
        evidence = {
            "old_pid": old_pid,
            "new_pid": new_pid,
            "observed_pid": None,
            "pid_changed": old_pid is None or new_pid != old_pid,
            "old_exited": old_exited,
            "generation_id": str(getattr(client, "generation_id", "") or ""),
            "observed_generation_id": None,
            "source_commit": source_commit or None,
            "runtime_id": expected_runtime or None,
            "observed_runtime_id": None,
            "observed_agent": None,
            "agent_matches": False,
            "process_alive": process_alive,
            "active": False,
            "accepting": False,
            "backend_ready": False,
            "startup_success": False,
            "online": False,
        }
        if not process_alive:
            return evidence
        try:
            metadata = await client.call("worker.metadata", timeout=10)
            evidence.update(
                observed_pid=metadata.get("worker_pid"),
                observed_generation_id=metadata.get("generation_id"),
                observed_runtime_id=metadata.get("runtime_id"),
                observed_agent=metadata.get("name"),
                agent_matches=metadata.get("name") == client.agent_name,
                active=metadata.get("worker_phase") == "ACTIVE",
                accepting=bool(metadata.get("worker_accepting")),
                backend_ready=bool(metadata.get("backend_ready")),
                startup_success=bool(metadata.get("startup_success")),
            )
            evidence["online"] = bool(
                evidence["pid_changed"]
                and evidence["old_exited"]
                and evidence["agent_matches"]
                and evidence["active"]
                and evidence["accepting"]
                and evidence["backend_ready"]
                and evidence["startup_success"]
                and evidence["observed_pid"] == new_pid
                and evidence["observed_generation_id"] == evidence["generation_id"]
                and (
                    not expected_runtime
                    or evidence["observed_runtime_id"] == expected_runtime
                )
            )
        except Exception:
            pass
        return evidence

    @staticmethod
    async def _online(client):
        evidence = await RebootManager._online_evidence(client)
        return bool(evidence["online"])

    def reload_project_modules(self, module_names=None):
        del module_names
        raise FunctionWorkerError(
            "In-process module reload is retired; /reboot replaces qualified Function processes"
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
        qualified_generation = getattr(
            self.kernel.function_workers, "qualified_generation", None
        )
        if callable(qualified_generation):
            generation = await qualified_generation()
        else:
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
        staged_shared = None
        result = False
        try:
            self.receipts.update(record["id"], status="running", phase="preparing")
            try:
                await self._deliver(record, starting=True)
            except Exception as exc:
                bridge_logger.warning(
                    "Reboot start notification failed (%s)", type(exc).__name__
                )
            if str(restart.get("mode") or "same") in BROAD_REBOOT_MODES:
                staged_shared = self._stage_shared_replacement(record)
                result = True
            else:
                result = await self._perform_restart(restart, record)
        except asyncio.CancelledError:
            current = self.receipts.get(record["id"])
            if current["status"] in ACTIVE:
                self._finish(record, "unconfirmed", reason="interrupted")
            raise
        except Exception:
            self._finish(record, "unconfirmed", reason="unexpected")
            bridge_logger.exception("Reboot result could not be confirmed")
            result = False
        finally:
            self.active_operation = None
            if staged_shared is not None:
                try:
                    self._publish_shared_replacement(staged_shared)
                except Exception:
                    failed = {**staged_shared, "status": "unconfirmed"}
                    self._finish(
                        record,
                        "unconfirmed",
                        reason="shared_replacement_unconfirmed",
                        shared_replacement=failed,
                    )
                    bridge_logger.exception(
                        "Whole-Function replacement request could not be published"
                    )
                    result = False
            try:
                await self.send_pending()
            except Exception as exc:
                bridge_logger.error(
                    "Reboot result is retained; notification pending (%s)",
                    type(exc).__name__,
                )
        return result

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

        # Read the owning Worker, not the asynchronously updated route cache.
        # This is a preflight, not an idle reservation: the transactional drain
        # below still handles work arriving after this observation.
        observations = await asyncio.gather(
            *(handle.client.call("worker.metadata", timeout=10) for handle in handles.values()),
            return_exceptions=True,
        )
        for name, state in zip(selected_targets, observations, strict=True):
            if (
                not isinstance(state, dict)
                or type(state.get("is_generating")) is not bool
                or type(state.get("queue_depth")) is not int
                or state["queue_depth"] < 0
            ):
                bridge_logger.warning("Reboot activity unavailable: target=%s", name)
                self._finish(record, "rejected", reason="activity_unavailable")
                return False
            if state["is_generating"] or state["queue_depth"]:
                bridge_logger.info(
                    "Reboot target busy: target=%s generating=%s queued=%s",
                    name, state["is_generating"], state["queue_depth"],
                )
                self._finish(record, "rejected", reason="target_busy")
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
            self._finish(
                record,
                "rejected",
                lifecycle_state="candidate_rejected",
                reason=(
                    "source_update_incomplete"
                    if isinstance(exc, UncommittedFunctionSourceError)
                    else "candidate_rejected"
                ),
            )
            return False

        old_clients: dict[str, FunctionWorkerClient] = {}
        quiesced: set[str] = set()
        failure_reason = "route_busy"
        try:
            self.receipts.update(record["id"], phase="switching")
            for name in selected_targets:
                old_clients[name] = await asyncio.wait_for(
                    handles[name].begin_cutover(),
                    timeout=REBOOT_DRAIN_TIMEOUT_SECONDS,
                )

            failure_reason = "drain_failed"
            drain_results = await asyncio.gather(
                *(
                    old_clients[name].call(
                        "worker.quiesce",
                        {"timeout": REBOOT_DRAIN_TIMEOUT_SECONDS},
                        timeout=REBOOT_DRAIN_TIMEOUT_SECONDS + 10.0,
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
            failure_reason = "source_changed"
            generation = next(iter(candidates.values())).generation
            verify = getattr(
                generation,
                "verify_qualified_source",
                generation.verify,
            )
            await asyncio.to_thread(verify, self.kernel.runtime_fingerprint)

            failure_reason = "activation_failed"
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
                lifecycle_state="rolled_back" if restored else "unconfirmed",
                restored=restored,
                online=restored_states,
                reason=failure_reason,
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
            "workers": {
                name: {
                    "old_pid": old_clients[name].pid,
                    "new_pid": handles[name].worker_pid,
                    "generation_id": handles[name].generation_id,
                    "source_commit": str(
                        getattr(
                            getattr(handles[name].client.generation, "receipt", None),
                            "source_commit",
                            "",
                        )
                        or ""
                    )
                    or None,
                }
                for name in selected_targets
            },
        }
        try:
            self.receipts.update(
                record["id"],
                phase="committed",
                lifecycle_state="committed",
                **committed,
            )
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
        evidence = dict(
            zip(
                selected_targets,
                await asyncio.gather(
                    *(
                        self._online_evidence(
                            handles[name].client,
                            old_client=old_clients[name],
                        )
                        for name in selected_targets
                    )
                ),
                strict=True,
            )
        )
        states = {name: bool(item["online"]) for name, item in evidence.items()}
        committed["workers"] = evidence
        self._finish(
            record,
            "succeeded" if all(states.values()) else "unconfirmed",
            lifecycle_state="online" if all(states.values()) else "unconfirmed",
            online=states,
            **committed,
            reason="" if all(states.values()) else "readiness",
        )
        return True
