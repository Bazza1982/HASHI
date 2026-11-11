"""Trusted per-Agent Bot API endpoint configuration shared by both connectors."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def bot_api_root(config) -> str | None:
    value = str((getattr(config, "extra", None) or {}).get("telegram_api_url") or "").strip()
    if not value:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("telegram_api_url requires an HTTP(S) origin without credentials")
    if parsed.scheme == "http":
        try:
            local = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = parsed.hostname.casefold() == "localhost"
        if not local:
            raise ValueError("Unencrypted Telegram Bot API must use loopback")
    return value.rstrip("/")


def bot_api_kwargs(config) -> dict[str, str]:
    root = bot_api_root(config)
    return {} if root is None else {
        "base_url": root + "/bot",
        "base_file_url": root + "/file/bot",
    }
# HASHI-Fingerprint: hfp_5ee47e44a994454286b6ebac689b8cc9
