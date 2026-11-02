"""Safe PAO operation failure facts, with no retry or mutation authority."""
from __future__ import annotations

import errno
import json
import re
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

import httpx

from orchestrator import ui_language
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError

_PHASES = frozenset({
    "read", "create", "reset_backend", "bind", "save", "archive", "default", "activate",
    "configuration", "initialize", "session", "fence", "persist", "retire", "cleanup", "request", "decode",
})


class OperationRejected(RuntimeError):
    """An owner-proved rejection; no exception text crosses the boundary."""

    def __init__(self, reason: str):
        if reason not in {
            "backend_refused", "backend_unavailable", "backend_not_allowed", "privacy_blocked",
            "response_invalid", "authentication_rejected",
        }:
            raise ValueError("invalid operation rejection")
        self.reason = reason
        super().__init__(reason)


def _reason(error: Exception) -> str:
    from orchestrator.session_store import SessionConflict, SessionNotFound

    if isinstance(error, OperationRejected):
        return error.reason

    if isinstance(error, (ConfigConflictError, SessionConflict)):
        return "state_changed"
    if isinstance(error, ConfigDurabilityError):
        return "durability_unconfirmed"
    if isinstance(error, SessionNotFound):
        return "scope_unavailable"
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return ("authentication_rejected" if status in {401, 403} else "rate_limited" if status == 429
                else "service_unavailable" if status >= 500 else "http_rejected")
    if isinstance(error, httpx.TimeoutException):
        return "timed_out"
    if isinstance(error, (httpx.DecodingError, httpx.ProtocolError)):
        return "response_invalid"
    if isinstance(error, httpx.TransportError):
        return "connection_failed"
    if isinstance(error, json.JSONDecodeError):
        return "configuration_invalid"
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        return {
            sqlite3.SQLITE_BUSY: "storage_busy", sqlite3.SQLITE_LOCKED: "storage_busy",
            sqlite3.SQLITE_READONLY: "access_denied", sqlite3.SQLITE_FULL: "disk_full",
            sqlite3.SQLITE_CORRUPT: "storage_corrupt", sqlite3.SQLITE_NOTADB: "storage_corrupt",
        }.get(code, "storage_failed")
    if isinstance(error, PermissionError):
        return "access_denied"
    if isinstance(error, FileNotFoundError):
        return "missing_file"
    if isinstance(error, TimeoutError):
        return "timed_out"
    if isinstance(error, ConnectionError):
        return "connection_failed"
    if isinstance(error, OSError):
        return "disk_full" if error.errno == errno.ENOSPC or getattr(error, "winerror", 0) == 112 else "io_error"
    if isinstance(error, ValueError):
        return "invalid_input"
    return "internal_error"


@dataclass(frozen=True)
class OperationFailure:
    operation: str
    phase: str
    reason: str
    error_type: str
    error_code: str
    reference: str
    unconfirmed: bool = False

    def render(self, *, locale: str | None = None) -> str:
        return ui_language.tr(
            "operation.failure.detail", locale=locale,
            stage=ui_language.tr("operation.failure.stage." + self.phase, locale=locale),
            reason=ui_language.tr("operation.failure.reason." + self.reason, locale=locale, error_type=self.error_type),
            code=self.error_code, reference=self.reference,
        )

    def diagnostic(self) -> dict[str, str | bool]:
        return {
            "operation": self.operation, "phase": self.phase, "reason": self.reason,
            "error_type": self.error_type, "error_code": self.error_code,
            "reference": self.reference, "unconfirmed": self.unconfirmed,
        }


class OperationFailed(RuntimeError):
    """Carry the owning boundary's immutable safe facts without reclassification."""

    def __init__(self, failure: OperationFailure):
        self.failure = failure
        super().__init__(failure.error_code)


def operation_failure(error: Exception, *, operation: str, phase: str, unconfirmed: bool = False) -> OperationFailure:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", operation) or phase not in _PHASES:
        raise ValueError("invalid operation failure facts")
    error_type = type(error).__name__
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", error_type):
        error_type = "Exception"
    reason = _reason(error)
    if phase == "decode" and isinstance(error, (ValueError, TypeError)):
        reason = "response_invalid"
    return OperationFailure(
        operation, phase, reason, error_type, f"{operation}_{phase}_{reason}",
        "operr_" + uuid4().hex[:24], bool(unconfirmed or isinstance(error, ConfigDurabilityError)),
    )
