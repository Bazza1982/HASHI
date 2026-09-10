"""Persistent PAO owner for automatic Agent move/clone finalisation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .coordinator import (
    confirm_outbound_move,
    continue_outbound_move,
    get_outbound_move,
)
from .package import AgentMoveError, utc_now_iso
from orchestrator.process_execution import process_is_alive
from orchestrator.agent_move_ui import render_background_notice
from orchestrator.telegram_delivery_failover import send_runtime_notice

bridge_logger = logging.getLogger("BridgeU.Bridge")

_SCHEMA_VERSION = 1
_RECOVERABLE_STATUSES = frozenset({"accepted", "running", "retry_wait"})
_MOVE_CONTINUATION_STATUSES = frozenset(
    {
        "source_disabled_target_committed",
        "activating_target",
        "move_completed_cleanup_pending",
        "moved_pending_reboots",
    }
)
_MAX_RECORDS = 128
_MAX_STATE_BYTES = 512 * 1024
_WATCH_INTERVAL_SECONDS = 0.25
_MAX_DELIVERY_ATTEMPTS = 5
_MAX_EXECUTION_ATTEMPTS = 5
_EMPTY_LOCK_STALE_SECONDS = 1.0


class AgentMoveManager:
    """Own terminal move progress outside the initiating Agent Worker.

    Coordinator state remains authoritative for transfer semantics.  This
    manager persists only the execution intent and the peer snapshot needed to
    resume that state machine after a shared Functions replacement.
    """

    def __init__(self, kernel: Any) -> None:
        self.kernel = kernel
        self.root = Path(kernel.paths.bridge_home).expanduser().resolve()
        self.state_path = (
            self.root / "state" / "instance" / "agent-move-autofinalize.json"
        )
        self._tasks: dict[str, asyncio.Task] = {}
        self._watch_task: asyncio.Task | None = None
        self._stopping = False
        self._last_scan_error: str | None = None

    async def start(self) -> None:
        """Resume only operations interrupted by process replacement/crash."""

        self._stopping = False
        try:
            records = self._load_operations()
        except (AgentMoveError, OSError) as exc:
            records = {}
            self._last_scan_error = f"{type(exc).__name__}: {exc}"
            bridge_logger.error(
                "Agent move recovery state is unavailable; transfers are paused: %s",
                self._last_scan_error,
            )
        for package_id, record in records.items():
            if _ready_to_execute(record):
                self._spawn(package_id)
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = asyncio.create_task(
                self._watch_submissions(),
                name="agent-move-submissions",
            )

    async def stop(self) -> None:
        """Yield ownership without converting resumable work into a failure."""

        self._stopping = True
        tasks = list(self._tasks.values())
        if self._watch_task is not None:
            tasks.append(self._watch_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._watch_task = None

    def submit(
        self,
        package_id: str,
        instances: Mapping[str, Any],
        *,
        requested_by: str | None = None,
        origin: Mapping[str, Any] | None = None,
        locale: str | None = None,
        _schedule: bool = True,
    ) -> dict[str, Any]:
        """Durably accept one confirmation and start its terminal lifecycle."""

        move_id = str(package_id or "").strip()
        if not move_id:
            raise AgentMoveError("Agent move package_id is required")
        with self._state_lock():
            directory = self._read_operations_unlocked()
            previous = directory.get(move_id)
            if previous is None:
                outbound = get_outbound_move(self.root, move_id)
                operation = str(outbound.get("operation") or "move").strip().lower()
                if operation not in {"move", "clone"}:
                    raise AgentMoveError(
                        "automatic finalisation does not support operation "
                        f"{operation!r}"
                    )
                record: dict[str, Any] = {
                    "package_id": move_id,
                    "operation": operation,
                    "agent_id": str(outbound.get("agent_id") or ""),
                    "requested_by": str(requested_by or ""),
                    "origin": _json_mapping(origin or {}, field="origin"),
                    "locale": str(locale or ""),
                    "status": "accepted",
                    "accepted_at": utc_now_iso(),
                    "updated_at": utc_now_iso(),
                    "instances": _json_mapping(instances, field="instances"),
                    "delivery": {
                        "status": (
                            "pending"
                            if (origin or {}).get("chat_id") is not None
                            else "not_requested"
                        ),
                        "attempts": 0,
                        "next_attempt_at": 0.0,
                    },
                }
                directory[move_id] = record
                self._write_operations_unlocked(directory)
                duplicate = False
            else:
                record = previous
                duplicate = True
                if record.get("status") == "needs_recovery":
                    record.update(
                        status="accepted",
                        execution_attempts=0,
                        retry_requested_at=utc_now_iso(),
                        updated_at=utc_now_iso(),
                    )
                    directory[move_id] = record
                    self._write_operations_unlocked(directory)
        if _schedule and _ready_to_execute(record):
            self._spawn(move_id)
        return {
            "accepted": True,
            "duplicate": duplicate,
            "operation": self._public_record(record),
        }

    def status(self, package_id: str) -> dict[str, Any] | None:
        with self._state_lock():
            record = self._read_operations_unlocked().get(
                str(package_id or "").strip()
            )
        return self._public_record(record) if record is not None else None

    async def wait(self, package_id: str) -> dict[str, Any]:
        """Wait for the in-process attempt; intended for tests/admin callers."""

        move_id = str(package_id or "").strip()
        deadline = asyncio.get_running_loop().time() + 10.0
        while True:
            task = self._tasks.get(move_id)
            if task is not None:
                await asyncio.shield(task)
            record = self.status(move_id)
            if record is None:
                raise AgentMoveError(f"unknown automatic Agent move {move_id!r}")
            if record.get("status") not in _RECOVERABLE_STATUSES:
                return record
            if asyncio.get_running_loop().time() >= deadline:
                return record
            await asyncio.sleep(0.01)

    async def _watch_submissions(self) -> None:
        while True:
            try:
                for package_id, record in self._load_operations().items():
                    if _ready_to_execute(record):
                        self._spawn(package_id)
                    elif record.get("status") in {"completed", "needs_recovery"}:
                        await self._deliver_result(package_id, record)
                self._last_scan_error = None
            except Exception as exc:  # noqa: BLE001 - watcher must remain recoverable
                error = f"{type(exc).__name__}: {exc}"
                if error != self._last_scan_error:
                    bridge_logger.warning(
                        "Agent move submission scan failed: %s",
                        error,
                    )
                    self._last_scan_error = error
            await asyncio.sleep(_WATCH_INTERVAL_SECONDS)

    async def _deliver_result(
        self,
        package_id: str,
        record: Mapping[str, Any],
    ) -> None:
        delivery = dict(record.get("delivery") or {})
        if delivery.get("status") != "pending":
            return
        now = time.time()
        if float(delivery.get("next_attempt_at") or 0.0) > now:
            return
        attempts = int(delivery.get("attempts") or 0)
        if attempts >= _MAX_DELIVERY_ATTEMPTS:
            self._update(
                package_id,
                delivery={**delivery, "status": "exhausted"},
            )
            return
        attempts += 1
        delivery.update(
            attempts=attempts,
            next_attempt_at=now + 5 * (3 ** (attempts - 1)),
        )
        self._update(package_id, delivery=delivery)
        origin = record.get("origin") or {}
        result = await send_runtime_notice(
            self.kernel,
            source_agent=str(record.get("requested_by") or record.get("agent_id") or ""),
            chat_id=int(origin["chat_id"]),
            thread_id=origin.get("thread_id"),
            render_text=lambda sender, display: render_background_notice(
                record,
                sender=sender,
                sender_display=display,
            ),
        )
        if result.get("sent"):
            delivery.update(
                status="sent",
                sender=result.get("sender"),
                message_id=result.get("message_id"),
                sent_at=utc_now_iso(),
            )
        elif attempts >= _MAX_DELIVERY_ATTEMPTS:
            delivery["status"] = "exhausted"
        self._update(package_id, delivery=delivery)

    def _spawn(self, package_id: str) -> None:
        if self._stopping:
            return
        existing = self._tasks.get(package_id)
        if existing is not None and not existing.done():
            return
        try:
            task = asyncio.create_task(
                self._execute(package_id),
                name=f"agent-move-finalize:{package_id}",
            )
        except RuntimeError as exc:
            raise AgentMoveError(
                "automatic Agent move requires a running Functions event loop"
            ) from exc
        self._tasks[package_id] = task
        task.add_done_callback(
            lambda completed, move_id=package_id: self._task_done(move_id, completed)
        )

    def _task_done(self, package_id: str, task: asyncio.Task) -> None:
        if self._tasks.get(package_id) is task:
            self._tasks.pop(package_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:  # Defensive: _execute records ordinary failures.
            bridge_logger.error(
                "Agent move manager task escaped: package=%s error=%s",
                package_id,
                type(error).__name__,
            )

    async def _execute(self, package_id: str) -> None:
        try:
            record = self._require_record(package_id)
            attempt = int(record.get("execution_attempts") or 0) + 1
            self._update(
                package_id,
                status="running",
                started_at=utc_now_iso(),
                execution_attempts=attempt,
                next_attempt_at=None,
            )
            record = self._require_record(package_id)
            instances = dict(record.get("instances") or {})
            outbound = await asyncio.to_thread(
                get_outbound_move,
                self.root,
                package_id,
            )
            operation = str(outbound.get("operation") or record.get("operation") or "")
            status = str(outbound.get("status") or "")

            if status == "completed":
                result = outbound
            elif operation == "move" and status in _MOVE_CONTINUATION_STATUSES:
                result = await self._stop_and_continue(outbound, instances)
            else:
                result = await asyncio.to_thread(
                    confirm_outbound_move,
                    self.root,
                    instances,
                    package_id,
                )
                if (
                    operation == "move"
                    and result.get("status") in _MOVE_CONTINUATION_STATUSES
                ):
                    result = await self._stop_and_continue(result, instances)

            if result.get("status") != "completed":
                raise AgentMoveError(
                    "automatic Agent transfer did not reach terminal completion "
                    f"(status={result.get('status')!r})"
                )
            self._update(
                package_id,
                status="completed",
                completed_at=utc_now_iso(),
                result=_json_mapping({key: value for key, value in result.items() if key not in {"workspace_inventory", "discarded_files"}}, field="result"),
                last_error=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable recovery boundary
            record = self._require_record(package_id)
            attempts = int(record.get("execution_attempts") or 1)
            retry = attempts < _MAX_EXECUTION_ATTEMPTS
            self._update(
                package_id,
                status="retry_wait" if retry else "needs_recovery",
                failed_at=utc_now_iso(),
                last_error=str(exc),
                next_attempt_at=(
                    time.time() + 3 ** (attempts - 1) if retry else None
                ),
            )
            bridge_logger.warning(
                "Automatic Agent transfer %s: package=%s attempt=%s error=%s",
                "will retry" if retry else "needs recovery",
                package_id,
                attempts,
                exc,
            )

    async def _stop_and_continue(
        self,
        outbound: Mapping[str, Any],
        instances: Mapping[str, Any],
    ) -> dict[str, Any]:
        package_id = str(outbound.get("package_id") or "")
        source_agent = str(
            outbound.get("source_agent_id") or outbound.get("agent_id") or ""
        )
        if not source_agent:
            raise AgentMoveError("automatic Agent move has no source Agent")
        stop_result = await self.kernel.stop_agent(
            source_agent,
            reason="agent-move-cutover",
        )
        ok, message = bool(stop_result[0]), str(stop_result[1])
        if not ok and not _already_stopped(message):
            raise AgentMoveError(f"source connector could not be stopped: {message}")
        return await asyncio.to_thread(
            continue_outbound_move,
            self.root,
            dict(instances),
            package_id,
        )

    def _require_record(self, package_id: str) -> dict[str, Any]:
        with self._state_lock():
            record = self._read_operations_unlocked().get(package_id)
        if record is None:
            raise AgentMoveError(f"automatic Agent move {package_id!r} disappeared")
        return record

    def _update(self, package_id: str, **changes: Any) -> dict[str, Any]:
        with self._state_lock():
            operations = self._read_operations_unlocked()
            record = operations.get(package_id)
            if record is None:
                raise AgentMoveError(f"unknown automatic Agent move {package_id!r}")
            for key, value in changes.items():
                if value is None:
                    record.pop(key, None)
                else:
                    record[key] = value
            record["updated_at"] = utc_now_iso()
            operations[package_id] = record
            self._write_operations_unlocked(operations)
            return record

    def _load_operations(self) -> dict[str, dict[str, Any]]:
        with self._state_lock():
            return self._read_operations_unlocked()

    def _read_operations_unlocked(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.state_path.read_bytes()
        except FileNotFoundError:
            return {}
        if len(raw) > _MAX_STATE_BYTES:
            raise AgentMoveError("automatic Agent move state exceeds its size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AgentMoveError("automatic Agent move state is invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(payload.get("operations"), dict)
        ):
            raise AgentMoveError("automatic Agent move state is invalid")
        operations = payload["operations"]
        if len(operations) > _MAX_RECORDS:
            raise AgentMoveError("automatic Agent move state has too many records")
        if any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in operations.items()
        ):
            raise AgentMoveError("automatic Agent move state is invalid")
        return {key: dict(value) for key, value in operations.items()}

    def _write_operations_unlocked(self, operations: Mapping[str, Any]) -> None:
        rows = dict(operations)
        if len(rows) > _MAX_RECORDS:
            terminal = sorted(
                (
                    (key, value)
                    for key, value in rows.items()
                    if value.get("status") not in _RECOVERABLE_STATUSES
                ),
                key=lambda item: str(item[1].get("updated_at") or ""),
            )
            for key, _value in terminal:
                if len(rows) <= _MAX_RECORDS:
                    break
                rows.pop(key, None)
        if len(rows) > _MAX_RECORDS:
            raise AgentMoveError("automatic Agent move state is full")
        payload = {"schema_version": _SCHEMA_VERSION, "operations": rows}
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if len(encoded) > _MAX_STATE_BYTES:
            raise AgentMoveError("automatic Agent move state exceeds its size limit")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=self.state_path.parent,
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.state_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @contextmanager
    def _state_lock(self):
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor: int | None = None
        deadline = time.monotonic() + 5.0
        while descriptor is None:
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError as exc:
                if _orphaned_lock(lock_path):
                    lock_path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise AgentMoveError(
                        "automatic Agent move state is busy"
                    ) from exc
                time.sleep(0.02)
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode())
            os.close(descriptor)
            descriptor = None
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    @staticmethod
    def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key != "instances"
        }


def _json_mapping(value: Mapping[str, Any], *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgentMoveError(f"automatic Agent move {field} must be an object")
    try:
        copied = json.loads(json.dumps(dict(value), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise AgentMoveError(
            f"automatic Agent move {field} is not JSON-compatible"
        ) from exc
    if not isinstance(copied, dict):  # pragma: no cover - Mapping always encodes object
        raise AgentMoveError(f"automatic Agent move {field} must be an object")
    return copied


def _already_stopped(message: str) -> bool:
    normalized = str(message or "").casefold()
    return any(marker in normalized for marker in ("not running", "already stopped"))


def _orphaned_lock(path: Path) -> bool:
    try:
        parts = path.read_text(encoding="utf-8", errors="replace").split()
        pid = int(next(part.removeprefix("pid=") for part in parts if part.startswith("pid=")))
    except (OSError, StopIteration, TypeError, ValueError):
        try:
            return time.time() - path.stat().st_mtime >= _EMPTY_LOCK_STALE_SECONDS
        except OSError:
            return False
    return not process_is_alive(pid)


def _ready_to_execute(record: Mapping[str, Any]) -> bool:
    status = str(record.get("status") or "")
    if status not in _RECOVERABLE_STATUSES:
        return False
    if status != "retry_wait":
        return True
    try:
        return float(record.get("next_attempt_at") or 0.0) <= time.time()
    except (TypeError, ValueError):
        return False


def enqueue_agent_move(
    hashi_root: Path | str,
    instances: Mapping[str, Any],
    package_id: str,
    *,
    requested_by: str = "cli",
    origin: Mapping[str, Any] | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Persist an inbox item for the running/recovering shared Functions owner."""

    kernel = SimpleNamespace(paths=SimpleNamespace(bridge_home=Path(hashi_root)))
    manager = AgentMoveManager(kernel)
    return manager.submit(
        package_id,
        instances,
        requested_by=requested_by,
        origin=origin,
        locale=locale,
        _schedule=False,
    )


