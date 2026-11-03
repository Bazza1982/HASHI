"""Bounded Functions continuation of one saved Bot credential, via PAO only.

This coordinator holds no credential and owns no Worker lifecycle. Only an
unreceipted admission-busy result may be retried. A durable receipt or unknown
acknowledgement always ends automatic submission.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from uuid import uuid4

logger = logging.getLogger("HASHI.TelegramCredentialActivation")


@dataclass
class _Activation:
    agent_id: str
    revision: str
    lifecycle_id: str
    stop_epoch: int
    owner_id: str
    deadline: float
    invocation_id: str = field(default_factory=lambda: "telegram-credential:" + uuid4().hex)
    phase: str = "waiting"
    operation_id: str | None = None
    error_code: str | None = None
    task: asyncio.Task | None = None


class TelegramCredentialActivation:
    def __init__(self, *, status, runtime_map, execute, operation):
        self._status = status
        self._runtime_map = runtime_map
        self._execute = execute
        self._operation = operation
        self._entries: dict[str, _Activation] = {}
        self.poll_seconds = 2.0
        self.wait_seconds = 15 * 60.0
        self._closed = False

    def _runtime(self, agent_id):
        runtime = self._runtime_map().get(agent_id.casefold())
        reader = getattr(runtime, "get_runtime_metadata", None)
        metadata = reader() if callable(reader) else {}
        return runtime, metadata

    @staticmethod
    def _identity(metadata):
        lifecycle = str(metadata.get("agent_lifecycle_id") or "").strip()
        epoch = metadata.get("agent_stop_epoch", 0)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            return "", -1
        return lifecycle, epoch

    def _matches(self, entry, status, metadata):
        return (entry.revision == status["credential_revision"]
                and (not entry.lifecycle_id or self._identity(metadata)
                     == (entry.lifecycle_id, entry.stop_epoch)))

    @staticmethod
    def _busy(metadata):
        # Unknown activity is not permission to interrupt a Worker.
        required = ("is_generating", "queue_depth", "detached_request_count")
        if any(key not in metadata for key in required):
            return None
        if (metadata.get("online") is not True
                or metadata.get("worker_phase") != "ACTIVE"
                or metadata.get("worker_accepting") is not True):
            return None
        if not isinstance(metadata["is_generating"], bool):
            return None
        counts = [metadata["queue_depth"], metadata["detached_request_count"]]
        if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts):
            return None
        return bool(metadata["is_generating"]) or any(counts)

    def project(self, status):
        result = dict(status)
        runtime, metadata = self._runtime(status["agent_id"])
        lifecycle, _epoch = self._identity(metadata)
        result.update(activation_lifecycle_id=lifecycle or None, activation_waiting=False,
                      activation_retry_allowed=bool(
                          lifecycle and status["activation_status"] == "pending"))
        entry = self._entries.get(status["agent_id"])
        if not entry or not self._matches(entry, status, metadata):
            return result
        if status["activation_status"] != "pending":
            return result  # Only actual connection observations can establish readiness.
        if entry.operation_id:
            result["activation_operation_id"] = entry.operation_id
        phase = entry.phase
        error = entry.error_code
        if phase == "accepted":
            try:
                operation = self._operation(entry)
            except Exception:
                operation = None
            if operation and operation.get("terminal") is True:
                phase, error = "failed", "telegram_activation_unavailable"
            elif time.monotonic() >= entry.deadline:
                phase, error = "unknown", "telegram_activation_unavailable"
        if phase == "waiting":
            result.update(activation_required="agent_idle", activation_waiting=True,
                          activation_retry_allowed=False, activation_error_code="AGENT_BUSY")
        elif phase == "accepted":
            result.update(activation_status="activating", activation_required=None,
                          activation_retry_allowed=False)
        else:
            result["activation_retry_allowed"] = bool(
                lifecycle and phase in {"failed", "expired"})
            if error:
                result["activation_error_code"] = error
        return result

    async def activate(self, agent_id, *, revision, owner_id):
        status = self._status(agent_id)
        runtime, metadata = self._runtime(agent_id)
        if revision != status["credential_revision"]:
            raise ValueError("credential_state_changed")
        if status["activation_status"] in {"inactive", "connected", "connecting", "user_required", "unconfigured"}:
            return self.project(status)
        if self._closed or not owner_id or not getattr(runtime, "is_function_worker_proxy", False):
            return {**self.project(status), "activation_error_code": "telegram_activation_unavailable"}
        current = self._entries.get(status["agent_id"])
        if current and self._matches(current, status, metadata):
            projected = self.project(status)
            if current.phase in {"waiting", "issuing", "unknown"} or not projected["activation_retry_allowed"]:
                return projected
        if current and current.task:
            current.task.cancel()
            await asyncio.gather(current.task, return_exceptions=True)
            # Cancellation yields: do not adopt a revision/creation changed meanwhile.
            status = self._status(agent_id)
            runtime, metadata = self._runtime(agent_id)
            if revision != status["credential_revision"]:
                raise ValueError("credential_state_changed")
            if status["activation_status"] != "pending":
                return self.project(status)
        # At most one continuation per configured Agent, with a finite ledger.
        for name in tuple(self._entries):
            if name not in self._runtime_map() and not self._entries[name].task:
                self._entries.pop(name, None)
        if len(self._entries) >= 256 and status["agent_id"] not in self._entries:
            return {**self.project(status), "activation_error_code": "telegram_activation_unavailable",
                    "activation_retry_allowed": False}
        lifecycle, epoch = self._identity(metadata)
        entry = _Activation(status["agent_id"], revision, lifecycle, epoch, str(owner_id),
                            time.monotonic() + self.wait_seconds)
        self._entries[entry.agent_id] = entry
        busy = self._busy(metadata)
        if lifecycle and busy is True:
            self._wait(entry)
        elif lifecycle and busy is None:
            entry.phase, entry.error_code = "failed", "telegram_activation_unavailable"
        else:
            await self._submit(entry, runtime)
        return self.project(self._status(agent_id))

    def _wait(self, entry):
        entry.phase = "waiting"
        entry.task = asyncio.create_task(self._until_idle(entry),
                                         name="hashi-telegram-activation-" + entry.agent_id)

    async def _until_idle(self, entry):
        try:
            while not self._closed:
                await asyncio.sleep(self.poll_seconds)
                if time.monotonic() >= entry.deadline:
                    entry.phase, entry.error_code = "expired", "telegram_activation_wait_expired"
                    return
                status = await asyncio.to_thread(self._status, entry.agent_id)
                runtime, metadata = self._runtime(entry.agent_id)
                if (self._entries.get(entry.agent_id) is not entry
                        or not self._matches(entry, status, metadata)
                        or status["activation_status"] != "pending"):
                    entry.phase, entry.error_code = "failed", "credential_state_changed"
                    return
                busy = self._busy(metadata)
                if busy is None:
                    entry.phase, entry.error_code = "failed", "telegram_activation_unavailable"
                    return
                if not busy:
                    await self._submit(entry, runtime, in_wait=True)
                    if entry.phase != "waiting":
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            entry.phase, entry.error_code = "failed", "telegram_activation_unavailable"
            logger.warning("Telegram continuation stopped: agent=%s error_type=%s",
                           entry.agent_id, type(exc).__name__)
        finally:
            entry.task = None

    async def _submit(self, entry, runtime, *, in_wait=False):
        status = self._status(entry.agent_id)
        current, metadata = self._runtime(entry.agent_id)
        if (self._entries.get(entry.agent_id) is not entry
                or not self._matches(entry, status, metadata)
                or status["activation_status"] != "pending"
                or current is not runtime):
            entry.phase, entry.error_code = "failed", "credential_state_changed"
            return
        entry.phase = "issuing"  # Concurrent requests observe this same intent.
        try:
            response = await self._execute(runtime, entry.owner_id, entry.invocation_id)
            activation = response.get("result") if isinstance(response, dict) else None
            record = (activation or {}).get("record") or {}
            if record.get("id"):
                entry.operation_id = str(record["id"])
            if (response and response.get("ok") is True and activation
                    and activation.get("accepted") is True and record.get("id")
                    and record.get("status") in {"accepted", "running", "succeeded"}):
                entry.phase = "accepted"
                return
            reason = ((activation or {}).get("reason") or record.get("reason")
                      or (response or {}).get("error"))
            # A durable target_busy receipt has already ended that exact attempt.
            if (reason in {"busy", "AGENT_BUSY"} and not record
                    and entry.lifecycle_id and time.monotonic() < entry.deadline):
                if in_wait:
                    entry.phase = "waiting"
                else:
                    self._wait(entry)
                return
            rejected = reason in {"busy", "AGENT_BUSY", "invalid_scope", "storage"}
            terminal_record = record.get("status") in {"failed", "cancelled"}
            entry.phase = "failed" if terminal_record or (rejected and not record) else "unknown"
            entry.error_code = ("AGENT_BUSY" if reason in {"busy", "target_busy", "AGENT_BUSY"}
                                else "telegram_activation_unavailable")
        except Exception as exc:
            entry.phase, entry.error_code = "unknown", "telegram_activation_unavailable"
            logger.warning("Telegram activation acknowledgement unavailable: agent=%s error_type=%s",
                           entry.agent_id, type(exc).__name__)

    async def close(self):
        self._closed = True
        tasks = [entry.task for entry in self._entries.values() if entry.task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._entries.clear()
