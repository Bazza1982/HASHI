"""Revision-checked replacement of one instance's Telegram owner actor.

The Telegram actor and PAO owner scope are deliberately separate. Replacing
the account keeps the same Sessions and preserves unrelated grants by default.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from orchestrator.agent_config_json import write_agent_config_json
from orchestrator.config_json import file_write_lock, new_config_json, read_config_json, write_config_json
from orchestrator.telegram_access import (
    AGENT_USERS_KEY, GLOBAL_USERS_KEY, LEGACY_AGENT_USERS_KEY,
    POLICY_VERSION, POLICY_VERSION_KEY, _id_list,
)
from orchestrator.telegram_configuration_lock import telegram_configuration_lock
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


def _optional_owner_id(value: Any) -> int | None:
    if isinstance(value, bool):
        raise TelegramOwnerMigrationError("Telegram owner must be a positive numeric user ID")
    if value in (None, "", 0, "0"):
        return None
    return _positive_owner_id(value)


def _owner_scope(
    global_config: dict[str, Any], current_owner_id: int | None, *, root: Path
) -> str:
    try:
        from orchestrator.personal_data_owner import read_personal_data_owner_for_config

        return (
            read_personal_data_owner_for_config(root, global_config)
            or resolve_owner_scope_id(global_config, current_owner_id)
        )
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
    candidate: int,
    now: datetime | None = None,
    max_age: timedelta = TELEGRAM_OWNER_PROOF_MAX_AGE,
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
                  AND actor_id=? AND channel_key=?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (
                    str(instance_id),
                    str(owner_scope_id),
                    str(agent_id).strip().casefold(),
                    str(candidate),
                    str(candidate),
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
    if age < timedelta(0) or age > max_age:
        raise TelegramOwnerMigrationError(
            f"Telegram owner migration proof must be fresh within {max_age.total_seconds() / 60:g} minutes"
        )
    return actor_id, str(row["updated_at"] or "")


def _stage_telegram_owner_replacement_locked(
    *,
    bridge_home: str | Path,
    new_owner_id: Any,
    expected_current_owner_id: Any,
    proof_agent_id: str = "hashiko",
    now: datetime | None = None,
    proof_mode: str = "administrator_selected",
    proof_max_age: timedelta = TELEGRAM_OWNER_PROOF_MAX_AGE,
    revoke_all_delegates: bool = False,
) -> dict[str, Any]:
    """Stage an account replacement in one revision-checked config write.

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
    expected_current = _optional_owner_id(expected_current_owner_id)
    config = read_config_json(config_path)
    global_config = config.get("global")
    agents = config.get("agents")
    if not isinstance(global_config, dict) or not isinstance(agents, list):
        raise TelegramOwnerMigrationError("Instance Agent configuration is invalid")
    try:
        secrets = read_config_json(secrets_path)
    except FileNotFoundError:
        # A new instance may have no private fallback yet. Its absent-file
        # revision still prevents finalization from overwriting a racing writer.
        secrets = new_config_json(secrets_path)
    try:
        resolved_current = resolve_telegram_owner_id(global_config, secrets)
    except ValueError as exc:
        raise TelegramOwnerMigrationError(
            "Configured Telegram owner is invalid"
        ) from exc
    current = _optional_owner_id(resolved_current)
    if current != expected_current:
        raise TelegramOwnerMigrationError(
            "Configured Telegram owner changed; reload before replacing it"
        )
    if candidate == current:
        raise TelegramOwnerMigrationError(
            "The requested Telegram owner is already configured"
        )
    owner_scope_id = _owner_scope(global_config, current, root=root)
    if proof_mode not in {"administrator_selected", "observed"}:
        raise TelegramOwnerMigrationError("Telegram account proof mode is invalid")
    observed_at = None
    if proof_mode == "observed":
        if not isinstance(proof_max_age, timedelta) or proof_max_age <= timedelta(0):
            raise TelegramOwnerMigrationError("Telegram account proof expiry is invalid")
        observed_actor, observed_at = _latest_direct_telegram_actor(
            sessions_path,
            instance_id=str(global_config.get("instance_id") or "HASHI"),
            owner_scope_id=owner_scope_id,
            agent_id=proof_agent_id,
            candidate=candidate,
            now=now,
            max_age=proof_max_age,
        )
        if observed_actor != candidate:
            raise TelegramOwnerMigrationError(
                "The requested owner does not match the latest runtime-observed Telegram account"
            )
    previous_fallback = _optional_owner_id(secrets.get("authorized_telegram_id"))

    if type(revoke_all_delegates) is not bool:
        raise TelegramOwnerMigrationError("Delegate revocation option must be boolean")
    try:
        global_users = _id_list(global_config.get(GLOBAL_USERS_KEY))
    except ValueError as exc:
        raise TelegramOwnerMigrationError("Global Telegram grants are invalid") from exc
    updated_agents = 0
    for row in agents:
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            raise TelegramOwnerMigrationError("Agent configuration is invalid")
        raw_extra = row.get("extra")
        if raw_extra is None or isinstance(raw_extra, dict):
            extra = raw_extra if raw_extra else row
        else:
            raise TelegramOwnerMigrationError(
                f"Agent '{row.get('name')}' has invalid extra configuration"
            )
        try:
            legacy = _id_list(extra.get(LEGACY_AGENT_USERS_KEY), legacy_scalar=True)
            scoped = _id_list(extra.get(AGENT_USERS_KEY))
        except ValueError as exc:
            raise TelegramOwnerMigrationError("Agent Telegram grants are invalid") from exc
        if LEGACY_AGENT_USERS_KEY in extra:
            extra[LEGACY_AGENT_USERS_KEY] = [] if revoke_all_delegates else [
                user for user in legacy if user != current
            ]
        if AGENT_USERS_KEY in extra:
            extra[AGENT_USERS_KEY] = [] if revoke_all_delegates else [
                user for user in scoped if user != current
            ]
        updated_agents += 1

    # Do not overwrite an existing rollback-era key. The independently
    # established local data binding remains authoritative for this runtime.
    if not str(global_config.get("owner_scope_id") or "").strip():
        global_config["owner_scope_id"] = owner_scope_id
    global_config[POLICY_VERSION_KEY] = POLICY_VERSION
    global_config["authorized_id"] = candidate
    global_config["telegram_owner_id"] = candidate
    global_config.pop("telegram_primary_disabled", None)
    if GLOBAL_USERS_KEY in global_config:
        global_config[GLOBAL_USERS_KEY] = [] if revoke_all_delegates else [
            user for user in global_users if user != current
        ]
    global_config["telegram_owner_transition"] = {
        "version": 1, "previous_owner_id": current, "new_owner_id": candidate,
        "previous_fallback_id": previous_fallback,
        "revoke_all_delegates": revoke_all_delegates,
    }

    revision = write_agent_config_json(config_path, config)
    return {
        "staged": True,
        "new_owner_id": candidate,
        "previous_owner_id": current,
        "owner_scope_id": owner_scope_id,
        "proof_mode": proof_mode,
        "proof_agent_id": str(proof_agent_id).strip().casefold() if proof_mode == "observed" else None,
        "proof_observed_at": observed_at,
        "agents_updated": updated_agents,
        "config_revision": revision,
        "secret_finalization_required": True,
    }


