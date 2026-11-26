from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orchestrator.reboot_receipts import (
    ACTIVE,
    MAX_DELIVERY_ATTEMPTS,
    REBOOT_TIMING_STAGES,
    REBOOT_WORK_COUNTS,
    RebootReceipts,
)
from orchestrator.agent_stop_fence import AgentStopFenceStore, stop_fence_path
from orchestrator.reboot_ui import operation_notice, render_notice
from orchestrator import reboot_capture, runtime_session, ui_language
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
# Progress is durable before cutover. Optional chat delivery gets a bounded
# head start, never a permission gate or a browser-render dependency.
REBOOT_START_NOTICE_TIMEOUT_SECONDS = 2.0
# Workbench submits /reboot max through the same shared HTTP runtime that
# Core must quiesce. Let that single control request leave admission tracking
# before publishing the handoff; unrelated work remains a fail-fast blocker.
WORKBENCH_CONTROL_RELEASE_TIMEOUT_SECONDS = 2.0

TARGETED_REBOOT_MODES = frozenset({"min", "same", "number"})
BROAD_REBOOT_MODES = frozenset({"max"})
# A delayed reboot waits for a quiet instance instead of rejecting busy Agents.
# The wait is always bounded, and it only ever observes: no route is fenced and
# admission stays open, so an instance that never calms down keeps serving.
REBOOT_DELAY_DEFAULT_SECONDS = 1800.0
REBOOT_DELAY_MAX_SECONDS = 21600.0
# A turn that just finished must not be allowed to hand over to its next turn
# inside the cutover, so the whole target set has to stay quiet for this long.
REBOOT_DELAY_IDLE_WINDOW_SECONDS = 15.0
REBOOT_DELAY_POLL_SECONDS = 2.0
REBOOT_DELAY_MIN_RECHECK_SECONDS = 0.05
# A forced stop is confirmed by observation, never by the /stop reply alone.
REBOOT_FORCE_STOP_CONFIRM_SECONDS = 20.0
REBOOT_FORCE_STOP_POLL_SECONDS = 0.5


