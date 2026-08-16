from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.her_rebuild import (
    CandidateMetadata,
    CandidateStore,
    DevelopmentSelectionStore,
    FailureKind,
    HERBuildController,
    HERQuickVerifier,
    HERRebuildError,
    HERSourceLayout,
    RebuildStage,
    compute_source_fingerprint,
    detect_host_target,
    inspect_toolchain,
)

logger = logging.getLogger("BridgeU.HERRebuild")

TERMINAL_STATES = frozenset(
    {
        RebuildStage.SUCCEEDED,
        RebuildStage.FAILED,
        RebuildStage.ACTIVATION_DEFERRED,
        RebuildStage.ROLLED_BACK,
        RebuildStage.ROLLBACK_FAILED,
    }
)

ALLOWED_TRANSITIONS: Mapping[RebuildStage, frozenset[RebuildStage]] = {
    RebuildStage.ACCEPTED: frozenset(
        {RebuildStage.SOURCE_PREFLIGHT, RebuildStage.FAILED}
    ),
    RebuildStage.SOURCE_PREFLIGHT: frozenset(
        {RebuildStage.WAITING_FOR_BUILD_LOCK, RebuildStage.FAILED}
    ),
    RebuildStage.WAITING_FOR_BUILD_LOCK: frozenset(
        {RebuildStage.BUILDING, RebuildStage.FAILED}
    ),
    RebuildStage.BUILDING: frozenset({RebuildStage.VERIFYING, RebuildStage.FAILED}),
    RebuildStage.VERIFYING: frozenset(
        {RebuildStage.CANDIDATE_READY, RebuildStage.FAILED}
    ),
    RebuildStage.CANDIDATE_READY: frozenset(
        {RebuildStage.WAITING_FOR_AGENT_IDLE, RebuildStage.FAILED}
    ),
    RebuildStage.WAITING_FOR_AGENT_IDLE: frozenset(
        {
            RebuildStage.ACTIVATING,
            RebuildStage.ACTIVATION_DEFERRED,
            RebuildStage.FAILED,
        }
    ),
    RebuildStage.ACTIVATING: frozenset(
        {
            RebuildStage.REBOOT_REQUESTED,
            RebuildStage.ROLLING_BACK,
            RebuildStage.FAILED,
        }
    ),
    RebuildStage.REBOOT_REQUESTED: frozenset(
        {RebuildStage.ADOPTING, RebuildStage.ROLLING_BACK}
    ),
    RebuildStage.ADOPTING: frozenset(
        {RebuildStage.POSTCHECK, RebuildStage.ROLLING_BACK}
    ),
    RebuildStage.POSTCHECK: frozenset(
        {RebuildStage.SUCCEEDED, RebuildStage.ROLLING_BACK}
    ),
    RebuildStage.ROLLING_BACK: frozenset(
        {RebuildStage.ROLLED_BACK, RebuildStage.ROLLBACK_FAILED}
    ),
    RebuildStage.SUCCEEDED: frozenset(),
    RebuildStage.FAILED: frozenset(),
    RebuildStage.ACTIVATION_DEFERRED: frozenset(),
    RebuildStage.ROLLED_BACK: frozenset(),
    RebuildStage.ROLLBACK_FAILED: frozenset(),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_rebuild_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"rebuild-{stamp}-{uuid.uuid4().hex[:8]}"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True)
class RebuildTransition:
    state: RebuildStage
    at: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "at": self.at,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RebuildTransition:
        return cls(
            state=RebuildStage(str(payload["state"])),
            at=str(payload["at"]),
            detail=str(payload["detail"])
            if payload.get("detail") is not None
            else None,
        )


