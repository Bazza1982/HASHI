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


def local_instance_id(
    instance_id: str | None = None,
    *,
    hashi_root: Path | str | None = None,
) -> str:
    return _normalize_instance(
        instance_id or remote_rescue._default_instance_id(hashi_root)
    )


def _resolved_local_instance_id(
    instance_id: str | None = None,
    *,
    hashi_root: Path | str | None = None,
) -> str:
    if instance_id is None and hashi_root is None:
        return local_instance_id()
    return local_instance_id(instance_id, hashi_root=hashi_root)


def _auth_kwargs(
    *,
    source_instance: str | None = None,
    hashi_root: Path | str | None = None,
) -> dict[str, str | None]:
    root = Path(hashi_root).expanduser().resolve() if hashi_root else remote_rescue.ROOT
    return {
        "shared_token": load_shared_token(root),
        "from_instance": (
            _normalize_instance(source_instance)
            if source_instance
            else _resolved_local_instance_id(hashi_root=hashi_root)
        ),
    }


def _peer_state_path(source_instance: str) -> Path:
    return Path.home() / ".hashi-remote" / f"peers_state_{source_instance.lower()}.json"


def _trusted_peer_record(
    target_instance: str,
    *,
    source_instance: str | None = None,
) -> dict[str, Any]:
    source = (
        _normalize_instance(source_instance)
        if source_instance
        else _resolved_local_instance_id()
    )
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


def _peer_handshake_state(peer: Any) -> str:
    if not isinstance(peer, dict):
        return ""
    properties = peer.get("properties") or {}
    if isinstance(properties, dict):
        state = str(properties.get("handshake_state") or "").strip().lower()
        if state:
            return state
    canonical = peer.get("canonical") or {}
    if isinstance(canonical, dict):
        properties = canonical.get("properties") or {}
        if isinstance(properties, dict):
            return str(properties.get("handshake_state") or "").strip().lower()
    return ""


def _peer_instance_id(peer: Any) -> str:
    if not isinstance(peer, dict):
        return ""
    value = peer.get("instance_id")
    if not value and isinstance(peer.get("canonical"), dict):
        value = peer["canonical"].get("instance_id")
    try:
        return _normalize_instance(value)
    except RestartProviderError:
        return ""


def _target_confirms_source_handshake(
    target_instance: str,
    *,
    base_url: str,
    source_instance: str | None = None,
    hashi_root: Path | str | None = None,
) -> dict[str, Any]:
    """Require the target Remote to still expose the source as an accepted peer.

    The request itself uses the shared-token HMAC identity. The trusted health
    view then proves that the target's current peer registry still accepts the
    same source instance; a stale source-side handshake record is insufficient.
    """

    explicit_context = source_instance is not None or hashi_root is not None
    source = (
        _normalize_instance(source_instance)
        if source_instance
        else _resolved_local_instance_id(hashi_root=hashi_root)
    )
    target = _normalize_instance(target_instance)
    auth_kwargs = (
        _auth_kwargs(source_instance=source, hashi_root=hashi_root)
        if explicit_context
        else _auth_kwargs()
    )
    try:
        result = remote_rescue._request_json_status(
            f"{str(base_url).rstrip('/')}/health",
            timeout=5,
            **auth_kwargs,
        )
    except Exception as exc:
        raise RestartProviderError(
            f"{target} could not confirm the live peer handshake: {exc}"
        ) from exc

    if not result.ok or not isinstance(result.body, dict):
        raise RestartProviderError(
            f"{target} could not confirm the live peer handshake"
        )
    if result.body.get("trusted_view") is not True:
        raise RestartProviderError(
            f"{target} did not return an authenticated trusted peer view"
        )

    instance = result.body.get("instance") or {}
    target_identity = ""
    if isinstance(instance, dict):
        target_identity = str(instance.get("instance_id") or "").strip().upper()
    if target_identity and target_identity != target:
        raise RestartProviderError(
            f"restart route identity mismatch: expected {target}, got {target_identity}"
        )

    peers = result.body.get("peers") or []
    for peer in peers if isinstance(peers, list) else []:
        if _peer_instance_id(peer) != source:
            continue
        state = _peer_handshake_state(peer)
        if state == "handshake_accepted":
            return peer
        raise RestartProviderError(
            f"{target} does not currently trust {source}: handshake state is {state or 'unknown'}"
        )

    raise RestartProviderError(
        f"{target} does not currently list {source} as a trusted peer; complete the bilateral handshake first"
    )


