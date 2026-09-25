"""Typed per-Run delivery policy for HASHI Frontend Connectors.

The legacy ``deliver_to_telegram`` boolean is deliberately not a public
suppression switch.  A TUI may opt one newly submitted Run out of Telegram
mirroring only by supplying this complete, typed policy together with its
ephemeral client identity.  The policy is copied into request metadata at
admission, so later preference changes cannot alter an in-flight Run.
"""
from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from typing import Any


DELIVERY_POLICY_TYPE = "hashi.frontend-delivery-policy"
DELIVERY_POLICY_VERSION = 2
DELIVERY_POLICY_SCOPE = "run"
LEGACY_TUI_DELIVERY_POLICY_TYPE = "hashi.frontend-delivery"
LEGACY_TUI_DELIVERY_POLICY_VERSION = 1
TUI_FRONTEND_KIND = "tui"
TUI_MUTATING_PROXY_OPERATIONS = frozenset(
    {
        "chat",
        "chat_attachment",
        "session_run",
        "session_command_invocation",
        "session_attachment_run",
        "session_workzone_attachment_run",
        "speech",
        "voice_profile",
    }
)
FRONTEND_CLIENT_METADATA_KEY = "frontend_client"
FRONTEND_DELIVERY_METADATA_KEY = "frontend_delivery_policy"
RUN_DELIVERY_ROUTE_METADATA_KEY = "_run_delivery_route"
RUN_DELIVERY_ROUTE_TYPE = "hashi.run-delivery-route"
RUN_DELIVERY_ROUTE_VERSION = 1


def _surface(value: Any, *, fallback: str = "unknown") -> str:
    normalized = str(value or "").strip().casefold()
    if (
        not normalized
        or len(normalized) > 80
        or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789._-"
            for character in normalized
        )
    ):
        return fallback
    return normalized


def _channel(value: Any, *, fallback: str = "default") -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 512 or any(
        ord(character) < 32 for character in normalized
    ):
        return fallback
    return normalized


def _destination(surface: Any, channel_key: Any) -> dict[str, str]:
    return {
        "surface": _surface(surface),
        "channel_key": _channel(channel_key),
    }


def normalize_run_delivery_route(value: Any) -> dict[str, Any]:
    """Validate PAO's private, immutable route snapshot for one Run."""

    if not isinstance(value, Mapping):
        raise ValueError("Run delivery route must be an object")
    if str(value.get("type") or "") != RUN_DELIVERY_ROUTE_TYPE:
        raise ValueError("unsupported Run delivery route type")
    if value.get("version") != RUN_DELIVERY_ROUTE_VERSION:
        raise ValueError("unsupported Run delivery route version")
    if str(value.get("scope") or "") != "run":
        raise ValueError("Run delivery route scope must be run")
    primary_raw = value.get("primary")
    primary = None
    if primary_raw is not None:
        if not isinstance(primary_raw, Mapping):
            raise ValueError("Run delivery route primary must be an object or null")
        primary = _destination(
            primary_raw.get("surface"), primary_raw.get("channel_key")
        )
        if primary["surface"] == "unknown":
            raise ValueError("Run delivery route primary surface is invalid")
    mirrors_raw = value.get("mirrors")
    if not isinstance(mirrors_raw, (list, tuple)):
        raise ValueError("Run delivery route mirrors must be a list")
    mirrors: list[dict[str, str]] = []
    seen = {primary["surface"]} if primary is not None else set()
    for raw in mirrors_raw:
        if not isinstance(raw, Mapping):
            raise ValueError("Run delivery route mirror must be an object")
        destination = _destination(raw.get("surface"), raw.get("channel_key"))
        surface = destination["surface"]
        if surface == "unknown":
            raise ValueError("Run delivery route mirror surface is invalid")
        if surface in seen:
            raise ValueError("Run delivery route destinations must be unique")
        seen.add(surface)
        mirrors.append(destination)
    automatic = value.get("automatic")
    if not isinstance(automatic, bool):
        raise ValueError("Run delivery route automatic must be boolean")
    if automatic != bool(primary is not None or mirrors):
        raise ValueError("Run delivery route automatic contradicts its destinations")
    return {
        "type": RUN_DELIVERY_ROUTE_TYPE,
        "version": RUN_DELIVERY_ROUTE_VERSION,
        "scope": "run",
        "primary": primary,
        "mirrors": mirrors,
        "automatic": automatic,
    }


