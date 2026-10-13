"""Bounded, private diagnostics for the shared Telegram poller.

Connection state and update ownership remain in CoreTelegramIngress. This
module writes facts only; it never retries, polls, acknowledges or edits offsets.
"""
from __future__ import annotations

import copy
import json
import logging
import hashlib
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from orchestrator.bootstrap_logging import CredentialRedactingFormatter, redact_log_text
from orchestrator.telegram_delivery_state import telegram_bot_fingerprint

MAX_BYTES = 524_288
BACKUP_COUNT = 3
MAX_STREAMS = 8
RETENTION_SECONDS = 7 * 24 * 3600
_MAINTENANCE_INTERVAL_SECONDS = 3600
_maintenance_at: dict[str, float] = {}
_STREAM_NAME = re.compile(r"^poller-[0-9a-f]{16}-[0-9]+\.jsonl(?:\.[1-3])?$")
SUMMARY_INTERVAL_SECONDS = 30.0
_sink_lock = threading.RLock()
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_STAGES = {"stopped", "bot_initialization", "webhook_initialization", "get_updates",
    "watchdog", "worker_delivery", "status_propagation", "route_lookup", "offset_checkpoint", "bot_shutdown"}
_MESSAGES = {
    "invalid_token": "Telegram rejected the Bot credential",
    "forbidden": "Telegram denied the poll operation",
    "conflict": "Telegram reported a conflicting poll or webhook",
    "timeout": "The Telegram request timed out",
    "watchdog_timeout": "The poll exceeded its effective watchdog deadline",
    "network_error": "The Telegram transport raised a network error",
    "retry_after": "Telegram requested a retry delay",
    "unknown": "Error details withheld; consult the recorded type and stage",
}


def _name(value: object) -> str:
    text = str(value or "")
    return text if _SAFE_NAME.fullmatch(text) else "unknown"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def safe_error(exc: BaseException, *, stage: str) -> dict[str, Any]:
    """Never copy an HTTP exception body, Telegram update or arbitrary message.

The observed class (and safe numeric attributes) is useful evidence. Free text
can contain chat content even after credential redaction, so unknown messages
are deliberately withheld at the first boundary, including the console.
"""
    from telegram.error import Conflict, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

    reason, code = "unknown", None
    if stage == "watchdog": reason = "watchdog_timeout"
    elif isinstance(exc, InvalidToken): reason, code = "invalid_token", 401
    elif isinstance(exc, Forbidden): reason, code = "forbidden", 403
    elif isinstance(exc, Conflict): reason, code = "conflict", 409
    elif isinstance(exc, RetryAfter): reason, code = "retry_after", 429
    elif isinstance(exc, (TimedOut, TimeoutError)): reason = "timeout"
    elif isinstance(exc, NetworkError): reason = "network_error"
    result: dict[str, Any] = {
        "type": _name(type(exc).__name__), "reason": reason,
        "message": redact_log_text(_MESSAGES[reason]), "cause_types": [],
    }
    if code is not None:
        # PTB types preserve the observed Telegram rejection class, not an
        # independently inspected wire HTTP status. Keep that distinction.
        result["telegram_error_code"] = code
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599: result["http_status"] = status
    retry = getattr(exc, "retry_after", None)
    if isinstance(retry, timedelta): retry = retry.total_seconds()
    if isinstance(retry, (int, float)) and 0 <= retry <= 86400:
        result["retry_after"] = float(retry)
    cause = exc.__cause__ or exc.__context__
    seen = {id(exc)}
    while cause is not None and id(cause) not in seen and len(result["cause_types"]) < 3:
        seen.add(id(cause))
        result["cause_types"].append(_name(type(cause).__name__))
        cause = cause.__cause__ or cause.__context__
    return result


