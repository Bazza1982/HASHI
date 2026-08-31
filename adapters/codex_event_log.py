"""Bounded, redacted persistence for Codex JSONL diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 2
DEFAULT_MAX_EVENT_BYTES = 64 * 1024
LOG_SCHEMA = "redacted-v1"
_MAX_STRING_CHARS = 16 * 1024

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "credentials",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_CONTENT_KEYS = {
    "aggregated_output",
    "arguments",
    "command",
    "content",
    "detail",
    "diff",
    "input",
    "output",
    "patch",
    "prompt",
    "query",
    "raw_delta",
    "reasoning",
    "request",
    "response",
    "result",
    "stderr",
    "stdout",
    "summary",
    "text",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;\"']+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"password|client[_-]?secret)\s*[:=]\s*)"
        r"([^\s,;\"']+)"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?:gh[opusr]|github_pat)_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
)
_POSIX_HOME_RE = re.compile(r"(?<![\w.-])/home/[^/\s\"']+")
_WINDOWS_HOME_RE = re.compile(
    r"(?i)(?<![\w.-])[A-Z]:\\Users\\[^\\\s\"']+"
)


def _sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key or "").casefold()).strip("_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(("_password", "_secret", "_api_key", "_credential"))


def _redact_text(value: str) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 1:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    text = _POSIX_HOME_RE.sub("$HOME", text)
    text = _WINDOWS_HOME_RE.sub("%USERPROFILE%", text)
    if len(text) > _MAX_STRING_CHARS:
        omitted = len(text) - _MAX_STRING_CHARS
        text = f"{text[:_MAX_STRING_CHARS]}… [truncated {omitted} chars]"
    return text


def _content_receipt(value: Any) -> dict[str, Any]:
    """Replace provider/model/tool content with non-reversible diagnostics."""

    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
        units = {"chars": len(value)}
    else:
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            serialized = repr(value)
        encoded = serialized.encode("utf-8", errors="replace")
        units = {"items": len(value)} if isinstance(value, (list, tuple)) else {}
    return {
        "redacted": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        **units,
    }


def _redact_value(value: Any, *, key: object = "") -> Any:
    if _sensitive_key(key):
        return "[REDACTED]"
    normalized_key = re.sub(
        r"[^a-z0-9]+",
        "_",
        str(key or "").casefold(),
    ).strip("_")
    if normalized_key in _CONTENT_KEYS and value not in (None, "", [], {}):
        return _content_receipt(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_value(child_value, key=child_key)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def sanitise_codex_event_line(
    raw_line: str,
    *,
    max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
) -> str:
    """Return one redacted, bounded, newline-free diagnostic record."""

    raw = str(raw_line or "").rstrip("\r\n")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = {"type": "hashi.codex_non_json", "content": raw}
    safe = _redact_value(decoded)
    if isinstance(safe, Mapping):
        safe = dict(safe)
        safe["_hashi_log_schema"] = LOG_SCHEMA
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    encoded_bytes = encoded.encode("utf-8", errors="replace")
    limit = max(1024, int(max_event_bytes))
    if len(encoded_bytes) <= limit:
        return encoded

    item = safe.get("item") if isinstance(safe, Mapping) else None
    envelope = {
        "type": "hashi.codex_event_truncated",
        "original_type": (
            str(safe.get("type") or "") if isinstance(safe, Mapping) else ""
        ),
        "item_type": (
            str(item.get("type") or "") if isinstance(item, Mapping) else ""
        ),
        "original_bytes": len(encoded_bytes),
        "sha256": hashlib.sha256(encoded_bytes).hexdigest(),
        "preview": _redact_text(encoded[: min(4096, limit // 2)]),
        "_hashi_log_schema": LOG_SCHEMA,
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


class CodexEventLogWriter:
    """Append sanitized events while enforcing a small rotating retention set."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        logger=None,
    ) -> None:
        self.path = Path(path)
        self.logger = logger
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.backup_count = max(0, int(backup_count))
        self.max_event_bytes = min(
            self.max_bytes - 1,
            max(1024, int(max_event_bytes)),
        )
        self._handle = None
        self._size = 0

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for index in range(1, self.backup_count + 1):
            self._sanitise_existing(self._backup(index))
        if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
            self._rotate()
        else:
            self._sanitise_existing(self.path)
        self._open()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _backup(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _open(self) -> None:
        self._handle = self.path.open("a", encoding="utf-8")
        self._size = self.path.stat().st_size if self.path.exists() else 0

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _bounded_sanitized_tail(self, path: Path) -> None:
        """Rewrite an oversized legacy backup as a redacted bounded tail."""

        if not path.exists() or path.stat().st_size <= self.max_bytes:
            return
        read_bytes = min(path.stat().st_size, self.max_bytes * 2)
        with path.open("rb") as source:
            source.seek(-read_bytes, os.SEEK_END)
            raw = source.read(read_bytes).decode("utf-8", errors="replace")
        if read_bytes < path.stat().st_size and "\n" in raw:
            raw = raw.split("\n", 1)[1]
        kept: list[str] = []
        kept_bytes = 0
        for line in reversed(raw.splitlines()):
            safe = sanitise_codex_event_line(
                line,
                max_event_bytes=self.max_event_bytes,
            )
            size = len(safe.encode("utf-8")) + 1
            if kept and kept_bytes + size > self.max_bytes:
                break
            kept.append(safe)
            kept_bytes += size
        temporary = path.with_name(f".{path.name}.retention.tmp")
        with temporary.open("w", encoding="utf-8") as target:
            for line in reversed(kept):
                target.write(line)
                target.write("\n")
        os.replace(temporary, path)

    @staticmethod
    def _is_managed(path: Path) -> bool:
        if not path.exists() or path.stat().st_size == 0:
            return True
        try:
            with path.open("r", encoding="utf-8", errors="replace") as source:
                for _ in range(4):
                    line = source.readline()
                    if not line:
                        break
                    try:
                        payload = json.loads(line)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        return False
                    if isinstance(payload, Mapping):
                        return payload.get("_hashi_log_schema") == LOG_SCHEMA
        except OSError:
            return False
        return False

    def _sanitise_existing(self, path: Path) -> None:
        """Upgrade a bounded legacy log once before accepting new records."""

        if not path.exists() or self._is_managed(path):
            return
        if path.stat().st_size > self.max_bytes:
            self._bounded_sanitized_tail(path)
            return
        temporary = path.with_name(f".{path.name}.redaction.tmp")
        with path.open("r", encoding="utf-8", errors="replace") as source, temporary.open(
            "w",
            encoding="utf-8",
        ) as target:
            for line in source:
                target.write(
                    sanitise_codex_event_line(
                        line,
                        max_event_bytes=self.max_event_bytes,
                    )
                )
                target.write("\n")
        os.replace(temporary, path)

    def _rotate(self) -> None:
        self.close()
        if self.backup_count <= 0:
            self.path.unlink(missing_ok=True)
            return
        self._backup(self.backup_count).unlink(missing_ok=True)
        for index in range(self.backup_count - 1, 0, -1):
            source = self._backup(index)
            if source.exists():
                os.replace(source, self._backup(index + 1))
        if self.path.exists():
            os.replace(self.path, self._backup(1))
            self._bounded_sanitized_tail(self._backup(1))
        if self.logger is not None:
            self.logger.info(
                "Rotated bounded Codex event log path=%s max_bytes=%s backups=%s",
                self.path,
                self.max_bytes,
                self.backup_count,
            )

    def append(self, raw_line: str) -> None:
        safe = sanitise_codex_event_line(
            raw_line,
            max_event_bytes=self.max_event_bytes,
        )
        payload = f"{safe}\n"
        payload_size = len(payload.encode("utf-8"))
        if self._handle is None:
            self._open()
        if self._size and self._size + payload_size > self.max_bytes:
            self._rotate()
            self._open()
        self._handle.write(payload)
        self._handle.flush()
        self._size += payload_size
