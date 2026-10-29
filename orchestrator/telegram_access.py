"""Revisioned instance and Agent Telegram access configuration.

The primary Telegram owner keeps the existing PAO owner scope. Additional
global IDs and Agent-local IDs are independent grants. This module owns their
configuration, but a caller must not advertise management until the receiving
Workers enforce the same policy.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDocument,
    ConfigDurabilityError,
    file_write_lock,
    new_config_json,
    read_config_json,
    write_config_json,
)
from orchestrator.telegram_configuration_lock import telegram_configuration_lock
from orchestrator.telegram_identity import resolve_owner_scope_id, resolve_telegram_owner_id

MAX_TELEGRAM_USER_ID = (1 << 52) - 1
MAX_TELEGRAM_USERS_PER_SCOPE = 500
GLOBAL_USERS_KEY = "authorized_telegram_user_ids"
POLICY_VERSION_KEY = "telegram_scoped_access_version"
POLICY_VERSION = 1
LEGACY_AGENT_USERS_KEY = "authorized_telegram_ids"
AGENT_USERS_KEY = "scoped_telegram_user_ids"
_REVISION = re.compile(r"^[0-9a-f]{64}$")
_DIGITS = re.compile(r"^[0-9]{1,16}$")


class TelegramAccessError(RuntimeError):
    def __init__(self, error_code: str, status_code: int):
        super().__init__(error_code)
        self.error_code = error_code
        self.status_code = status_code


def normalize_telegram_user_id(value: Any, *, input_string: bool = False) -> int:
    """Normalize a Telegram numeric user ID without lossy number conversion."""
    if isinstance(value, bool) or (input_string and not isinstance(value, str)):
        raise ValueError("invalid Telegram user ID")
    if not isinstance(value, (int, str)):
        raise ValueError("invalid Telegram user ID")
    raw = str(value).strip()
    if not _DIGITS.fullmatch(raw):
        raise ValueError("invalid Telegram user ID")
    resolved = int(raw)
    if not 0 < resolved <= MAX_TELEGRAM_USER_ID:
        raise ValueError("invalid Telegram user ID")
    return resolved


def _id_list(value: Any, *, legacy_scalar: bool = False) -> list[int]:
    if value is None:
        return []
    if legacy_scalar and isinstance(value, (int, str)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list) or len(value) > MAX_TELEGRAM_USERS_PER_SCOPE:
        raise ValueError("invalid Telegram access list")
    result: list[int] = []
    for item in value:
        item_id = normalize_telegram_user_id(item)
        if item_id not in result:
            result.append(item_id)
    return result


@dataclass(frozen=True)
class TelegramAccessSnapshot:
    agent_id: str
    primary_user_id: int | None
    global_user_ids: tuple[int, ...]
    agent_user_ids: tuple[int, ...]
    owner_scope_id: str

    @property
    def effective_user_ids(self) -> frozenset[int]:
        return frozenset((*self.global_user_ids, *self.agent_user_ids))


def access_snapshot(
    config: Mapping[str, Any], secrets_doc: Mapping[str, Any], agent_id: str
) -> TelegramAccessSnapshot:
    """Read one authoritative snapshot; malformed configuration fails closed."""
    global_config = config.get("global")
    agents = config.get("agents")
    requested = str(agent_id or "").strip().casefold()
    if not isinstance(global_config, Mapping) or not isinstance(agents, list):
        raise ValueError("invalid Telegram access configuration")
    if POLICY_VERSION_KEY in global_config and (
        type(global_config[POLICY_VERSION_KEY]) is not int
        or global_config[POLICY_VERSION_KEY] != POLICY_VERSION
    ):
        raise ValueError("unsupported Telegram access policy")
    if not requested or len(requested) > 160:
        raise ValueError("invalid Agent ID")
    matches = [
        row for row in agents
        if isinstance(row, Mapping)
        and str(row.get("name") or "").strip().casefold() == requested
    ]
    if len(matches) != 1:
        raise LookupError("Agent is missing or ambiguous")
    row = matches[0]
    canonical = str(row.get("name") or "").strip()
    nested_extra = row.get("extra")
    if nested_extra is not None and not isinstance(nested_extra, Mapping):
        raise ValueError("invalid Agent configuration")
    # ConfigManager uses the flattened Agent fields when nested extra is empty.
    extra = nested_extra if nested_extra else row
    primary = resolve_telegram_owner_id(global_config, secrets_doc)
    if primary is not None:
        primary = normalize_telegram_user_id(primary)
    global_users = _id_list(global_config.get(GLOBAL_USERS_KEY))
    legacy_agent_users = _id_list(extra.get(LEGACY_AGENT_USERS_KEY), legacy_scalar=True)
    scoped_agent_users = _id_list(extra.get(AGENT_USERS_KEY))
    agent_users = list(dict.fromkeys((*legacy_agent_users, *scoped_agent_users)))
    if len(agent_users) > MAX_TELEGRAM_USERS_PER_SCOPE:
        raise ValueError("too many Agent Telegram users")
    if primary is not None:
        global_users = [primary, *(user for user in global_users if user != primary)]
    elif global_users:
        raise ValueError("global Telegram users require a primary user")
    if len(global_users) > MAX_TELEGRAM_USERS_PER_SCOPE:
        raise ValueError("too many global Telegram users")
    return TelegramAccessSnapshot(
        agent_id=canonical,
        primary_user_id=primary,
        global_user_ids=tuple(global_users),
        agent_user_ids=tuple(agent_users),
        owner_scope_id=resolve_owner_scope_id(global_config, primary),
    )


class TelegramAccessService:
    def __init__(self, *, config_path: str | Path, secrets_path: str | Path):
        self.config_path = Path(config_path).expanduser().resolve()
        self.secrets_path = Path(secrets_path).expanduser().resolve()
        self._revision_key = secrets.token_bytes(32)

    def _documents(self) -> tuple[ConfigDocument, ConfigDocument]:
        try:
            config = read_config_json(self.config_path)
            secrets_doc = (
                read_config_json(self.secrets_path)
                if self.secrets_path.is_file()
                else new_config_json(self.secrets_path)
            )
            return config, secrets_doc
        except (OSError, ValueError) as exc:
            raise TelegramAccessError("telegram_access_unavailable", 503) from exc

    def _context(
        self, agent_id: str
    ) -> tuple[ConfigDocument, ConfigDocument, TelegramAccessSnapshot, str]:
        config, secrets_doc = self._documents()
        try:
            snapshot = access_snapshot(config, secrets_doc, agent_id)
        except LookupError as exc:
            raise TelegramAccessError("agent_not_found", 404) from exc
        except (ValueError, TypeError) as exc:
            raise TelegramAccessError("telegram_access_unavailable", 503) from exc
        source = (
            f"{config.revision or 'absent'}\0{snapshot.primary_user_id or 0}"
            f"\0{snapshot.agent_id.casefold()}"
        )
        revision = hmac.new(
            self._revision_key, source.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return config, secrets_doc, snapshot, revision

    @staticmethod
    def _projection(snapshot: TelegramAccessSnapshot, revision: str) -> dict[str, Any]:
        return {
            "agent_id": snapshot.agent_id,
            "revision": revision,
            "primary_user_id": (
                str(snapshot.primary_user_id) if snapshot.primary_user_id is not None else None
            ),
            "global_user_ids": [str(user) for user in snapshot.global_user_ids],
            "agent_user_ids": [str(user) for user in snapshot.agent_user_ids],
        }

    def status(self, agent_id: str) -> dict[str, Any]:
        _, _, snapshot, revision = self._context(agent_id)
        return self._projection(snapshot, revision)

    def change(
        self, agent_id: str, *, expected_revision: str, scope: str, action: str, user_id: str
    ) -> dict[str, Any]:
        if (
            not isinstance(expected_revision, str)
            or not _REVISION.fullmatch(expected_revision)
            or not isinstance(scope, str) or scope not in {"global", "agent"}
            or not isinstance(action, str) or action not in {"add", "remove"}
        ):
            raise TelegramAccessError("invalid_request", 400)
        try:
            actor = normalize_telegram_user_id(user_id, input_string=True)
        except ValueError as exc:
            raise TelegramAccessError("invalid_request", 400) from exc

        # Legacy owners may live in secrets.json. Hold that document's lock
        # while comparing the observed owner and publishing the Agent config.
        # Existing config writers are revision checked; lock contention is a
        # bounded failure, never a reason to replay a committed mutation.
        try:
            with telegram_configuration_lock(self.secrets_path), file_write_lock(self.secrets_path):
                config, _, snapshot, current_revision = self._context(agent_id)
                if not hmac.compare_digest(current_revision, expected_revision):
                    raise TelegramAccessError("telegram_access_changed", 409)
                target = list(
                    snapshot.global_user_ids if scope == "global" else snapshot.agent_user_ids
                )
                if action == "add" and actor in target:
                    return self._projection(snapshot, current_revision)
                if action == "remove" and actor not in target:
                    raise TelegramAccessError("telegram_access_changed", 409)
                if scope == "global" and action == "remove" and actor == snapshot.primary_user_id:
                    raise TelegramAccessError("telegram_access_primary_required", 409)
                if action == "add":
                    if len(target) >= MAX_TELEGRAM_USERS_PER_SCOPE:
                        raise TelegramAccessError("telegram_access_limit", 400)
                    target.append(actor)
                else:
                    target.remove(actor)

                global_config = config["global"]
                # Persist activation with the first grant in the same atomic write.
                global_config[POLICY_VERSION_KEY] = POLICY_VERSION
                if scope == "global":
                    if snapshot.primary_user_id is None:
                        global_config["owner_scope_id"] = snapshot.owner_scope_id
                        global_config["telegram_owner_id"] = actor
                        global_config["authorized_id"] = actor
                        primary = actor
                    else:
                        primary = snapshot.primary_user_id
                    global_config[GLOBAL_USERS_KEY] = [
                        user for user in target if user != primary
                    ]
                else:
                    row = next(
                        row for row in config["agents"]
                        if isinstance(row, dict)
                        and str(row.get("name") or "").strip().casefold()
                        == snapshot.agent_id.casefold()
                    )
                    nested_extra = row.get("extra")
                    nested_mode = bool(nested_extra)
                    extra = dict(nested_extra) if nested_mode else row
                    legacy = _id_list(extra.get(LEGACY_AGENT_USERS_KEY), legacy_scalar=True)
                    if action == "remove" and actor in legacy:
                        legacy.remove(actor)
                        extra[LEGACY_AGENT_USERS_KEY] = legacy
                    # Keep new grants out of the legacy field: an older Worker
                    # would otherwise treat them as its primary owner. Preserve
                    # flattened settings instead of replacing them with extra.
                    extra[AGENT_USERS_KEY] = [user for user in target if user not in legacy]
                    if nested_mode:
                        row["extra"] = extra
                write_config_json(self.config_path, config)
        except TelegramAccessError:
            raise
        except ConfigConflictError as exc:
            raise TelegramAccessError("telegram_access_changed", 409) from exc
        except ConfigDurabilityError as exc:
            raise TelegramAccessError("telegram_access_outcome_unknown", 503) from exc
        except (OSError, TimeoutError) as exc:
            raise TelegramAccessError("telegram_access_unavailable", 503) from exc
        return self.status(agent_id)
