"""Instance-selected standard Session attachment limits.

A new qualified Functions process reads the instance document; changing this
policy never requires changing or cold-restarting Core. HChat remains governed
by its separate versioned transport contract.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from orchestrator.config_json import read_config_json
from orchestrator.pathing import resolve_bridge_home


class AttachmentSizeExceeded(ValueError):
    """Trusted byte-budget facts for ingress and safe failure presentation."""

    def __init__(
        self, actual_bytes: int | None, maximum_bytes: int, *, scope: str = "file"
    ):
        self.actual_bytes = actual_bytes
        self.maximum_bytes = maximum_bytes
        self.scope = scope
        super().__init__(
            f"attachment exceeds the {maximum_bytes} byte limit"
            + (f" (received {actual_bytes} bytes)" if actual_bytes is not None else "")
        )


@dataclass(frozen=True)
class SessionAttachmentLimits:
    count: int
    attachment_bytes: int
    total_bytes: int


def load_session_attachment_limits(
    environ: Mapping[str, str] | None = None,
    *,
    config_path: Path | None = None,
) -> SessionAttachmentLimits:
    selected = os.environ if environ is None else environ
    if config_path is None:
        home = resolve_bridge_home(
            Path(__file__).resolve().parents[1], selected.get("BRIDGE_HOME", "")
        )
        config_path = home / "state" / "session_attachment_limits.json"
    try:
        configured = read_config_json(config_path)
    except FileNotFoundError:
        configured = {}
    else:
        fields = {
            "version", "max_attachments_per_message", "max_attachment_bytes",
            "max_total_attachment_bytes_per_message",
        }
        if type(configured.get("version")) is not int or configured["version"] != 1:
            raise ValueError("Session attachment limits require version 1")
        if set(configured) - fields:
            raise ValueError("Session attachment limits contain unknown fields")

    def read(field: str, name: str, default: int, maximum: int) -> int:
        # A persisted instance choice overrides an old inherited Core
        # environment when a replacement Functions process starts.
        if field in configured:
            raw = configured[field]
        elif name in selected:
            raw = selected[name]
        else:
            return default
        clean = str(raw).strip()
        if not clean.isascii() or not clean.isdecimal():
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        value = int(clean)
        if not 1 <= value <= maximum:
            raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        return value

    return SessionAttachmentLimits(
        count=read(
            "max_attachments_per_message",
            "HASHI_SESSION_MAX_ATTACHMENTS_PER_MESSAGE", 16, 1024,
        ),
        attachment_bytes=read(
            "max_attachment_bytes",
            "HASHI_SESSION_MAX_ATTACHMENT_BYTES", 64 * 1024 * 1024, 1024**3,
        ),
        total_bytes=read(
            "max_total_attachment_bytes_per_message",
            "HASHI_SESSION_MAX_TOTAL_ATTACHMENT_BYTES", 64 * 1024 * 1024, 1024**3,
        ),
    )


SESSION_ATTACHMENT_LIMITS = load_session_attachment_limits()
# HASHI-Fingerprint: hfp_0b4b97eae81f4867b40f4bdea7f6ea36
