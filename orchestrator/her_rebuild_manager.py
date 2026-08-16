from __future__ import annotations

import json
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
    FailureKind,
    HERRebuildError,
    RebuildStage,
)

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
    RebuildStage.BUILDING: frozenset(
        {RebuildStage.VERIFYING, RebuildStage.FAILED}
    ),
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
        {RebuildStage.REBOOT_REQUESTED, RebuildStage.FAILED}
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
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}-{time.time_ns()}")
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
            detail=str(payload["detail"]) if payload.get("detail") is not None else None,
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
                item
                for item in active
                if item.source_fingerprint == source_fingerprint
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
                candidate_id=(candidate_id if candidate_id is not None else current.candidate_id),
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
                raise ValueError("terminal notification cannot be recorded before terminal state")
            actual_requester_id = requester_id or str(current.requesters[0]["requester_id"])
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
            }:
                continue
            if record.state == RebuildStage.ROLLING_BACK:
                recovered.append(
                    self.transition(
                        record.job_id,
                        RebuildStage.ROLLBACK_FAILED,
                        detail=reason,
                        failure_kind=FailureKind.INTERNAL_ERROR,
                        error=reason,
                        details={"interrupted": True, "manual_reconciliation_required": True},
                    )
                )
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
        return any(Path(argument).name in {"cargo", "cargo.exe"} for argument in arguments[:2])
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
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
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
