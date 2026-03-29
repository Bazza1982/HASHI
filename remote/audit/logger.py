"""
Audit logger for Hashi Remote.
Records all security-sensitive events: hchat relay, terminal execution, pairing.
Adapted from Lily Remote.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

_audit_logger_instance: Optional["AuditLogger"] = None

logger = logging.getLogger(__name__)


class AuditLogger:
    """Logs security-sensitive Hashi Remote events to a JSONL file."""

    def __init__(self, log_path: Optional[Path] = None):
        self._log_path = log_path or (Path.home() / ".hashi-remote" / "audit.jsonl")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, event_type: str, data: dict) -> None:
        entry = {
            "ts": time.time(),
            "event": event_type,
            **data,
        }
        try:
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("Audit log write failed: %s", e)

    def log_hchat_received(self, from_instance: str, to_agent: str, text_snippet: str) -> None:
        self._write("hchat_received", {
            "from": from_instance,
            "to_agent": to_agent,
            "snippet": text_snippet[:100],
        })

    def log_terminal_exec(self, client_id: str, command: str, allowed: bool) -> None:
        self._write("terminal_exec", {
            "client": client_id,
            "command": command[:200],
            "allowed": allowed,
        })

    def log_pairing_request(self, client_id: str, client_name: str, auto_approved: bool) -> None:
        self._write("pairing_request", {
            "client": client_id,
            "name": client_name,
            "auto_approved": auto_approved,
        })

    def log_peer_discovered(self, instance_id: str, host: str, port: int) -> None:
        self._write("peer_discovered", {
            "instance": instance_id,
            "host": host,
            "port": port,
        })


def get_audit_logger() -> AuditLogger:
    global _audit_logger_instance
    if _audit_logger_instance is None:
        _audit_logger_instance = AuditLogger()
    return _audit_logger_instance