def freeze_run_delivery_route(
    *,
    message_source_id: str,
    session_surface: str,
    session_channel_key: str,
    chat_id: Any,
    telegram_requested: bool,
    primary_channel_key: Any | None = None,
    terminal_exchange: bool = False,
) -> dict[str, Any]:
    """Resolve the sole Connector route before PAO persists or projects a Run.

    ``telegram_requested`` is already the server-side admission decision.  It
    represents a mirror for non-Telegram frontends, not a right for caller
    metadata to suppress or redirect another connector.
    """

    source_id = _surface(message_source_id)
    resolved_surface = _surface(session_surface)
    resolved_channel = _channel(session_channel_key)
    primary_channel = _channel(primary_channel_key, fallback=resolved_channel)
    telegram_channel = _channel(chat_id)

    if source_id == "telegram":
        primary = _destination("telegram", telegram_channel)
    elif source_id == "tui":
        primary = _destination("tui", primary_channel)
    elif source_id == "whatsapp":
        primary = _destination("whatsapp", primary_channel)
    elif source_id == "hchat" and terminal_exchange:
        primary = (
            _destination("telegram", telegram_channel)
            if telegram_requested
            else _destination(resolved_surface, resolved_channel)
        )
    elif source_id == "hchat":
        primary = _destination("hchat", primary_channel)
    elif source_id == "hashi.internal":
        primary = (
            _destination("telegram", telegram_channel)
            if telegram_requested
            else None
        )
    else:
        primary_surface = (
            resolved_surface
            if resolved_surface not in {"unknown", "scheduled"}
            else ("api" if source_id == "api" else source_id)
        )
        primary = (
            _destination(primary_surface, primary_channel)
            if primary_surface not in {"unknown", "hashi.internal"}
            else None
        )

    mirrors: list[dict[str, str]] = []
    primary_surface = primary["surface"] if primary is not None else ""
    # WhatsApp owns its authenticated reply callback and must never inherit the
    # legacy Telegram visibility default.  TUI, HChat and API-compatible
    # clients preserve their established optional Telegram mirror.
    if (
        telegram_requested
        and primary_surface != "telegram"
        and source_id != "whatsapp"
        and not terminal_exchange
    ):
        mirrors.append(_destination("telegram", telegram_channel))

    return normalize_run_delivery_route(
        {
            "type": RUN_DELIVERY_ROUTE_TYPE,
            "version": RUN_DELIVERY_ROUTE_VERSION,
            "scope": "run",
            "primary": primary,
            "mirrors": mirrors,
            "automatic": bool(primary is not None or mirrors),
        }
    )


def route_destination(
    route: Mapping[str, Any] | None, surface: str
) -> dict[str, str] | None:
    """Return one destination from a validated route without exposing aliases."""

    try:
        normalized = normalize_run_delivery_route(route)
    except ValueError:
        return None
    wanted = _surface(surface)
    destinations = [normalized.get("primary"), *normalized["mirrors"]]
    for destination in destinations:
        if isinstance(destination, Mapping) and destination.get("surface") == wanted:
            return copy.deepcopy(dict(destination))
    return None


