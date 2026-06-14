from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_WRITE_LOCK = threading.Lock()

_SENSITIVE_COMMANDS = frozenset(
    {
        "notepad",
        "token",
        "paswd",
        "pswd",
        "hchat",
        "memory",
        "sys",
        "credit",
    }
)
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|password|passwd|secret|bearer)\s*[:=]\s*\S+"
)
_MAX_ARG_CHARS = 240


def default_audit_path(workspace_dir: Path) -> Path:
    return Path(workspace_dir) / "slash_command_audit.jsonl"


def bridge_audit_path(base_logs_dir: Path) -> Path:
    return Path(base_logs_dir) / "whatsapp_slash_command_audit.jsonl"


def redact_args(command_name: str, args: list[str] | None) -> list[str]:
    raw = [str(arg) for arg in (args or [])]
    if (command_name or "").lower() in _SENSITIVE_COMMANDS:
        return ["[redacted]"] if raw else []
    redacted: list[str] = []
    for arg in raw:
        cleaned = _SECRET_PATTERN.sub(r"\1=[redacted]", arg)
        if len(cleaned) > _MAX_ARG_CHARS:
            cleaned = cleaned[:_MAX_ARG_CHARS] + "...[truncated]"
        redacted.append(cleaned)
    return redacted


def build_audit_record(
    *,
    agent: str,
    command_name: str,
    args: list[str] | None = None,
    source_channel: str,
    handler_kind: str,
    status: str,
    duration_ms: int | float,
    actor_id: int | str | None = None,
    chat_id: int | str | None = None,
    error: str | None = None,
    blocked_reason: str | None = None,
    side_effects: list[str] | None = None,
    ts: str | None = None,
) -> dict[str, Any]:
    return {
        "ts": ts or datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "command_name": command_name,
        "args_redacted": redact_args(command_name, args),
        "source_channel": source_channel,
        "handler_kind": handler_kind,
        "status": status,
        "duration_ms": max(0, int(duration_ms)),
        "actor_id": actor_id,
        "chat_id": chat_id,
        "error": error,
        "blocked_reason": blocked_reason,
        "side_effects": list(side_effects or []),
    }


def append_audit_record(path: Path, record: dict[str, Any]) -> Path:
    audit_path = Path(path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with _WRITE_LOCK:
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return audit_path


def resolve_handler_kind(runtime: Any, command_name: str) -> str:
    name = (command_name or "").lower()
    if hasattr(runtime, f"cmd_{name}"):
        return "native"
    try:
        from orchestrator.command_registry import runtime_command_map

        if name in runtime_command_map():
            return "registry"
    except Exception:
        pass
    return "unknown"


def record_command_audit(
    *,
    audit_path: Path,
    agent: str,
    command_name: str,
    args: list[str] | None,
    source_channel: str,
    handler_kind: str,
    status: str,
    duration_ms: int | float,
    actor_id: int | str | None = None,
    chat_id: int | str | None = None,
    error: str | None = None,
    blocked_reason: str | None = None,
    side_effects: list[str] | None = None,
) -> None:
    try:
        append_audit_record(
            audit_path,
            build_audit_record(
                agent=agent,
                command_name=command_name,
                args=args,
                source_channel=source_channel,
                handler_kind=handler_kind,
                status=status,
                duration_ms=duration_ms,
                actor_id=actor_id,
                chat_id=chat_id,
                error=error,
                blocked_reason=blocked_reason,
                side_effects=side_effects,
            ),
        )
    except Exception:
        pass


class SlashCommandAuditSession:
    def __init__(
        self,
        *,
        audit_path: Path,
        agent: str,
        command_name: str,
        args: list[str] | None,
        source_channel: str,
        handler_kind: str,
        actor_id: int | str | None = None,
        chat_id: int | str | None = None,
    ) -> None:
        self.audit_path = audit_path
        self.agent = agent
        self.command_name = command_name
        self.args = list(args or [])
        self.source_channel = source_channel
        self.handler_kind = handler_kind
        self.actor_id = actor_id
        self.chat_id = chat_id
        self._started = time.monotonic()
        self.status = "success"
        self.error: str | None = None
        self.blocked_reason: str | None = None
        self.side_effects: list[str] = []
        self._finished = False

    def deny(self, reason: str) -> None:
        self.status = "denied"
        self.blocked_reason = reason

    def block(self, reason: str) -> None:
        self.status = "blocked"
        self.blocked_reason = reason

    def fail(self, exc: BaseException | str) -> None:
        self.status = "failed"
        self.error = str(exc) if not isinstance(exc, str) else exc

    def add_side_effect(self, name: str) -> None:
        if name and name not in self.side_effects:
            self.side_effects.append(name)

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        record_command_audit(
            audit_path=self.audit_path,
            agent=self.agent,
            command_name=self.command_name,
            args=self.args,
            source_channel=self.source_channel,
            handler_kind=self.handler_kind,
            status=self.status,
            duration_ms=int((time.monotonic() - self._started) * 1000),
            actor_id=self.actor_id,
            chat_id=self.chat_id,
            error=self.error,
            blocked_reason=self.blocked_reason,
            side_effects=self.side_effects,
        )

def looks_like_slash_command(text: str) -> bool:
    raw = (text or "").strip()
    if not raw.startswith("/"):
        return False
    body = raw[1:].strip()
    if not body:
        return False
    if body.startswith(" "):
        return False
    return bool(body.split()[0])


def parse_slash_command_text(text: str) -> tuple[str, list[str]]:
    import shlex

    raw = (text or "").strip()
    if not raw.startswith("/"):
        return "", []
    body = raw[1:]
    try:
        parts = shlex.split(body)
    except Exception:
        parts = body.split()
    if not parts:
        return "", []
    return parts[0].split("@", 1)[0].lower(), parts[1:]


def parse_inline_callback_command(callback_data: str) -> tuple[str, list[str]]:
    raw = (callback_data or "").strip()
    if not raw:
        return "callback", []
    if ":" in raw:
        head, tail = raw.split(":", 1)
        if ":" in tail:
            target, value = tail.split(":", 1)
            return f"{head}:{target}", [value]
        return head, [tail]
    return raw, []


def is_supported_slash_command(runtime: Any, command_name: str) -> bool:
    if not command_name:
        return False
    try:
        from orchestrator.admin_local_testing import supported_commands

        return command_name in supported_commands(runtime)
    except Exception:
        return False

