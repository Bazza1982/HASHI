"""Bounded in-memory lan_readiness snapshot for Remote /health.

Contract: docs/HASHI_REMOTE_PROTOCOL_SPEC.md ("lan_readiness v1") and
docs/implementation/REMOTE_LAN_OBSERVABILITY_2026-11-19.md.

Hard rules (performance gate):
- Reads (`projection`) are pure in-memory copies. No file I/O, no PowerShell,
  no network probe, no timer, no thread.
- Updates arrive only through the explicit authenticated report endpoint
  (Windows doctor/repair). This module never schedules its own work.
- Durable checkpoint writes happen only on a material snapshot change or at
  most every CHECKPOINT_MIN_SECONDS for identical healthy state, mirroring
  peer-registry persistence semantics.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
STATE_FILENAME = "lan_readiness.json"
MAX_REPORT_BYTES = 32 * 1024
MAX_STRING_BYTES = 400
CHECKPOINT_MIN_SECONDS = 600.0
STALE_AFTER_SECONDS = 3600.0

# Stable reason codes from the master plan. Future reporters may introduce new
# codes; unknown codes are preserved verbatim instead of being dropped.
KNOWN_REASON_CODES = frozenset(
    {
        "remote_process_down",
        "loopback_only_bind",
        "listening_port_mismatch",
        "port_owned_by_other_process",
        "diagnostics_unavailable",
        "firewall_rule_missing",
        "firewall_rule_disabled",
        "firewall_profile_mismatch",
        "firewall_remote_address_mismatch",
        "firewall_rule_port_mismatch",
        "firewall_explicitly_blocked",
        "firewall_local_rules_ignored_by_policy",
        "firewall_policy_unverified",
        "mdns_advertise_failed",
        "mdns_browse_failed",
        "mdns_rule_missing",
        "mdns_visibility_unverified",
        "lan_reachability_unverified",
        "tcp_connection_timeout",
        "tcp_connection_refused",
        "tls_handshake_failed",
        "authentication_rejected",
        "identity_mismatch",
        "gateway_unavailable",
    }
)

KNOWN_STATES = frozenset({"ok", "degraded", "unknown", "disabled", "unavailable"})
KNOWN_SCOPE_STATES = frozenset({"ok", "degraded", "unknown", "disabled", "unavailable"})


def _bounded_string(value: Any) -> str:
    text = str(value or "").strip()
    return text.encode("utf-8")[:MAX_STRING_BYTES].decode("utf-8", "ignore")


def _schema_version(value: Any) -> int | None:
    """Best-effort schema version read that can never raise.

    A corrupted checkpoint or report may carry any JSON value in
    ``schema_version`` (a string like ``"invalid"``, a list, a float, ...).
    ``int(...)`` on those raises ValueError, which previously escaped
    ``_load()`` during cache initialisation and could take the Remote down at
    startup. This helper returns None for anything unreadable so the caller
    degrades diagnostics instead of crashing.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _normalize_state(value: Any, default: str = "unknown") -> str:
    text = _bounded_string(value).lower()
    return text if text in KNOWN_STATES else default


def _normalize_reason_codes(values: Any) -> list[str]:
    codes: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, (list, tuple)) else []:
        code = _bounded_string(value)
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes[:32]


def _signature(snapshot: dict[str, Any]) -> str:
    """Non-observed_at signature used to suppress identical durable writes."""

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): walk(item) for key, item in sorted(value.items())}
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    stable = walk({key: value for key, value in snapshot.items() if key != "observed_at"})
    return json.dumps(stable, sort_keys=True, separators=(",", ":"))