class TelegramIngressDiagnostics:
    def __init__(self, *, bridge_home: Path | None, instance_id: str,
                 agent: str, generation_id: str, token: str) -> None:
        self.home = Path(bridge_home) if bridge_home is not None else None
        generation_tag = hashlib.sha256(str(generation_id).encode()).hexdigest()[:16]
        self.relative_path = f"logs/telegram-ingress/poller-{generation_tag}-{os.getpid()}.jsonl"
        self.identity = {"schema_version": 1, "instance_id": _name(instance_id),
            "agent": _name(agent), "generation_id": _name(generation_id),
            "bot_fingerprint": telegram_bot_fingerprint(token)}
        self.summary: dict[str, Any] = {**self.identity, "stage": "stopped",
            "last_success_at": None, "first_disconnected_at": None,
            "last_failure_at": None, "recovered_at": None,
            "consecutive_failures": 0, "retry_interval_seconds": None,
            "next_retry_at": None, "poll_deadline_seconds": None,
            "last_failure": None, "sink": {"path": self.relative_path,
                "encoding": "utf-8", "max_bytes": MAX_BYTES,
                "backup_count": BACKUP_COUNT, "max_streams": MAX_STREAMS,
                "retention_seconds": RETENTION_SECONDS, "last_error": None,
                "last_error_at": None, "enabled": self.home is not None}}
        self.operation_id = ""
        self._last_signature: tuple[Any, ...] | None = None
        self._last_written = 0.0

    def begin(self, stage: str, *, deadline: float | None = None) -> None:
        self.operation_id = "tg-poll-" + uuid.uuid4().hex
        self.summary.update(stage=stage, poll_deadline_seconds=deadline)

    def stage(self, value: str) -> None:
        self.summary["stage"] = value

    def _write(self, record: dict[str, Any]) -> None:
        if self.home is None: return
        handler = None
        try:
            with _sink_lock:
                path = self.home / self.relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.parent.chmod(0o700)
                self._maintain(path.parent, active_path=path)
                handler = RotatingFileHandler(path, maxBytes=MAX_BYTES,
                    backupCount=BACKUP_COUNT, encoding="utf-8")
                handler.setFormatter(CredentialRedactingFormatter("%(message)s"))
                # Call rollover/write directly so sink I/O failures are retained
                # instead of logging.Handler.handleError silently swallowing them.
                line = handler.format(logging.LogRecord("BridgeU.TelegramIngress", logging.INFO,
                    "", 0, json.dumps(record, ensure_ascii=False, separators=(",", ":")), (), None))
                log_record = logging.LogRecord("BridgeU.TelegramIngress", logging.INFO, "", 0,
                    line, (), None)
                if handler.shouldRollover(log_record): handler.doRollover()
                handler.stream.write(line + "\n")
                handler.stream.flush()
                path.chmod(0o600)
            self.summary["sink"].update(last_error=None, last_error_at=None)
        except Exception as exc:
            self.summary["sink"].update(last_error=_name(type(exc).__name__), last_error_at=_now())
        finally:
            if handler is not None:
                try: handler.close()
                except Exception: pass

    def failure(self, exc: BaseException, *, stage: str, retry_seconds: float | None) -> dict[str, Any]:
        at = _now()
        error = safe_error(exc, stage=stage)
        summary = self.summary
        summary["consecutive_failures"] += 1
        summary.update(stage=stage, last_failure_at=at,
            retry_interval_seconds=retry_seconds,
            next_retry_at=(datetime.now(timezone.utc)+timedelta(seconds=retry_seconds)).isoformat(timespec="milliseconds")
                if retry_seconds is not None else None)
        if summary["first_disconnected_at"] is None: summary["first_disconnected_at"] = at
        record = {**self.identity, "event": "failure", "at": at,
            "operation_id": self.operation_id, "stage": stage, "error": error,
            "first_disconnected_at": summary["first_disconnected_at"],
            "consecutive_failures": summary["consecutive_failures"],
            "retry_interval_seconds": retry_seconds, "next_retry_at": summary["next_retry_at"],
            "poll_deadline_seconds": summary["poll_deadline_seconds"]}
        summary["last_failure"] = copy.deepcopy(record)
        signature = (stage, error["type"], error["reason"], error.get("telegram_error_code"),
            error.get("retry_after"), retry_seconds)
        now = time.monotonic()
        if signature != self._last_signature or now-self._last_written >= SUMMARY_INTERVAL_SECONDS:
            self._write(record)
            self._last_signature, self._last_written = signature, now
            logging.getLogger("BridgeU.TelegramIngress").warning(
                "Telegram ingress failure: agent=%s stage=%s type=%s reason=%s operation=%s",
                self.identity["agent"], stage, error["type"], error["reason"], self.operation_id)
        return error

    def poll_succeeded(self) -> None:
        self.summary.update(last_success_at=_now(), stage="get_updates")
        # Successful polls also maintain retention. A quiet recovered connector
        # must not keep stale files forever waiting for another failure.
        if self.home is not None:
            path = self.home / self.relative_path
            key = str(path.parent)
            if (path.parent.is_dir() and time.monotonic()-_maintenance_at.get(key, 0)
                    >= _MAINTENANCE_INTERVAL_SECONDS):
                try:
                    with _sink_lock:
                        self._maintain(path.parent, active_path=path)
                except Exception as exc:
                    self.summary["sink"].update(last_error=_name(type(exc).__name__), last_error_at=_now())

    def recovered(self) -> None:
        at = _now()
        if self.summary["consecutive_failures"]:
            self.summary["recovered_at"] = at
            self._write({**self.identity, "event": "recovered", "at": at,
                "operation_id": self.operation_id, "stage": "get_updates",
                "consecutive_failures": self.summary["consecutive_failures"]})
        self.summary.update(stage="get_updates",
            first_disconnected_at=None, consecutive_failures=0,
            retry_interval_seconds=None, next_retry_at=None)
        self._last_signature = None

    def stopped(self) -> None:
        self.summary.update(stage="stopped", retry_interval_seconds=None, next_retry_at=None)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.summary)

    def read(self, *, limit: int = 20) -> dict[str, Any]:
        records = []
        if self.home is not None:
            path = self.home / self.relative_path
            with _sink_lock:
                items = self._streams(path.parent)
                for item in sorted(items, key=lambda candidate: candidate.stat().st_mtime):
                    try:
                        with item.open("rb") as stream: data = stream.read(MAX_BYTES + 4096)
                        for line in data.decode("utf-8", errors="replace").splitlines():
                            try: record = json.loads(line)
                            except (ValueError, TypeError): continue
                            if not isinstance(record, dict): continue
                            try:
                                stamp = datetime.fromisoformat(str(record.get("at"))).timestamp()
                            except (ValueError, TypeError): continue
                            if time.time()-stamp > RETENTION_SECONDS: continue
                            if (record.get("instance_id") == self.identity["instance_id"]
                                and record.get("agent") == self.identity["agent"]
                                and record.get("bot_fingerprint") == self.identity["bot_fingerprint"]):
                                records.append(self._project_record(record))
                    except FileNotFoundError: pass
                    except OSError as exc:
                        self.summary["sink"].update(last_error=_name(type(exc).__name__), last_error_at=_now())
        return {"ok": True, **self.identity, "summary": self.snapshot(),
                "records": records[-max(1, min(int(limit), 50)): ]}

    @staticmethod
    def _streams(directory: Path) -> list[Path]:
        try:
            return [item for item in directory.iterdir() if _STREAM_NAME.fullmatch(item.name) and item.is_file()]
        except FileNotFoundError:
            return []

    @staticmethod
    def _maintain(directory: Path, *, active_path: Path) -> None:
        key = str(directory)
        now = time.monotonic()
        expire_records = now-_maintenance_at.get(key, -_MAINTENANCE_INTERVAL_SECONDS) >= _MAINTENANCE_INTERVAL_SECONDS
        items = TelegramIngressDiagnostics._streams(directory)
        groups: dict[str, list[Path]] = {}
        wall_time = time.time()
        for item in items:
            group = item.name.split(".jsonl", 1)[0]
            groups.setdefault(group, []).append(item)
        newest = sorted(groups, key=lambda name:max(p.stat().st_mtime for p in groups[name]), reverse=True)
        active_group = active_path.name.split(".jsonl", 1)[0]
        retained = [name for name in newest if name != active_group][:MAX_STREAMS-1] + [active_group]
        for group, paths in groups.items():
            for item in paths:
                if group not in retained or wall_time-item.stat().st_mtime > RETENTION_SECONDS:
                    item.unlink(missing_ok=True)
                    continue
                if not expire_records:
                    continue
                # A stream can span more than a week without rotating. Retain
                # only timestamped facts within the declared TTL, never a raw
                # appended HTTP payload. Each owned file is size bounded.
                with item.open("rb") as stream: data=stream.read(MAX_BYTES+4096)
                kept=[]
                for line in data.decode("utf-8", errors="replace").splitlines():
                    try:
                        record=json.loads(line)
                        stamp=datetime.fromisoformat(str(record.get("at"))).timestamp()
                    except (ValueError, TypeError, AttributeError): continue
                    if wall_time-stamp <= RETENTION_SECONDS: kept.append(line)
                text="\n".join(kept)+( "\n" if kept else "")
                if text.encode()!=data:
                    temporary=item.with_name(item.name+".retention.tmp")
                    temporary.write_text(text,encoding="utf-8")
                    temporary.chmod(0o600)
                    os.replace(temporary,item)
        _maintenance_at[key]=now

    def _project_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """Treat disk records as data; never expose arbitrary appended payloads."""
        result = {key: record.get(key) for key in self.identity}
        result["generation_id"] = _name(result["generation_id"])
        result["schema_version"] = 1
        event = str(record.get("event") or "")
        result["event"] = event if event in {"failure", "recovered"} else "unknown"
        stage = str(record.get("stage") or "")
        result["stage"] = stage if stage in _STAGES else "unknown"
        for key in ("at", "first_disconnected_at", "next_retry_at"):
            value = record.get(key)
            try:
                result[key] = datetime.fromisoformat(str(value)).isoformat()
            except (ValueError, TypeError): result[key] = None
        op = str(record.get("operation_id") or "")
        result["operation_id"] = op if re.fullmatch(r"tg-poll-[0-9a-f]{32}", op) else "unknown"
        for key in ("consecutive_failures", "retry_interval_seconds", "poll_deadline_seconds"):
            value = record.get(key)
            result[key] = value if isinstance(value, (int, float)) and 0 <= value <= 1_000_000_000 else None
        if event == "failure":
            raw = record.get("error") if isinstance(record.get("error"), dict) else {}
            reason = raw.get("reason")
            reason = reason if reason in _MESSAGES else "unknown"
            result["error"] = {"type": _name(raw.get("type")), "reason": reason,
                "message": _MESSAGES[reason], "cause_types": [_name(item) for item in (raw.get("cause_types") or [])[:3]]}
            for key in ("http_status", "telegram_error_code", "retry_after"):
                value = raw.get(key)
                if isinstance(value, (int, float)) and 0 <= value <= 86400: result["error"][key] = value
        return result
