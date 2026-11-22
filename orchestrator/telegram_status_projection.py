"""Bounded, owner-scoped Telegram status derived from existing connector facts.

No request to Telegram or to individual Workers is made by this projection.
Configuration is loaded only on lifecycle/configuration events; commands read
the cached binding set and in-memory transport observations.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.agent_delivery_policy import resolve_telegram_mirror_policy
from orchestrator.config_json import read_config_json
from orchestrator.telegram_access import access_snapshot
from orchestrator.telegram_actor_scope import _delegated_owner
from orchestrator.telegram_delivery_state import telegram_bot_fingerprint
from orchestrator.telegram_identity import resolve_telegram_token_key
from orchestrator.telegram_ingress_diagnostics import _MESSAGES as _SAFE_FAILURE_REASONS

_MAX_BINDINGS = 1024
_MAX_POLL_AGE = timedelta(seconds=90)


def _signature(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = path.stat()
        return info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino
    except FileNotFoundError:
        return None


class TelegramStatusProjection:
    """One shared-Functions projection. It does not persist identities."""

    def __init__(self, config_path: Path, secrets_path: Path) -> None:
        self.config_path = Path(config_path)
        self.secrets_path = Path(secrets_path)
        self._signature: tuple[Any, Any] | None = None
        self._bindings: list[dict[str, Any]] | None = None
        self._handles: dict[str, Any] = {}
        self._ingress: dict[str, tuple[Any, str | None]] = {}
        self._diagnostics: dict[str, Any] = {}
        self._known_ids: dict[str, int] = {}
        self._revision = 0

    def refresh_configuration(self) -> bool:
        """Refresh on a config or route event; never from a menu request."""
        try:
            before = (_signature(self.config_path), _signature(self.secrets_path))
            if self._bindings is not None and before == self._signature:
                return True
            if before[0] is None:
                raise ValueError("configuration unavailable")
            config = read_config_json(self.config_path)
            secrets = read_config_json(self.secrets_path) if before[1] else {}
            rows = config.get("agents")
            if not isinstance(rows, list) or len(rows) > _MAX_BINDINGS:
                raise ValueError("invalid or oversized Agent configuration")
            bindings = []
            names = set()
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ValueError("invalid Agent configuration")
                name = str(row.get("name") or "").strip()
                if not name or name.casefold() in names:
                    raise ValueError("missing or duplicate Agent name")
                names.add(name.casefold())
                access = access_snapshot(config, secrets, name)
                owners: dict[str, set[int]] = {access.owner_scope_id: set()}
                if access.primary_user_id is not None:
                    owners[access.owner_scope_id].add(access.primary_user_id)
                owners[access.owner_scope_id].update(access.legacy_user_ids)
                for actor in access.effective_user_ids:
                    if actor == access.primary_user_id or actor in access.legacy_user_ids:
                        continue
                    owners[_delegated_owner(access, actor)] = {actor}
                key = resolve_telegram_token_key(row.get("telegram_token_key"), name)
                token = secrets.get(key)
                fingerprint = telegram_bot_fingerprint(token) if isinstance(token, str) and token.strip() else None
                bindings.append({
                    "name": name,
                    "owners": owners,
                    "fingerprint": fingerprint,
                    "policy": row.get("telegram_mirror_policy"),
                })
            after = (_signature(self.config_path), _signature(self.secrets_path))
            if before != after:
                raise ValueError("configuration changed during observation")
        except (OSError, TypeError, ValueError, LookupError):
            self._bindings = None
            self._signature = None
            return False
        self._bindings = bindings
        self._signature = after
        self._revision += 1
        retained = {binding["fingerprint"] for binding in bindings if binding["fingerprint"]}
        names = {binding["name"] for binding in bindings}
        self._known_ids = {key: value for key, value in self._known_ids.copy().items() if key in retained}
        self._handles = {key: value for key, value in self._handles.copy().items() if key in names}
        self._ingress = {key: value for key, value in self._ingress.copy().items() if key in names}
        self._diagnostics = {key: value for key, value in self._diagnostics.copy().items() if key in names}
        return True

    def update_handle(self, name: str, handle: Any | None) -> None:
        if handle is None:
            self._handles.pop(str(name), None)
        else:
            if str(name) not in self._handles and len(self._handles) >= _MAX_BINDINGS:
                self._bindings = None
                return
            self._handles[str(name)] = handle

    def update_ingress(self, name: str, ingress: Any | None) -> None:
        if ingress is None:
            self._ingress.pop(str(name), None)
        else:
            if str(name) not in self._ingress and len(self._ingress) >= _MAX_BINDINGS:
                self._bindings = None
                return
            fingerprint = telegram_bot_fingerprint(getattr(ingress, "token", None))
            self._ingress[str(name)] = (ingress, fingerprint)

    def update_diagnostics(self, name: str, diagnostics: Any) -> None:
        if str(name) not in self._diagnostics and len(self._diagnostics) >= _MAX_BINDINGS:
            self._bindings = None
            return
        self._diagnostics[str(name)] = diagnostics

    def snapshot(self, owner_id: str, *, actor_id: int | None, mirror_enabled: bool) -> dict[str, Any]:
        """Return only aggregate facts authorised for the requesting owner."""
        try:
            current = (_signature(self.config_path), _signature(self.secrets_path))
        except OSError:
            current = None
        if self._bindings is None or current != self._signature:
            return {"ok": False, "reason": "status_unavailable", "revision": self._revision}
        now = datetime.now(timezone.utc)
        groups: dict[int, dict[str, bool]] = {}
        unknown: set[str] = set()
        stale = 0
        latest_success: datetime | None = None
        failure_reasons: set[str] = set()
        for binding in self._bindings:
            actors = binding["owners"].get(str(owner_id))
            if not actors or (actor_id is not None and actor_id not in actors):
                continue
            fingerprint = binding["fingerprint"]
            if not fingerprint:
                continue
            name = binding["name"]
            ingress_entry = self._ingress.get(name)
            ingress = ingress_entry[0] if ingress_entry else None
            ingress_fingerprint = ingress_entry[1] if ingress_entry else None
            handle = self._handles.get(name)
            metadata = getattr(handle, "metadata", {}) if handle is not None else {}
            metadata = metadata if isinstance(metadata, Mapping) else {}
            diagnostics = getattr(ingress, "diagnostics", None) or self._diagnostics.get(name)
            diagnostic = getattr(diagnostics, "summary", {})
            process = getattr(getattr(handle, "client", None), "process", None)
            try:
                alive = bool(process is not None and process.is_alive())
            except Exception:
                alive = False
            outbound_reason = metadata.get("telegram_connector_permanent_failure")
            if alive and isinstance(outbound_reason, str) and outbound_reason in _SAFE_FAILURE_REASONS:
                failure_reasons.add(outbound_reason)
            if (isinstance(diagnostic, Mapping) and diagnostic.get("consecutive_failures")
                    and diagnostic.get("stage") != "stopped"):
                failure = diagnostic.get("last_failure")
                error = failure.get("error") if isinstance(failure, Mapping) else None
                reason = error.get("reason") if isinstance(error, Mapping) else None
                if isinstance(reason, str) and reason in _SAFE_FAILURE_REASONS:
                    failure_reasons.add(reason)
            bot_id = None
            if ingress is not None and ingress_fingerprint == fingerprint and getattr(ingress, "_initialized", False):
                try:
                    raw_id = getattr(ingress.bot, "id")
                    if type(raw_id) is int and raw_id > 0:
                        bot_id = raw_id
                        self._known_ids[fingerprint] = raw_id
                except (AttributeError, RuntimeError, ValueError):
                    pass
            bot_id = bot_id or self._known_ids.get(fingerprint)
            if bot_id is None:
                unknown.add(fingerprint)
                continue
            status = groups.setdefault(bot_id, {"ready": False, "mirror": False})
            last_success = None
            if isinstance(diagnostic, Mapping):
                try:
                    last_success = datetime.fromisoformat(
                        str(diagnostic.get("last_success_at") or "").replace("Z", "+00:00")
                    )
                    if last_success.tzinfo is None:
                        last_success = None
                except (ValueError, TypeError):
                    pass
            if last_success is not None and (latest_success is None or last_success > latest_success):
                latest_success = last_success
            fresh = last_success is not None and timedelta(0) <= now - last_success <= _MAX_POLL_AGE
            if not fresh:
                stale += 1
            ready = bool(
                alive and metadata.get("worker_accepting") is True
                and metadata.get("telegram_outbound_connected") is True
                and metadata.get("telegram_bot_fingerprint") == fingerprint
                and ingress is not None and ingress_fingerprint == fingerprint
                and getattr(ingress, "is_running", False)
                and getattr(ingress, "connected", False) and fresh
            )
            status["ready"] = status["ready"] or ready
            try:
                effective = resolve_telegram_mirror_policy(
                    binding["policy"], instance_enabled=mirror_enabled
                )
            except (TypeError, ValueError):
                effective = False
            status["mirror"] = status["mirror"] or (ready and effective)
        ready_count = sum(item["ready"] for item in groups.values())
        return {
            "ok": True,
            "revision": self._revision,
            "observed_at": now.isoformat(),
            "latest_inbound_success_at": latest_success.isoformat() if latest_success else None,
            "configured_verified": len(groups),
            "unverified_bindings": len(unknown),
            "ready": ready_count,
            "mirror_active": sum(item["mirror"] for item in groups.values()),
            "not_ready": len(groups) - ready_count,
            "stale_bindings": stale,
            "failure_reasons": sorted(failure_reasons),
        }
