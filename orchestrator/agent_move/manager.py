"""Persistent PAO owner for automatic Agent move/clone finalisation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from collections.abc import Mapping
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import HTTPError, URLError

from .coordinator import (
    confirm_outbound_move,
    continue_outbound_move,
    get_outbound_move,
)
from .package import AgentMoveError, utc_now_iso
from orchestrator.config_json import file_write_lock
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
_JOURNAL_SCHEMA_VERSION = 1
_CROSS_PROCESS_RECOVERY_SECONDS = 30.0
_CROSS_PROCESS_INBOX_CHECK_SECONDS = 1.0
_MAX_DELIVERY_ATTEMPTS = 0  # Zero means no automatic attempt limit.
_MAX_EXECUTION_ATTEMPTS = 0


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
        self.journal_path = self.state_path.with_suffix(".sqlite3")
        self._tasks: dict[str, asyncio.Task] = {}
        self._watch_task: asyncio.Task | None = None
        self._watch_event = asyncio.Event()
        self._stopping = False
        self._last_scan_error: str | None = None
        self._state_signature = self._state_file_signature()

    async def start(self) -> None:
        """Resume only operations interrupted by process replacement/crash."""

        self._stopping = False
        try:
            records = self._load_operations_snapshot()
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
                self._watch_submissions(records),
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
            previous = self._read_record_unlocked(move_id)
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
                            if (origin or {}).get("session_id") or (origin or {}).get("chat_id") is not None
                            else "not_requested"
                        ),
                        "attempts": 0,
                        "next_attempt_at": 0.0,
                    },
                }
                self._write_record_unlocked(move_id, record)
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
                        instances=_json_mapping(instances, field="instances"),
                    )
                    if (record.get("origin") or {}).get("session_id") or (record.get("origin") or {}).get("chat_id") is not None:
                        record["delivery"] = {"status": "pending", "attempts": 0, "next_attempt_at": 0.0}
                    self._write_record_unlocked(move_id, record)
                elif record.get("status") == "completed":
                    delivery = dict(record.get("delivery") or {})
                    if delivery.get("status") == "exhausted" and delivery.get("retry_safe") is True:
                        # An explicit repeat confirmation may resume only a
                        # proven-unsent notice, never the completed transfer.
                        delivery.update(status="pending", attempts=0, next_attempt_at=0.0)
                        record["delivery"] = delivery
                        self._write_record_unlocked(move_id, record)
                        self._watch_event.set()
        if _schedule and _ready_to_execute(record):
            self._spawn(move_id)
        return {
            "accepted": True,
            "duplicate": duplicate,
            "operation": self._public_record(record),
        }

    def status(self, package_id: str) -> dict[str, Any] | None:
        with self._state_lock():
            record = self._read_record_unlocked(
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

    async def _watch_submissions(
        self,
        records: Mapping[str, Mapping[str, Any]],
    ) -> None:
        while True:
            try:
                for package_id, record in records.items():
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
            delay = _next_watcher_delay(records)
            await self._wait_for_submission_change(delay)
            records = self._load_operations_snapshot()

    def _load_operations_snapshot(self) -> dict[str, dict]:
        signature_before = self._state_file_signature()
        records = self._load_operations()
        signature_after = self._state_file_signature()
        # Atomic replacement can race the read. Retain the earlier signature
        # in that narrow case so the next metadata check reloads immediately.
        self._state_signature = (
            signature_after
            if signature_before == signature_after
            else signature_before
        )
        return records

    def _state_file_signature(self) -> tuple[str, int, int, int] | None:
        path = self.journal_path if self.journal_path.exists() else self.state_path
        try:
            stat = path.stat()
        except OSError:
            return None
        return path.name, int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_ino)

    async def _wait_for_submission_change(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.01, float(delay))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    self._watch_event.wait(),
                    timeout=min(_CROSS_PROCESS_INBOX_CHECK_SECONDS, remaining),
                )
            except asyncio.TimeoutError:
                signature = self._state_file_signature()
                if signature != self._state_signature:
                    self._state_signature = signature
                    return
                continue
            self._watch_event.clear()
            return

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
        budget = _retry_budget("HASHI_AGENT_MOVE_DELIVERY_MAX_ATTEMPTS", _MAX_DELIVERY_ATTEMPTS)
        if budget and attempts >= budget:
            self._update(
                package_id,
                delivery={**delivery, "status": "exhausted"},
            )
            return
        attempts += 1
        origin = record.get("origin") or {}
        delivery.update(
            attempts=attempts,
            next_attempt_at=now + min(300, 5 * (3 ** min(attempts - 1, 4))),
        )
        delivery.pop("retry_safe", None)
        if not origin.get("session_id"):
            # Legacy direct Telegram has no durable claim or provider
            # idempotency key. If the process dies during the send, the
            # previous outcome is unknown and must not be replayed blindly.
            delivery["status"] = "unknown"
        self._update(package_id, delivery=delivery)
        if origin.get("session_id"):
            from orchestrator.frontend_notice_delivery import deliver_notice

            result = await deliver_notice(
                self.kernel, agent_id=str(record.get("requested_by") or record.get("agent_id") or ""),
                origin=origin, text=render_background_notice(record),
                publication_id=f"agent-transfer:{package_id}:{record.get('status')}",
                send_notice=send_runtime_notice,
                recovery_category="transfer_result",
            )
        else:
            result = await send_runtime_notice(
                self.kernel,
                source_agent=str(record.get("requested_by") or record.get("agent_id") or ""),
                operation_id=f"agent-transfer:{package_id}:{record.get('status')}",
                notice_kind="final",
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
        elif result.get("deferred"):
            # Nothing was sent while the connector was offline or another
            # dispatcher owned the claim; this is not a failed attempt.
            delivery["attempts"] = attempts - 1
            delivery["status"] = "pending"
        elif result.get("delivery_state") in {"unknown", "failed", "expired", "suppressed"}:
            delivery["status"] = result["delivery_state"]
        elif result.get("retry_safe") is not True:
            delivery["status"] = "unknown"
        else:
            delivery["retry_safe"] = True
            delivery["status"] = "exhausted" if budget and attempts >= budget else "pending"
        if delivery.get("status") == "pending" and result.get("retry_after") is not None:
            try:
                retry_after = max(0.0, float(result["retry_after"]))
            except (ValueError, TypeError):
                retry_after = 0.0
            delivery["next_attempt_at"] = max(delivery["next_attempt_at"], now + retry_after)
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
        self._watch_event.set()

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

            if outbound.get("rollback_completed"):
                raise AgentMoveError("transfer was rolled back; prepare a fresh transfer")
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
            try:
                failed_outbound = await asyncio.to_thread(get_outbound_move, self.root, package_id)
            except Exception:
                failed_outbound = {}
            failed_operation = str(
                failed_outbound.get("operation") or record.get("operation") or ""
            )
            budget = _retry_budget("HASHI_AGENT_MOVE_EXECUTION_MAX_ATTEMPTS", _MAX_EXECUTION_ATTEMPTS)
            forward_only = failed_operation == "move" and (
                failed_outbound.get("source_disabled") is True
                or failed_outbound.get("status") in _MOVE_CONTINUATION_STATUSES
            )
            transient = _retryable_transfer_error(exc)
            retry = not failed_outbound.get("rollback_completed") and (
                forward_only
                or failed_outbound.get("scope_abort_pending") is True
                or (transient and (failed_operation == "move" or not budget or attempts < budget))
            )
            self._update(
                package_id,
                status="retry_wait" if retry else "needs_recovery",
                failed_at=utc_now_iso(),
                last_error=str(exc),
                last_error_kind="transient" if transient else "requires_recovery",
                next_attempt_at=(
                    time.time() + min(60, 3 ** min(attempts - 1, 4))
                    if retry
                    else None
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
            record = self._read_record_unlocked(package_id)
        if record is None:
            raise AgentMoveError(f"automatic Agent move {package_id!r} disappeared")
        return record

    def _update(self, package_id: str, **changes: Any) -> dict[str, Any]:
        with self._state_lock():
            record = self._read_record_unlocked(package_id)
            if record is None:
                raise AgentMoveError(f"unknown automatic Agent move {package_id!r}")
            for key, value in changes.items():
                if value is None:
                    record.pop(key, None)
                else:
                    record[key] = value
            record["updated_at"] = utc_now_iso()
            self._write_record_unlocked(package_id, record)
        self._watch_event.set()
        return record

    def _load_operations(self) -> dict[str, dict[str, Any]]:
        with self._state_lock():
            return self._read_operations_unlocked()

    def _read_legacy_operations_unlocked(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self.state_path.read_bytes()
        except FileNotFoundError:
            return {}
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
        if any(
            not isinstance(key, str) or not isinstance(value, dict)
            for key, value in operations.items()
        ):
            raise AgentMoveError("automatic Agent move state is invalid")
        return {key: dict(value) for key, value in operations.items()}

    @staticmethod
    def _encode_record(key: str, value: Any) -> str:
        if not isinstance(key, str) or not isinstance(value, dict):
            raise AgentMoveError("automatic Agent move state is invalid")
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise AgentMoveError("automatic Agent move state is invalid") from exc

    @staticmethod
    def _decode_record(raw: str) -> dict[str, Any]:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AgentMoveError("automatic Agent move state is invalid") from exc
        if not isinstance(value, dict):
            raise AgentMoveError("automatic Agent move state is invalid")
        return value

    def _ensure_journal_unlocked(self) -> None:
        if self.journal_path.exists():
            return
        # The old JSON remains an untouched migration snapshot. Install the
        # database only after every legacy row has been committed to a new file.
        legacy = self._read_legacy_operations_unlocked()
        legacy_fingerprint = self._legacy_fingerprint_unlocked()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=self.state_path.parent,
            prefix=f".{self.journal_path.name}.",
            suffix=".tmp",
        )
        os.close(handle)
        try:
            with closing(sqlite3.connect(temp_name)) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute(
                    "CREATE TABLE operations (package_id TEXT PRIMARY KEY, record TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO metadata (key, value) VALUES ('legacy_fingerprint', ?)",
                    (legacy_fingerprint,),
                )
                connection.execute(f"PRAGMA user_version={_JOURNAL_SCHEMA_VERSION}")
                connection.executemany(
                    "INSERT INTO operations (package_id, record) VALUES (?, ?)",
                    ((key, self._encode_record(key, value)) for key, value in legacy.items()),
                )
                connection.commit()
            with open(temp_name, "r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temp_name, self.journal_path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @contextmanager
    def _journal_unlocked(self):
        try:
            self._ensure_journal_unlocked()
            with closing(sqlite3.connect(self.journal_path, timeout=30)) as connection:
                connection.execute("PRAGMA synchronous=FULL")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version != _JOURNAL_SCHEMA_VERSION:
                    raise AgentMoveError("automatic Agent move journal has an unsupported version")
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'legacy_fingerprint'"
                ).fetchone()
                if row is None or row[0] != self._legacy_fingerprint_unlocked():
                    raise AgentMoveError(
                        "legacy Agent move state changed after journal migration; "
                        "reconcile both copies before continuing"
                    )
                with connection:
                    yield connection
        except sqlite3.Error as exc:
            raise AgentMoveError(f"automatic Agent move journal is unavailable: {exc}") from exc

    def _legacy_fingerprint_unlocked(self) -> str:
        try:
            with self.state_path.open("rb") as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except FileNotFoundError:
            return "missing"
        return digest.hexdigest()

    def _read_operations_unlocked(self) -> dict[str, dict[str, Any]]:
        with self._journal_unlocked() as connection:
            return {
                key: self._decode_record(raw)
                for key, raw in connection.execute("SELECT package_id, record FROM operations")
            }

    def _read_record_unlocked(self, package_id: str) -> dict[str, Any] | None:
        with self._journal_unlocked() as connection:
            row = connection.execute(
                "SELECT record FROM operations WHERE package_id = ?", (package_id,)
            ).fetchone()
            return self._decode_record(row[0]) if row is not None else None

    def _apply_retention_unlocked(self, connection: sqlite3.Connection, *, keep: str | None) -> None:
        configured = os.environ.get("HASHI_AGENT_MOVE_DELIVERED_RECEIPT_RETENTION", "").strip()
        if not configured:
            return  # No automatic removal unless the operator explicitly configures it.
        try:
            retain = int(configured)
            if retain < 0:
                raise ValueError
        except ValueError:
            bridge_logger.warning(
                "Ignoring invalid Agent move delivered receipt retention; no receipts will be pruned"
            )
            return
        eligible = []
        for key, raw in connection.execute("SELECT package_id, record FROM operations"):
            record = self._decode_record(raw)
            delivery = record.get("delivery") or {}
            if (
                record.get("status") in {"completed", "cancelled"}
                and isinstance(delivery, dict)
                and delivery.get("status", "not_requested") in {"sent", "not_requested"}
            ):
                eligible.append((str(record.get("updated_at") or ""), key))
        eligible.sort(reverse=True)
        connection.executemany(
            "DELETE FROM operations WHERE package_id = ?",
            ((key,) for _updated, key in eligible[retain:] if key != keep),
        )

    def _write_record_unlocked(self, package_id: str, record: dict[str, Any]) -> None:
        encoded = self._encode_record(package_id, record)
        with self._journal_unlocked() as connection:
            connection.execute(
                "INSERT INTO operations (package_id, record) VALUES (?, ?) "
                "ON CONFLICT(package_id) DO UPDATE SET record = excluded.record",
                (package_id, encoded),
            )
            self._apply_retention_unlocked(connection, keep=package_id)

    def _write_operations_unlocked(self, operations: Mapping[str, Any], *, keep: str | None = None) -> None:
        # Bulk replacement is used for importing a complete snapshot, not for
        # ordinary progress updates, which touch only their own operation.
        rows = [(key, self._encode_record(key, value)) for key, value in operations.items()]
        with self._journal_unlocked() as connection:
            connection.execute("DELETE FROM operations")
            connection.executemany(
                "INSERT INTO operations (package_id, record) VALUES (?, ?)", rows
            )
            self._apply_retention_unlocked(connection, keep=keep)

    @contextmanager
    def _state_lock(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with ExitStack() as locks:
            try:
                locks.enter_context(file_write_lock(self.state_path))
            except TimeoutError as exc:
                raise AgentMoveError("automatic Agent transfer state is busy") from exc
            yield

    @staticmethod
    def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key != "instances"
        }


def _retry_budget(name: str, default: int) -> int:
    configured = os.environ.get(name, "").strip()
    if not configured:
        return default
    try:
        value = int(configured)
        if value < 0:
            raise ValueError
        return value
    except ValueError:
        bridge_logger.warning("Ignoring invalid %s; using the default retry budget", name)
        return default


def _retryable_transfer_error(error: BaseException) -> bool:
    """Classify known transient causes without guessing from rendered text."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, HTTPError):
            return current.code in {408, 425, 429} or 500 <= current.code <= 599
        if isinstance(current, (FileNotFoundError, PermissionError)):
            return False
        if isinstance(current, (URLError, TimeoutError, ConnectionError, OSError)):
            return True
        current = current.__cause__
    return False


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


def _next_watcher_delay(records: Mapping[str, Mapping[str, Any]]) -> float:
    """Sleep until real retry work or the bounded cross-process fallback."""

    now = time.time()
    deadlines: list[float] = []
    for record in records.values():
        if str(record.get("status") or "") == "retry_wait":
            try:
                deadline = float(record.get("next_attempt_at") or 0.0)
            except (TypeError, ValueError):
                deadline = 0.0
            if deadline > now:
                deadlines.append(deadline)
        if record.get("status") not in {"completed", "needs_recovery"}:
            continue
        delivery = record.get("delivery")
        if not isinstance(delivery, Mapping) or delivery.get("status") != "pending":
            continue
        try:
            deadline = float(delivery.get("next_attempt_at") or 0.0)
        except (TypeError, ValueError):
            deadline = 0.0
        if deadline > now:
            deadlines.append(deadline)
    if not deadlines:
        return _CROSS_PROCESS_RECOVERY_SECONDS
    return max(
        0.01,
        min(_CROSS_PROCESS_RECOVERY_SECONDS, min(deadlines) - now),
    )


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
        record = manager._read_record_unlocked(move_id)
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
        manager._write_record_unlocked(move_id, record)
