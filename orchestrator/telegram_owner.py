"""Revision-checked replacement of one instance's Telegram owner actor.

The Telegram actor and PAO owner scope are deliberately separate.  Replacing
the account must revoke every other Telegram actor without forking the same
human's Sessions, Workzones, or owner-scoped connector preferences.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.telegram_identity import (
    resolve_owner_scope_id,
    resolve_telegram_owner_id,
)


TELEGRAM_OWNER_PROOF_MAX_AGE = timedelta(minutes=10)


class TelegramOwnerMigrationError(RuntimeError):
    """The requested owner replacement failed a security or state precondition."""


def _positive_owner_id(value: Any) -> int:
    try:
        owner_id = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TelegramOwnerMigrationError(
            "Telegram owner must be a positive numeric user ID"
        ) from exc
    if owner_id <= 0:
        raise TelegramOwnerMigrationError(
            "Telegram owner must be a positive numeric user ID"
        )
    return owner_id


def _owner_scope(global_config: dict[str, Any], current_owner_id: int) -> str:
    try:
        return resolve_owner_scope_id(global_config, current_owner_id)
    except ValueError as exc:
        raise TelegramOwnerMigrationError(
            "Configured PAO owner scope is invalid"
        ) from exc


def _proof_timestamp(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        observed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise TelegramOwnerMigrationError(
            "The latest Telegram binding has no valid proof timestamp"
        ) from exc
    if observed.tzinfo is None:
        raise TelegramOwnerMigrationError(
            "The latest Telegram binding has no valid proof timestamp"
        )
    return observed.astimezone(timezone.utc)


def _latest_direct_telegram_actor(
    sessions_path: Path,
    *,
    instance_id: str,
    owner_scope_id: str,
    agent_id: str,
    now: datetime | None = None,
) -> tuple[int, str]:
    if not sessions_path.is_file():
        raise TelegramOwnerMigrationError("PAO Session database is unavailable")
    uri = f"{sessions_path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT channel_key, actor_id, actor_role, updated_at
                FROM channel_bindings
                WHERE instance_id=? AND owner_id=? AND agent_id=?
                  AND surface='telegram'
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (
                    str(instance_id),
                    str(owner_scope_id),
                    str(agent_id).strip().casefold(),
                ),
            ).fetchone()
    except sqlite3.Error as exc:
        raise TelegramOwnerMigrationError(
            "Could not verify the latest Telegram ingress binding"
        ) from exc
    if row is None:
        raise TelegramOwnerMigrationError(
            "No runtime-observed Telegram binding is available for proof"
        )
    try:
        chat_id = int(str(row["channel_key"]).strip())
        actor_id = int(str(row["actor_id"]).strip())
    except (TypeError, ValueError) as exc:
        raise TelegramOwnerMigrationError(
            "The latest Telegram binding has no authenticated numeric actor"
        ) from exc
    if chat_id != actor_id or actor_id <= 0:
        raise TelegramOwnerMigrationError(
            "The latest Telegram binding is not a direct private chat"
        )
    if str(row["actor_role"] or "") not in {"owner", "secondary"}:
        raise TelegramOwnerMigrationError(
            "The latest Telegram binding has no authenticated actor role"
        )
    observed_at = _proof_timestamp(row["updated_at"])
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise TelegramOwnerMigrationError("Proof validation time must include a timezone")
    age = checked_at.astimezone(timezone.utc) - observed_at
    if age < timedelta(0) or age > TELEGRAM_OWNER_PROOF_MAX_AGE:
        raise TelegramOwnerMigrationError(
            "Telegram owner migration proof must be fresh within 10 minutes"
        )
    return actor_id, str(row["updated_at"] or "")


def stage_telegram_owner_replacement(
    *,
    bridge_home: str | Path,
    new_owner_id: Any,
    expected_current_owner_id: Any,
    proof_agent_id: str = "hashiko",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Stage the sole actor in instance config after runtime-observed proof.

    This intentionally does not rewrite ``secrets.json``.  The explicit
    ``global.telegram_owner_id`` is authoritative for the new Function
    generation, while the old private fallback remains available if adoption
    rolls back.  ``finalize_telegram_owner_secret`` removes that fallback only
    after the new generation is live.
    """

    root = Path(bridge_home).expanduser().resolve()
    config_path = root / "agents.json"
    secrets_path = root / "secrets.json"
    sessions_path = root / "state" / "sessions.sqlite3"
    candidate = _positive_owner_id(new_owner_id)
    expected_current = _positive_owner_id(expected_current_owner_id)
    config = read_config_json(config_path)
    global_config = config.get("global")
    agents = config.get("agents")
    if not isinstance(global_config, dict) or not isinstance(agents, list):
        raise TelegramOwnerMigrationError("Instance Agent configuration is invalid")
    secrets = read_config_json(secrets_path)
    try:
        resolved_current = resolve_telegram_owner_id(global_config, secrets)
    except ValueError as exc:
        raise TelegramOwnerMigrationError(
            "Configured Telegram owner is invalid"
        ) from exc
    current = _positive_owner_id(resolved_current)
    if current != expected_current:
        raise TelegramOwnerMigrationError(
            "Configured Telegram owner changed; reload before replacing it"
        )
    if candidate == current:
        raise TelegramOwnerMigrationError(
            "The requested Telegram owner is already configured"
        )
    owner_scope_id = _owner_scope(global_config, current)
    observed_actor, observed_at = _latest_direct_telegram_actor(
        sessions_path,
        instance_id=str(global_config.get("instance_id") or "HASHI"),
        owner_scope_id=owner_scope_id,
        agent_id=proof_agent_id,
        now=now,
    )
    if observed_actor != candidate:
        raise TelegramOwnerMigrationError(
            "The requested owner does not match the latest runtime-observed Telegram account"
        )

    updated_agents = 0
    for row in agents:
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            raise TelegramOwnerMigrationError("Agent configuration is invalid")
        raw_extra = row.get("extra")
        if raw_extra is None:
            extra: dict[str, Any] = {}
        elif isinstance(raw_extra, dict):
            extra = dict(raw_extra)
        else:
            raise TelegramOwnerMigrationError(
                f"Agent '{row.get('name')}' has invalid extra configuration"
            )
        extra["authorized_telegram_ids"] = [candidate]
        row["extra"] = extra
        updated_agents += 1

    global_config["owner_scope_id"] = owner_scope_id
    global_config["authorized_id"] = candidate
    global_config["telegram_owner_id"] = candidate

    revision = write_config_json(config_path, config)
    return {
        "staged": True,
        "new_owner_id": candidate,
        "previous_owner_id": current,
        "owner_scope_id": owner_scope_id,
        "proof_agent_id": str(proof_agent_id).strip().casefold(),
        "proof_observed_at": observed_at,
        "agents_updated": updated_agents,
        "config_revision": revision,
        "secret_finalization_required": True,
    }


