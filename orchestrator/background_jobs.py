from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("BridgeU.BackgroundJobs")

TERMINAL_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "timeout",
    "abandoned_after_restart",
    "adoption_failed",
    "policy_denied",
    "start_failed",
}
NONTERMINAL_STATES = {"created", "starting", "running", "cancel_requested"}
DEFAULT_MAX_STREAM_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_RUNTIME_SECONDS = 4 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_job_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"job_{stamp}_{uuid.uuid4().hex[:8]}"


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


@dataclass(frozen=True)
class BackgroundJobRecord:
    job_id: str
    state: str
    agent: str
    command: dict[str, Any]
    origin: dict[str, Any]
    policy: dict[str, Any]
    process: dict[str, Any]
    logs: dict[str, Any]
    notification: dict[str, Any]
    created_at: str
    updated_at: str
    ended_at: str | None = None
    returncode: int | None = None
    error: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "state": self.state,
            "agent": self.agent,
            "command": self.command,
            "origin": self.origin,
            "policy": self.policy,
            "process": self.process,
            "logs": self.logs,
            "notification": self.notification,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "returncode": self.returncode,
            "error": self.error,
        }


class BackgroundJobStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS background_jobs (
                    job_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    origin_json TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    process_json TEXT NOT NULL,
                    logs_json TEXT NOT NULL,
                    notification_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ended_at TEXT,
                    returncode INTEGER,
                    error TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_background_jobs_state ON background_jobs(state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_background_jobs_agent ON background_jobs(agent)")

    def create(self, record: BackgroundJobRecord) -> BackgroundJobRecord:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO background_jobs (
                    job_id, state, agent, command_json, origin_json, policy_json,
                    process_json, logs_json, notification_json, created_at, updated_at,
                    ended_at, returncode, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._record_values(record),
            )
        logger.info("Background job %s created state=%s agent=%s", record.job_id, record.state, record.agent)
        return record

    def update(
        self,
        job_id: str,
        *,
        state: str | None = None,
        process: dict[str, Any] | None = None,
        logs: dict[str, Any] | None = None,
        notification: dict[str, Any] | None = None,
        ended_at: str | None = None,
        returncode: int | None = None,
        error: str | None = None,
    ) -> BackgroundJobRecord:
        current = self.get(job_id)
        if current is None:
            raise KeyError(job_id)
        record = BackgroundJobRecord(
            job_id=current.job_id,
            state=state or current.state,
            agent=current.agent,
            command=current.command,
            origin=current.origin,
            policy=current.policy,
            process=process if process is not None else current.process,
            logs=logs if logs is not None else current.logs,
            notification=notification if notification is not None else current.notification,
            created_at=current.created_at,
            updated_at=utc_now(),
            ended_at=ended_at if ended_at is not None else current.ended_at,
            returncode=returncode if returncode is not None else current.returncode,
            error=error if error is not None else current.error,
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE background_jobs
                SET state = ?, agent = ?, command_json = ?, origin_json = ?,
                    policy_json = ?, process_json = ?, logs_json = ?,
                    notification_json = ?, created_at = ?, updated_at = ?,
                    ended_at = ?, returncode = ?, error = ?
                WHERE job_id = ?
                """,
                (*self._record_values(record)[1:], record.job_id),
            )
        logger.info("Background job %s transitioned to %s", job_id, record.state)
        return record

    def get(self, job_id: str) -> BackgroundJobRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM background_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def list(self, *, agent: str | None = None, states: set[str] | None = None, limit: int = 50) -> list[BackgroundJobRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if agent:
            clauses.append("agent = ?")
            params.append(agent)
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(sorted(states))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM background_jobs {where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def recover_nonterminal(self, *, reason: str = "manager_recovered_without_process_adoption") -> list[BackgroundJobRecord]:
        recovered: list[BackgroundJobRecord] = []
        for record in self.list(states=NONTERMINAL_STATES, limit=10_000):
            process = dict(record.process)
            process["recovery_reason"] = reason
            recovered.append(
                self.update(
                    record.job_id,
                    state="abandoned_after_restart",
                    process=process,
                    ended_at=utc_now(),
                    error=reason,
                )
            )
        if recovered:
            logger.warning("Marked %s background job(s) abandoned during recovery", len(recovered))
        return recovered

    def _record_values(self, record: BackgroundJobRecord) -> tuple[Any, ...]:
        return (
            record.job_id,
            record.state,
            record.agent,
            _json_dumps(record.command),
            _json_dumps(record.origin),
            _json_dumps(record.policy),
            _json_dumps(record.process),
            _json_dumps(record.logs),
            _json_dumps(record.notification),
            record.created_at,
            record.updated_at,
            record.ended_at,
            record.returncode,
            record.error,
        )

    def _row_to_record(self, row: sqlite3.Row) -> BackgroundJobRecord:
        return BackgroundJobRecord(
            job_id=row["job_id"],
            state=row["state"],
            agent=row["agent"],
            command=_json_loads(row["command_json"], {}),
            origin=_json_loads(row["origin_json"], {}),
            policy=_json_loads(row["policy_json"], {}),
            process=_json_loads(row["process_json"], {}),
            logs=_json_loads(row["logs_json"], {}),
            notification=_json_loads(row["notification_json"], {}),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            ended_at=row["ended_at"],
            returncode=row["returncode"],
            error=row["error"],
        )


class BackgroundJobManager:
    def __init__(self, base_dir: Path, *, kernel: Any | None = None):
        self.base_dir = Path(base_dir)
        self.logs_dir = self.base_dir / "logs"
        self.store = BackgroundJobStore(self.base_dir / "jobs.db")
        self.kernel = kernel
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._monitor_tasks: dict[str, asyncio.Task[Any]] = {}
        self._stream_tasks: dict[str, list[asyncio.Task[Any]]] = {}
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.store.recover_nonterminal(reason="background_job_manager_started_without_adoption")

    async def stop(self) -> None:
        for task in list(self._monitor_tasks.values()):
            task.cancel()
        for task in list(self._monitor_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._monitor_tasks.clear()
        self._started = False

    async def start_job(
        self,
        *,
        agent: str,
        cwd: str | Path,
        argv: list[str] | None = None,
        command: str | None = None,
        origin: dict[str, Any] | None = None,
        notify_on_complete: bool = True,
        notify_on_failure: bool = True,
        trigger_agent_on_complete: bool = True,
        trigger_agent_on_failure: bool = True,
        max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
        max_stdout_bytes: int = DEFAULT_MAX_STREAM_BYTES,
        max_stderr_bytes: int = DEFAULT_MAX_STREAM_BYTES,
    ) -> BackgroundJobRecord:
        if not argv and not command:
            raise ValueError("argv or command is required")
        if argv and command:
            raise ValueError("provide argv or command, not both")
        cwd_path = Path(cwd).expanduser().resolve()
        if not cwd_path.exists():
            raise FileNotFoundError(str(cwd_path))

        job_id = new_job_id()
        job_log_dir = self.logs_dir / str(agent or "unknown") / job_id
        job_log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = job_log_dir / "stdout.log"
        stderr_path = job_log_dir / "stderr.log"
        now = utc_now()
        command_payload = {
            "mode": "argv" if argv else "shell",
            "display": " ".join(argv or []) if argv else command,
            "argv": list(argv) if argv else None,
            "cwd": str(cwd_path),
            "env_keys": [],
        }
        policy = {
            "max_runtime_seconds": int(max_runtime_seconds),
            "max_stdout_bytes": int(max_stdout_bytes),
            "max_stderr_bytes": int(max_stderr_bytes),
        }
        logs = {
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "stdout_truncated_bytes": 0,
            "stderr_truncated_bytes": 0,
            "last_output_excerpt": "",
        }
        notification = {
            "notify_on_complete": bool(notify_on_complete),
            "notify_on_failure": bool(notify_on_failure),
            "trigger_agent_on_complete": bool(trigger_agent_on_complete),
            "trigger_agent_on_failure": bool(trigger_agent_on_failure),
            "delivered": False,
            "delivery_errors": [],
            "agent_event_enqueued": False,
            "agent_event_request_id": None,
            "agent_event_errors": [],
        }
        record = self.store.create(
            BackgroundJobRecord(
                job_id=job_id,
                state="created",
                agent=str(agent or "unknown"),
                command=command_payload,
                origin=dict(origin or {}),
                policy=policy,
                process={},
                logs=logs,
                notification=notification,
                created_at=now,
                updated_at=now,
            )
        )
        self.store.update(job_id, state="starting")
        try:
            if argv:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=str(cwd_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command or "",
                    cwd=str(cwd_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
        except Exception as exc:
            return self.store.update(job_id, state="start_failed", ended_at=utc_now(), error=str(exc))

        process_meta = {
            "pid": process.pid,
            "pgid": self._pgid(process.pid),
            "started_at": utc_now(),
            "ended_at": None,
            "returncode": None,
        }
        record = self.store.update(job_id, state="running", process=process_meta)
        self._processes[job_id] = process
        self._monitor_tasks[job_id] = asyncio.create_task(self._monitor_job(job_id, process), name=f"background-job:{job_id}")
        logger.info("Background job %s started pid=%s", job_id, process.pid)
        return record

    def get(self, job_id: str) -> BackgroundJobRecord | None:
        return self.store.get(job_id)

    def list(self, *, agent: str | None = None, states: set[str] | None = None, limit: int = 50) -> list[BackgroundJobRecord]:
        return self.store.list(agent=agent, states=states, limit=limit)

    def tail(self, job_id: str, *, stream: str = "stdout", lines: int = 80) -> str:
        record = self.store.get(job_id)
        if record is None:
            raise KeyError(job_id)
        key = "stderr_path" if stream == "stderr" else "stdout_path"
        path = Path(record.logs.get(key) or "")
        if not path.exists():
            return ""
        rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(rows[-max(1, int(lines)):])

    async def cancel(self, job_id: str, *, grace_seconds: float = 2.0) -> BackgroundJobRecord:
        record = self.store.get(job_id)
        if record is None:
            raise KeyError(job_id)
        if record.is_terminal:
            return record
        self.store.update(job_id, state="cancel_requested")
        process = self._processes.get(job_id)
        if process is None:
            return self.store.update(job_id, state="abandoned_after_restart", ended_at=utc_now(), error="process_not_attached")
        await self._terminate_process_group(process, grace_seconds=grace_seconds)
        return self.store.update(job_id, state="cancelled", ended_at=utc_now(), returncode=process.returncode)

    async def _monitor_job(self, job_id: str, process: asyncio.subprocess.Process) -> None:
        record = self.store.get(job_id)
        if record is None:
            return
        stdout_path = Path(record.logs["stdout_path"])
        stderr_path = Path(record.logs["stderr_path"])
        stdout_task = asyncio.create_task(
            self._copy_stream(process.stdout, stdout_path, int(record.policy["max_stdout_bytes"])),
            name=f"{job_id}:stdout",
        )
        stderr_task = asyncio.create_task(
            self._copy_stream(process.stderr, stderr_path, int(record.policy["max_stderr_bytes"])),
            name=f"{job_id}:stderr",
        )
        self._stream_tasks[job_id] = [stdout_task, stderr_task]
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout=int(record.policy["max_runtime_seconds"]))
                latest_state = (self.store.get(job_id) or record).state
                if latest_state in {"cancel_requested", "cancelled"}:
                    state = "cancelled"
                    error = None
                else:
                    state = "succeeded" if process.returncode == 0 else "failed"
                    error = None if process.returncode == 0 else f"process exited with {process.returncode}"
            except asyncio.TimeoutError:
                await self._terminate_process_group(process, grace_seconds=2.0)
                state = "timeout"
                error = "max_runtime_seconds exceeded"
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            latest = self.store.get(job_id)
            process_meta = dict((latest or record).process)
            process_meta["ended_at"] = utc_now()
            process_meta["returncode"] = process.returncode
            logs = dict((latest or record).logs)
            logs["last_output_excerpt"] = self._last_output_excerpt(logs)
            final = self.store.update(
                job_id,
                state=state,
                process=process_meta,
                logs=logs,
                ended_at=process_meta["ended_at"],
                returncode=process.returncode,
                error=error,
            )
            await self._notify(final)
            await self._enqueue_agent_event(final)
        except asyncio.CancelledError:
            raise
        finally:
            self._processes.pop(job_id, None)
            self._monitor_tasks.pop(job_id, None)
            self._stream_tasks.pop(job_id, None)

    async def _copy_stream(self, stream: asyncio.StreamReader | None, path: Path, max_bytes: int) -> int:
        if stream is None:
            return 0
        written = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                remaining = max(0, max_bytes - written)
                if remaining:
                    clipped = chunk[:remaining]
                    fh.write(clipped)
                    fh.flush()
                    written += len(clipped)
                if written >= max_bytes:
                    # Drain to let the child exit, but do not write beyond cap.
                    continue
        return written

    async def _terminate_process_group(self, process: asyncio.subprocess.Process, *, grace_seconds: float) -> None:
        if process.returncode is not None:
            return
        pid = process.pid
        pgid = self._pgid(pid)
        try:
            if pgid is not None and os.name != "nt":
                os.killpg(pgid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
            return
        except asyncio.TimeoutError:
            pass
        try:
            if pgid is not None and os.name != "nt":
                os.killpg(pgid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    def _pgid(self, pid: int | None) -> int | None:
        if not pid or os.name == "nt":
            return None
        try:
            return os.getpgid(pid)
        except Exception:
            return None

    def _last_output_excerpt(self, logs: dict[str, Any]) -> str:
        chunks = []
        for key in ("stdout_path", "stderr_path"):
            path = Path(logs.get(key) or "")
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    chunks.append(text[-1000:])
        return "\n".join(chunks)[-2000:]

    async def _notify(self, record: BackgroundJobRecord) -> None:
        notification = dict(record.notification)
        should_notify = (
            (record.state == "succeeded" and notification.get("notify_on_complete"))
            or (record.state != "succeeded" and notification.get("notify_on_failure"))
        )
        if not should_notify:
            return
        delivered = False
        errors: list[str] = []
        runtime = self._runtime_for_agent(record.agent)
        chat_id = record.origin.get("chat_id")
        if runtime is not None and chat_id is not None and hasattr(runtime, "send_long_message"):
            try:
                await runtime.send_long_message(
                    chat_id=chat_id,
                    text=self.format_notification(record),
                    request_id=record.origin.get("request_id") or record.job_id,
                    purpose="background-job",
                )
                delivered = True
            except Exception as exc:
                errors.append(str(exc))
        else:
            errors.append("no_runtime_chat_delivery_target")
        notification["delivered"] = delivered
        notification["delivery_errors"] = errors
        self.store.update(record.job_id, notification=notification)

    async def _enqueue_agent_event(self, record: BackgroundJobRecord) -> None:
        record = self.store.get(record.job_id) or record
        notification = dict(record.notification)
        if notification.get("agent_event_enqueued"):
            return
        should_trigger = (
            (record.state == "succeeded" and notification.get("trigger_agent_on_complete"))
            or (record.state != "succeeded" and notification.get("trigger_agent_on_failure"))
        )
        if not should_trigger:
            return

        errors: list[str] = []
        runtime = self._runtime_for_agent(record.agent)
        enqueue_api_text = getattr(runtime, "enqueue_api_text", None) if runtime is not None else None
        if callable(enqueue_api_text):
            try:
                request_id = await enqueue_api_text(
                    self.format_agent_event(record),
                    source="background-job-event",
                    deliver_to_telegram=True,
                )
                notification["agent_event_enqueued"] = request_id is not None
                notification["agent_event_request_id"] = request_id
                if request_id is None:
                    errors.append("runtime_declined_background_job_event")
            except Exception as exc:
                errors.append(str(exc))
        else:
            errors.append("no_runtime_agent_event_target")

        notification["agent_event_errors"] = errors
        self.store.update(record.job_id, notification=notification)

    def _runtime_for_agent(self, agent: str) -> Any | None:
        for runtime in getattr(self.kernel, "runtimes", []) if self.kernel is not None else []:
            if getattr(runtime, "name", None) == agent:
                return runtime
        return None

    def format_notification(self, record: BackgroundJobRecord) -> str:
        duration = ""
        started = record.process.get("started_at")
        ended = record.process.get("ended_at") or record.ended_at
        if started and ended:
            duration = f"\nDuration: {started} -> {ended}"
        return (
            "Background job finished\n"
            f"ID: {record.job_id}\n"
            f"State: {record.state}\n"
            f"Exit code: {record.returncode}\n"
            f"Command: {record.command.get('display')}\n"
            f"CWD: {record.command.get('cwd')}"
            f"{duration}\n\n"
            f"Last output:\n{record.logs.get('last_output_excerpt') or '(no output)'}"
        )

    def format_agent_event(self, record: BackgroundJobRecord) -> str:
        original_task = record.origin.get("summary") or record.origin.get("original_task") or ""
        last_output = record.logs.get("last_output_excerpt") or "(no output)"
        return (
            "[background-job-event]\n"
            "This is an internal one-shot event from HASHI BackgroundJobManager.\n"
            "Use the included terminal status, paths, and last output to decide the next responsible action: "
            "summarize for the user, continue the workflow, ask for confirmation, or report failure. "
            "Only run extra inspection tools if the included evidence is insufficient or inconsistent. "
            "Do not restart the same background job unless the user explicitly requested that behavior.\n\n"
            f"event: {record.state}\n"
            f"job_id: {record.job_id}\n"
            f"agent: {record.agent}\n"
            f"returncode: {record.returncode}\n"
            f"error: {record.error or ''}\n"
            f"original_task: {original_task}\n"
            f"command: {record.command.get('display')}\n"
            f"cwd: {record.command.get('cwd')}\n"
            f"stdout_path: {record.logs.get('stdout_path')}\n"
            f"stderr_path: {record.logs.get('stderr_path')}\n\n"
            f"last_output:\n{last_output}"
        )
