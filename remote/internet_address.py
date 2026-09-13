"""Typed HChat addresses for local Remote and the independent Exchange.

The two namespaces are deliberately separate.  A dotted public address that
fails validation is never retried as a local/LAN instance address.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


USER_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$", re.ASCII)
INSTANCE_ALIAS_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.ASCII
)
AGENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]{0,63}$", re.ASCII)
LOCAL_INSTANCE_RE = re.compile(r"^[A-Za-z0-9_-]+$", re.ASCII)
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$", re.ASCII)


class AddressError(ValueError):
    """An address or immutable Exchange identity tuple is invalid."""


def _ascii(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or not value.isascii():
        raise AddressError(f"invalid {field}")
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        raise AddressError(f"invalid {field}")
    return value


def exchange_identifier(value: Any, *, field: str) -> str:
    text = _ascii(value, field=field)
    if IDENTIFIER_RE.fullmatch(text) is None:
        raise AddressError(f"invalid {field}")
    return text


def normalize_public_agent(value: Any) -> str:
    text = _ascii(value, field="agent_id").lower()
    if AGENT_RE.fullmatch(text) is None:
        raise AddressError("invalid agent_id")
    return text


@dataclass(frozen=True, slots=True)
class LocalAddress:
    agent: str
    instance_id: str | None = None

    @property
    def canonical(self) -> str:
        if self.instance_id:
            return f"{self.agent}@{self.instance_id}"
        return self.agent


@dataclass(frozen=True, slots=True)
class GroupAddress:
    name: str

    @property
    def canonical(self) -> str:
        return f"@{self.name}"


@dataclass(frozen=True, slots=True)
class PublicAddress:
    agent: str
    instance_alias: str
    username: str

    @classmethod
    def parse(cls, value: Any) -> "PublicAddress":
        text = _ascii(value, field="public address").lower()
        if text.count("@") != 1:
            raise AddressError("invalid public address")
        agent, owner = text.split("@", 1)
        if owner.count(".") != 1:
            raise AddressError("invalid public address")
        instance_alias, username = owner.split(".", 1)
        if (
            AGENT_RE.fullmatch(agent) is None
            or INSTANCE_ALIAS_RE.fullmatch(instance_alias) is None
            or USER_RE.fullmatch(username) is None
        ):
            raise AddressError("invalid public address")
        return cls(
            agent=agent,
            instance_alias=instance_alias,
            username=username,
        )

    @property
    def instance_address(self) -> str:
        return f"{self.instance_alias}.{self.username}"

    @property
    def canonical(self) -> str:
        return f"{self.agent}@{self.instance_address}"

    def __str__(self) -> str:
        return self.canonical


HChatAddress = LocalAddress | GroupAddress | PublicAddress


def parse_hchat_address(
    value: Any,
    *,
    allow_group: bool = True,
) -> HChatAddress:
    """Parse one HChat target without crossing namespace boundaries."""

    text = _ascii(value, field="HChat address").strip()
    if any(character.isspace() for character in text):
        raise AddressError("invalid HChat address")
    if text.startswith("@"):
        if not allow_group:
            raise AddressError("group address is not allowed")
        name = text[1:].lower()
        if AGENT_RE.fullmatch(name) is None:
            raise AddressError("invalid group address")
        return GroupAddress(name=name)
    if text.count("@") > 1:
        raise AddressError("invalid HChat address")
    if "@" not in text:
        agent = text.lower()
        if AGENT_RE.fullmatch(agent) is None and agent != "all":
            raise AddressError("invalid local agent address")
        return LocalAddress(agent=agent)

    agent, suffix = text.split("@", 1)
    # Presence of a dot commits parsing to the public namespace.  Failure is
    # terminal and must not be downgraded to a LAN instance lookup.
    if "." in suffix:
        return PublicAddress.parse(text)
    normalized_agent = agent.lower()
    if (
        AGENT_RE.fullmatch(normalized_agent) is None
        or LOCAL_INSTANCE_RE.fullmatch(suffix) is None
    ):
        raise AddressError("invalid local instance address")
    return LocalAddress(agent=normalized_agent, instance_id=suffix.upper())


@dataclass(frozen=True, slots=True)
class ExchangeAddress:
    authority_id: str
    actor_id: str
    registered_instance_id: str
    agent_id: str
    public_address: PublicAddress

    @classmethod
    def from_mapping(cls, value: Any) -> "ExchangeAddress":
        if not isinstance(value, Mapping):
            raise AddressError("Exchange address must be an object")
        expected = {
            "authority_id",
            "actor_id",
            "registered_instance_id",
            "agent_id",
            "address",
        }
        if set(value) != expected:
            raise AddressError("invalid Exchange address fields")
        public_address = PublicAddress.parse(value["address"])
        agent_id = normalize_public_agent(value["agent_id"])
        if public_address.agent != agent_id:
            raise AddressError("Exchange agent/address binding mismatch")
        return cls(
            authority_id=exchange_identifier(
                value["authority_id"], field="authority_id"
            ),
            actor_id=exchange_identifier(value["actor_id"], field="actor_id"),
            registered_instance_id=exchange_identifier(
                value["registered_instance_id"],
                field="registered_instance_id",
            ),
            agent_id=agent_id,
            public_address=public_address,
        )

    @property
    def address(self) -> str:
        return self.public_address.canonical

    def to_mapping(self) -> dict[str, str]:
        return {
            "authority_id": self.authority_id,
            "actor_id": self.actor_id,
            "registered_instance_id": self.registered_instance_id,
            "agent_id": self.agent_id,
            "address": self.address,
        }


__all__ = [
    "AGENT_RE",
    "AddressError",
    "ExchangeAddress",
    "GroupAddress",
    "HChatAddress",
    "IDENTIFIER_RE",
    "INSTANCE_ALIAS_RE",
    "LOCAL_INSTANCE_RE",
    "LocalAddress",
    "PublicAddress",
    "USER_RE",
    "exchange_identifier",
    "normalize_public_agent",
    "parse_hchat_address",
]