def finalize_telegram_owner_secret(
    *,
    bridge_home: str | Path,
    owner_id: Any,
    expected_previous_owner_id: Any,
    private_full_control_sids: Iterable[str] = (),
) -> dict[str, Any]:
    """Replace the private legacy fallback after Function adoption succeeds."""

    if isinstance(private_full_control_sids, (str, bytes)):
        raise TypeError("private_full_control_sids must be an iterable of SIDs")
    root = Path(bridge_home).expanduser().resolve()
    config_path = root / "agents.json"
    secrets_path = root / "secrets.json"
    candidate = _positive_owner_id(owner_id)
    expected_previous = _positive_owner_id(expected_previous_owner_id)
    config = read_config_json(config_path)
    global_config = config.get("global")
    agents = config.get("agents")
    if not isinstance(global_config, dict) or not isinstance(agents, list):
        raise TelegramOwnerMigrationError("Instance Agent configuration is invalid")
    if _positive_owner_id(global_config.get("telegram_owner_id")) != candidate:
        raise TelegramOwnerMigrationError(
            "The staged Telegram owner does not match this finalization"
        )
    if _positive_owner_id(global_config.get("authorized_id")) != candidate:
        raise TelegramOwnerMigrationError(
            "The compatibility owner projection is not staged"
        )
    _owner_scope(global_config, expected_previous)
    for row in agents:
        extra = row.get("extra") if isinstance(row, dict) else None
        if not isinstance(extra, dict) or extra.get("authorized_telegram_ids") != [candidate]:
            raise TelegramOwnerMigrationError(
                "Every Agent must authorize only the staged Telegram owner"
            )

    secrets = read_config_json(secrets_path)
    current_secret_owner = secrets.get("authorized_telegram_id")
    if current_secret_owner not in (None, 0, "0"):
        current_secret_owner = _positive_owner_id(current_secret_owner)
        if current_secret_owner not in {expected_previous, candidate}:
            raise TelegramOwnerMigrationError(
                "Private Telegram owner fallback changed; refusing to overwrite it"
            )
    secrets["authorized_telegram_id"] = candidate
    revision = write_config_json(
        secrets_path,
        secrets,
        private_full_control_sids=tuple(private_full_control_sids),
    )
    return {
        "finalized": True,
        "owner_id": candidate,
        "secrets_revision": revision,
    }


__all__ = [
    "TELEGRAM_OWNER_PROOF_MAX_AGE",
    "TelegramOwnerMigrationError",
    "finalize_telegram_owner_secret",
    "stage_telegram_owner_replacement",
]