def stage_telegram_owner_replacement(
    *,
    bridge_home: str | Path,
    new_owner_id: Any,
    expected_current_owner_id: Any,
    proof_agent_id: str = "hashiko",
    now: datetime | None = None,
    proof_mode: str = "administrator_selected",
    proof_max_age: timedelta = TELEGRAM_OWNER_PROOF_MAX_AGE,
    revoke_all_delegates: bool = False,
) -> dict[str, Any]:
    """Stage an explicitly authorized local administrator's selected account.

    Callers must authenticate the administrator before invoking this helper.
    The optional observed mode verifies a recent candidate-specific channel.
    """
    root = Path(bridge_home).expanduser().resolve()
    with telegram_configuration_lock(root / "secrets.json"), file_write_lock(root / "secrets.json"):
        return _stage_telegram_owner_replacement_locked(
            bridge_home=root, new_owner_id=new_owner_id,
            expected_current_owner_id=expected_current_owner_id,
            proof_agent_id=proof_agent_id, now=now,
            proof_mode=proof_mode, proof_max_age=proof_max_age,
            revoke_all_delegates=revoke_all_delegates,
        )


def _finalize_telegram_owner_secret_locked(
    *,
    bridge_home: str | Path,
    owner_id: Any,
    expected_previous_owner_id: Any,
    expected_config_revision: str,
    private_full_control_sids: Iterable[str] = (),
) -> dict[str, Any]:
    """Replace the private legacy fallback after Function adoption succeeds."""

    if isinstance(private_full_control_sids, (str, bytes)):
        raise TypeError("private_full_control_sids must be an iterable of SIDs")
    root = Path(bridge_home).expanduser().resolve()
    config_path = root / "agents.json"
    secrets_path = root / "secrets.json"
    candidate = _positive_owner_id(owner_id)
    expected_previous = _optional_owner_id(expected_previous_owner_id)
    config = read_config_json(config_path)
    if not isinstance(expected_config_revision, str) or config.revision != expected_config_revision:
        raise TelegramOwnerMigrationError("Telegram configuration changed before finalization")
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
    _owner_scope(global_config, expected_previous, root=root)
    transition = global_config.get("telegram_owner_transition")
    if transition is None:
        # Support a staged transition created by the older sole-actor helper.
        if (expected_previous is None or global_config.get(GLOBAL_USERS_KEY, []) != [] or any(
            not isinstance(row, dict) or not isinstance(row.get("extra"), dict)
            or row["extra"].get(LEGACY_AGENT_USERS_KEY) != [candidate]
            for row in agents
        )):
            raise TelegramOwnerMigrationError(
                "The legacy staged Telegram transition no longer matches"
            )
    elif (not isinstance(transition, dict) or transition.get("version") != 1
          or transition.get("previous_owner_id") != expected_previous
          or transition.get("new_owner_id") != candidate):
        raise TelegramOwnerMigrationError("The staged Telegram transition no longer matches")
    if global_config.get("telegram_primary_disabled") is True:
        raise TelegramOwnerMigrationError("The staged Telegram primary has been removed")

    try:
        secrets = read_config_json(secrets_path)
    except FileNotFoundError:
        # A new instance may have no private fallback yet. Its absent-file
        # revision still prevents finalization from overwriting a racing writer.
        secrets = new_config_json(secrets_path)
    current_secret_owner = _optional_owner_id(secrets.get("authorized_telegram_id"))
    expected_fallback = _optional_owner_id(
        transition.get("previous_fallback_id", expected_previous)
        if isinstance(transition, dict) else expected_previous
    )
    if current_secret_owner not in {expected_fallback, candidate}:
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


def finalize_telegram_owner_secret(
    *,
    bridge_home: str | Path,
    owner_id: Any,
    expected_previous_owner_id: Any,
    expected_config_revision: str,
    private_full_control_sids: Iterable[str] = (),
) -> dict[str, Any]:
    root = Path(bridge_home).expanduser().resolve()
    # Serialize with Telegram policy writers, and hold the config stable until
    # the separately revision-checked private fallback has been published.
    with telegram_configuration_lock(root / "secrets.json"), file_write_lock(root / "agents.json"):
        return _finalize_telegram_owner_secret_locked(
            bridge_home=root, owner_id=owner_id,
            expected_previous_owner_id=expected_previous_owner_id,
            expected_config_revision=expected_config_revision,
            private_full_control_sids=private_full_control_sids,
        )


__all__ = [
    "TELEGRAM_OWNER_PROOF_MAX_AGE",
    "TelegramOwnerMigrationError",
    "finalize_telegram_owner_secret",
    "stage_telegram_owner_replacement",
]
# HASHI-Fingerprint: hfp_684b8d98a5394e5da7ecfe2ab3df6e0d