def _read_captures(
    bridge_home: Any,
    operation_id: str,
    names: tuple[str, ...],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The exact record each target's stop wrote, and the targets without one.

    A target with no finished record is unknown, never "nothing to restore": an
    armed capture writes nothing yet, and a record the stop could not finish
    writing says so instead of claiming the work is safe.
    """
    records: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in names:
        path = reboot_capture.capture_path(bridge_home, operation_id, name)
        payload = reboot_capture.read_capture(path) if path is not None else None
        if not reboot_capture.is_complete(payload):
            missing.append(name)
            continue
        records[name] = payload
    return records, missing


def _capture_payloads(
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Normalise the per-target records into the manifest's agent payloads."""
    payloads: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for name, record in records.items():
        payloads[str(name)] = {
            "pending": [
                dict(item)
                for item in (record.get("pending") or ())
                if isinstance(item, Mapping)
            ],
            "delayed": [
                dict(item)
                for item in (record.get("delayed") or ())
                if isinstance(item, Mapping)
            ],
            "interrupted": [
                dict(item)
                for item in (record.get("interrupted") or ())
                if isinstance(item, Mapping)
            ],
            "unconfirmed": [
                dict(item)
                for item in (record.get("unconfirmed") or ())
                if isinstance(item, Mapping)
            ],
        }
    return payloads


def _resolve_restart_targets(kernel, restart: Mapping[str, Any]) -> tuple[str, ...]:
    """Resolve an immutable target set; invalid input never widens scope."""

    mode = restart.get("mode", "same")
    requesting_agent = restart.get("agent_name")
    agent_number = restart.get("agent_number")
    if mode in {"min", "same"}:
        if not isinstance(requesting_agent, str) or not requesting_agent.strip():
            raise ValueError("agent reboot requires a requesting agent")
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
        self._delay_tasks: dict[str, asyncio.Task] = {}
        self._force_tasks: dict[str, asyncio.Task] = {}
        self.delivery_task = None
        self.delivery_lock = asyncio.Lock()
        self.receipt_fault = False
        self._delivery_wake = asyncio.Event()
        self._operation_metrics: dict[str, dict[str, dict[str, float | int]]] = {}

    def _metrics_for(self, record: Mapping[str, Any]) -> dict[str, dict[str, float | int]]:
        operation_id = str(record["id"])
        return self._operation_metrics.setdefault(
            operation_id,
            {
                "stage_timings_ms": {
                    name: float((record.get("stage_timings_ms") or {}).get(name, 0.0))
                    for name in REBOOT_TIMING_STAGES
                },
                "work_counts": {
                    name: int((record.get("work_counts") or {}).get(name, 0))
                    for name in REBOOT_WORK_COUNTS
                },
            },
        )

    def _record_stage(
        self,
        record: Mapping[str, Any],
        name: str,
        started: float,
    ) -> None:
        metrics = self._metrics_for(record)
        timings = metrics["stage_timings_ms"]
        timings[name] = round(
            float(timings.get(name, 0.0))
            + max(0.0, (time.perf_counter() - started) * 1000),
            1,
        )

    def _metrics_snapshot(self, record: Mapping[str, Any]) -> dict[str, Any]:
        metrics = self._metrics_for(record)
        return {
            "stage_timings_ms": dict(metrics["stage_timings_ms"]),
            "work_counts": dict(metrics["work_counts"]),
        }

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
        if restart.get("cancel"):
            return self.cancel_waiting()
        if self.receipt_fault:
            return {"accepted": False, "reason": "storage"}
        try:
            previous = self.receipts.by_request(
                restart.get("agent_name"), restart.get("request_key")
            )
        except (OSError, ValueError):
            return {"accepted": False, "reason": "storage"}
        if previous is not None:
            return {
                "accepted": True,
                "duplicate": True,
                "record": previous,
                "operation": self._operation_projection(previous),
            }
        raw_delay = restart.get("delay_seconds")
        if raw_delay is None:
            delay_seconds = 0.0
        elif (
            isinstance(raw_delay, bool)
            or not isinstance(raw_delay, (int, float))
            or not 0 < float(raw_delay) <= REBOOT_DELAY_MAX_SECONDS
        ):
            return {"accepted": False, "reason": "invalid_scope"}
        else:
            delay_seconds = float(raw_delay)
        if (
            self.active_operation
            or getattr(self.kernel, "_restart_request", None) is not None
            or getattr(self.kernel, "_handoff_draining", False)
            or getattr(self.kernel, "is_stopping", False)
        ):
            return {"accepted": False, "reason": "busy"}
        try:
            targets = _resolve_restart_targets(self.kernel, restart)
            handles = self._target_handles(targets)
            if not targets:
                raise ValueError("empty reboot scope")
        except ValueError:
            return {"accepted": False, "reason": "invalid_scope"}
        if any(handle._cutover for handle in handles.values()):
            return {"accepted": False, "reason": "busy"}
        try:
            record = self._new_receipt(restart, targets)
        except (OSError, ValueError):
            return {"accepted": False, "reason": "storage"}
        if delay_seconds:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return {"accepted": False, "reason": "unexpected"}
            try:
                record = self._reserve_delay_wait(record, delay_seconds)
            except (OSError, ValueError):
                try:
                    self._finish(record, "rejected", reason="storage")
                except (OSError, ValueError):
                    pass
                return {"accepted": False, "reason": "storage"}
            # The receipt is the queue entry: admission stays open, no route is
            # fenced and no Worker is touched until the wait actually ends.
            self.active_operation = record["id"]
            self._delay_tasks[record["id"]] = loop.create_task(
                self._wait_for_idle_then_restart(restart, record, targets, handles)
            )
            bridge_logger.info(
                "Reboot queued until idle: operation=%s source=%s mode=%s "
                "targets=%s limit=%.0fs",
                record["id"],
                record["source_agent"],
                record["mode"],
                record["targets"],
                delay_seconds,
            )
            return {
                "accepted": True,
                "waiting": True,
                "record": record,
                "operation": self._operation_projection(record),
            }
        if restart.get("force") is True:
            # A forced reboot stops busy Agents before the cutover, so it runs
            # as a task: the receipt exists first, and the stop is observed
            # instead of assumed.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return {"accepted": False, "reason": "unexpected"}
            self.active_operation = record["id"]
            try:
                self.receipts.update(record["id"], phase="stopping_targets")
            except (OSError, ValueError):
                self._finish(record, "rejected", reason="storage")
                self.active_operation = None
                return {"accepted": False, "reason": "storage"}
            task = loop.create_task(
                self._force_stop_then_cutover(restart, record, targets, handles)
            )
            self._force_tasks[record["id"]] = task
            task.add_done_callback(
                lambda _task, operation=record["id"]: self._force_tasks.pop(
                    operation, None
                )
            )
            bridge_logger.info(
                "Reboot force accepted: operation=%s source=%s mode=%s targets=%s",
                record["id"],
                record["source_agent"],
                record["mode"],
                record["targets"],
            )
            return {
                "accepted": True,
                "stopping": True,
                "record": record,
                "operation": self._operation_projection(record),
            }
        # Admission is intentionally non-disruptive.  The accepted receipt and
        # origin-visible start notice must exist before any selected route is
        # fenced; otherwise Workbench can only discover the start after the
        # target Worker returns.
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
        return {
            "accepted": True,
            "record": record,
            "operation": self._operation_projection(record),
        }

    def _reserve_delay_wait(self, record, delay_seconds: float):
        """Record the bounded wait on the receipt before the first observation."""
        now = time.time()
        self.receipts.update(
            record["id"],
            phase="waiting_idle",
            wait_limits={
                "delay_seconds": float(delay_seconds),
                "idle_window_seconds": float(REBOOT_DELAY_IDLE_WINDOW_SECONDS),
                "deadline": now + float(delay_seconds),
            },
        )
        return self.receipts.get(record["id"])

    def cancel_waiting(self):
        """Drop a queued reboot that never reached the cutover; no side effects."""
        operation_id = self.active_operation
        if not operation_id or operation_id not in self._delay_tasks:
            return {"accepted": False, "reason": "not_waiting"}
        record = self.receipts.get(operation_id)
        if record is not None and str(record.get("phase") or "") == "stopping_targets":
            # A forced reboot is already stopping Agents: no longer cancellable.
            return {"accepted": False, "reason": "not_waiting"}
        task = self._delay_tasks.pop(operation_id)
        task.cancel()
        if self.active_operation == operation_id:
            self.active_operation = None
        if record is None:
            return {"accepted": False, "reason": "not_waiting"}
        bridge_logger.info("Reboot wait cancelled by request: operation=%s", operation_id)
        self._finish(
            record,
            "rejected",
            reason="delay_cancelled",
            busy_targets=list(record.get("busy_targets") or ()),
        )
        return {
            "accepted": True,
            "record": self.receipts.get(operation_id) or record,
            "operation": self._operation_projection(
                self.receipts.get(operation_id) or record
            ),
        }

    async def _wait_for_idle_then_restart(
        self,
        restart: Mapping[str, Any],
        record: Mapping[str, Any],
        targets: tuple[str, ...],
        handles: Mapping[str, AgentRuntimeHandle],
    ) -> bool:
        """Hold an accepted reboot until the whole target set has gone quiet.

        This only observes. Admission stays open, no route is fenced and no
        Worker is touched, so a request that is never satisfiable expires
        instead of degrading the instance that is still serving.
        """
        operation_id = str(record["id"])
        limits = dict(record.get("wait_limits") or {})
        idle_window = float(
            limits.get("idle_window_seconds") or REBOOT_DELAY_IDLE_WINDOW_SECONDS
        )
        delay_budget = float(limits.get("delay_seconds") or 0.0)
        if delay_budget <= 0:
            delay_budget = max(0.0, float(limits.get("deadline") or 0.0) - time.time())
        # The wait itself is measured on a monotonic clock: a wall-clock jump
        # must neither shorten nor extend the budget the caller was promised.
        started = time.monotonic()
        handed_off = False
        idle_since: float | None = None
        busy_targets: list[str] = []
        unavailable_targets: list[str] = []
        try:
            while True:
                activity, unavailable_targets = await self._observe_target_activity(
                    targets, handles
                )
                busy_targets = [
                    name
                    for name, (generating, queue_depth, detached) in activity.items()
                    if generating or queue_depth or detached
                ]
                now = time.monotonic()
                remaining = started + delay_budget - now
                # Expiry is decided before the idle window. A metadata read is
                # allowed to consume the rest of the budget, but an observation
                # that returns past the deadline must never start a restart.
                if remaining <= 0:
                    if restart.get("force") is True:
                        bridge_logger.info(
                            "Reboot wait expired; forcing busy targets: "
                            "operation=%s busy=%s unavailable=%s",
                            operation_id,
                            busy_targets,
                            unavailable_targets,
                        )
                        handed_off = await self._force_stop_then_cutover(
                            restart,
                            record,
                            targets,
                            handles,
                            busy_targets=list(busy_targets),
                            unavailable_targets=list(unavailable_targets),
                        )
                        return handed_off
                    bridge_logger.info(
                        "Reboot wait expired before idle: operation=%s "
                        "busy=%s unavailable=%s",
                        operation_id,
                        busy_targets,
                        unavailable_targets,
                    )
                    self._finish(
                        record,
                        "rejected",
                        reason="idle_timeout",
                        busy_targets=list(busy_targets),
                        activity_targets=list(unavailable_targets),
                    )
                    return False
                if busy_targets or unavailable_targets:
                    idle_since = None
                elif idle_since is None:
                    idle_since = now
                    bridge_logger.info(
                        "Reboot wait: all targets idle, holding window | "
                        "operation=%s window=%.0fs",
                        operation_id,
                        idle_window,
                    )
                if idle_since is not None and now - idle_since >= idle_window:
                    break
                await asyncio.sleep(
                    min(
                        REBOOT_DELAY_POLL_SECONDS,
                        max(REBOOT_DELAY_MIN_RECHECK_SECONDS, remaining),
                    )
                )
            self.kernel._restart_request = {
                **restart,
                "operation_id": operation_id,
                "targets": record["targets"],
            }
            bridge_logger.info(
                "Reboot wait satisfied; starting cutover | operation=%s", operation_id
            )
            self.kernel.shutdown_event.set()
            handed_off = True
            return True
        except asyncio.CancelledError:
            if operation_id in self._delay_tasks:
                self._delay_tasks.pop(operation_id, None)
                self._finish(
                    record,
                    "rejected",
                    reason="delay_cancelled",
                    busy_targets=list(busy_targets),
                    activity_targets=list(unavailable_targets),
                )
            raise
        finally:
            self._delay_tasks.pop(operation_id, None)
            if not handed_off and self.active_operation == operation_id:
                self.active_operation = None

    async def _force_stop_then_cutover(
        self,
        restart: Mapping[str, Any],
        record: Mapping[str, Any],
        targets: tuple[str, ...],
        handles: Mapping[str, AgentRuntimeHandle],
        *,
        busy_targets: list[str] | None = None,
        unavailable_targets: list[str] | None = None,
    ) -> bool:
        """Capture queued work, stop what still runs, then start the cutover.

        Every stop is confirmed by a fresh observation. If a target is still
        busy at the deadline the reboot is rejected and the old instance keeps
        serving: a forced reboot never trades a lost Agent for a restart.
        """
        operation_id = str(record["id"])
        try:
            self.receipts.update(operation_id, phase="stopping_targets")
        except (OSError, ValueError):
            self._finish(record, "rejected", reason="storage")
            if self.active_operation == operation_id:
                self.active_operation = None
            return False
        if busy_targets is None or unavailable_targets is None:
            activity, missing = await self._observe_target_activity(targets, handles)
            busy_targets = [
                name
                for name, (generating, depth, detached) in activity.items()
                if generating or depth or detached
            ]
            unavailable_targets = list(missing or ())
        stop_targets = list(
            dict.fromkeys([*(busy_targets or ()), *(unavailable_targets or ())])
        )
        # Arm the record before anything is removed. Each target's stop writes
        # down exactly what it takes, at the point it takes it: no queue
        # difference is interpreted afterwards, which is the only way to tell a
        # finished request from a removed one and to see work enqueued after the
        # arm. An Agent that cannot be armed is not stopped at all.
        for name in stop_targets:
            handle = handles.get(name)
            if handle is None:
                continue
            try:
                armed = dict(await handle.arm_reboot_capture(operation_id) or {})
            except Exception:
                armed = {}
            if not armed.get("armed"):
                bridge_logger.warning(
                    "Reboot force: capture could not be armed, nothing stopped | "
                    "operation=%s agent=%s",
                    operation_id,
                    name,
                )
                self._finish(
                    record,
                    "rejected",
                    reason="force_capture_failed",
                    busy_targets=[name],
                )
                if self.active_operation == operation_id:
                    self.active_operation = None
                return False
        for name in stop_targets:
            handle = handles.get(name)
            if handle is None:
                continue
            try:
                await handle.execute_slash_command("/stop", source_channel="reboot")
            except Exception:
                bridge_logger.warning(
                    "Reboot force: stop request failed | operation=%s agent=%s",
                    operation_id,
                    name,
                )
        # What the stops recorded, read from disk: the worker wrote it before it
        # removed anything, so it survives the worker itself.
        bridge_home = getattr(getattr(self.kernel, "paths", None), "bridge_home", None)
        records, missing = _read_captures(bridge_home, operation_id, tuple(stop_targets))
        recorded = _capture_payloads(records)
        if missing:
            # Unknown is not "nothing to restore": the old instance keeps serving
            # and every record that does exist stays on disk for the hand-back.
            bridge_logger.warning(
                "Reboot force: stop record missing, cutover abandoned | "
                "operation=%s agents=%s",
                operation_id,
                sorted(missing),
            )
            self._finish(
                record,
                "rejected",
                reason="force_capture_unconfirmed",
                busy_targets=sorted(missing),
            )
            if self.active_operation == operation_id:
                self.active_operation = None
            await self._hand_back_after_abort(
                operation_id, recorded, stop_targets, handles
            )
            return False
        if stop_targets:
            deadline = time.monotonic() + REBOOT_FORCE_STOP_CONFIRM_SECONDS
            while True:
                activity, missing = await self._observe_target_activity(targets, handles)
                still_busy = [
                    name
                    for name, (generating, depth, detached) in activity.items()
                    if generating or depth or detached
                ] + list(missing or ())
                if not still_busy:
                    break
                if time.monotonic() >= deadline:
                    bridge_logger.warning(
                        "Reboot force: stop unconfirmed, keeping the old instance | "
                        "operation=%s agents=%s",
                        operation_id,
                        sorted(set(still_busy)),
                    )
                    self._finish(
                        record,
                        "rejected",
                        reason="force_stop_unconfirmed",
                        busy_targets=sorted(set(still_busy)),
                    )
                    if self.active_operation == operation_id:
                        self.active_operation = None
                    # Whatever did stop goes back into the queue that still serves.
                    await self._hand_back_after_abort(
                        operation_id, recorded, stop_targets, handles
                    )
                    return False
                await asyncio.sleep(REBOOT_FORCE_STOP_POLL_SECONDS)
        try:
            manifest = self._write_resume_manifest(
                operation_id, recorded, phase="stopped"
            )
        except OSError:
            bridge_logger.warning(
                "Reboot force: stop record not stored, cutover abandoned | "
                "operation=%s",
                operation_id,
            )
            self._finish(record, "rejected", reason="force_manifest_unavailable")
            if self.active_operation == operation_id:
                self.active_operation = None
            await self._hand_back_after_abort(
                operation_id, recorded, stop_targets, handles
            )
            return False
        request = {
            **restart,
            "operation_id": operation_id,
            "targets": record["targets"],
        }
        if manifest is not None:
            request["resume_manifest"] = str(manifest)
            self.receipts.update(operation_id, resume_manifest=str(manifest))
        self.kernel._restart_request = request
        bridge_logger.info(
            "Reboot force: cutover starting | operation=%s stopped=%s",
            operation_id,
            stop_targets,
        )
        self.kernel.shutdown_event.set()
        return True

    def _resume_manifest_path(self, operation_id: str) -> Path | None:
        bridge_home = getattr(getattr(self.kernel, "paths", None), "bridge_home", None)
        if not bridge_home:
            return None
        return Path(bridge_home) / "state" / "reboot_resume" / f"{operation_id}.json"

    @staticmethod
    def _manifest_agents(
        manifest: Mapping[str, Any],
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        agents: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for name, payload in dict(manifest.get("agents") or {}).items():
            if not isinstance(payload, Mapping):
                continue
            interrupted = payload.get("interrupted")
            agents[str(name)] = {
                "pending": [
                    dict(item)
                    for item in (payload.get("pending") or ())
                    if isinstance(item, Mapping)
                ],
                "delayed": [
                    dict(item)
                    for item in (payload.get("delayed") or ())
                    if isinstance(item, Mapping)
                ],
                "interrupted": (
                    [
                        dict(item)
                        for item in interrupted
                        if isinstance(item, Mapping)
                    ]
                    if isinstance(interrupted, (list, tuple))
                    else []
                ),
                "unconfirmed": [
                    dict(item)
                    for item in (payload.get("unconfirmed") or ())
                    if isinstance(item, Mapping)
                ],
            }
        return agents

    def _discard_resume_manifest(self, operation_id: str) -> None:
        path = self._resume_manifest_path(operation_id)
        if path is not None:
            try:
                path.unlink()
            except OSError:
                pass
        self._discard_capture_files(operation_id)

    def _discard_capture_files(self, operation_id: str) -> None:
        """Drop the per-target stop records once the manifest carries them."""
        bridge_home = getattr(getattr(self.kernel, "paths", None), "bridge_home", None)
        directory = reboot_capture.capture_directory(bridge_home)
        if directory is None:
            return
        prefix = f"capture-{reboot_capture.safe_name(operation_id)}-"
        for path in directory.glob(f"{prefix}*.json"):
            try:
                path.unlink()
            except OSError:
                pass

    def _write_resume_manifest(
        self,
        operation_id: str,
        captured: Mapping[str, Mapping[str, Any]],
        *,
        phase: str,
    ) -> Path | None:
        """Store, atomically, the work this operation must hand back.

        Raises OSError: a manifest that cannot be stored is a reboot that must
        not proceed, so callers decide instead of losing the record silently.
        """
        agents = {
            name: payload
            for name, payload in self._manifest_agents({"agents": captured}).items()
            if any(payload.values())
        }
        if not agents:
            self._discard_resume_manifest(operation_id)
            return None
        path = self._resume_manifest_path(operation_id)
        if path is None:
            raise OSError("bridge home is unavailable")
        path.parent.mkdir(parents=True, exist_ok=True)
        staged = path.with_suffix(".tmp")
        staged.write_text(
            json.dumps(
                {
                    "operation_id": operation_id,
                    "phase": phase,
                    "created_at": time.time(),
                    "agents": agents,
                }
            ),
            encoding="utf-8",
        )
        os.replace(staged, path)
        return path

    async def _resume_agents(
        self,
        operation_id: str,
        agents: Mapping[str, Mapping[str, Any]],
        *,
        handles: Mapping[str, AgentRuntimeHandle] | None = None,
        discard_when_done: bool = True,
    ) -> dict[str, int]:
        """Hand recorded work back, counting only confirmed admissions."""
        totals = {"pending": 0, "turns": 0, "delayed": 0, "unconfirmed": 0}
        if not agents:
            if discard_when_done:
                self._discard_resume_manifest(operation_id)
            return totals
        if handles is None:
            try:
                handles = self._target_handles(tuple(agents))
            except ValueError:
                handles = {}
        remaining: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for name, payload in agents.items():
            pending = list(payload.get("pending") or ())
            delayed = list(payload.get("delayed") or ())
            interrupted = list(payload.get("interrupted") or ())
            # The stop already knows it could not confirm these; they are work we
            # cannot prove anything about, so they are kept and reported.
            unconfirmed = [
                dict(item)
                for item in (payload.get("unconfirmed") or ())
                if isinstance(item, Mapping)
            ]
            # Delayed messages live in Core's Scheduler, so they are put back
            # here against the real schedule instead of through the Worker.
            delayed_resumed, delayed_unconfirmed = await self._restore_delayed(
                name, delayed
            )
            if not (pending or delayed or interrupted):
                # The stop took nothing this Agent can be asked to put back.
                if unconfirmed or delayed_unconfirmed:
                    remaining[name] = {
                        "unconfirmed": unconfirmed + delayed_unconfirmed
                    }
                    totals["unconfirmed"] += len(unconfirmed) + len(
                        delayed_unconfirmed
                    )
                continue
            handle = handles.get(name)
            result: dict[str, Any] = {}
            failed = handle is None
            if handle is not None and (pending or interrupted):
                try:
                    result = dict(
                        await handle.resume_after_reboot(
                            pending,
                            interrupted=interrupted,
                            operation_id=operation_id,
                        )
                        or {}
                    )
                except Exception:
                    bridge_logger.warning(
                        "Reboot force: resume failed | operation=%s agent=%s",
                        operation_id,
                        name,
                    )
                    failed = True
                    result = {}
            confirmed = {
                "pending": int(result.get("pending_resumed") or 0),
                "delayed": delayed_resumed,
                "interrupted": int(result.get("turns_resumed") or 0),
            }
            totals["pending"] += confirmed["pending"]
            totals["delayed"] += confirmed["delayed"]
            totals["turns"] += confirmed["interrupted"]
            bucket: dict[str, list[dict[str, Any]]] = {
                "pending": [],
                "delayed": [],
                "interrupted": [],
                "unconfirmed": unconfirmed + delayed_unconfirmed,
            }
            if failed:
                bucket.update(
                    {
                        "pending": pending,
                        "interrupted": interrupted,
                    }
                )
            else:
                for item in result.get("unconfirmed") or ():
                    if not isinstance(item, Mapping):
                        continue
                    entry = item.get("entry")
                    if not isinstance(entry, Mapping):
                        continue
                    kind = {"pending": "pending", "delayed": "delayed"}.get(
                        str(item.get("kind") or ""), "interrupted"
                    )
                    bucket[kind].append(dict(entry))
                # Work the Agent neither restored nor explained is unknown, never
                # restored: it stays recorded and shows up in the receipt.
                for kind, submitted in (
                    ("pending", pending),
                    ("delayed", delayed),
                    ("interrupted", interrupted),
                ):
                    explained = len(bucket[kind])
                    unknown = len(submitted) - confirmed[kind] - explained
                    if unknown <= 0:
                        continue
                    seen_markers = {
                        json.dumps(entry, sort_keys=True, default=str)
                        for entry in bucket[kind]
                    }
                    for entry in submitted:
                        if unknown <= 0:
                            break
                        marker = json.dumps(entry, sort_keys=True, default=str)
                        if marker in seen_markers:
                            continue
                        seen_markers.add(marker)
                        bucket[kind].append(dict(entry))
                        unknown -= 1
            if any(bucket.values()):
                remaining[name] = bucket
                totals["unconfirmed"] += len(unconfirmed) + sum(
                    len(bucket[kind])
                    for kind in ("pending", "delayed", "interrupted")
                )
        if remaining:
            # Work that is not confirmed stays recorded: never deleted, never
            # reported as restored.
            try:
                self._write_resume_manifest(operation_id, remaining, phase="stopped")
            except OSError:
                bridge_logger.warning(
                    "Reboot force: unconfirmed work could not be re-recorded | "
                    "operation=%s",
                    operation_id,
                )
        elif discard_when_done:
            self._discard_resume_manifest(operation_id)
        if operation_id:
            try:
                self.receipts.update(operation_id, resume=dict(totals))
            except (OSError, ValueError):
                pass
        bridge_logger.info(
            "Reboot force: work restored | operation=%s pending=%s delayed=%s "
            "turns=%s unconfirmed=%s",
            operation_id,
            totals["pending"],
            totals["delayed"],
            totals["turns"],
            totals["unconfirmed"],
        )
        return totals

    async def resume_restored_work(self, restart: Mapping[str, Any]) -> dict[str, int]:
        """Re-queue what a forced reboot captured, once the new generation is up."""
        raw = restart.get("resume_manifest")
        totals = {"pending": 0, "turns": 0, "delayed": 0, "unconfirmed": 0}
        if not raw:
            return totals
        path = Path(str(raw))
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            bridge_logger.warning(
                "Reboot force: resume manifest unreadable | path=%s", raw
            )
            return totals
        operation_id = str(
            manifest.get("operation_id") or restart.get("operation_id") or ""
        )
        phase = str(manifest.get("phase") or "stopped")
        if phase != "stopped":
            # A capture-only record proves the work was listed, not that it was
            # removed. Restoring it would resurrect work that is still queued.
            bridge_logger.warning(
                "Reboot force: capture-only manifest, work not restored | "
                "operation=%s path=%s",
                operation_id,
                raw,
            )
            if operation_id:
                try:
                    self.receipts.update(
                        operation_id,
                        resume={"pending": 0, "turns": 0, "delayed": 0, "unconfirmed": 1},
                    )
                except (OSError, ValueError):
                    pass
            return totals
        return await self._resume_agents(
            operation_id, self._manifest_agents(manifest)
        )

    def _agent_stop_epoch(self, agent: str) -> int | None:
        """The fence the stop of ``agent`` left behind, read from its own store."""
        bridge_home = getattr(getattr(self.kernel, "paths", None), "bridge_home", None)
        if not bridge_home:
            return None
        try:
            return int(AgentStopFenceStore(stop_fence_path(bridge_home)).current(agent).epoch)
        except Exception:
            return None

    async def _restore_delayed(
        self,
        agent: str,
        entries: Sequence[Any],
    ) -> tuple[int, list[dict[str, Any]]]:
        """Put a stop's cancelled delayed records back through the real Scheduler.

        Only the schedule's own receipt counts. A record that cannot be put back
        stays recorded as unconfirmed instead of being reported as restored.
        """
        if not entries:
            return 0, []
        restore = getattr(
            getattr(self.kernel, "scheduler", None), "restore_delayed_message", None
        )
        epoch = self._agent_stop_epoch(agent)
        resumed = 0
        unconfirmed: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            record = dict(entry.get("record") or {}) or {
                key: value for key, value in entry.items() if key != "record"
            }
            record.setdefault("agent", agent)
            record.setdefault("due_at", entry.get("due_at"))
            delay_id = str(record.get("id") or entry.get("id") or "")
            if delay_id:
                record["id"] = delay_id
            reason = ""
            if not callable(restore):
                reason = "restore_unsupported"
            elif epoch is None:
                reason = "fence_unavailable"
            elif not delay_id or not record.get("prompt"):
                reason = "record_missing"
            else:
                # Same record, same id, same due time; only the fence reopened by
                # the stop is restamped so the message is accepted again.
                metadata = dict(record.get("request_metadata") or {})
                metadata["agent_stop_epoch"] = epoch
                record["request_metadata"] = metadata
                try:
                    if await restore(record):
                        resumed += 1
                        continue
                    reason = "not_scheduled"
                except Exception:
                    reason = "restore_failed"
            unconfirmed.append(
                {"kind": "delayed", "entry": dict(entry), "reason": reason}
            )
        return resumed, unconfirmed

    async def _hand_back_after_abort(
        self,
        operation_id: str,
        recorded: Mapping[str, Mapping[str, Any]],
        stop_targets: list[str],
        handles: Mapping[str, AgentRuntimeHandle],
    ) -> None:
        """Give stopped work back to the instance that is still serving.

        ``recorded`` is what the stops wrote, so it is handed back exactly. A
        target with no record is unknown, never empty: it stays named in the
        manifest instead of being dropped on the floor.
        """
        restored: dict[str, dict[str, list[dict[str, Any]]]] = {}
        for name in stop_targets:
            handle = handles.get(name)
            payload = recorded.get(name)
            disarm = getattr(handle, "disarm_reboot_capture", None)
            if callable(disarm):
                # This operation is over; its record must not arm a later stop.
                try:
                    await disarm(operation_id)
                except Exception:
                    bridge_logger.warning(
                        "Reboot force: capture disarm failed | operation=%s agent=%s",
                        operation_id,
                        name,
                    )
            unconfirmed = [
                dict(item) for item in (payload or {}).get("unconfirmed") or ()
            ]
            if payload is None:
                # No finished record: what this stop took is unknown, and that
                # is exactly what has to stay visible.
                unconfirmed.append(
                    {
                        "kind": "capture",
                        "entry": {"agent": name, "operation_id": operation_id},
                        "reason": "capture_incomplete",
                    }
                )
            restored[name] = {
                "pending": [dict(item) for item in (payload or {}).get("pending") or ()],
                "delayed": [dict(item) for item in (payload or {}).get("delayed") or ()],
                "interrupted": [
                    dict(item) for item in (payload or {}).get("interrupted") or ()
                ],
                "unconfirmed": unconfirmed,
            }
        totals = await self._resume_agents(
            operation_id, restored, handles=handles, discard_when_done=False
        )
        if not totals.get("unconfirmed"):
            # Everything the stop took is provably back in the queue.
            self._discard_capture_files(operation_id)

    def latest(
        self,
        *,
        owner_id=None,
        actor_id=None,
        chat_id=None,
        thread_id=None,
        surface="telegram",
    ):
        if not actor_id or not chat_id:
            return None
        for record in reversed(self.receipts.records()):
            origin = record.get("origin", {})
            recorded_owner = str(origin.get("owner_id") or "")
            if not recorded_owner:
                legacy_actor = str(origin.get("actor_id") or "")
                if legacy_actor.isdigit():
                    recorded_owner = f"user:{legacy_actor}"
            if (
                (
                    owner_id is None
                    or recorded_owner == str(owner_id)
                )
                and
                str(origin.get("actor_id")) == str(actor_id)
                and str(origin.get("chat_id")) == str(chat_id)
                and origin.get("thread_id") == thread_id
                and origin.get("surface", "telegram") == surface
            ):
                if self.receipt_fault and record["status"] in ACTIVE:
                    record.update(status="unconfirmed", reason="storage")
                return record
        return None

    @staticmethod
    def _operation_projection(record, *, after_sequence: int = 0):
        if (
            isinstance(after_sequence, bool) or not isinstance(after_sequence, int)
        ):
            raise ValueError("after_sequence must be an integer")
        receipt_status = str(record.get("status") or "")
        terminal = receipt_status not in ACTIVE
        if receipt_status == "succeeded":
            status = "completed"
        elif receipt_status == "failed" and record.get("restored"):
            status = "rolled_back"
        elif terminal:
            status = "failed"
        else:
            status = receipt_status
        progress = [
            event
            for event in record.get("progress") or ()
            if int(event.get("sequence") or 0) > after_sequence
        ]
        latest_sequence = max(
            (int(event.get("sequence") or 0) for event in record.get("progress") or ()),
            default=0,
        )
        reason = str(record.get("reason") or "")
        if reason in {"target_busy", "target_became_busy", "idle_timeout"}:
            blocking_ids = record.get("busy_targets") or ()
        elif reason in {"activity_unavailable", "activity_became_unavailable"}:
            blocking_ids = record.get("activity_targets") or ()
        else:
            blocking_ids = ()
        raw_display_names = record.get("display_names") or {}
        display_names = raw_display_names if isinstance(raw_display_names, Mapping) else {}
        blocking_targets = [
            {
                "agent_id": agent_id,
                "display_name": str(display_names.get(agent_id) or agent_id),
            }
            for agent_id in dict.fromkeys(
                str(value) for value in blocking_ids if str(value)
            )
        ]
        presentation_ack = record.get("presentation_ack") or {}
        return {
            "operation_id": record["id"],
            "action": "reboot",
            "mode": record["mode"],
            "status": status,
            "phase": record.get("phase"),
            "terminal": terminal,
            "shared_generation_id": (record.get("shared_replacement") or {}).get("generation_id"),
            "outcome": record.get("outcome"),
            "notice": operation_notice(record),
            "warnings": list(record.get("warnings") or ()),
            "duration_seconds": record.get("duration_seconds"),
            "created_at": record.get("created_at"),
            "finished_at": record.get("finished_at"),
            "latest_sequence": latest_sequence,
            "progress": progress,
            "targets": list(record.get("targets") or ()),
            "source_agent": record.get("source_agent"),
            "start_message": dict((record.get("start_delivery") or {}).get("presentation_message") or {}),
            "lifecycle_state": record.get("lifecycle_state"),
            "reason": reason,
            "blocking_targets": blocking_targets,
            "presentation_ack": {
                "required": False,
                "status": presentation_ack.get("status") or "not_required",
                "expected_sequence": presentation_ack.get("expected_sequence"),
                "sequence": presentation_ack.get("sequence"),
            },
        }

    def visible_operations(self, *, owner_id: str):
        """Discover owner-scoped lifecycle changes, including Telegram origins."""
        result = []
        for record in reversed(self.receipts.records()):
            origin = record.get("origin") or {}
            recorded_owner = str(origin.get("owner_id") or "")
            if not recorded_owner and str(origin.get("actor_id") or "").isdigit():
                recorded_owner = "user:" + str(origin["actor_id"])
            if recorded_owner != str(owner_id or ""):
                continue
            # Existing sessions can recover a recent completion after disconnect.
            timestamps = [float(event.get("created_at") or 0) for event in record.get("progress") or ()]
            if record.get("status") not in ACTIVE and max(timestamps, default=0) < time.time() - 60:
                continue
            result.append(self._operation_projection(record))
        return result

    def operation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        agent_id: str,
        after_sequence: int = 0,
    ):
        """Return one owner/Agent-scoped monotonic PAO operation projection."""

        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(char not in "0123456789abcdef" for char in operation_id)
        ):
            return None
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
            raise ValueError("after_sequence must be an integer")
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        owner = str(owner_id or "").strip()
        agent = str(agent_id or "").strip().casefold()
        if not owner or not agent:
            return None
        record = self.receipts.get(operation_id)
        if record is None:
            return None
        origin = record.get("origin") or {}
        recorded_owner = str(origin.get("owner_id") or "")
        if not recorded_owner:
            legacy_actor = str(origin.get("actor_id") or "")
            if legacy_actor.isdigit():
                recorded_owner = f"user:{legacy_actor}"
        if (
            recorded_owner != owner
            or str(record.get("source_agent") or "").casefold() != agent
        ):
            return None
        return self._operation_projection(record, after_sequence=after_sequence)

    def acknowledge_start_presentation(
        self,
        operation_id: str,
        *,
        owner_id: str,
        agent_id: str,
        sequence: int,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Retain legacy browser-render observations without gating execution.

        Authentication supplies ``owner_id``; callers must not take it from
        the request body.  Scope mismatch is intentionally indistinguishable
        from a missing operation.
        """

        operation = self.operation(
            operation_id,
            owner_id=owner_id,
            agent_id=agent_id,
        )
        if operation is None:
            return {"acknowledged": False, "reason": "not_found"}
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            return {"acknowledged": False, "reason": "invalid_sequence"}
        if message_id is not None and (
            not isinstance(message_id, str) or len(message_id) > 200
        ):
            return {"acknowledged": False, "reason": "invalid_message_id"}
        record = self.receipts.get(operation_id)
        notice = (record.get("start_delivery") or {}).get("presentation_message") or {}
        if notice.get("message_id") and message_id != notice["message_id"]:
            return {"acknowledged": False, "reason": "message_mismatch"}
        ack = dict(record.get("presentation_ack") or {})
        status = ack.get("status") or "not_required"
        if status == "not_required":
            return {"acknowledged": False, "reason": "not_required"}
        expected_sequence = ack.get("expected_sequence")
        if sequence != expected_sequence:
            return {"acknowledged": False, "reason": "sequence_mismatch"}
        if status == "confirmed":
            if ack.get("sequence") != sequence:
                return {"acknowledged": False, "reason": "sequence_mismatch"}
            return {
                "acknowledged": True,
                "duplicate": True,
                "operation": self._operation_projection(record),
            }
        if status == "expired" or record.get("status") not in ACTIVE:
            return {"acknowledged": False, "reason": "not_pending"}
        ack.update(
            status="confirmed",
            sequence=sequence,
            message_id=message_id,
            confirmed_at=time.time(),
        )
        record = self.receipts.update(
            operation_id,
            presentation_ack=ack,
        )
        return {
            "acknowledged": True,
            "duplicate": False,
            "operation": self._operation_projection(record),
        }

    async def _deliver(self, record, *, starting=False):
        origin = record.get("origin", {})
        surface = origin.get("surface", "telegram")
        delivery_key = "start_delivery" if starting else "delivery"
        if record[delivery_key]["status"] == "not_requested":
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

        if origin.get("session_id"):
            from orchestrator.frontend_notice_delivery import deliver_notice

            return await deliver_notice(
                self.kernel, agent_id=record["source_agent"], origin=origin,
                text=render(record["source_agent"], None), publication_id=idempotency_key,
                send_notice=send_runtime_notice,
                recovery_category="transient" if starting else "command_result",
                recovery=int(record[delivery_key].get("attempts") or 0) > 1,
                original_at=record.get("created_at") if starting else record.get("finished_at"),
            )

        if surface == "telegram":
            result = await send_runtime_notice(
                self.kernel,
                source_agent=record["source_agent"],
                operation_id=record["id"],
                notice_kind="starting" if starting else "final",
                chat_id=origin["chat_id"],
                thread_id=origin.get("thread_id"),
                render_text=render,
            )
            if result.get("sent") and rendered.get("text"):
                message = runtime_session.record_kernel_presentation_notice(
                    self.kernel,
                    agent_id=record["source_agent"],
                    text=rendered["text"],
                    idempotency_key=idempotency_key,
                )
                if message and starting:
                    from orchestrator.frontend_projection import transport_text_component
                    result["presentation_message"] = {
                        "message_id": message.get("message_id"), "session_id": message.get("session_id"),
                        "text": transport_text_component(rendered["text"], "telegram-html")["text"],
                    }
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
            from orchestrator.frontend_projection import transport_text_component
            return {
                "sent": True,
                "sender": "workbench",
                "message_id": message.get("message_id"),
                **({"presentation_message": {"message_id": message.get("message_id"), "session_id": message.get("session_id"),
                    "text": transport_text_component(text, "telegram-html")["text"]}} if starting else {}),
            }
        return {"sent": False}

    def _record_start_delivery(self, record, result):
        delivery = dict(record.get("start_delivery") or {})
        if delivery.get("status") == "not_requested":
            return record
        delivery["attempts"] = int(delivery.get("attempts") or 0) + 1
        if result.get("sent"):
            delivery.update(
                status="sent",
                sender=result.get("sender"),
                message_id=result.get("message_id"),
                sent_at=time.time(),
                presentation_message=result.get("presentation_message") or {},
            )
            return self.receipts.update(
                record["id"],
                phase="start_visible",
                start_delivery=delivery,
                progress_message_id=result.get("message_id"),
            )
        delivery.update(status="exhausted", next_attempt_at=0)
        return self.receipts.update(record["id"], start_delivery=delivery)

    def _fence_reboot_routes(self, record: Mapping[str, Any]) -> None:
        """Gate the frozen target set only after start progress is readable."""

        handles = self._target_handles(tuple(record.get("targets") or ()))
        broad = str(record.get("mode") or "") in BROAD_REBOOT_MODES
        for handle in handles.values():
            handle.fence_for_reboot(reject_new=broad)
        if broad:
            gateway = getattr(self.kernel, "api_gateway", None)
            if gateway is not None and hasattr(gateway, "_accepting_requests"):
                gateway._accepting_requests = False

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
                origin = record.get("origin") or {}
                if origin.get("surface", "telegram") == "telegram":
                    runtimes = self.kernel._runtime_map()
                    if runtimes and not any(getattr(handle, "telegram_connected", False)
                                            for handle in runtimes.values()):
                        continue  # Keep the confirmation durable throughout an outage.
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
                elif result.get("delivery_state") in {"expired", "suppressed"}:
                    # FC has intentionally closed this endpoint; stop the
                    # notice retry owner without claiming a successful send.
                    delivery.update(status="exhausted", next_attempt_at=0,
                                    reason="telegram_replay_" + result["delivery_state"])
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
                "old_worker_nonce": str(
                    getattr(handle.client, "nonce", "")
                    or handle.metadata.get("worker_nonce")
                    or ""
                ),
                "old_worker_started_at": handle.metadata.get("worker_started_at"),
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
        }
        self.receipts.update(
            record["id"],
            status="running",
            phase="shared_replacement_requested",
            lifecycle_state="accepted",
            workers=workers,
            shared_replacement=shared,
            **self._metrics_snapshot(record),
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

    async def _wait_for_workbench_control_release(
        self, record: Mapping[str, Any]
    ) -> None:
        """Let the initiating Workbench POST leave admission tracking."""

        origin = record.get("origin") or {}
        if str(origin.get("surface") or "") != "workbench":
            return
        outstanding = max(
            0, int(getattr(self.kernel, "_handoff_requests", 0) or 0)
        )
        if outstanding != 1:
            return
        request_idle = getattr(self.kernel, "_handoff_requests_idle", None)
        if request_idle is None:
            return
        try:
            await asyncio.wait_for(
                request_idle.wait(),
                timeout=WORKBENCH_CONTROL_RELEASE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            bridge_logger.warning(
                "Workbench reboot control request did not release before "
                "shared handoff: operation=%s",
                record["id"],
            )

    async def _replacement_worker_evidence(
        self,
        handle: AgentRuntimeHandle,
        previous: Mapping[str, Any],
        *,
        expected_generation: str,
    ) -> dict[str, Any]:
        evidence = await self._online_evidence(
            handle.client,
            previous=previous,
            route_client=handle.client,
        )
        if (
            evidence.get("generation_id") != expected_generation
            or evidence.get("observed_generation_id") != expected_generation
        ):
            failed_checks = list(evidence.get("failed_checks") or ())
            if "generation_mismatch" not in failed_checks:
                failed_checks.append("generation_mismatch")
            evidence["failed_checks"] = failed_checks
            evidence["online"] = False
            evidence["state"] = "unavailable"
        return evidence

    async def reconcile_shared_replacements(self, *, now: float | None = None) -> None:
        """Complete broad reboot receipts from the successor shared process."""

        if getattr(self.kernel, "_handoff_draining", False) or getattr(
            self.kernel, "is_stopping", False
        ):
            return
        state_dir = self._runtime_state_dir()
        self._promote_legacy_shared_replacements(state_dir)
        for record in self.receipts.records():
            shared = record.get("shared_replacement") or {}
            if (
                record.get("status") not in ACTIVE
                or shared.get("status") != "requested"
            ):
                continue
            if record.get("recovered") and not getattr(
                self.kernel, "_shared_replacement_continuity", False
            ):
                # A normal cold/recovery startup may observe the same committed
                # generation and online Workers after the previous Core died.
                # That is not continuity of the original hot transaction. Only
                # the shared process prepared by the live Core handoff may
                # confirm an inherited broad reboot receipt.
                await self._release_reboot_fences(record)
                interrupted = {
                    **shared,
                    "status": "unconfirmed",
                }
                self._finish(
                    record,
                    "unconfirmed",
                    reason="interrupted",
                    shared_replacement=interrupted,
                )
                continue
            request_id = str(shared.get("request_id") or "")
            receipt_path = state_dir / f"replacement-{request_id}.json"
            if not receipt_path.exists():
                # Core owns preparation/activation budgets and its result receipt.
                # A second wall-clock timer must not invent a failed transaction.
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
                await self._release_reboot_fences(record)
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
                await self._release_reboot_fences(record)
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
                await self._release_reboot_fences(record)
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

            startup = dict(getattr(self.kernel, "startup_status", {}) or {})
            # Local service readiness and Worker identity determine lifecycle
            # completion. External connector recovery remains a health warning.
            shared_ready = (
                startup.get("services_ready", startup.get("ready")) is True
                and not startup.get("failed_agents", 0)
                and not any(issue.get("severity") in {"error", "critical"}
                            for issue in startup.get("issues") or ())
            )
            connector_warnings = ["connectors_unavailable"] if (
                startup.get("degraded")
                or getattr(self.kernel, "_connector_activation_pending", False)
            ) else []

            shared_result = {
                **shared,
                "status": "committed",
                "generation_id": generation_id,
                "adoption": dict(
                    getattr(self.kernel, "function_release_adoption", None) or {}
                ),
            }
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
            metrics = self._metrics_for(record)
            qualification_stats = dict(
                getattr(
                    self.kernel.function_workers,
                    "last_qualification_stats",
                    {},
                )
                or {}
            )
            materialization_stats = dict(
                getattr(
                    self.kernel.function_workers,
                    "last_materialization_stats",
                    {},
                )
                or {}
            )
            qualification_stage = (
                "generation_cache_lookup"
                if qualification_stats.get("source")
                in {"memory_cache", "disk_cache"}
                else "qualification"
            )
            metrics["stage_timings_ms"][qualification_stage] = float(
                qualification_stats.get("duration_ms", 0.0)
            )
            metrics["stage_timings_ms"]["materialization"] = float(
                materialization_stats.get("duration_ms", 0.0)
            )
            candidate_timings = dict(
                getattr(
                    self.kernel.function_workers,
                    "candidate_prepare_timings_ms",
                    {},
                )
                or {}
            )
            metrics["stage_timings_ms"]["candidate_start"] = max(
                (float(candidate_timings.get(name, 0.0)) for name in record["targets"]),
                default=0.0,
            )
            metrics["work_counts"]["candidate_worker_count"] = len(
                record["targets"]
            )
            metrics["work_counts"]["manifest_hash_count"] = (
                int(qualification_stats.get("manifest_hash_count", 0))
                + int(materialization_stats.get("manifest_hash_count", 0))
                + len(record["targets"])
            )
            metrics["work_counts"]["full_closure_import_count"] = int(
                qualification_stats.get("full_closure_import_count", 0)
            )
            observation_started = time.perf_counter()
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
            for name in record["targets"]:
                if name not in evidence:
                    evidence[name] = {
                        "state": "unconfirmed",
                        "failed_checks": ["status_read_failed"],
                        "observation_confidence": "unconfirmed",
                        "online": False,
                    }
            self._record_stage(record, "final_observation", observation_started)
            online = {
                name: bool(evidence[name].get("online"))
                for name in record["targets"]
            }
            confirmed = all(
                evidence[name].get("observation_confidence") == "confirmed"
                for name in record["targets"]
            )
            online_count = sum(online.values())
            if shared_ready and online_count == len(record["targets"]):
                self._finish(
                    record,
                    "succeeded",
                    lifecycle_state="online",
                    outcome="succeeded_with_warning" if connector_warnings else "succeeded",
                    warnings=connector_warnings,
                    observation_confidence="confirmed",
                    committed=True,
                    reason="",
                    online=online,
                    workers=evidence,
                    generations={name: generation_id for name in record["targets"]},
                    shared_replacement=shared_result,
                )
                if record.get("resume_manifest"):
                    # The new generation is live; only now can the captured work
                    # be handed back without blocking the handoff.
                    await self.resume_restored_work(record)
            else:
                outcome = "partial" if online_count else "failed"
                self._finish(
                    record,
                    "unconfirmed",
                    committed=True,
                    lifecycle_state=("committed" if online_count else "unconfirmed"),
                    outcome=outcome,
                    observation_confidence="confirmed" if confirmed else "unconfirmed",
                    reason="shared_services_unready" if not shared_ready else "readiness",
                    online=online,
                    workers=evidence,
                    shared_replacement=shared_result,
                )
                if record.get("resume_manifest"):
                    bridge_logger.warning(
                        "Reboot force: work not handed back, kept on disk | "
                        "operation=%s path=%s",
                        record.get("id"),
                        record.get("resume_manifest"),
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
                record.get("mode") not in BROAD_REBOOT_MODES | {"same"}
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
                try:
                    await asyncio.wait_for(self._delivery_wake.wait(), timeout=5)
                except TimeoutError:
                    pass
                self._delivery_wake.clear()

        self.delivery_task = asyncio.create_task(watch(), name="reboot-receipts")

    async def stop_delivery(self):
        if self.delivery_task:
            self.delivery_task.cancel()
            await asyncio.gather(self.delivery_task, return_exceptions=True)
            self.delivery_task = None

    def _finish(self, record, status, **fields):
        finished_at = time.time()
        duration_seconds = round(
            max(0.0, finished_at - float(record.get("created_at") or finished_at)),
            1,
        )
        lifecycle_state = fields.pop("lifecycle_state", None)
        warnings = list(dict.fromkeys(fields.get("warnings") or ()))
        fields["warnings"] = warnings
        outcome = fields.pop("outcome", None)
        if outcome is None:
            if status == "succeeded":
                outcome = "succeeded_with_warning" if warnings else "succeeded"
            elif status == "rejected":
                outcome = "rejected"
            elif status == "failed" and fields.get("restored"):
                outcome = "rolled_back"
            elif status == "unconfirmed" and any(
                bool(value) for value in (fields.get("online") or {}).values()
            ):
                outcome = "partial"
            else:
                outcome = "failed"
        observation_confidence = fields.pop("observation_confidence", None)
        if observation_confidence is None:
            observation_confidence = (
                "unconfirmed" if status == "unconfirmed" else "confirmed"
            )
        metrics = self._metrics_snapshot(record)
        fields.setdefault("stage_timings_ms", metrics["stage_timings_ms"])
        fields.setdefault("work_counts", metrics["work_counts"])
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
                outcome=outcome,
                observation_confidence=observation_confidence,
                finished_at=finished_at,
                duration_seconds=duration_seconds,
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
    async def _online_evidence(
        client,
        *,
        old_client=None,
        previous: Mapping[str, Any] | None = None,
        route_client=None,
    ):
        generation = getattr(client, "generation", None)
        receipt = getattr(generation, "receipt", None)
        expected_runtime = str(
            getattr(getattr(receipt, "runtime", None), "runtime_id", "") or ""
        )
        source_commit = str(getattr(receipt, "source_commit", "") or "")
        prior = dict(previous or {})
        old_pid = getattr(old_client, "pid", prior.get("old_pid"))
        new_pid = getattr(client, "pid", None)
        expected_nonce = str(
            getattr(client, "nonce", "")
            or getattr(client, "metadata", {}).get("worker_nonce")
            or ""
        )
        old_nonce = str(
            getattr(old_client, "nonce", "")
            or prior.get("old_worker_nonce")
            or ""
        )
        old_started_at = (
            getattr(old_client, "metadata", {}).get("worker_started_at")
            if old_client is not None
            else prior.get("old_worker_started_at")
        )
        process_alive = bool(client.process.is_alive())
        old_exited = (
            not old_client.process.is_alive() if old_client is not None else None
        )
        worker_identity_changed = not old_nonce or expected_nonce != old_nonce
        evidence = {
            "old_pid": old_pid,
            "new_pid": new_pid,
            "observed_pid": None,
            "pid_changed": old_pid is None or new_pid != old_pid,
            "old_exited": old_exited,
            "old_worker_nonce": old_nonce or None,
            "expected_worker_nonce": expected_nonce or None,
            "observed_worker_nonce": None,
            "worker_identity_changed": worker_identity_changed,
            "old_worker_started_at": old_started_at,
            "worker_started_at": None,
            "worker_adopted_at": None,
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
            "state": "unavailable",
            "failed_checks": [],
            "observation_confidence": "confirmed",
            "online": False,
        }
        if not process_alive:
            evidence["failed_checks"] = ["candidate_exited"]
            return evidence
        try:
            metadata = await client.call("worker.metadata", timeout=10)
            evidence.update(
                observed_pid=metadata.get("worker_pid"),
                observed_worker_nonce=metadata.get("worker_nonce"),
                worker_started_at=metadata.get("worker_started_at"),
                worker_adopted_at=metadata.get("worker_adopted_at"),
                observed_generation_id=metadata.get("generation_id"),
                observed_runtime_id=metadata.get("runtime_id"),
                observed_agent=metadata.get("name"),
                agent_matches=metadata.get("name") == client.agent_name,
                active=metadata.get("worker_phase") == "ACTIVE",
                accepting=bool(metadata.get("worker_accepting")),
                backend_ready=bool(metadata.get("backend_ready")),
                startup_success=bool(metadata.get("startup_success")),
            )
            failed_checks = []
            if (
                not evidence["agent_matches"]
                or evidence["observed_pid"] != new_pid
                or not expected_nonce
                or evidence["observed_worker_nonce"] != expected_nonce
                or not worker_identity_changed
            ):
                failed_checks.append("identity_mismatch")
            if evidence["observed_generation_id"] != evidence["generation_id"]:
                failed_checks.append("generation_mismatch")
            if expected_runtime and evidence["observed_runtime_id"] != expected_runtime:
                failed_checks.append("runtime_mismatch")
            if not evidence["active"] or not evidence["accepting"]:
                failed_checks.append("route_not_accepting")
            if not evidence["backend_ready"]:
                failed_checks.append("backend_not_ready")
            if not evidence["startup_success"]:
                failed_checks.append("startup_failed")
            if route_client is not None and route_client is not client:
                failed_checks.append("old_route_not_retired")
            evidence["failed_checks"] = list(dict.fromkeys(failed_checks))
            evidence["online"] = not evidence["failed_checks"]
            evidence["state"] = "online_new" if evidence["online"] else "unavailable"
        except Exception as exc:
            evidence["failed_checks"] = ["status_read_failed"]
            evidence["state"] = "unconfirmed"
            evidence["observation_confidence"] = "unconfirmed"
            evidence["status_error"] = type(exc).__name__
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

    async def _observe_target_activity(
        self,
        selected_targets: tuple[str, ...],
        handles: Mapping[str, AgentRuntimeHandle],
    ) -> tuple[dict[str, tuple[bool, int, int]], list[str]]:
        """Read the owning Workers, not asynchronously updated route caches.

        This is an observation, not an idle reservation: the transactional
        route fence and drain still handle work arriving after this check.
        """

        observations = await asyncio.gather(
            *(
                handles[name].client.call("worker.metadata", timeout=10)
                for name in selected_targets
            ),
            return_exceptions=True,
        )
        unavailable_targets: list[str] = []
        activity_by_target: dict[str, tuple[bool, int, int]] = {}
        for name, state in zip(selected_targets, observations, strict=True):
            if (
                not isinstance(state, dict)
                or type(state.get("is_generating")) is not bool
                or type(state.get("queue_depth")) is not int
                or state["queue_depth"] < 0
            ):
                unavailable_targets.append(name)
                continue
            detached = state.get("detached_request_count")
            activity_by_target[name] = (
                state["is_generating"],
                state["queue_depth"],
                detached if type(detached) is int and detached > 0 else 0,
            )
        return activity_by_target, unavailable_targets

    async def _preflight_target_activity(
        self,
        selected_targets: tuple[str, ...],
        handles: Mapping[str, AgentRuntimeHandle],
        record: Mapping[str, Any],
        *,
        busy_reason: str = "target_busy",
        unavailable_reason: str = "activity_unavailable",
    ) -> bool:
        """Reject a reboot before cutover when live Worker work cannot drain."""

        started = time.perf_counter()
        activity_by_target, unavailable_targets = await self._observe_target_activity(
            selected_targets, handles
        )
        busy_targets = [
            name
            for name, (generating, queue_depth, _detached) in activity_by_target.items()
            if generating or queue_depth
        ]

        self._record_stage(record, "activity_preflight", started)

        if unavailable_targets:
            for name in unavailable_targets:
                bridge_logger.warning(
                    "Reboot activity unavailable: target=%s", name
                )
            self._finish(
                record,
                "rejected",
                reason=unavailable_reason,
                activity_targets=unavailable_targets,
            )
            return False
        if busy_targets:
            for name in busy_targets:
                generating, queue_depth, _detached = activity_by_target[name]
                bridge_logger.info(
                    "Reboot target busy: target=%s generating=%s queued=%s",
                    name,
                    generating,
                    queue_depth,
                )
            self._finish(
                record,
                "rejected",
                reason=busy_reason,
                busy_targets=busy_targets,
            )
            return False
        return True

    async def _release_reboot_fences(self, record: Mapping[str, Any]) -> None:
        runtime_map = self.kernel._runtime_map()
        await asyncio.gather(
            *(
                runtime_map[name].release_reboot_fence()
                for name in record.get("targets", ())
                if name in runtime_map
            )
        )
        if record.get("mode") in BROAD_REBOOT_MODES and not getattr(
            self.kernel, "_handoff_draining", False
        ):
            gateway = getattr(self.kernel, "api_gateway", None)
            if gateway is not None and hasattr(gateway, "_accepting_requests"):
                gateway._accepting_requests = True

    async def _prepare_candidates(
        self,
        targets: tuple[str, ...],
        *,
        record: Mapping[str, Any] | None = None,
    ) -> dict[str, FunctionWorkerClient]:
        qualification_started = time.perf_counter()
        qualified_generation = getattr(
            self.kernel.function_workers, "qualified_generation", None
        )
        if callable(qualified_generation):
            generation = await qualified_generation()
        else:
            generation = await asyncio.to_thread(
                self.kernel.function_workers.qualify_generation
            )
        if record is not None:
            qualification_stats = dict(
                getattr(
                    self.kernel.function_workers,
                    "last_qualification_stats",
                    {},
                )
                or {}
            )
            source = qualification_stats.get("source")
            stage = (
                "generation_cache_lookup"
                if source in {"memory_cache", "disk_cache"}
                else "qualification"
            )
            self._record_stage(record, stage, qualification_started)
            counts = self._metrics_for(record)["work_counts"]
            counts["manifest_hash_count"] = int(
                qualification_stats.get("manifest_hash_count", 0)
            )
            counts["full_closure_import_count"] = int(
                qualification_stats.get("full_closure_import_count", 0)
            )
        self.kernel.function_workers.remember_generation(generation)
        prepare_generation = getattr(
            self.kernel.function_workers,
            "prepare_generation",
            None,
        )
        materialization_started = time.perf_counter()
        if callable(prepare_generation):
            generation, generation_root = await prepare_generation(
                generation,
                source_already_verified=True,
            )
        else:
            generation_root = None
        if record is not None:
            self._record_stage(record, "materialization", materialization_started)
            materialization_stats = dict(
                getattr(
                    self.kernel.function_workers,
                    "last_materialization_stats",
                    {},
                )
                or {}
            )
            counts = self._metrics_for(record)["work_counts"]
            counts["manifest_hash_count"] = int(
                counts.get("manifest_hash_count", 0)
            ) + int(materialization_stats.get("manifest_hash_count", 0))
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
        candidate_started = time.perf_counter()
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
        if record is not None:
            self._record_stage(record, "candidate_start", candidate_started)
            counts = self._metrics_for(record)["work_counts"]
            counts["candidate_worker_count"] = len(candidates)
            # Worker admission rechecks only the nonce-bound generation
            # receipt. It does not read or hash the artifact again.
            counts["manifest_hash_count"] = int(
                counts.get("manifest_hash_count", 0)
            )
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
        self._metrics_for(record)
        staged_shared = None
        result = False
        try:
            record = self.receipts.update(
                record["id"], status="running", phase="announcing"
            )
            notice_started = time.perf_counter()
            try:
                async with asyncio.timeout(REBOOT_START_NOTICE_TIMEOUT_SECONDS):
                    start_result = await self._deliver(record, starting=True)
            except Exception as exc:
                bridge_logger.warning(
                    "Reboot start notification failed (%s)", type(exc).__name__
                )
                start_result = {"sent": False}
            record = self._record_start_delivery(record, start_result)
            self._record_stage(record, "start_notice", notice_started)
            self.receipts.update(record["id"], phase="preparing")
            if str(restart.get("mode") or "same") in BROAD_REBOOT_MODES:
                selected_targets = tuple(record["targets"])
                try:
                    handles = self._target_handles(selected_targets)
                except ValueError as exc:
                    bridge_logger.error(
                        "Shared Function reboot scope rejected: %s", exc
                    )
                    self._finish(record, "rejected", reason="invalid_scope")
                    result = False
                else:
                    idle = await self._preflight_target_activity(
                        selected_targets, handles, record
                    )
                    if idle:
                        staged_shared = self._stage_shared_replacement(record)
                        result = True
            else:
                result = await self._perform_restart(restart, record)
                if result:
                    await self.resume_restored_work(restart)
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
                    await self._wait_for_workbench_control_release(record)
                    self._publish_shared_replacement(staged_shared)
                except Exception:
                    await self._release_reboot_fences(record)
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
            else:
                await self._release_reboot_fences(record)
            # The independent watcher owns retry I/O. A backlog must not keep
            # the lifecycle loop inside this reboot or delay the next command.
            enqueue_started = time.perf_counter()
            self._delivery_wake.set()
            self._record_stage(record, "notification_enqueue", enqueue_started)
            try:
                self.receipts.update(record["id"], **self._metrics_snapshot(record))
            except (OSError, ValueError):
                self.receipt_fault = True
                bridge_logger.exception("Reboot timing receipt could not be persisted")
            self._operation_metrics.pop(record["id"], None)
        return result

    async def _hand_back_stopped_work(self, restart, record) -> None:
        """After a failed cutover, return stopped work to the instance still serving."""
        raw = restart.get("resume_manifest") or record.get("resume_manifest")
        if not raw:
            return
        try:
            await self.resume_restored_work(
                {
                    **restart,
                    "operation_id": str(
                        record.get("id") or restart.get("operation_id") or ""
                    ),
                    "resume_manifest": raw,
                }
            )
        except Exception:
            bridge_logger.warning(
                "Reboot force: stopped work could not be handed back | operation=%s",
                record.get("id"),
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

        if not await self._preflight_target_activity(
            selected_targets, handles, record
        ):
            return False

        try:
            candidates = await self._prepare_candidates(
                selected_targets,
                record=record,
            )
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
            await self._hand_back_stopped_work(restart, record)
            return False

        if not await self._preflight_target_activity(
            selected_targets,
            handles,
            record,
            busy_reason="target_became_busy",
            unavailable_reason="activity_became_unavailable",
        ):
            await asyncio.gather(
                *(candidate.shutdown(force=True) for candidate in candidates.values()),
                return_exceptions=True,
            )
            await self._hand_back_stopped_work(restart, record)
            return False

        old_clients: dict[str, FunctionWorkerClient] = {}
        quiesced: set[str] = set()
        failure_reason = "route_busy"
        cutover_started = time.perf_counter()
        try:
            self._fence_reboot_routes(record)
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
                "verify_qualification_seal",
                getattr(generation, "verify_qualified_source", generation.verify),
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
            self._record_stage(record, "cutover", cutover_started)
        except (Exception, asyncio.CancelledError) as exc:
            self._record_stage(record, "cutover", cutover_started)
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
            if restored:
                # The previous generation serves again, so give its stopped work
                # back instead of leaving it only on disk.
                await self._hand_back_stopped_work(restart, record)
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
        warnings: list[str] = []
        try:
            self._publish_generation_state()
        except Exception as exc:
            warnings.append("generation_state_publish_failed")
            main_logger.exception(
                "Function Worker generation state publication failed after commit: %s",
                exc,
            )
            bridge_logger.exception(
                "Function Worker generation state publication failed after commit: %s",
                exc,
            )
        try:
            self.kernel.function_workers.schedule_topology()
        except Exception:
            warnings.append("topology_publish_failed")
            bridge_logger.exception("Topology notification failed after reboot commit")
        try:
            # Connector recovery follows the committed route as a separate,
            # non-blocking lifecycle action. Topology publication remains pure.
            activate_committed = getattr(
                self.kernel.function_workers,
                "activate_committed_connectors",
                None,
            )
            activate_workers = getattr(
                self.kernel.function_workers,
                "activate_worker_connectors",
                None,
            )
            if callable(activate_committed):
                await activate_committed(
                    selected_targets,
                    refresh_configuration=True,
                )
            elif callable(activate_workers):
                activate_workers(selected_targets)
        except Exception:
            warnings.append("connector_activation_deferred")
            bridge_logger.exception(
                "Worker connector activation was deferred after reboot commit"
            )
        # All old routes are fenced and every old Worker is QUIESCED. Their
        # bounded teardown is owned by the supervisor, outside local readiness.
        self.kernel.function_workers.retire_workers(old_clients.values())
        if any(
            bool(activation[name].get("telegram_bot_fingerprint"))
            and not bool(activation[name].get("telegram_connected"))
            for name in selected_targets
        ):
            warnings.append("connectors_unavailable")
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
        observation_started = time.perf_counter()
        evidence = dict(
            zip(
                selected_targets,
                await asyncio.gather(
                    *(
                        self._online_evidence(
                            handles[name].client,
                            old_client=old_clients[name],
                            route_client=handles[name].client,
                        )
                        for name in selected_targets
                    )
                ),
                strict=True,
            )
        )
        states = {name: bool(item["online"]) for name, item in evidence.items()}
        self._record_stage(record, "final_observation", observation_started)
        if any(item.get("old_exited") is False for item in evidence.values()):
            warnings.append("old_worker_exit_pending")
        warnings = list(dict.fromkeys(warnings))
        observation_confidence = (
            "confirmed"
            if all(
                item.get("observation_confidence") == "confirmed"
                for item in evidence.values()
            )
            else "unconfirmed"
        )
        online_count = sum(states.values())
        if online_count == len(states):
            legacy_status = "succeeded"
            lifecycle_state = "online"
            outcome = "succeeded_with_warning" if warnings else "succeeded"
        elif online_count:
            legacy_status = "unconfirmed"
            lifecycle_state = "committed"
            outcome = "partial"
        else:
            legacy_status = "unconfirmed"
            lifecycle_state = "unconfirmed"
            outcome = "failed"
        committed["workers"] = evidence
        self._finish(
            record,
            legacy_status,
            lifecycle_state=lifecycle_state,
            outcome=outcome,
            observation_confidence=observation_confidence,
            warnings=warnings,
            online=states,
            **committed,
            reason="" if online_count == len(states) else "readiness",
        )
        return True
# HASHI-Fingerprint: hfp_deb15dd9a31148b8b6fe122cb48e95bd
