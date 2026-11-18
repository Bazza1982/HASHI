"""Revisioned instance and Agent Telegram access configuration.

The primary Telegram account, when configured, uses the existing PAO owner scope.
Global IDs and Agent-local IDs are independent grants. This module owns their
configuration, but a caller must not advertise management until the receiving
Workers enforce the same policy.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.agent_config_json import write_agent_config_json
from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDocument,
    ConfigDurabilityError,
    file_write_lock,
    new_config_json,
    read_config_json,
)
from orchestrator.telegram_configuration_lock import telegram_configuration_lock
from orchestrator.telegram_identity import resolve_owner_scope_id, resolve_telegram_owner_id

MAX_TELEGRAM_USER_ID = (1 << 52) - 1
DEFAULT_TELEGRAM_USERS_PER_SCOPE = 10_000
CAPACITY_KEY = "telegram_users_per_scope_max"
GLOBAL_USERS_KEY = "authorized_telegram_user_ids"
POLICY_VERSION_KEY = "telegram_scoped_access_version"
POLICY_VERSION = 1
# Protocol 3 understands global grants without a primary Telegram account.
# A Worker with the old protocol treats that configuration as invalid.
SCOPED_ACCESS_PROTOCOL_VERSION = 3
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
    if not isinstance(value, list):
        raise ValueError("invalid Telegram access list")
    result: list[int] = []
    seen: set[int] = set()
    for item in value:
        item_id = normalize_telegram_user_id(item)
        if item_id not in seen:
            seen.add(item_id)
            result.append(item_id)
    return result


def _demote_removed_primary(config: Mapping[str, Any], actor: int) -> None:
    """Keep explicit Agent grants without retaining their legacy owner identity."""
    rows = config.get("agents")
    if not isinstance(rows, list):
        raise ValueError("invalid Agent configuration")
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid Agent configuration")
        nested = row.get("extra")
        if nested is not None and not isinstance(nested, dict):
            raise ValueError("invalid Agent configuration")
        extra = nested if nested else row
        legacy = _id_list(extra.get(LEGACY_AGENT_USERS_KEY), legacy_scalar=True)
        if actor not in legacy:
            continue
        scoped = _id_list(extra.get(AGENT_USERS_KEY))
        extra[LEGACY_AGENT_USERS_KEY] = [user for user in legacy if user != actor]
        if actor not in scoped:
            scoped.append(actor)
        extra[AGENT_USERS_KEY] = scoped
        extra[POLICY_VERSION_KEY] = POLICY_VERSION


@dataclass(frozen=True)
class TelegramAccessSnapshot:
    agent_id: str
    primary_user_id: int | None
    global_user_ids: tuple[int, ...]
    agent_user_ids: tuple[int, ...]
    owner_scope_id: str
    legacy_user_ids: tuple[int, ...]
    users_per_scope_max: int

    @property
    def effective_user_ids(self) -> frozenset[int]:
        return frozenset((*self.global_user_ids, *self.agent_user_ids))


def _access_snapshot(
    config: Mapping[str, Any], secrets_doc: Mapping[str, Any], agent_id: str,
    *, ingress_only: bool = False, ignore_capacity: bool = False,
    bound_data_owner: str | None = None,
) -> TelegramAccessSnapshot:
    """Read grants and, for management, their local resource binding."""
    global_config = config.get("global")
    agents = config.get("agents")
    requested = str(agent_id or "").strip().casefold()
    if not isinstance(global_config, Mapping) or not isinstance(agents, list):
        raise ValueError("invalid Telegram access configuration")
    capacity = global_config.get(CAPACITY_KEY, DEFAULT_TELEGRAM_USERS_PER_SCOPE)
    if ingress_only or ignore_capacity:
        # An administration budget is not an incoming-message credential.
        capacity = DEFAULT_TELEGRAM_USERS_PER_SCOPE
    elif type(capacity) is not int or not 1 <= capacity <= (1 << 53) - 1:
        raise ValueError("invalid Telegram user capacity")
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
    if POLICY_VERSION_KEY in extra and (
        type(extra[POLICY_VERSION_KEY]) is not int
        or extra[POLICY_VERSION_KEY] != POLICY_VERSION
    ):
        raise ValueError("unsupported Agent Telegram access policy")
    primary = resolve_telegram_owner_id(global_config, secrets_doc)
    if primary is not None:
        primary = normalize_telegram_user_id(primary)
    global_users = _id_list(global_config.get(GLOBAL_USERS_KEY))
    legacy_agent_users = _id_list(extra.get(LEGACY_AGENT_USERS_KEY), legacy_scalar=True)
    scoped_agent_users = _id_list(extra.get(AGENT_USERS_KEY))
    agent_users = list(dict.fromkeys((*legacy_agent_users, *scoped_agent_users)))
    if primary is not None:
        global_users = [primary, *(user for user in global_users if user != primary)]
    return TelegramAccessSnapshot(
        agent_id=canonical,
        primary_user_id=primary,
        global_user_ids=tuple(global_users),
        agent_user_ids=tuple(agent_users),
        owner_scope_id="" if ingress_only else (
            bound_data_owner or resolve_owner_scope_id(global_config, primary)
        ),
        legacy_user_ids=tuple(legacy_agent_users),
        users_per_scope_max=capacity,
    )


def access_snapshot(
    config: Mapping[str, Any], secrets_doc: Mapping[str, Any], agent_id: str,
    *, bound_data_owner: str | None = None,
) -> TelegramAccessSnapshot:
    """Read one authoritative management and local-binding snapshot."""
    return _access_snapshot(config, secrets_doc, agent_id, bound_data_owner=bound_data_owner)


def actor_snapshot(
    config: Mapping[str, Any], secrets_doc: Mapping[str, Any], agent_id: str,
    *, bound_data_owner: str | None = None,
) -> TelegramAccessSnapshot:
    """Resolve an actor and its local data binding without a management budget."""
    return _access_snapshot(
        config, secrets_doc, agent_id, ignore_capacity=True, bound_data_owner=bound_data_owner
    )


def ingress_user_ids(
    config: Mapping[str, Any], secrets_doc: Mapping[str, Any], agent_id: str
) -> frozenset[int]:
    """Read Telegram grants without making local owner or budget an ingress gate."""
    return _access_snapshot(config, secrets_doc, agent_id, ingress_only=True).effective_user_ids


def access_policy_revision(snapshot: TelegramAccessSnapshot) -> str:
    """Identify the effective policy seen by an individual Function Worker."""
    policy = {
        "agent_id": snapshot.agent_id.casefold(),
        "primary_user_id": snapshot.primary_user_id,
        "global_user_ids": sorted(snapshot.global_user_ids),
        "agent_user_ids": sorted(snapshot.agent_user_ids),
        "legacy_user_ids": sorted(snapshot.legacy_user_ids),
        "owner_scope_id": snapshot.owner_scope_id,
    }
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_access_paging(query: Any, *, allow_revision: bool = True) -> tuple[int, int, int, str | None] | None:
    """Parse optional bounded presentation pages without changing legacy responses."""
    fields = ("page_size", "global_page", "agent_page", "revision")
    values = {}
    for key in fields:
        getall = getattr(query, "getall", None)
        raw = list(getall(key, [])) if callable(getall) else (
            [query[key]] if key in query else []
        )
        if len(raw) > 1:
            raise TelegramAccessError("invalid_request", 400)
        if raw:
            values[key] = raw[0]
    if not values:
        return None
    if "page_size" not in values or (not allow_revision and "revision" in values):
        raise TelegramAccessError("invalid_request", 400)

    def number(key: str, default: str) -> int:
        value = values.get(key, default)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}", value):
            raise TelegramAccessError("invalid_request", 400)
        return int(value)

    size = number("page_size", "")
    global_page = number("global_page", "0")
    agent_page = number("agent_page", "0")
    revision = values.get("revision")
    if not 1 <= size <= 500 or (
        revision is not None and (
            not isinstance(revision, str) or not _REVISION.fullmatch(revision)
        )
    ):
        raise TelegramAccessError("invalid_request", 400)
    return size, global_page, agent_page, revision


def page_access_projection(
    snapshot: dict[str, Any], paging: tuple[int, int, int, str | None] | None,
) -> dict[str, Any]:
    """Page the response, retaining its authoritative revision and totals."""
    if paging is None:
        return snapshot
    size, global_page, agent_page, revision = paging
    if revision is not None and not hmac.compare_digest(snapshot["revision"], revision):
        raise TelegramAccessError("telegram_access_changed", 409)
    result = dict(snapshot)
    pages: dict[str, Any] = {"page_size": size}
    for scope, requested in (("global", global_page), ("agent", agent_page)):
        key = f"{scope}_user_ids"
        users = snapshot[key]
        total = len(users)
        page = min(requested, max(0, (total - 1) // size))
        result[key] = users[page * size : (page + 1) * size]
        pages[scope] = {"page": page, "total": total}
    result["pagination"] = pages
    return result


class TelegramAccessService:
    def __init__(
        self, *, config_path: str | Path, secrets_path: str | Path,
        state_root: str | Path | None = None,
    ):
        self.config_path = Path(config_path).expanduser().resolve()
        self.secrets_path = Path(secrets_path).expanduser().resolve()
        self.state_root = Path(state_root or self.config_path.parent).expanduser().resolve()
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
            from orchestrator.personal_data_owner import read_personal_data_owner_for_config

            global_config = config.get("global")
            if not isinstance(global_config, Mapping):
                raise ValueError("invalid Telegram access configuration")
            bound_owner = read_personal_data_owner_for_config(self.state_root, global_config)
            snapshot = access_snapshot(
                config, secrets_doc, agent_id, bound_data_owner=bound_owner
            )
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
            "users_per_scope_max": snapshot.users_per_scope_max,
            "policy_revision": access_policy_revision(snapshot),
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
                if action == "add":
                    if len(target) >= snapshot.users_per_scope_max:
                        raise TelegramAccessError("telegram_access_limit", 400)
                    target.append(actor)
                else:
                    target.remove(actor)

                global_config = config["global"]
                if scope == "global":
                    # Global changes activate the policy across all Agents.
                    global_config[POLICY_VERSION_KEY] = POLICY_VERSION
                    removing_primary = action == "remove" and actor == snapshot.primary_user_id
                    if (snapshot.primary_user_id is None or removing_primary) and not str(
                        global_config.get("owner_scope_id") or ""
                    ).strip():
                        # Only fill a missing rollback-era projection; preserve
                        # any pre-existing value even if it differs from the
                        # independently established local data binding.
                        global_config["owner_scope_id"] = snapshot.owner_scope_id
                    if removing_primary:
                        # The private legacy value is intentionally retained,
                        # but may no longer act as the current primary.
                        global_config["telegram_primary_disabled"] = True
                        global_config["telegram_owner_id"] = 0
                        global_config["authorized_id"] = 0
                        _demote_removed_primary(config, actor)
                    global_config[GLOBAL_USERS_KEY] = [
                        user for user in target if user != snapshot.primary_user_id
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
                    # A local grant activates only this Agent's policy.
                    extra[POLICY_VERSION_KEY] = POLICY_VERSION
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
                write_agent_config_json(self.config_path, config)
        except TelegramAccessError:
            raise
        except ConfigConflictError as exc:
            raise TelegramAccessError("telegram_access_changed", 409) from exc
        except ConfigDurabilityError as exc:
            raise TelegramAccessError("telegram_access_outcome_unknown", 503) from exc
        except (ValueError, TypeError) as exc:
            raise TelegramAccessError("telegram_access_unavailable", 503) from exc
        except (OSError, TimeoutError) as exc:
            raise TelegramAccessError("telegram_access_unavailable", 503) from exc
        return self.status(agent_id)
