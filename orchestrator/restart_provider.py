from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from remote.security.shared_token import load_shared_token
from tools import remote_rescue


class RestartProviderError(RuntimeError):
    """Raised when a safe cold-restart provider is not currently available."""


def _normalize_instance(value: str | None) -> str:
    text = str(value or "").strip()
    if text.startswith("@"):
        text = text[1:]
    normalized = remote_rescue._normalize_instance_id(text)
    if not normalized:
        raise RestartProviderError("restart target instance is required")
    return str(normalized).upper()


def local_instance_id() -> str:
    return _normalize_instance(remote_rescue._default_instance_id())


def _auth_kwargs() -> dict[str, str | None]:
    return {
        "shared_token": load_shared_token(remote_rescue.ROOT),
        "from_instance": local_instance_id(),
    }


def _peer_state_path(source_instance: str) -> Path:
    return Path.home() / ".hashi-remote" / f"peers_state_{source_instance.lower()}.json"


def _trusted_peer_record(target_instance: str) -> dict[str, Any]:
    source = local_instance_id()
    target = _normalize_instance(target_instance)
    if source == target:
        raise RestartProviderError("local restart does not use a peer handshake")

    path = _peer_state_path(source)
    if not path.exists():
        raise RestartProviderError(
            f"Hashi Remote peer state is unavailable for {source}; start Remote and complete a handshake first"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RestartProviderError(f"cannot read Hashi Remote peer state: {exc}") from exc

    peers = data.get("peers") if isinstance(data, dict) else None
    item = (peers or {}).get(target) if isinstance(peers, dict) else None
    canonical = item.get("canonical") if isinstance(item, dict) else None
    if not isinstance(canonical, dict):
        raise RestartProviderError(
            f"{target} is not a known peer of {source}; complete the Hashi Remote handshake first"
        )

    properties = canonical.get("properties") or {}
    handshake_state = str(properties.get("handshake_state") or "").strip().lower()
    if handshake_state != "handshake_accepted":
        raise RestartProviderError(
            f"{target} is not trusted by {source}: handshake state is {handshake_state or 'unknown'}"
        )

    capabilities = {str(item) for item in (canonical.get("capabilities") or [])}
    if "rescue_restart" not in capabilities:
        raise RestartProviderError(
            f"{target} completed the handshake but did not advertise rescue_restart"
        )
    return canonical


def _live_restart_capabilities(target_instance: str) -> dict[str, Any]:
    target = _normalize_instance(target_instance)
    try:
        payload = remote_rescue.probe_capabilities(
            target,
            timeout=5,
            **_auth_kwargs(),
        )
    except Exception as exc:
        raise RestartProviderError(f"{target} Remote is not reachable: {exc}") from exc

    capabilities = payload.get("capabilities") or {}
    if not bool(capabilities.get("rescue_restart")):
        raise RestartProviderError(
            f"{target} Remote is reachable but rescue_restart is not enabled; enable L3_RESTART"
        )

    supervisor = payload.get("remote_supervisor") or {}
    mode = str(supervisor.get("mode") or "").strip().lower()
    if mode != "supervised":
        raise RestartProviderError(
            f"{target} Remote is not rescue-grade: supervisor mode is {mode or 'unknown'}"
        )
    return payload


def local_restart_provider() -> dict[str, Any]:
    """Return the local supervised Remote provider for a self cold restart."""

    target = local_instance_id()
    live = _live_restart_capabilities(target)
    return {
        "kind": "local_remote",
        "source_instance": target,
        "target_instance": target,
        "provider_instance": target,
        "base_url": live.get("base_url"),
        "remote_supervisor": live.get("remote_supervisor") or {},
        "capabilities": live.get("capabilities") or {},
    }


def peer_restart_provider(target_instance: str) -> dict[str, Any]:
    """Return a trusted peer Remote provider after handshake + live capability checks."""

    source = local_instance_id()
    target = _normalize_instance(target_instance)
    if target == source:
        return local_restart_provider()

    peer = _trusted_peer_record(target)
    live = _live_restart_capabilities(target)
    return {
        "kind": "peer_remote",
        "source_instance": source,
        "target_instance": target,
        "provider_instance": target,
        "base_url": live.get("base_url"),
        "remote_supervisor": live.get("remote_supervisor") or {},
        "capabilities": live.get("capabilities") or {},
        "handshake_state": str((peer.get("properties") or {}).get("handshake_state") or ""),
    }


def restart_via_provider(
    provider: dict[str, Any],
    *,
    reason: str,
    timeout: int = 15,
) -> tuple[int, dict[str, Any]]:
    """Revalidate the provider immediately before issuing the destructive request."""

    kind = str(provider.get("kind") or "")
    target = _normalize_instance(provider.get("target_instance"))
    if kind == "local_remote":
        verified = local_restart_provider()
    elif kind == "peer_remote":
        verified = peer_restart_provider(target)
    else:
        raise RestartProviderError(f"unsupported restart provider kind: {kind or 'unknown'}")

    if _normalize_instance(verified.get("target_instance")) != target:
        raise RestartProviderError("restart provider target changed during revalidation")

    return remote_rescue.rescue_restart(
        target,
        reason=reason,
        timeout=timeout,
        **_auth_kwargs(),
    )


__all__ = [
    "RestartProviderError",
    "local_instance_id",
    "local_restart_provider",
    "peer_restart_provider",
    "restart_via_provider",
]
