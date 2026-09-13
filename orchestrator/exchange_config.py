"""Instance-owned configuration for the optional independent Exchange client."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from orchestrator.config_json import (
    ConfigConflictError,
    read_config_json,
    write_config_json,
)
from remote.internet_address import (
    AddressError,
    INSTANCE_ALIAS_RE,
    exchange_identifier,
    normalize_public_agent,
)


EXCHANGE_CONFIG_KEY = "exchange"
EXCHANGE_SECRET_MIN_BYTES = 16


class ExchangeConfigError(ValueError):
    """Exchange configuration is unsafe or incomplete."""


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    enabled: bool = False
    authority_id: str | None = None
    url: str | None = None
    registered_instance_id: str | None = None
    instance_alias: str | None = None
    credential_ref: str | None = None
    published_agents: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["published_agents"] = list(self.published_agents)
        return value

    def connection_fingerprint(self) -> str:
        safe = {
            "enabled": self.enabled,
            "authority_id": self.authority_id,
            "url": self.url,
            "registered_instance_id": self.registered_instance_id,
            "instance_alias": self.instance_alias,
            "credential_ref": self.credential_ref,
        }
        return hashlib.sha256(
            json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


def _loopback_host(hostname: str | None) -> bool:
    host = str(hostname or "").strip().strip("[]").casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _endpoint(value: Any) -> str:
    endpoint = str(value or "").strip()
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"wss", "ws"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1/connect"
    ):
        raise ExchangeConfigError(
            "exchange.url must be a WSS /v1/connect endpoint"
        )
    if parsed.scheme == "ws" and not _loopback_host(parsed.hostname):
        raise ExchangeConfigError(
            "plain WS is permitted only for a loopback local lab"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise ExchangeConfigError("exchange.url has an invalid port") from exc
    return endpoint


def parse_exchange_config(value: Any) -> ExchangeConfig:
    if value in (None, {}):
        return ExchangeConfig()
    if not isinstance(value, Mapping):
        raise ExchangeConfigError("exchange configuration must be an object")
    allowed = {
        "enabled",
        "authority_id",
        "url",
        "registered_instance_id",
        "instance_alias",
        "credential_ref",
        "published_agents",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ExchangeConfigError(
            "unknown exchange configuration fields: " + ", ".join(sorted(unknown))
        )
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ExchangeConfigError("exchange.enabled must be boolean")
    published = value.get("published_agents", [])
    if not isinstance(published, list):
        raise ExchangeConfigError("exchange.published_agents must be a list")
    try:
        published_agents = tuple(
            dict.fromkeys(normalize_public_agent(item) for item in published)
        )
    except AddressError as exc:
        raise ExchangeConfigError(str(exc)) from exc
    if len(published_agents) > 100:
        raise ExchangeConfigError("exchange.published_agents exceeds 100")
    if not enabled:
        return ExchangeConfig(enabled=False, published_agents=published_agents)
    try:
        authority_id = exchange_identifier(
            value.get("authority_id"), field="authority_id"
        )
        registered_instance_id = exchange_identifier(
            value.get("registered_instance_id"),
            field="registered_instance_id",
        )
        credential_ref = exchange_identifier(
            value.get("credential_ref"), field="credential_ref"
        )
    except AddressError as exc:
        raise ExchangeConfigError(str(exc)) from exc
    instance_alias = str(value.get("instance_alias") or "").strip().lower()
    if (
        not instance_alias.isascii()
        or INSTANCE_ALIAS_RE.fullmatch(instance_alias) is None
    ):
        raise ExchangeConfigError("invalid instance_alias")
    return ExchangeConfig(
        enabled=True,
        authority_id=authority_id,
        url=_endpoint(value.get("url")),
        registered_instance_id=registered_instance_id,
        instance_alias=instance_alias,
        credential_ref=credential_ref,
        published_agents=published_agents,
    )


def load_exchange_config(hashi_root: Path | str) -> ExchangeConfig:
    root = Path(hashi_root)
    document = read_config_json(root / "agents.json")
    return parse_exchange_config(document.get(EXCHANGE_CONFIG_KEY))


def load_exchange_credential(
    hashi_root: Path | str,
    config: ExchangeConfig,
) -> str:
    if not config.enabled or not config.credential_ref:
        raise ExchangeConfigError("Exchange is disabled")
    path = Path(hashi_root) / "secrets.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExchangeConfigError("Exchange credential store is unavailable") from exc
    if not isinstance(value, Mapping):
        raise ExchangeConfigError("Exchange credential store must be an object")
    secret = value.get(config.credential_ref)
    if (
        not isinstance(secret, str)
        or len(secret.encode("utf-8")) < EXCHANGE_SECRET_MIN_BYTES
        or not secret.isascii()
        or any(ord(character) < 33 or ord(character) > 126 for character in secret)
    ):
        raise ExchangeConfigError(
            "Exchange credential_ref is missing or not bearer-safe"
        )
    return secret


def published_agent_records(
    hashi_root: Path | str,
    config: ExchangeConfig,
) -> list[dict[str, Any]]:
    """Return only explicitly selected, locally active agents."""

    root = Path(hashi_root)
    try:
        document = read_config_json(root / "agents.json")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return []
    selected = set(config.published_agents)
    records: list[dict[str, Any]] = []
    for raw in document.get("agents", []):
        if not isinstance(raw, Mapping):
            continue
        try:
            agent = normalize_public_agent(raw.get("name"))
        except AddressError:
            continue
        if agent not in selected or raw.get("is_active", True) is False:
            continue
        display_name = str(raw.get("display_name") or agent).strip()
        if (
            not display_name
            or len(display_name) > 128
            or any(0xD800 <= ord(character) <= 0xDFFF for character in display_name)
        ):
            display_name = agent
        records.append(
            {
                "agent_id": agent,
                "display_name": display_name,
                "message_kinds": ["agent_message", "agent_reply"],
            }
        )
    return records


def update_exchange_config(
    config_path: Path | str,
    replacement: Mapping[str, Any],
    *,
    expected_revision: str,
) -> str:
    """CAS-update only the Exchange section of agents.json."""

    parsed = parse_exchange_config(replacement)
    document = read_config_json(config_path)
    if document.revision != expected_revision:
        raise ConfigConflictError(
            "configuration changed since it was read; reload before editing"
        )
    document[EXCHANGE_CONFIG_KEY] = parsed.public_dict()
    return write_config_json(
        config_path,
        document,
        expected_revision=expected_revision,
    )


__all__ = [
    "EXCHANGE_CONFIG_KEY",
    "ExchangeConfig",
    "ExchangeConfigError",
    "load_exchange_config",
    "load_exchange_credential",
    "parse_exchange_config",
    "published_agent_records",
    "update_exchange_config",
]