def get_agent_move_execution_status(
    hashi_root: Path | str,
    package_id: str,
) -> dict[str, Any] | None:
    kernel = SimpleNamespace(paths=SimpleNamespace(bridge_home=Path(hashi_root)))
    return AgentMoveManager(kernel).status(package_id)


def record_agent_move_admin_outcome(
    hashi_root: Path | str,
    package_id: str,
    result: Mapping[str, Any],
) -> None:
    """Reconcile an explicit continue/cancel with the background receipt."""

    kernel = SimpleNamespace(paths=SimpleNamespace(bridge_home=Path(hashi_root)))
    manager = AgentMoveManager(kernel)
    move_id = str(package_id or "").strip()
    with manager._state_lock():
        operations = manager._read_operations_unlocked()
        record = operations.get(move_id)
        if record is None:
            return
        status = str(result.get("status") or "")
        record.update(
            status=(
                "completed"
                if status == "completed"
                else "cancelled"
                if status == "cancelled"
                else "needs_recovery"
            ),
            result=_json_mapping({key: value for key, value in result.items() if key not in {"workspace_inventory", "discarded_files"}}, field="result"),
            updated_at=utc_now_iso(),
        )
        record.pop("next_attempt_at", None)
        operations[move_id] = record
        manager._write_operations_unlocked(operations)
