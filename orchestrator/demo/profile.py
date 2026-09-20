from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping


def _flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = default
    return max(minimum, min(maximum, resolved))


@dataclass(frozen=True)
class DemoProfile:
    enabled: bool
    service_token: str
    max_live_visitors: int = 200
    absolute_ttl_seconds: int = 86400
    idle_ttl_seconds: int = 1800
    max_sessions: int = 3
    max_input_chars: int = 4000
    max_request_bytes: int = 32768
    max_running_workers: int = 12
    worker_idle_seconds: int = 60
    cleanup_interval_seconds: int = 60
    event_wait_seconds: int = 20
    display_name: str = "HASHI Guide"

    @classmethod
    def from_runtime(
        cls,
        global_config: Any,
        secrets: Mapping[str, Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "DemoProfile":
        env = os.environ if environ is None else environ
        secret_map = dict(secrets or {})
        configured_enabled = getattr(global_config, "demo_enabled", None)
        enabled = _flag(env.get("HASHI_DEMO_ENABLED"), _flag(configured_enabled, False))
        token = str(
            secret_map.get("demo_service_token")
            or secret_map.get("hashi_demo_service_token")
            or env.get("HASHI_DEMO_SERVICE_TOKEN")
            or ""
        ).strip()
        return cls(
            enabled=enabled,
            service_token=token,
            max_live_visitors=_integer(env.get("HASHI_DEMO_MAX_VISITORS"), 200, 1, 2000),
            absolute_ttl_seconds=_integer(env.get("HASHI_DEMO_TTL_SECONDS"), 86400, 60, 86400),
            idle_ttl_seconds=_integer(env.get("HASHI_DEMO_IDLE_TTL_SECONDS"), 1800, 0, 86400),
            max_sessions=_integer(env.get("HASHI_DEMO_MAX_SESSIONS"), 3, 1, 3),
            max_input_chars=_integer(env.get("HASHI_DEMO_MAX_INPUT_CHARS"), 4000, 1, 4000),
            max_request_bytes=_integer(env.get("HASHI_DEMO_MAX_REQUEST_BYTES"), 32768, 128, 32768),
            max_running_workers=_integer(env.get("HASHI_DEMO_MAX_WORKERS"), 12, 1, 256),
            worker_idle_seconds=_integer(env.get("HASHI_DEMO_WORKER_IDLE_SECONDS"), 60, 5, 3600),
            cleanup_interval_seconds=_integer(env.get("HASHI_DEMO_CLEANUP_SECONDS"), 60, 5, 3600),
            event_wait_seconds=_integer(env.get("HASHI_DEMO_EVENT_WAIT_SECONDS"), 20, 1, 20),
            display_name=str(env.get("HASHI_DEMO_AGENT_NAME") or "HASHI Guide").strip()[:120]
            or "HASHI Guide",
        )

    @property
    def ready(self) -> bool:
        return bool(self.enabled and len(self.service_token) >= 24)

    def public_config(self) -> dict[str, Any]:
        return {
            "ok": True,
            "protocol": "hashi.shared-demo",
            "version": 1,
            "mode": "demo",
            "ready": self.ready,
            "capabilities": {
                "new_session": True,
                "cancel": True,
                "text_deltas": False,
                "uploads": False,
                "voice": False,
                "tools": False,
                "agent_switch": False,
                "events_transport": "long-poll-json-v1",
            },
            "limits": {
                "absolute_ttl_seconds": self.absolute_ttl_seconds,
                "idle_ttl_seconds": self.idle_ttl_seconds,
                "max_sessions": self.max_sessions,
                "max_input_chars": self.max_input_chars,
                "max_request_bytes": self.max_request_bytes,
            },
        }