class LanReadinessCache:
    """Validates, stores and projects the optional lan_readiness snapshot."""

    def __init__(self, hashi_root: Path | str, instance_id: str) -> None:
        self._hashi_root = Path(hashi_root)
        self._instance_id = str(instance_id or "").strip().upper()
        self._state_path = self._hashi_root / "state" / STATE_FILENAME
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_signature = ""
        self._last_persist_monotonic = 0.0
        self._activity = {
            "snapshot_updates": 0,
            "snapshot_rejections": 0,
            "persist_writes": 0,
            "persist_writes_suppressed": 0,
        }
        self._load()

    # ── durable state ─────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = self._state_path.read_bytes()
        except (FileNotFoundError, OSError):
            return
        if len(raw) > MAX_REPORT_BYTES:
            return
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        if _schema_version(data.get("schema_version")) != SCHEMA_VERSION:
            return
        if str(data.get("instance_id") or "").strip().upper() != self._instance_id:
            return
        snapshot = self._normalize(data, reported_instance=data.get("instance_id"))
        if snapshot is None:
            return
        self._snapshot = snapshot
        self._snapshot_signature = _signature(snapshot)

    def _persist(self, snapshot: dict[str, Any], *, force: bool) -> None:
        now_monotonic = time.monotonic()
        due = now_monotonic - self._last_persist_monotonic >= CHECKPOINT_MIN_SECONDS
        if not force and not due:
            self._activity["persist_writes_suppressed"] += 1
            return
        payload = dict(snapshot)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        payload["instance_id"] = self._instance_id
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(self._state_path)
            self._activity["persist_writes"] += 1
            self._last_persist_monotonic = now_monotonic
        except OSError:
            # A diagnostic checkpoint must never take the Remote down.
            return

    # ── validation / redaction ────────────────────────────────────

    @staticmethod
    def _bounded_scope(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            return None
        scope: dict[str, Any] = {}
        for key, item in list(value.items())[:16]:
            name = _bounded_string(key)[:64]
            if not name:
                continue
            if isinstance(item, (dict, list)):
                bounded = json.dumps(item, default=str, separators=(",", ":"))
                scope[name] = bounded.encode("utf-8")[:MAX_STRING_BYTES].decode("utf-8", "ignore")
            elif isinstance(item, bool):
                scope[name] = bool(item)
            elif isinstance(item, (int, float)):
                scope[name] = item
            else:
                scope[name] = _bounded_string(item)
        return scope

    def _normalize(
        self,
        report: dict[str, Any],
        *,
        reported_instance: str | None,
    ) -> dict[str, Any] | None:
        if _schema_version(report.get("schema_version")) != SCHEMA_VERSION:
            return None
        instance_id = str(report.get("instance_id") or reported_instance or "").strip().upper()
        if instance_id != self._instance_id:
            return None
        observed_at = report.get("observed_at")
        try:
            observed_at = float(observed_at) if observed_at is not None else time.time()
        except (TypeError, ValueError):
            observed_at = time.time()
        snapshot: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "instance_id": self._instance_id,
            "state": _normalize_state(report.get("state")),
            "observed_at": round(min(max(observed_at, time.time() - 86400), time.time() + 60), 1),
            "source": _bounded_string(report.get("source"))[:64] or "external_report",
            "process": self._bounded_scope(report.get("process")),
            "listener": self._bounded_scope(report.get("listener")),
            "firewall": self._bounded_scope(report.get("firewall")),
            "mdns": self._bounded_scope(report.get("mdns")),
            "external_reachability": self._bounded_scope(report.get("external_reachability")),
            "reason_codes": _normalize_reason_codes(report.get("reason_codes")),
        }
        snapshot = {key: value for key, value in snapshot.items() if value is not None}
        # Redact credential-shaped material defensively: error strings must never
        # contain the shared token, HMAC proof or raw credential material.
        for key in ("process", "listener", "firewall", "mdns", "external_reachability"):
            scope = snapshot.get(key)
            if not isinstance(scope, dict):
                continue
            for field in ("error", "last_error", "detail", "reason"):
                if isinstance(scope.get(field), str):
                    lowered = scope[field].lower()
                    if "token" in lowered or "hmac" in lowered or "credential" in lowered:
                        scope[field] = "[redacted]"
        return snapshot

    # ── update / read API ─────────────────────────────────────────

    def update(self, report: dict[str, Any]) -> dict[str, Any]:
        """Validate and store an external diagnostics report. Raises ValueError."""
        if not isinstance(report, dict):
            raise ValueError("lan_readiness report must be a JSON object")
        snapshot = self._normalize(report, reported_instance=report.get("instance_id"))
        if snapshot is None:
            self._activity["snapshot_rejections"] += 1
            raise ValueError("lan_readiness report schema or instance mismatch")
        signature = _signature(snapshot)
        changed = signature != self._snapshot_signature
        self._snapshot = snapshot
        self._snapshot_signature = signature
        self._activity["snapshot_updates"] += 1
        self._persist(snapshot, force=changed)
        return self.projection()

    def projection(self) -> dict[str, Any]:
        """Read-only /health projection. Pure memory access, no side effects."""
        if self._snapshot is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "state": "unknown",
                "observed_at": None,
                "stale": True,
                "source": "none",
                "diagnostics_available": False,
                "reason_codes": ["lan_reachability_unverified"],
            }
        now = time.time()
        age = max(0.0, now - float(self._snapshot.get("observed_at") or now))
        projection = dict(self._snapshot)
        projection["stale"] = age > STALE_AFTER_SECONDS
        projection.pop("instance_id", None)
        return projection

    def activity(self) -> dict[str, int]:
        return dict(self._activity)
