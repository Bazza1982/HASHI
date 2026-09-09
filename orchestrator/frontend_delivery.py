"""Typed per-Run delivery policy for HASHI Frontend Connectors.

The legacy ``deliver_to_telegram`` boolean is deliberately not a public
suppression switch.  A TUI may opt one newly submitted Run out of Telegram
mirroring only by supplying this complete, typed policy together with its
ephemeral client identity.  The policy is copied into request metadata at
admission, so later preference changes cannot alter an in-flight Run.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DELIVERY_POLICY_TYPE = "hashi.frontend-delivery"
DELIVERY_POLICY_VERSION = 1
DELIVERY_POLICY_SCOPE = "run"
TUI_FRONTEND_KIND = "tui"
FRONTEND_CLIENT_METADATA_KEY = "frontend_client"
FRONTEND_DELIVERY_METADATA_KEY = "frontend_delivery_policy"


def _client_id(value: Any) -> str:
    client_id = str(value or "").strip()
    if (
        not client_id
        or len(client_id) > 128
        or any(ord(character) < 33 or ord(character) > 126 for character in client_id)
    ):
        raise ValueError("TUI delivery policy requires a valid client_id")
    return client_id


def tui_run_delivery_policy(*, telegram_mirror: bool, client_id: str) -> dict[str, Any]:
    """Build the canonical wire representation for one TUI-origin Run."""

    return {
        "type": DELIVERY_POLICY_TYPE,
        "version": DELIVERY_POLICY_VERSION,
        "scope": DELIVERY_POLICY_SCOPE,
        "frontend": TUI_FRONTEND_KIND,
        "client_id": _client_id(client_id),
        "telegram": {"mirror": bool(telegram_mirror)},
    }


def normalize_tui_run_delivery_policy(
    value: Any,
    *,
    client_id: str,
) -> dict[str, Any]:
    """Validate a TUI policy and return its canonical, JSON-safe form."""

    if not isinstance(value, Mapping):
        raise ValueError("delivery_policy must be an object")
    expected_client_id = _client_id(client_id)
    if str(value.get("type") or "") != DELIVERY_POLICY_TYPE:
        raise ValueError("unsupported delivery_policy type")
    if value.get("version") != DELIVERY_POLICY_VERSION:
        raise ValueError("unsupported delivery_policy version")
    if str(value.get("scope") or "") != DELIVERY_POLICY_SCOPE:
        raise ValueError("delivery_policy scope must be run")
    if str(value.get("frontend") or "") != TUI_FRONTEND_KIND:
        raise ValueError("delivery_policy frontend must be tui")
    if _client_id(value.get("client_id")) != expected_client_id:
        raise ValueError("delivery_policy client_id mismatch")
    telegram = value.get("telegram")
    if not isinstance(telegram, Mapping) or not isinstance(
        telegram.get("mirror"), bool
    ):
        raise ValueError("delivery_policy telegram.mirror must be boolean")
    return tui_run_delivery_policy(
        telegram_mirror=bool(telegram["mirror"]),
        client_id=expected_client_id,
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


def telegram_delivery_for_admission(
    *,
    source: str,
    request_metadata: Mapping[str, Any] | None,
) -> bool:
    """Resolve Telegram delivery without honoring legacy hidden-turn flags.

    Invalid, incomplete, non-TUI, and forged metadata all fail visible.  Only a
    canonical policy bound to the same TUI client can turn mirroring off.
    """

    if str(source or "").strip().casefold() != TUI_FRONTEND_KIND:
        return True
    metadata = request_metadata if isinstance(request_metadata, Mapping) else {}
    frontend = metadata.get(FRONTEND_CLIENT_METADATA_KEY)
    if not isinstance(frontend, Mapping):
        return True
    if str(frontend.get("kind") or "").strip().casefold() != TUI_FRONTEND_KIND:
        return True
    try:
        client_id = _client_id(frontend.get("client_id"))
        policy = normalize_tui_run_delivery_policy(
            metadata.get(FRONTEND_DELIVERY_METADATA_KEY),
            client_id=client_id,
        )
    except ValueError:
        return True
    return bool(policy["telegram"]["mirror"])


__all__ = [
    "DELIVERY_POLICY_TYPE",
    "DELIVERY_POLICY_VERSION",
    "FRONTEND_CLIENT_METADATA_KEY",
    "FRONTEND_DELIVERY_METADATA_KEY",
    "normalize_tui_run_delivery_policy",
    "telegram_delivery_for_admission",
    "tui_request_metadata",
    "tui_run_delivery_policy",
]