def _live_restart_capabilities(
    target_instance: str,
    *,
    source_instance: str | None = None,
    hashi_root: Path | str | None = None,
) -> dict[str, Any]:
    target = _normalize_instance(target_instance)
    source = (
        _normalize_instance(source_instance)
        if source_instance
        else _resolved_local_instance_id(hashi_root=hashi_root)
    )
    try:
        payload = remote_rescue.probe_capabilities(
            target,
            timeout=5,
            root=hashi_root,
            local_instance_id=source,
            **_auth_kwargs(
                source_instance=source,
                hashi_root=hashi_root,
            ),
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


def local_restart_provider(
    *,
    instance_id: str | None = None,
    hashi_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return the local supervised Remote provider for a self cold restart."""

    target = _resolved_local_instance_id(instance_id, hashi_root=hashi_root)
    live = _live_restart_capabilities(
        target,
        source_instance=target,
        hashi_root=hashi_root,
    )
    provider = {
        "kind": "local_remote",
        "source_instance": target,
        "target_instance": target,
        "provider_instance": target,
        "base_url": live.get("base_url"),
        "remote_supervisor": live.get("remote_supervisor") or {},
        "capabilities": live.get("capabilities") or {},
    }
    if hashi_root:
        provider["hashi_root"] = str(Path(hashi_root).expanduser().resolve())
    return provider


def peer_restart_provider(
    target_instance: str,
    *,
    source_instance: str | None = None,
    hashi_root: Path | str | None = None,
) -> dict[str, Any]:
    """Return a trusted peer Remote after bilateral handshake + live capability checks."""

    source = _resolved_local_instance_id(source_instance, hashi_root=hashi_root)
    target = _normalize_instance(target_instance)
    if target == source:
        return local_restart_provider(instance_id=source, hashi_root=hashi_root)

    peer = _trusted_peer_record(target, source_instance=source)
    live = _live_restart_capabilities(
        target,
        source_instance=source,
        hashi_root=hashi_root,
    )
    base_url = str(live.get("base_url") or "").strip()
    if not base_url:
        raise RestartProviderError(f"{target} Remote did not provide a live base URL")
    target_peer = _target_confirms_source_handshake(
        target,
        base_url=base_url,
        source_instance=source,
        hashi_root=hashi_root,
    )
    provider = {
        "kind": "peer_remote",
        "source_instance": source,
        "target_instance": target,
        "provider_instance": target,
        "base_url": base_url,
        "remote_supervisor": live.get("remote_supervisor") or {},
        "capabilities": live.get("capabilities") or {},
        "handshake_state": str((peer.get("properties") or {}).get("handshake_state") or ""),
        "target_handshake_state": _peer_handshake_state(target_peer),
    }
    if hashi_root:
        provider["hashi_root"] = str(Path(hashi_root).expanduser().resolve())
    return provider


def restart_via_provider(
    provider: dict[str, Any],
    *,
    reason: str,
    requester_agent: str | None = None,
    request_source: str | None = None,
    timeout: int = remote_rescue.RESTART_REQUEST_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, Any]]:
    """Revalidate the provider immediately before issuing the destructive request."""

    kind = str(provider.get("kind") or "")
    target = _normalize_instance(provider.get("target_instance"))
    source = _normalize_instance(provider.get("source_instance"))
    hashi_root = provider.get("hashi_root")
    if kind == "local_remote":
        verified = (
            local_restart_provider(instance_id=source, hashi_root=hashi_root)
            if hashi_root
            else local_restart_provider()
        )
    elif kind == "peer_remote":
        verified = (
            peer_restart_provider(
                target,
                source_instance=source,
                hashi_root=hashi_root,
            )
            if hashi_root
            else peer_restart_provider(target)
        )
    else:
        raise RestartProviderError(f"unsupported restart provider kind: {kind or 'unknown'}")

    if _normalize_instance(verified.get("target_instance")) != target:
        raise RestartProviderError("restart provider target changed during revalidation")

    auth_kwargs = (
        _auth_kwargs(source_instance=source, hashi_root=hashi_root)
        if hashi_root
        else _auth_kwargs()
    )
    return remote_rescue.rescue_restart(
        target,
        reason=reason,
        extra_payload={
            "requester_agent": str(requester_agent or "").strip() or None,
            "request_source": str(request_source or "").strip() or None,
            "notify_agent": str(requester_agent or "").strip() or None,
            "notify_via": str(request_source or "").strip() or None,
        },
        timeout=timeout,
        root=hashi_root,
        local_instance_id=source,
        **auth_kwargs,
    )


__all__ = [
    "RestartProviderError",
    "local_instance_id",
    "local_restart_provider",
    "peer_restart_provider",
    "restart_via_provider",
]