def project_run_delivery_route(route: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project only non-secret destination facts into current-message PCM."""

    normalized = normalize_run_delivery_route(route)
    primary = normalized.get("primary")
    mirrors = [item["surface"] for item in normalized["mirrors"]]
    return {
        "surface": primary["surface"] if isinstance(primary, Mapping) else "none",
        "mirrors": mirrors,
        "automatic": bool(normalized["automatic"]),
        # Compatibility for older TUI consumers.  This is a plan, never a
        # delivery-success flag.
        "telegram_mirror": "telegram" in mirrors,
    }


def delivery_intent_from_run_route(
    route: Mapping[str, Any] | None,
    *,
    event_id: str,
    session_id: str,
    idempotency_key: str,
    content_modes: list[str] | tuple[str, ...] = ("text",),
) -> dict[str, Any]:
    """Project the frozen compatibility route into endpoint-level FC intent."""

    from orchestrator.frontend_connector_registry import (
        canonical_connector_id,
        endpoint_id_for,
    )
    from orchestrator.frontend_contracts import normalize_delivery_intent

    normalized_route = normalize_run_delivery_route(route)
    raw_destinations = [
        ("primary", normalized_route.get("primary")),
        *[("mirror", item) for item in normalized_route["mirrors"]],
    ]
    destinations = []
    for role, item in raw_destinations:
        if not isinstance(item, Mapping):
            continue
        surface = str(item["surface"])
        channel_key = str(item["channel_key"])
        connector_id = canonical_connector_id(
            surface, ingress_transport=surface, surface=surface
        )
        destinations.append(
            {
                "connector_id": connector_id,
                "endpoint_id": endpoint_id_for(
                    connector_id,
                    ingress_transport=surface,
                    channel_key=channel_key,
                ),
                "channel_key": channel_key,
                "role": role,
                "content_modes": list(content_modes),
                # The old Telegram transport cannot prove idempotent retries.
                "retry_class": "query_before_retry",
            }
        )
    digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()
    return normalize_delivery_intent(
        {
            "type": "hashi.delivery-intent",
            "version": 2,
            "scope": "run",
            "event_id": event_id,
            "session_id": session_id,
            "idempotency_digest": f"sha256:{digest}",
            "destinations": destinations,
        }
    )


def _client_id(value: Any) -> str:
    client_id = str(value or "").strip()
    if (
        not client_id
        or len(client_id) > 128
        or any(ord(character) < 33 or ord(character) > 126 for character in client_id)
    ):
        raise ValueError("frontend delivery policy requires a valid client_id")
    return client_id


def frontend_run_delivery_policy(
    *,
    connector_id: str,
    client_id: str,
    targets: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    """Build a client-bound per-Run policy over connector-neutral targets."""

    normalized_connector = _surface(connector_id, fallback="")
    if normalized_connector == "unknown":
        raise ValueError("frontend delivery policy requires a valid connector_id")
    return {
        "type": DELIVERY_POLICY_TYPE,
        "version": DELIVERY_POLICY_VERSION,
        "scope": DELIVERY_POLICY_SCOPE,
        "connector_id": normalized_connector,
        "client_id": _client_id(client_id),
        "targets": [
            {
                "connector_id": _surface(item.get("connector_id"), fallback=""),
                "role": str(item.get("role") or "").strip().casefold(),
                "enabled": item.get("enabled"),
            }
            for item in targets
        ],
    }


def normalize_frontend_run_delivery_policy(
    value: Any,
    *,
    connector_id: str,
    client_id: str,
) -> dict[str, Any]:
    """Validate generic delivery preferences, with a v1 TUI compatibility read."""

    if not isinstance(value, Mapping):
        raise ValueError("delivery_policy must be an object")
    expected_client_id = _client_id(client_id)
    expected_connector = _surface(connector_id, fallback="")
    if expected_connector == "unknown":
        raise ValueError("frontend delivery policy connector is invalid")
    # Read-only migration for older TUI clients. New writers emit only v2.
    if (
        str(value.get("type") or "") == LEGACY_TUI_DELIVERY_POLICY_TYPE
        and value.get("version") == LEGACY_TUI_DELIVERY_POLICY_VERSION
        and expected_connector == TUI_FRONTEND_KIND
        and str(value.get("frontend") or "") == TUI_FRONTEND_KIND
    ):
        telegram = value.get("telegram")
        if not isinstance(telegram, Mapping) or not isinstance(
            telegram.get("mirror"), bool
        ):
            raise ValueError("legacy delivery_policy telegram.mirror must be boolean")
        if _client_id(value.get("client_id")) != expected_client_id:
            raise ValueError("delivery_policy client_id mismatch")
        return frontend_run_delivery_policy(
            connector_id=expected_connector,
            client_id=expected_client_id,
            targets=[
                {
                    "connector_id": "telegram",
                    "role": "mirror",
                    "enabled": bool(telegram["mirror"]),
                }
            ],
        )
    if str(value.get("type") or "") != DELIVERY_POLICY_TYPE:
        raise ValueError("unsupported delivery_policy type")
    if value.get("version") != DELIVERY_POLICY_VERSION:
        raise ValueError("unsupported delivery_policy version")
    if str(value.get("scope") or "") != DELIVERY_POLICY_SCOPE:
        raise ValueError("delivery_policy scope must be run")
    if _surface(value.get("connector_id"), fallback="") != expected_connector:
        raise ValueError("delivery_policy connector_id mismatch")
    if _client_id(value.get("client_id")) != expected_client_id:
        raise ValueError("delivery_policy client_id mismatch")
    targets = value.get("targets")
    if not isinstance(targets, (list, tuple)) or len(targets) > 16:
        raise ValueError("delivery_policy targets must be a list of at most 16 entries")
    normalized_targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for target in targets:
        if not isinstance(target, Mapping):
            raise ValueError("delivery_policy target must be an object")
        target_connector = _surface(target.get("connector_id"), fallback="")
        role = str(target.get("role") or "").strip().casefold()
        enabled = target.get("enabled")
        if target_connector == "unknown" or role not in {"mirror", "subscriber"}:
            raise ValueError("delivery_policy target is invalid")
        if not isinstance(enabled, bool):
            raise ValueError("delivery_policy target enabled must be boolean")
        identity = (target_connector, role)
        if identity in seen:
            raise ValueError("delivery_policy targets must be unique")
        seen.add(identity)
        normalized_targets.append(
            {
                "connector_id": target_connector,
                "role": role,
                "enabled": enabled,
            }
        )
    return frontend_run_delivery_policy(
        connector_id=expected_connector,
        client_id=expected_client_id,
        targets=normalized_targets,
    )


def tui_run_delivery_policy(*, telegram_mirror: bool, client_id: str) -> dict[str, Any]:
    """Compatibility builder; its wire representation is the generic v2 policy."""

    return frontend_run_delivery_policy(
        connector_id=TUI_FRONTEND_KIND,
        client_id=client_id,
        targets=[
            {
                "connector_id": "telegram",
                "role": "mirror",
                "enabled": bool(telegram_mirror),
            }
        ],
    )


def normalize_tui_run_delivery_policy(
    value: Any,
    *,
    client_id: str,
) -> dict[str, Any]:
    """Compatibility validator for the built-in TUI per-Run preference."""

    return normalize_frontend_run_delivery_policy(
        value,
        connector_id=TUI_FRONTEND_KIND,
        client_id=client_id,
    )


def tui_request_metadata(
    *,
    telegram_mirror: bool,
    client_id: str,
) -> dict[str, Any]:
    """Return the server-owned metadata snapshot for one TUI submission."""

    normalized_client_id = _client_id(client_id)
    return {
        FRONTEND_CLIENT_METADATA_KEY: {
            "kind": TUI_FRONTEND_KIND,
            "client_id": normalized_client_id,
        },
        FRONTEND_DELIVERY_METADATA_KEY: tui_run_delivery_policy(
            telegram_mirror=telegram_mirror,
            client_id=normalized_client_id,
        ),
    }


def _owner_telegram_mirror_for_admission(
    *,
    request_metadata: Mapping[str, Any] | None,
    state_root: Any | None,
) -> bool:
    """Resolve the server-authoritative owner mirror preference.

    The preference applies to non-Telegram frontend connectors for the owner.
    Read failures keep the historical visible default (mirror on).
    """

    if state_root is None:
        return True
    metadata = request_metadata if isinstance(request_metadata, Mapping) else {}
    owner_id = str(metadata.get("owner_id") or "").strip()
    if not owner_id:
        return True
    try:
        from orchestrator.connector_delivery_preferences import (
            get_connector_preference,
        )

        return get_connector_preference(
            state_root,
            owner_id,
            "telegram",
            "mirror",
            default=True,
        )
    except Exception:
        return True


def telegram_delivery_for_admission(
    *,
    source: str,
    request_metadata: Mapping[str, Any] | None,
    state_root: Any | None = None,
) -> bool:
    """Resolve the Telegram adapter target without honoring hidden-turn flags.

    Replies stay on Telegram for Telegram-origin Runs, and internal sources
    retain their explicit route. TUI may set a per-Run Telegram target; other
    authenticated frontend connectors use the server-owned owner preference.
    """

    metadata = request_metadata if isinstance(request_metadata, Mapping) else {}
    from orchestrator.frontend_connector_registry import canonical_connector_id

    connector_id = canonical_connector_id(
        str(source or ""),
        ingress_transport=str(metadata.get("ingress_transport") or ""),
        surface=str(metadata.get("session_surface") or ""),
    )
    if connector_id in {"telegram", "internal"}:
        return True
    frontend = metadata.get(FRONTEND_CLIENT_METADATA_KEY)
    if connector_id not in {TUI_FRONTEND_KIND, "session_api"}:
        return _owner_telegram_mirror_for_admission(
            request_metadata=request_metadata,
            state_root=state_root,
        )
    if not isinstance(frontend, Mapping):
        return _owner_telegram_mirror_for_admission(
            request_metadata=request_metadata,
            state_root=state_root,
        )
    frontend_kind = str(frontend.get("kind") or "").strip().casefold()
    if frontend_kind != connector_id:
        return _owner_telegram_mirror_for_admission(
            request_metadata=request_metadata,
            state_root=state_root,
        )
    try:
        client_id = _client_id(frontend.get("client_id"))
        policy = normalize_frontend_run_delivery_policy(
            metadata.get(FRONTEND_DELIVERY_METADATA_KEY),
            connector_id=connector_id,
            client_id=client_id,
        )
    except ValueError:
        return True
    mirror = next(
        (
            item["enabled"]
            for item in policy["targets"]
            if item["connector_id"] == "telegram" and item["role"] == "mirror"
        ),
        True,
    )
    return bool(mirror)


__all__ = [
    "DELIVERY_POLICY_TYPE",
    "DELIVERY_POLICY_VERSION",
    "FRONTEND_CLIENT_METADATA_KEY",
    "FRONTEND_DELIVERY_METADATA_KEY",
    "RUN_DELIVERY_ROUTE_METADATA_KEY",
    "RUN_DELIVERY_ROUTE_TYPE",
    "RUN_DELIVERY_ROUTE_VERSION",
    "freeze_run_delivery_route",
    "delivery_intent_from_run_route",
    "frontend_run_delivery_policy",
    "normalize_frontend_run_delivery_policy",
    "normalize_tui_run_delivery_policy",
    "normalize_run_delivery_route",
    "project_run_delivery_route",
    "route_destination",
    "telegram_delivery_for_admission",
    "tui_request_metadata",
    "tui_run_delivery_policy",
]
