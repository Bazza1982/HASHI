from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from orchestrator.enterprise.store import EnterpriseStore


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class ChannelType(str, Enum):
    WORKBENCH = "workbench"
    HCHAT = "hchat"
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    SLACK = "slack"
    TEAMS = "teams"
    GOOGLE_CHAT = "google_chat"
    FEISHU = "feishu"

    @classmethod
    def from_value(cls, value: str) -> "ChannelType":
        normalized = (value or "").strip().lower()
        for item in cls:
            if item.value == normalized:
                return item
        raise ValueError(f"unsupported channel type: {value!r}")


class ChannelScopeType(str, Enum):
    USER = "user"
    TEAM = "team"
    PROJECT = "project"
    AGENT = "agent"

    @classmethod
    def from_value(cls, value: str) -> "ChannelScopeType":
        normalized = (value or "").strip().lower()
        for item in cls:
            if item.value == normalized:
                return item
        raise ValueError(f"unsupported channel scope type: {value!r}")


class ChannelPermission(str, Enum):
    INGRESS = "ingress"
    EGRESS = "egress"
    BOTH = "both"

    @classmethod
    def from_value(cls, value: str) -> "ChannelPermission":
        normalized = (value or "").strip().lower()
        for item in cls:
            if item.value == normalized:
                return item
        raise ValueError(f"unsupported channel permission: {value!r}")

    def allows(self, direction: str) -> bool:
        normalized = (direction or "").strip().lower()
        return self == ChannelPermission.BOTH or self.value == normalized


@dataclass(frozen=True)
class Channel:
    id: str
    org_id: str
    type: str
    display_name: str
    config: dict
    enabled: bool
    risk_tier: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChannelBinding:
    channel_id: str
    scope_type: str
    scope_id: str
    permission: str
    created_at: str


@dataclass(frozen=True)
class ChannelAccess:
    allowed: bool
    reason: str
    channel_id: str | None = None


DEFAULT_CHANNEL_DEFINITIONS = (
    {
        "channel_type": ChannelType.WORKBENCH,
        "display_name": "Workbench",
        "enabled": True,
        "risk_tier": "low",
    },
    {
        "channel_type": ChannelType.HCHAT,
        "display_name": "HChat",
        "enabled": False,
        "risk_tier": "medium",
    },
    {
        "channel_type": ChannelType.TELEGRAM,
        "display_name": "Telegram",
        "enabled": False,
        "risk_tier": "high",
    },
    {
        "channel_type": ChannelType.WHATSAPP,
        "display_name": "WhatsApp",
        "enabled": False,
        "risk_tier": "high",
    },
    {
        "channel_type": ChannelType.EMAIL,
        "display_name": "Email",
        "enabled": False,
        "risk_tier": "high",
    },
    {
        "channel_type": ChannelType.SLACK,
        "display_name": "Slack",
        "enabled": False,
        "risk_tier": "high",
    },
    {
        "channel_type": ChannelType.TEAMS,
        "display_name": "Microsoft Teams",
        "enabled": False,
        "risk_tier": "high",
    },
    {
        "channel_type": ChannelType.GOOGLE_CHAT,
        "display_name": "Google Chat",
        "enabled": False,
        "risk_tier": "high",
    },
    {
        "channel_type": ChannelType.FEISHU,
        "display_name": "Feishu",
        "enabled": False,
        "risk_tier": "high",
    },
)