@dataclass(frozen=True)
class RebuildJobRecord:
    schema_version: int
    job_id: str
    state: RebuildStage
    source_fingerprint: str
    target_agent: str
    actor_id: str
    origin: Mapping[str, Any]
    requesters: tuple[Mapping[str, Any], ...]
    created_at: str
    updated_at: str
    transitions: tuple[RebuildTransition, ...]
    candidate_id: str | None = None
    failure_kind: FailureKind | None = None
    error: str | None = None
    exit_code: int | None = None
    details: Mapping[str, Any] | None = None
    terminal_notification_event_id: str | None = None
    terminal_notification_delivered: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.value
        payload["failure_kind"] = self.failure_kind.value if self.failure_kind else None
        payload["transitions"] = [item.to_dict() for item in self.transitions]
        payload["origin"] = dict(self.origin)
        payload["requesters"] = [dict(item) for item in self.requesters]
        payload["details"] = dict(self.details or {})
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RebuildJobRecord:
        failure = payload.get("failure_kind")
        return cls(
            schema_version=int(payload["schema_version"]),
            job_id=str(payload["job_id"]),
            state=RebuildStage(str(payload["state"])),
            source_fingerprint=str(payload["source_fingerprint"]),
            target_agent=str(payload["target_agent"]),
            actor_id=str(payload["actor_id"]),
            origin=dict(payload.get("origin", {})),
            requesters=tuple(
                dict(item)
                for item in payload.get("requesters", [])
                if isinstance(item, Mapping)
            ),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            transitions=tuple(
                RebuildTransition.from_dict(item)
                for item in payload.get("transitions", [])
            ),
            candidate_id=(
                str(payload["candidate_id"])
                if payload.get("candidate_id") is not None
                else None
            ),
            failure_kind=FailureKind(str(failure)) if failure else None,
            error=str(payload["error"]) if payload.get("error") is not None else None,
            exit_code=(
                int(payload["exit_code"])
                if payload.get("exit_code") is not None
                else None
            ),
            details=dict(payload.get("details", {})),
            terminal_notification_event_id=(
                str(payload["terminal_notification_event_id"])
                if payload.get("terminal_notification_event_id") is not None
                else None
            ),
            terminal_notification_delivered=bool(
                payload.get("terminal_notification_delivered", False)
            ),
        )