class ChannelRegistry:
    def __init__(self, store: EnterpriseStore):
        self.store = store
        self.store.init_schema()

    @classmethod
    def from_path(cls, db_path: Path | str) -> "ChannelRegistry":
        return cls(EnterpriseStore(db_path))

    def register_channel(
        self,
        *,
        org_id: str,
        channel_type: ChannelType | str,
        display_name: str | None = None,
        config: dict | None = None,
        enabled: bool = False,
        risk_tier: str = "medium",
        channel_id: str | None = None,
    ) -> Channel:
        channel_type_value = _channel_type_value(channel_type)
        org_id = _require_id(org_id, "org_id")
        channel_id = _require_id(channel_id or f"{org_id}:{channel_type_value}", "channel_id")
        display_name = _require_text(display_name or channel_type_value.replace("_", " ").title(), "display_name")
        risk_tier = _require_text(risk_tier, "risk_tier").lower()
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT INTO channels(
                    id, org_id, type, display_name, config_json, enabled, risk_tier, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(org_id, type) DO UPDATE SET
                    display_name = excluded.display_name,
                    config_json = excluded.config_json,
                    enabled = excluded.enabled,
                    risk_tier = excluded.risk_tier,
                    updated_at = excluded.updated_at
                """,
                (
                    channel_id,
                    org_id,
                    channel_type_value,
                    display_name,
                    json.dumps(config or {}, sort_keys=True),
                    1 if enabled else 0,
                    risk_tier,
                    now,
                    now,
                ),
            )
        return self.get_channel(org_id=org_id, channel_type=channel_type_value)

    def get_channel(self, *, org_id: str, channel_type: ChannelType | str) -> Channel | None:
        channel_type_value = _channel_type_value(channel_type)
        with self.store.connect() as con:
            row = con.execute(
                "SELECT * FROM channels WHERE org_id = ? AND type = ?",
                (_require_id(org_id, "org_id"), channel_type_value),
            ).fetchone()
        return _channel_from_row(row) if row else None

    def list_channels(self, *, org_id: str) -> list[Channel]:
        with self.store.connect() as con:
            rows = con.execute(
                "SELECT * FROM channels WHERE org_id = ? ORDER BY type",
                (_require_id(org_id, "org_id"),),
            ).fetchall()
        return [_channel_from_row(row) for row in rows]

    def ensure_default_channels(self, *, org_id: str) -> list[Channel]:
        org_id = _require_id(org_id, "org_id")
        for definition in DEFAULT_CHANNEL_DEFINITIONS:
            channel_type = definition["channel_type"]
            if self.get_channel(org_id=org_id, channel_type=channel_type) is not None:
                continue
            self.register_channel(org_id=org_id, **definition)
        return self.list_channels(org_id=org_id)

    def set_enabled(self, *, org_id: str, channel_type: ChannelType | str, enabled: bool) -> Channel:
        channel = self.get_channel(org_id=org_id, channel_type=channel_type)
        if channel is None:
            raise ValueError(f"channel is not registered: {_channel_type_value(channel_type)!r}")
        with self.store.connect() as con:
            con.execute(
                "UPDATE channels SET enabled = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, _utc_now_iso(), channel.id),
            )
        return self.get_channel(org_id=org_id, channel_type=channel_type)

    def bind_channel(
        self,
        *,
        org_id: str,
        channel_type: ChannelType | str,
        scope_type: ChannelScopeType | str,
        scope_id: str,
        permission: ChannelPermission | str = ChannelPermission.BOTH,
    ) -> ChannelBinding:
        channel = self.get_channel(org_id=org_id, channel_type=channel_type)
        if channel is None:
            raise ValueError(f"channel is not registered: {_channel_type_value(channel_type)!r}")
        scope_type_value = _scope_type_value(scope_type)
        permission_value = _permission_value(permission)
        now = _utc_now_iso()
        with self.store.connect() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO channel_bindings(channel_id, scope_type, scope_id, permission, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    channel.id,
                    scope_type_value,
                    _require_id(scope_id, "scope_id"),
                    permission_value,
                    now,
                ),
            )
        return ChannelBinding(
            channel_id=channel.id,
            scope_type=scope_type_value,
            scope_id=scope_id,
            permission=permission_value,
            created_at=now,
        )

    def list_bindings(self, *, channel_id: str) -> list[ChannelBinding]:
        with self.store.connect() as con:
            rows = con.execute(
                """
                SELECT *
                FROM channel_bindings
                WHERE channel_id = ?
                ORDER BY scope_type, scope_id, permission
                """,
                (_require_id(channel_id, "channel_id"),),
            ).fetchall()
        return [_binding_from_row(row) for row in rows]

    def check_access(
        self,
        *,
        org_id: str,
        channel_type: ChannelType | str,
        direction: ChannelPermission | str,
        user_id: str | None = None,
        team_id: str | None = None,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> ChannelAccess:
        channel = self.get_channel(org_id=org_id, channel_type=channel_type)
        if channel is None:
            return ChannelAccess(allowed=False, reason="channel_not_registered")
        if not channel.enabled:
            return ChannelAccess(allowed=False, reason="channel_disabled", channel_id=channel.id)
        direction_permission = ChannelPermission.from_value(
            direction.value if isinstance(direction, ChannelPermission) else str(direction)
        )
        requested_scopes = {
            ChannelScopeType.USER.value: str(user_id or "").strip(),
            ChannelScopeType.TEAM.value: str(team_id or "").strip(),
            ChannelScopeType.PROJECT.value: str(project_id or "").strip(),
            ChannelScopeType.AGENT.value: str(agent_id or "").strip(),
        }
        for binding in self.list_bindings(channel_id=channel.id):
            if requested_scopes.get(binding.scope_type) != binding.scope_id:
                continue
            if ChannelPermission.from_value(binding.permission).allows(direction_permission.value):
                return ChannelAccess(allowed=True, reason="allowed", channel_id=channel.id)
        return ChannelAccess(allowed=False, reason="channel_not_bound", channel_id=channel.id)


def _channel_type_value(value: ChannelType | str) -> str:
    if isinstance(value, ChannelType):
        return value.value
    return ChannelType.from_value(str(value)).value


def _scope_type_value(value: ChannelScopeType | str) -> str:
    if isinstance(value, ChannelScopeType):
        return value.value
    return ChannelScopeType.from_value(str(value)).value


def _permission_value(value: ChannelPermission | str) -> str:
    if isinstance(value, ChannelPermission):
        return value.value
    return ChannelPermission.from_value(str(value)).value


def _require_id(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _require_text(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _channel_from_row(row) -> Channel:
    return Channel(
        id=row["id"],
        org_id=row["org_id"],
        type=row["type"],
        display_name=row["display_name"],
        config=json.loads(row["config_json"] or "{}"),
        enabled=bool(row["enabled"]),
        risk_tier=row["risk_tier"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _binding_from_row(row) -> ChannelBinding:
    return ChannelBinding(
        channel_id=row["channel_id"],
        scope_type=row["scope_type"],
        scope_id=row["scope_id"],
        permission=row["permission"],
        created_at=row["created_at"],
    )