def _requester_key(
    *,
    target_agent: str,
    actor_id: str,
    origin: Mapping[str, Any],
) -> tuple[str, str, str]:
    serialized_origin = json.dumps(
        dict(origin),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(target_agent), str(actor_id), serialized_origin


def _new_requester(
    *,
    target_agent: str,
    actor_id: str,
    origin: Mapping[str, Any],
    accepted_at: str | None = None,
) -> dict[str, Any]:
    return {
        "requester_id": f"requester-{uuid.uuid4().hex[:12]}",
        "target_agent": str(target_agent),
        "actor_id": str(actor_id),
        "origin": dict(origin),
        "accepted_at": accepted_at or utc_now(),
        "terminal_event_id": None,
        "terminal_delivered": False,
    }


class HERRebuildJobStore:
    """Durable rebuild state without owning a live Agent or restart implementation."""

    def __init__(self, jobs_root: Path):
        self.root = Path(jobs_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._mutex = threading.RLock()

    @property
    def latest_path(self) -> Path:
        return self.root / "latest.json"

    def _job_path(self, job_id: str) -> Path:
        if not job_id.startswith("rebuild-") or any(char in job_id for char in "/\\"):
            raise ValueError("invalid rebuild job id")
        return self.root / f"{job_id}.json"

    def create(
        self,
        *,
        source_fingerprint: str,
        target_agent: str,
        actor_id: str,
        origin: Mapping[str, Any],
        job_id: str | None = None,
    ) -> RebuildJobRecord:
        with self._mutex:
            now = utc_now()
            actual_job_id = job_id or new_rebuild_job_id()
            requester = _new_requester(
                target_agent=target_agent,
                actor_id=actor_id,
                origin=origin,
                accepted_at=now,
            )
            record = RebuildJobRecord(
                schema_version=1,
                job_id=actual_job_id,
                state=RebuildStage.ACCEPTED,
                source_fingerprint=str(source_fingerprint),
                target_agent=str(target_agent),
                actor_id=str(actor_id),
                origin=dict(origin),
                requesters=(requester,),
                created_at=now,
                updated_at=now,
                transitions=(RebuildTransition(RebuildStage.ACCEPTED, now),),
                details={},
            )
            path = self._job_path(record.job_id)
            if path.exists():
                raise FileExistsError(path)
            self._persist(record)
            return record

    def accept_or_join(
        self,
        *,
        source_fingerprint: str,
        target_agent: str,
        actor_id: str,
        origin: Mapping[str, Any],
    ) -> tuple[RebuildJobRecord, bool]:
        with self._mutex:
            active = self.active()
            matching = [
                item for item in active if item.source_fingerprint == source_fingerprint
            ]
            if matching:
                matching.sort(key=lambda item: item.created_at, reverse=True)
                current = matching[0]
                requester_key = _requester_key(
                    target_agent=target_agent,
                    actor_id=actor_id,
                    origin=origin,
                )
                existing_keys = {
                    _requester_key(
                        target_agent=str(item.get("target_agent", "")),
                        actor_id=str(item.get("actor_id", "")),
                        origin=dict(item.get("origin", {})),
                    )
                    for item in current.requesters
                }
                if requester_key not in existing_keys:
                    updated = replace(
                        current,
                        updated_at=utc_now(),
                        requesters=(
                            *current.requesters,
                            _new_requester(
                                target_agent=target_agent,
                                actor_id=actor_id,
                                origin=origin,
                            ),
                        ),
                    )
                    self._persist(updated)
                    return updated, True
                return current, True
            if active:
                current = max(active, key=lambda item: item.created_at)
                raise HERRebuildError(
                    FailureKind.BUILD_LOCK_BUSY,
                    RebuildStage.WAITING_FOR_BUILD_LOCK,
                    "A different HER source fingerprint is already rebuilding "
                    f"under job {current.job_id}.",
                )
            return (
                self.create(
                    source_fingerprint=source_fingerprint,
                    target_agent=target_agent,
                    actor_id=actor_id,
                    origin=origin,
                ),
                False,
            )

    def get(self, job_id: str) -> RebuildJobRecord | None:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HERRebuildError(
                FailureKind.INTERNAL_ERROR,
                RebuildStage.FAILED,
                f"Rebuild job record is unreadable: {job_id}.",
            ) from exc
        return RebuildJobRecord.from_dict(payload)

    def latest(self) -> RebuildJobRecord | None:
        if not self.latest_path.exists():
            return None
        try:
            payload = json.loads(self.latest_path.read_text(encoding="utf-8"))
            job_id = str(payload["job_id"])
        except (OSError, KeyError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise HERRebuildError(
                FailureKind.INTERNAL_ERROR,
                RebuildStage.FAILED,
                "Latest HER rebuild pointer is unreadable.",
            ) from exc
        return self.get(job_id)

    def list(self) -> list[RebuildJobRecord]:
        records: list[RebuildJobRecord] = []
        for path in sorted(self.root.glob("rebuild-*.json")):
            record = self.get(path.stem)
            if record is not None:
                records.append(record)
        return records

    def active(self) -> list[RebuildJobRecord]:
        return [record for record in self.list() if not record.is_terminal]

    def transition(
        self,
        job_id: str,
        state: RebuildStage,
        *,
        detail: str | None = None,
        candidate_id: str | None = None,
        failure_kind: FailureKind | None = None,
        error: str | None = None,
        exit_code: int | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> RebuildJobRecord:
        with self._mutex:
            current = self.get(job_id)
            if current is None:
                raise KeyError(job_id)
            if state not in ALLOWED_TRANSITIONS[current.state]:
                raise ValueError(
                    f"invalid HER rebuild transition: {current.state.value} -> {state.value}"
                )
            if state == RebuildStage.FAILED and failure_kind is None:
                raise ValueError("failed rebuild transition requires failure_kind")
            now = utc_now()
            merged_details = dict(current.details or {})
            if details:
                merged_details.update(details)
            updated = replace(
                current,
                state=state,
                updated_at=now,
                transitions=(
                    *current.transitions,
                    RebuildTransition(state=state, at=now, detail=detail),
                ),
                candidate_id=(
                    candidate_id if candidate_id is not None else current.candidate_id
                ),
                failure_kind=failure_kind,
                error=error,
                exit_code=exit_code,
                details=merged_details,
            )
            self._persist(updated)
            return updated

    def mark_notification(
        self,
        job_id: str,
        *,
        event_id: str,
        delivered: bool,
        requester_id: str | None = None,
    ) -> RebuildJobRecord:
        with self._mutex:
            current = self.get(job_id)
            if current is None:
                raise KeyError(job_id)
            if not current.is_terminal:
                raise ValueError(
                    "terminal notification cannot be recorded before terminal state"
                )
            actual_requester_id = requester_id or str(
                current.requesters[0]["requester_id"]
            )
            found = False
            requesters = []
            for item in current.requesters:
                requester = dict(item)
                if requester.get("requester_id") == actual_requester_id:
                    found = True
                    existing_event_id = requester.get("terminal_event_id")
                    if existing_event_id and existing_event_id != event_id:
                        raise ValueError("terminal notification event id is immutable")
                    requester["terminal_event_id"] = event_id
                    requester["terminal_delivered"] = bool(
                        requester.get("terminal_delivered") or delivered
                    )
                requesters.append(requester)
            if not found:
                raise KeyError(actual_requester_id)
            all_delivered = all(
                bool(item.get("terminal_delivered")) for item in requesters
            )
            primary = requesters[0]
            updated = replace(
                current,
                updated_at=utc_now(),
                requesters=tuple(requesters),
                terminal_notification_event_id=primary.get("terminal_event_id"),
                terminal_notification_delivered=all_delivered,
            )
            self._persist(updated)
            return updated

    def recover_nonterminal(
        self,
        *,
        reason: str = "kernel_restarted_during_rebuild",
    ) -> list[RebuildJobRecord]:
        recovered: list[RebuildJobRecord] = []
        for record in self.active():
            if record.state in {
                RebuildStage.ACTIVATING,
                RebuildStage.REBOOT_REQUESTED,
                RebuildStage.ADOPTING,
                RebuildStage.POSTCHECK,
                RebuildStage.ROLLING_BACK,
            }:
                continue
            recovered.append(
                self.transition(
                    record.job_id,
                    RebuildStage.FAILED,
                    detail=reason,
                    failure_kind=FailureKind.INTERNAL_ERROR,
                    error=reason,
                    details={"interrupted": True},
                )
            )
        return recovered

    def _persist(self, record: RebuildJobRecord) -> None:
        _atomic_json(self._job_path(record.job_id), record.to_dict())
        _atomic_json(self.latest_path, {"schema_version": 1, "job_id": record.job_id})


def pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def cargo_process_exists(pid: int) -> bool:
    if not pid_exists(pid):
        return False
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    if proc_cmdline.exists():
        try:
            arguments = [
                item.decode("utf-8", errors="replace")
                for item in proc_cmdline.read_bytes().split(b"\x00")
                if item
            ]
        except OSError:
            return True
        return any(
            Path(argument).name in {"cargo", "cargo.exe"} for argument in arguments[:2]
        )
    # On hosts without /proc, a live recorded PID is treated conservatively.
    return True


class HERBuildLock:
    """Cross-process build lock with stale Cargo-child protection."""

    def __init__(
        self,
        path: Path,
        *,
        source_fingerprint: str,
        pid_probe: Callable[[int], bool] = cargo_process_exists,
    ):
        self.path = Path(path).resolve()
        self.source_fingerprint = str(source_fingerprint)
        self.pid_probe = pid_probe
        self._handle = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> None:
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            self._lock_handle(handle)
        except OSError as exc:
            handle.close()
            raise HERRebuildError(
                FailureKind.BUILD_LOCK_BUSY,
                RebuildStage.WAITING_FOR_BUILD_LOCK,
                "Another process currently owns the HER Cargo build lock.",
            ) from exc

        stale = self._read_metadata(handle)
        stale_cargo_pid = _coerce_pid(stale.get("cargo_pid"))
        if stale_cargo_pid and self.pid_probe(stale_cargo_pid):
            self._unlock_handle(handle)
            handle.close()
            raise HERRebuildError(
                FailureKind.STALE_LOCK_UNRECOVERABLE,
                RebuildStage.WAITING_FOR_BUILD_LOCK,
                f"A previously recorded Cargo process is still running (PID {stale_cargo_pid}).",
            )

        self._handle = handle
        try:
            self._write_metadata(
                {
                    "schema_version": 1,
                    "owner_pid": os.getpid(),
                    "cargo_pid": None,
                    "source_fingerprint": self.source_fingerprint,
                    "acquired_at": utc_now(),
                }
            )
        except OSError as exc:
            with suppress(Exception):
                self._unlock_handle(handle)
            handle.close()
            self._handle = None
            raise HERRebuildError(
                FailureKind.INTERNAL_ERROR,
                RebuildStage.WAITING_FOR_BUILD_LOCK,
                f"Could not persist HER build lock metadata: {type(exc).__name__}: {exc}",
            ) from exc

    def set_cargo_pid(self, cargo_pid: int | None) -> None:
        if not self.acquired:
            raise RuntimeError("HER build lock is not acquired")
        current = self.metadata()
        current["cargo_pid"] = int(cargo_pid) if cargo_pid is not None else None
        current["updated_at"] = utc_now()
        self._write_metadata(current)

    def metadata(self) -> dict[str, Any]:
        if not self.acquired:
            return {}
        return self._read_metadata(self._handle)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        with suppress(Exception):
            metadata = self._read_metadata(handle)
            metadata["cargo_pid"] = None
            metadata["released_at"] = utc_now()
            self._write_metadata(metadata)
        with suppress(Exception):
            self._unlock_handle(handle)
        with suppress(Exception):
            handle.close()
        self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    def _read_metadata(self, handle) -> dict[str, Any]:
        handle.seek(0)
        raw = handle.read().decode("utf-8", errors="replace").strip("\x00\r\n ")
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_metadata(self, payload: Mapping[str, Any]) -> None:
        assert self._handle is not None
        encoded = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        self._handle.seek(0)
        self._handle.truncate(0)
        self._handle.write(encoded)
        self._handle.flush()
        os.fsync(self._handle.fileno())

    @staticmethod
    def _lock_handle(handle) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"\x00")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle) -> None:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _coerce_pid(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


class HERRebuildManager:
    """Kernel-owned `/rebuild` transaction coordinator.

    The instance deliberately sits outside the hot-manager bundle so an Agent
    restart cannot destroy the task that requested it.
    """

    def __init__(
        self,
        kernel: Any,
        *,
        idle_timeout_seconds: float = 120.0,
        idle_poll_seconds: float = 1.0,
    ):
        self.kernel = kernel
        self.state_root = (
            Path(kernel.paths.bridge_home).resolve() / "state" / "her_rebuild"
        )
        self.layout = HERSourceLayout.from_code_root(kernel.paths.code_root)
        self.jobs = HERRebuildJobStore(self.state_root / "jobs")
        self.candidates = CandidateStore(self.state_root / "candidates")
        self.selection = DevelopmentSelectionStore(
            self.state_root / "development-selection.json",
            candidates_root=self.state_root / "candidates",
        )
        self.controller = HERBuildController(self.layout, state_root=self.state_root)
        self.verifier = HERQuickVerifier()
        self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))
        self.idle_poll_seconds = max(0.05, float(idle_poll_seconds))
        self._submit_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.jobs.recover_nonterminal()

    def reconcile_before_agent_startup(self) -> list[RebuildJobRecord]:
        """Fail safe after a cold kernel interruption, before any Agent starts."""
        reconciled: list[RebuildJobRecord] = []
        for record in self.jobs.active():
            if record.state not in {
                RebuildStage.ACTIVATING,
                RebuildStage.REBOOT_REQUESTED,
                RebuildStage.ADOPTING,
                RebuildStage.POSTCHECK,
                RebuildStage.ROLLING_BACK,
            }:
                continue
            try:
                if record.state != RebuildStage.ROLLING_BACK:
                    record = self.jobs.transition(
                        record.job_id,
                        RebuildStage.ROLLING_BACK,
                        detail="cold kernel restart interrupted candidate adoption",
                        failure_kind=FailureKind.INTERNAL_ERROR,
                        error="cold kernel restart interrupted candidate adoption",
                    )
                self.selection.restore_previous(job_id=record.job_id)
                reconciled.append(
                    self.jobs.transition(
                        record.job_id,
                        RebuildStage.ROLLED_BACK,
                        detail="previous HER restored before Agent startup",
                        failure_kind=FailureKind.INTERNAL_ERROR,
                        error="candidate adoption was interrupted by a cold kernel restart",
                        details={"cold_start_reconciled": True},
                    )
                )
            except Exception as exc:  # noqa: BLE001 - startup must persist manual-recovery state
                reconciled.append(
                    self.jobs.transition(
                        record.job_id,
                        RebuildStage.ROLLBACK_FAILED,
                        detail=str(exc),
                        failure_kind=FailureKind.ROLLBACK_SELECTION_FAILED,
                        error=f"cold-start HER rollback failed: {type(exc).__name__}: {exc}",
                        details={"manual_reconciliation_required": True},
                    )
                )
        return reconciled

    async def submit(
        self,
        *,
        target_agent: str,
        actor_id: str,
        origin: Mapping[str, Any],
    ) -> tuple[RebuildJobRecord, bool]:
        async with self._submit_lock:
            target = detect_host_target()
            toolchain = await inspect_toolchain()
            fingerprint = compute_source_fingerprint(
                self.layout,
                toolchain=toolchain,
                target=target,
            )
            record, joined = self.jobs.accept_or_join(
                source_fingerprint=fingerprint.digest,
                target_agent=target_agent,
                actor_id=actor_id,
                origin=origin,
            )
            if not joined:
                task = asyncio.create_task(
                    self._run(
                        record.job_id, fingerprint=fingerprint, toolchain=toolchain
                    ),
                    name=f"her-rebuild:{record.job_id}",
                )
                self._tasks[record.job_id] = task
                task.add_done_callback(
                    lambda _task, job_id=record.job_id: self._tasks.pop(job_id, None)
                )
            return record, joined

    def latest(self) -> RebuildJobRecord | None:
        return self.jobs.latest()

    def get(self, job_id: str | None = None) -> RebuildJobRecord | None:
        return self.jobs.get(job_id) if job_id else self.jobs.latest()

    async def wait(self, job_id: str) -> RebuildJobRecord | None:
        task = self._tasks.get(job_id)
        if task is not None:
            await asyncio.shield(task)
        return self.jobs.get(job_id)

    async def retry_pending_notifications(self) -> int:
        retried = 0
        for record in self.jobs.list():
            if record.is_terminal and not record.terminal_notification_delivered:
                await self._notify_terminal(record.job_id)
                retried += 1
        return retried

    async def _run(self, job_id: str, *, fingerprint: Any, toolchain: Any) -> None:
        selected = False
        candidate: CandidateMetadata | None = None
        try:
            self.jobs.transition(job_id, RebuildStage.SOURCE_PREFLIGHT)
            self.jobs.transition(job_id, RebuildStage.WAITING_FOR_BUILD_LOCK)
            build_lock = HERBuildLock(
                self.state_root / "build.lock",
                source_fingerprint=fingerprint.digest,
            )
            with build_lock:
                cached = self.candidates.find_by_fingerprint(fingerprint.digest)
                self.jobs.transition(
                    job_id,
                    RebuildStage.BUILDING,
                    detail="reusing immutable candidate"
                    if cached
                    else "cargo build started",
                    details={"candidate_reused": bool(cached)},
                )
                if cached is None:
                    artifact = await self.controller.build(
                        job_id=job_id,
                        fingerprint=fingerprint,
                        toolchain=toolchain,
                        on_process_started=build_lock.set_cargo_pid,
                        on_process_finished=lambda: build_lock.set_cargo_pid(None),
                    )
                    binary_path = artifact.binary_path
                else:
                    artifact = None
                    binary_path = Path(cached.binary_path)

                self.jobs.transition(job_id, RebuildStage.VERIFYING)
                verification = await self.verifier.verify(
                    binary_path,
                    fingerprint=fingerprint,
                    work_root=self.state_root / "verification" / job_id,
                )
                candidate = (
                    cached
                    if cached is not None
                    else self.candidates.stage(
                        artifact,
                        toolchain=toolchain,
                        quick_verification=verification,
                    )
                )

            self.jobs.transition(
                job_id,
                RebuildStage.CANDIDATE_READY,
                candidate_id=candidate.candidate_id,
                details={
                    "binary_sha256": candidate.binary_sha256,
                    "build_duration_seconds": candidate.build_duration_seconds,
                    "target": candidate.target,
                },
            )
            self.jobs.transition(job_id, RebuildStage.WAITING_FOR_AGENT_IDLE)
            target_agents = self._target_agents(job_id)
            if not await self._wait_for_idle(target_agents):
                self.jobs.transition(
                    job_id,
                    RebuildStage.ACTIVATION_DEFERRED,
                    detail="target Agent remained busy; candidate retained",
                    failure_kind=FailureKind.ACTIVATION_DEFERRED,
                    error="Verified candidate is ready but activation was deferred until a later /rebuild.",
                    details={"current_her_unchanged": True},
                )
                return

            self.jobs.transition(job_id, RebuildStage.ACTIVATING)
            self.selection.select(candidate.candidate_id, job_id=job_id)
            selected = True
            self.jobs.transition(job_id, RebuildStage.REBOOT_REQUESTED)
            for agent_name in target_agents:
                reboot_ok = await self.kernel.reboot_manager.hot_restart(
                    {"mode": "min", "agent_name": agent_name, "agent_number": None}
                )
                if not reboot_ok:
                    raise HERRebuildError(
                        FailureKind.AGENT_RESTART_FAILED,
                        RebuildStage.REBOOT_REQUESTED,
                        f"Target Agent {agent_name!r} did not restart successfully.",
                    )

            self.jobs.transition(job_id, RebuildStage.ADOPTING)
            self._assert_adopted(target_agents, candidate)
            self.jobs.transition(job_id, RebuildStage.POSTCHECK)
            await self._postcheck(target_agents, candidate)
            self.selection.mark_adopted(candidate.candidate_id, job_id=job_id)
            self.jobs.transition(
                job_id,
                RebuildStage.SUCCEEDED,
                detail="development HER adopted and postcheck passed",
            )
        except asyncio.CancelledError:
            raise
        except HERRebuildError as exc:
            await self._handle_failure(job_id, exc, selected=selected)
        except Exception as exc:  # noqa: BLE001 - transaction boundary classifies unexpected failures
            wrapped = HERRebuildError(
                FailureKind.INTERNAL_ERROR,
                self.jobs.get(job_id).state
                if self.jobs.get(job_id)
                else RebuildStage.FAILED,
                f"Unexpected HER rebuild failure: {type(exc).__name__}: {exc}",
            )
            await self._handle_failure(job_id, wrapped, selected=selected)
        finally:
            await self._notify_terminal(job_id)

    async def _handle_failure(
        self,
        job_id: str,
        error: HERRebuildError,
        *,
        selected: bool,
    ) -> None:
        record = self.jobs.get(job_id)
        if record is None or record.is_terminal:
            return
        if not selected or record.state not in {
            RebuildStage.REBOOT_REQUESTED,
            RebuildStage.ADOPTING,
            RebuildStage.POSTCHECK,
        }:
            self.jobs.transition(
                job_id,
                RebuildStage.FAILED,
                detail=str(error),
                failure_kind=error.failure_kind,
                error=str(error),
                exit_code=error.exit_code,
                details={
                    "diagnostics": error.diagnostics,
                    "current_her_unchanged": not selected,
                },
            )
            return

        self.jobs.transition(
            job_id,
            RebuildStage.ROLLING_BACK,
            detail=str(error),
            failure_kind=error.failure_kind,
            error=str(error),
            exit_code=error.exit_code,
        )
        try:
            self.selection.restore_previous(job_id=job_id)
            rollback_ok = True
            for agent_name in self._target_agents(job_id):
                rollback_ok = (
                    bool(
                        await self.kernel.reboot_manager.hot_restart(
                            {
                                "mode": "min",
                                "agent_name": agent_name,
                                "agent_number": None,
                            }
                        )
                    )
                    and rollback_ok
                )
            if not rollback_ok:
                raise RuntimeError(
                    "one or more target Agents failed the rollback restart"
                )
            self.jobs.transition(
                job_id,
                RebuildStage.ROLLED_BACK,
                detail="candidate rejected; previous HER restored",
                failure_kind=error.failure_kind,
                error=str(error),
                details={"rollback_succeeded": True},
            )
        except Exception as rollback_error:  # noqa: BLE001 - rollback must report every failure mode
            self.jobs.transition(
                job_id,
                RebuildStage.ROLLBACK_FAILED,
                detail=str(rollback_error),
                failure_kind=FailureKind.ROLLBACK_RESTART_FAILED,
                error=f"{error}; rollback failed: {rollback_error}",
                details={"manual_reconciliation_required": True},
            )

    def _target_agents(self, job_id: str) -> list[str]:
        record = self.jobs.get(job_id)
        if record is None:
            return []
        names = [
            str(item.get("target_agent") or "").strip() for item in record.requesters
        ]
        return list(dict.fromkeys(name for name in names if name)) or [
            record.target_agent
        ]

    def _runtime(self, agent_name: str) -> Any | None:
        return next(
            (
                item
                for item in getattr(self.kernel, "runtimes", [])
                if item.name == agent_name
            ),
            None,
        )

    @staticmethod
    def _backend(runtime: Any) -> Any | None:
        manager = getattr(runtime, "backend_manager", None)
        return (
            getattr(manager, "current_backend", None)
            if manager is not None
            else getattr(runtime, "backend", None)
        )

    def _runtime_idle(self, agent_name: str) -> bool:
        runtime = self._runtime(agent_name)
        if runtime is None or not getattr(runtime, "startup_success", False):
            return False
        queue = getattr(runtime, "queue", None)
        background = getattr(runtime, "_background_tasks", set())
        backend = self._backend(runtime)
        active_processes = getattr(backend, "_active_processes", {}) if backend else {}
        return not any(
            (
                bool(getattr(runtime, "is_generating", False)),
                bool(queue is not None and not queue.empty()),
                bool(getattr(runtime, "current_request_meta", None)),
                any(not task.done() for task in background),
                bool(active_processes),
            )
        )

    async def _wait_for_idle(self, agent_names: list[str]) -> bool:
        deadline = time.monotonic() + self.idle_timeout_seconds
        while True:
            if all(self._runtime_idle(name) for name in agent_names):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.idle_poll_seconds)

    def _assert_adopted(
        self,
        agent_names: list[str],
        candidate: CandidateMetadata,
    ) -> None:
        for agent_name in agent_names:
            runtime = self._runtime(agent_name)
            backend = self._backend(runtime) if runtime is not None else None
            resolution = getattr(backend, "_binary_resolution", None)
            binary = getattr(backend, "_binary", None)
            if (
                backend is None
                or getattr(resolution, "source", None) != "development-source-build"
                or Path(binary).resolve() != Path(candidate.binary_path).resolve()
            ):
                raise HERRebuildError(
                    FailureKind.ADAPTER_INITIALIZATION_FAILED,
                    RebuildStage.ADOPTING,
                    f"Target Agent {agent_name!r} did not adopt the selected HER candidate.",
                )

    async def _postcheck(
        self,
        agent_names: list[str],
        candidate: CandidateMetadata,
    ) -> None:
        for agent_name in agent_names:
            runtime = self._runtime(agent_name)
            if runtime is None or not getattr(runtime, "backend_ready", False):
                raise HERRebuildError(
                    FailureKind.POSTCHECK_HEALTH_FAILED,
                    RebuildStage.POSTCHECK,
                    f"Target Agent {agent_name!r} is not backend-ready after adoption.",
                )
        await self.verifier.verify(
            Path(candidate.binary_path),
            fingerprint=self._fingerprint_from_candidate(candidate),
            work_root=self.state_root / "postcheck" / candidate.candidate_id,
        )

    @staticmethod
    def _fingerprint_from_candidate(candidate: CandidateMetadata) -> Any:
        from orchestrator.her_rebuild import SourceFingerprint

        return SourceFingerprint(
            digest=candidate.source_fingerprint,
            git_head=candidate.source_git_head,
            dirty=candidate.source_dirty,
            file_count=0,
            source_bytes=0,
            target=candidate.target,
            profile=candidate.profile,
            features=candidate.features,
            cargo_version=candidate.cargo_version,
            rustc_version=candidate.rustc_version,
        )

    async def _notify_terminal(self, job_id: str) -> None:
        record = self.jobs.get(job_id)
        if record is None or not record.is_terminal:
            return
        for requester in record.requesters:
            if requester.get("terminal_delivered"):
                continue
            requester_id = str(requester["requester_id"])
            event_id = str(
                requester.get("terminal_event_id")
                or f"{job_id}:terminal:{requester_id}"
            )
            delivered = False
            try:
                origin = dict(requester.get("origin", {}))
                chat_id = origin.get("chat_id")
                runtime = self._runtime(str(requester.get("target_agent") or ""))
                if runtime is not None and chat_id is not None:
                    await runtime.send_long_message(
                        int(chat_id),
                        self.format_result(record),
                        request_id=event_id,
                        purpose="her-rebuild-terminal",
                    )
                    delivered = True
            except Exception:
                logger.exception(
                    "Failed to deliver terminal HER rebuild event %s", event_id
                )
            self.jobs.mark_notification(
                job_id,
                requester_id=requester_id,
                event_id=event_id,
                delivered=delivered,
            )

    @staticmethod
    def format_result(record: RebuildJobRecord) -> str:
        duration = (
            record.details.get("build_duration_seconds") if record.details else None
        )
        duration_text = (
            f" · build {float(duration):.1f}s" if duration is not None else ""
        )
        if record.state == RebuildStage.SUCCEEDED:
            return f"✅ HER rebuild succeeded · {record.job_id}{duration_text} · candidate {record.candidate_id}"
        if record.state == RebuildStage.ACTIVATION_DEFERRED:
            return f"⏸️ HER rebuild verified but activation was deferred · {record.job_id} · run /rebuild again when the Agent is idle."
        if record.state == RebuildStage.ROLLED_BACK:
            return f"↩️ HER rebuild failed and the previous runtime was restored · {record.job_id} · {record.error or 'postcheck failed'}"
        return f"❌ HER rebuild failed · {record.job_id} · {record.failure_kind.value if record.failure_kind else 'unknown'} · {record.error or 'no details'}"
