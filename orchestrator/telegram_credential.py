"""Safe, owner-authenticated Telegram Bot credential replacement for one Agent.

The public contract never reads a token back.  A caller first observes an
opaque credential revision, then supplies that revision with a new token.
Telegram verifies the token before activation; explicit offline drafts remain private.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Awaitable, Callable

import aiohttp

from orchestrator.agent_delivery_policy import (
    normalize_telegram_mirror_policy,
    resolve_telegram_mirror_policy,
    telegram_mirror_policy_from_document,
    update_agent_telegram_mirror_policy,
)
from orchestrator.connector_delivery_preferences import get_connector_preference
from orchestrator.agent_config_json import write_agent_config_json
from orchestrator.agent_incarnation import (
    AGENT_LIFECYCLE_FIELD,
    ensure_agent_lifecycle_id,
    project_lifecycle_identity,
)
from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDocument,
    ConfigDurabilityError,
    file_write_lock,
    new_config_json,
    read_config_json,
    write_config_json,
)
from orchestrator.telegram_configuration_lock import (
    TELEGRAM_PENDING_SECRET_KEY as _PENDING_SECRET_KEY,
    TELEGRAM_PROTECTED_SECRET_KEYS as _PROTECTED_SECRET_KEYS,
    telegram_configuration_lock,
)
from orchestrator.telegram_access import ingress_user_ids
from orchestrator.telegram_identity import (
    resolve_owner_scope_id,
    resolve_telegram_owner_id,
)
from orchestrator.telegram_delivery_state import telegram_bot_fingerprint
from orchestrator.telegram_transport import bot_api_root


_TOKEN_PATTERN = re.compile(r"^[1-9][0-9]{4,19}:[A-Za-z0-9_-]{20,200}$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
class TelegramCredentialError(RuntimeError):
    """Stable public error for Telegram credential management."""

    def __init__(self, message: str, *, error_code: str, status_code: int):
        super().__init__(message)
        self.error_code = error_code
        self.status_code = status_code


@dataclass(frozen=True)
class TelegramCredentialContext:
    agent_id: str
    token_key: str
    agent_lifecycle_id: str | None
    telegram_api_root: str | None
    configured: bool
    authorized_user_configured: bool
    authorized_owner_id: int | None
    owner_scope_id: str
    shared_reference: bool
    credential_revision: str
    mirror_policy: str
    mirror_policy_revision: str
    config_revision: str
    secrets: ConfigDocument


async def verify_telegram_bot_token(token: str, *, api_root: str | None = None) -> dict:
    """Verify a Bot token without returning or logging the credential."""

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                f"{api_root or 'https://api.telegram.org'}/bot{token}/getMe",
                allow_redirects=False,
            ) as response:
                payload = await response.json()
                bot = payload.get("result") if isinstance(payload, dict) else None
                if (
                    response.status != 200
                    or not isinstance(payload, dict)
                    or payload.get("ok") is not True
                    or not isinstance(bot, dict)
                    or bot.get("is_bot") is not True
                ):
                    raise TelegramCredentialError(
                        "Telegram rejected this Bot token.",
                        error_code="telegram_token_rejected",
                        status_code=400,
                    )
    except TelegramCredentialError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        raise TelegramCredentialError(
            "Telegram could not verify this Bot token.",
            error_code="telegram_verification_unavailable",
            status_code=502,
        ) from None

    try:
        bot_id = int(bot.get("id"))
    except (TypeError, ValueError):
        raise TelegramCredentialError(
            "Telegram returned an invalid Bot identity.",
            error_code="telegram_token_rejected",
            status_code=400,
        ) from None
    public_bot = {"id": bot_id}
    username = str(bot.get("username") or "").strip()
    first_name = str(bot.get("first_name") or "").strip()
    if username:
        public_bot["username"] = username[:64]
    if first_name:
        public_bot["first_name"] = first_name[:128]
    return public_bot


def _pending_entries(document: ConfigDocument) -> dict:
    """Copy the private draft map; never silently overwrite an unknown format."""
    value = document.get(_PENDING_SECRET_KEY, {})
    if not isinstance(value, dict):
        raise TelegramCredentialError(
            "Telegram draft storage is invalid.",
            error_code="telegram_credential_config_invalid",
            status_code=409,
        )
    return dict(value)


def _opaque_revision(document: ConfigDocument, key: bytes, domain: str) -> str:
    source_revision = document.revision or "absent"
    return hmac.new(
        key,
        f"{domain}\0{source_revision}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _credential_revision(document: ConfigDocument, key: bytes) -> str:
    """Return a process-scoped opaque revision, never a raw secrets-file hash."""

    source_revision = document.revision or "absent"
    return hmac.new(
        key,
        source_revision.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _mirror_policy_revision(document: ConfigDocument, key: bytes) -> str:
    return _opaque_revision(document, key, "telegram-mirror-policy")


class TelegramCredentialService:
    """Manage one Agent's Telegram credential and outbound mirror policy."""

    def __init__(
        self,
        *,
        config_path: str | Path,
        secrets_path: str | Path,
        state_root: str | Path | None = None,
        verify_bot: Callable[[str], Awaitable[dict]] = verify_telegram_bot_token,
        private_full_control_sids: Iterable[str] = (),
        private_full_control_sids_resolver: Callable[[Mapping], Iterable[str]] | None = None,
    ):
        if isinstance(private_full_control_sids, (str, bytes)):
            raise TypeError(
                "private_full_control_sids must be an iterable of SIDs"
            )
        self.config_path = Path(config_path).expanduser().resolve()
        self.secrets_path = Path(secrets_path).expanduser().resolve()
        self.state_root = Path(state_root or self.config_path.parent).expanduser().resolve()
        self.verify_bot = verify_bot
        self.private_full_control_sids = tuple(private_full_control_sids)
        self.private_full_control_sids_resolver = private_full_control_sids_resolver
        self._revision_key = secrets.token_bytes(32)

    def _read_secrets(self) -> ConfigDocument:
        if self.secrets_path.is_file():
            return read_config_json(self.secrets_path)
        return new_config_json(self.secrets_path)

    def _context_from_documents(
        self,
        *,
        agent_id: str,
        config: ConfigDocument,
        secrets: ConfigDocument,
    ) -> TelegramCredentialContext:
        requested = str(agent_id or "").strip().casefold()
        if not requested or len(requested) > 160:
            raise TelegramCredentialError(
                "Agent ID is invalid.",
                error_code="invalid_agent_id",
                status_code=400,
            )
        rows = config.get("agents")
        if not isinstance(rows, list):
            raise TelegramCredentialError(
                "Agent configuration is invalid.",
                error_code="telegram_credential_config_invalid",
                status_code=409,
            )
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and str(row.get("name") or "").strip().casefold() == requested
        ]
        if len(matches) != 1:
            raise TelegramCredentialError(
                "Agent was not found." if not matches else "Agent configuration is ambiguous.",
                error_code=(
                    "agent_not_found"
                    if not matches
                    else "telegram_credential_config_invalid"
                ),
                status_code=404 if not matches else 409,
            )
        row = matches[0]
        canonical_id = str(row.get("name") or "").strip()
        token_key = str(row.get("telegram_token_key") or canonical_id).strip()
        if (
            not token_key
            or len(token_key) > 256
            or token_key in _PROTECTED_SECRET_KEYS
        ):
            raise TelegramCredentialError(
                "Agent Telegram credential reference is invalid.",
                error_code="telegram_credential_config_invalid",
                status_code=409,
            )
        references = [
            str(candidate.get("name") or "").strip()
            for candidate in rows
            if isinstance(candidate, dict)
            and str(
                candidate.get("telegram_token_key")
                or candidate.get("name")
                or ""
            ).strip()
            == token_key
        ]
        configured = isinstance(secrets.get(token_key), str) and bool(
            str(secrets.get(token_key)).strip()
        )
        global_config = config.get("global")
        if not isinstance(global_config, dict):
            raise TelegramCredentialError(
                "Agent configuration is invalid.",
                error_code="telegram_credential_config_invalid",
                status_code=409,
            )
        try:
            authorized_owner_id = resolve_telegram_owner_id(global_config, secrets)
            from orchestrator.personal_data_owner import read_personal_data_owner_for_config

            owner_scope_id = (
                read_personal_data_owner_for_config(self.state_root, global_config)
                or resolve_owner_scope_id(global_config, authorized_owner_id)
            )
            has_authorized_user = bool(ingress_user_ids(config, secrets, canonical_id))
            telegram_api_root = bot_api_root(SimpleNamespace(extra=row.get("extra")))
            projected = project_lifecycle_identity(canonical_id, row, row.get("extra"))
            agent_lifecycle_id = (projected or {}).get(AGENT_LIFECYCLE_FIELD)
        except (ValueError, LookupError, TypeError, AttributeError):
            raise TelegramCredentialError(
                "Telegram owner configuration is invalid.",
                error_code="telegram_credential_config_invalid",
                status_code=409,
            ) from None
        return TelegramCredentialContext(
            agent_id=canonical_id,
            token_key=token_key,
            agent_lifecycle_id=agent_lifecycle_id,
            telegram_api_root=telegram_api_root,
            configured=configured,
            authorized_user_configured=has_authorized_user,
            authorized_owner_id=authorized_owner_id,
            owner_scope_id=owner_scope_id,
            shared_reference=len(references) > 1,
            credential_revision=_credential_revision(secrets, self._revision_key),
            mirror_policy=telegram_mirror_policy_from_document(config, canonical_id),
            mirror_policy_revision=_mirror_policy_revision(config, self._revision_key),
            config_revision=str(config.revision or ""),
            secrets=secrets,
        )

    def context(self, agent_id: str) -> TelegramCredentialContext:
        return self._context_from_documents(
            agent_id=agent_id,
            config=read_config_json(self.config_path),
            secrets=self._read_secrets(),
        )

    def status(
        self, agent_id: str, *, runtime_active: bool = False,
        runtime_metadata: Mapping | None = None, include_activation_binding: bool = False,
    ) -> dict:
        context = self.context(agent_id)
        result = self._status_payload(
            context, runtime_active=runtime_active, runtime_metadata=runtime_metadata
        )
        if include_activation_binding:
            # Process-private identity of the active credential and its route.
            # Draft/unrelated secret writes do not change the intended adoption.
            active = context.secrets.get(context.token_key)
            if isinstance(active, str) and active.strip():
                values = (context.agent_id, context.token_key, context.telegram_api_root or "", active)
                parts = [value.encode("utf-8") for value in values]
                framed = b"".join(len(part).to_bytes(8, "big") + part for part in parts)
                result["_activation_binding"] = hmac.new(
                    self._revision_key, b"telegram-activation-binding\0" + framed, hashlib.sha256,
                ).hexdigest()
            else:
                result["_activation_binding"] = None
        return result

    def _status_payload(
        self,
        context: TelegramCredentialContext,
        *,
        runtime_active: bool,
        runtime_metadata: Mapping | None = None,
    ) -> dict:
        metadata = runtime_metadata or {}
        expected_bot = telegram_bot_fingerprint(context.secrets.get(context.token_key))
        adopted = bool(expected_bot and metadata.get("telegram_bot_fingerprint") == expected_bot)
        live = bool(runtime_active and metadata.get("online"))
        ingress = metadata.get("telegram_ingress") or {}
        receiver_adopted = bool(expected_bot and ingress.get("bot_fingerprint") == expected_bot)
        inbound = bool(live and adopted and receiver_adopted and ingress.get("running") and ingress.get("connected"))
        outbound = bool(live and adopted and metadata.get("telegram_outbound_connected"))
        ready = inbound and outbound and context.authorized_user_configured
        activation_status = (
            "unconfigured" if not context.configured else
            "user_required" if not context.authorized_user_configured else
            "inactive" if not runtime_active else
            "connected" if ready else
            "connecting" if adopted and receiver_adopted and live and ingress.get("running") else "pending"
        )
        instance_mirror_enabled = get_connector_preference(
            self.state_root,
            context.owner_scope_id,
            "telegram",
            "mirror",
            default=True,
        )
        resolved_mirror_enabled = resolve_telegram_mirror_policy(
            context.mirror_policy,
            instance_enabled=instance_mirror_enabled,
        )
        return {
            "ok": True,
            "schema_version": 1,
            "agent_id": context.agent_id,
            "configured": context.configured,
            "authorized_user_configured": context.authorized_user_configured,
            "manageable": not context.shared_reference,
            "shared_reference": context.shared_reference,
            "pending_verification": context.agent_id in _pending_entries(context.secrets),
            "credential_revision": context.credential_revision,
            "runtime_active": bool(runtime_active),
            "activation_required": (
                "agent_restart" if runtime_metadata is not None and activation_status == "pending"
                else None
            ),
            "activation_status": activation_status,
            "inbound_connected": inbound,
            "outbound_connected": outbound,
            "bidirectional_ready": ready,
            "mirror_policy": context.mirror_policy,
            "mirror_policy_revision": context.mirror_policy_revision,
            "instance_mirror_enabled": instance_mirror_enabled,
            "resolved_mirror_enabled": resolved_mirror_enabled,
            "mirror_delivery_ready": bool(
                context.configured and context.authorized_user_configured
            ),
        }

    def update_mirror_policy(
        self,
        agent_id: str,
        *,
        policy: str,
        expected_revision: str,
        runtime_active: bool = False,
        runtime_metadata: Mapping | None = None,
    ) -> dict:
        try:
            normalized_policy = normalize_telegram_mirror_policy(policy)
        except (TypeError, ValueError):
            raise TelegramCredentialError(
                "Telegram mirror policy is invalid.",
                error_code="invalid_request",
                status_code=400,
            ) from None
        if (
            not isinstance(expected_revision, str)
            or not _REVISION_PATTERN.fullmatch(expected_revision)
        ):
            raise TelegramCredentialError(
                "Telegram mirror policy revision is invalid.",
                error_code="invalid_request",
                status_code=400,
            )

        config = read_config_json(self.config_path)
        secrets_document = self._read_secrets()
        context = self._context_from_documents(
            agent_id=agent_id,
            config=config,
            secrets=secrets_document,
        )
        if expected_revision != context.mirror_policy_revision:
            raise TelegramCredentialError(
                "Telegram mirror policy changed. Reload and try again.",
                error_code="telegram_mirror_policy_changed",
                status_code=409,
            )

        durability_warning = None
        try:
            update_agent_telegram_mirror_policy(
                config,
                context.agent_id,
                normalized_policy,
            )
            try:
                write_agent_config_json(self.config_path, config)
            except ConfigDurabilityError:
                durability_warning = "mirror_policy_saved_durability_unconfirmed"
        except ConfigConflictError:
            raise TelegramCredentialError(
                "Telegram mirror policy changed. Reload and try again.",
                error_code="telegram_mirror_policy_changed",
                status_code=409,
            ) from None
        except TelegramCredentialError:
            raise
        except (OSError, TypeError, ValueError):
            raise TelegramCredentialError(
                "Telegram mirror policy could not be saved.",
                error_code="telegram_mirror_policy_save_failed",
                status_code=500,
            ) from None

        published_context = self._context_from_documents(
            agent_id=agent_id,
            config=config,
            secrets=secrets_document,
        )
        result = self._status_payload(
            published_context,
            runtime_active=runtime_active,
            runtime_metadata=runtime_metadata,
        )
        # Mirror policy is immediate; a disconnected receive path is a
        # separate connection fact, not a restart prerequisite for this save.
        result["activation_required"] = None
        result["mirror_policy_updated"] = True
        if durability_warning:
            result["warning_code"] = durability_warning
        return result

    async def replace(
        self,
        agent_id: str,
        *,
        token: str,
        expected_revision: str,
        replace_existing: bool,
        runtime_active: bool = False,
        save_unverified: bool = False,
        runtime_metadata: Mapping | None = None,
    ) -> dict:
        if not isinstance(save_unverified, bool):
            raise TelegramCredentialError(
                "save_unverified must be a boolean.", error_code="invalid_request", status_code=400
            )
        if not isinstance(token, str) or not _TOKEN_PATTERN.fullmatch(token):
            raise TelegramCredentialError(
                "Telegram Bot token is invalid.",
                error_code="telegram_token_invalid",
                status_code=400,
            )
        if (
            not isinstance(expected_revision, str)
            or not _REVISION_PATTERN.fullmatch(expected_revision)
        ):
            raise TelegramCredentialError(
                "Credential revision is invalid.",
                error_code="invalid_request",
                status_code=400,
            )
        if not isinstance(replace_existing, bool):
            raise TelegramCredentialError(
                "replace_existing must be a boolean.",
                error_code="invalid_request",
                status_code=400,
            )

        config = read_config_json(self.config_path)
        context = self._context_from_documents(
            agent_id=agent_id,
            config=config,
            secrets=self._read_secrets(),
        )
        if expected_revision != context.credential_revision:
            raise TelegramCredentialError(
                "Telegram credential status changed. Reload and try again.",
                error_code="credential_state_changed",
                status_code=409,
            )
        if context.shared_reference:
            raise TelegramCredentialError(
                "This Telegram credential is shared by multiple Agents.",
                error_code="telegram_credential_shared",
                status_code=409,
            )
        if context.configured and not replace_existing:
            raise TelegramCredentialError(
                "Confirm replacement of the existing Telegram credential.",
                error_code="telegram_replace_confirmation_required",
                status_code=409,
            )

        if save_unverified and not context.agent_lifecycle_id:
            try:
                await asyncio.to_thread(
                    ensure_agent_lifecycle_id, self.config_path, context.agent_id
                )
                config = read_config_json(self.config_path)
                refreshed = self._context_from_documents(
                    agent_id=agent_id, config=config, secrets=self._read_secrets(),
                )
                if (
                    not refreshed.agent_lifecycle_id
                    or refreshed.token_key != context.token_key
                    or refreshed.shared_reference
                    or refreshed.credential_revision != expected_revision
                    or (refreshed.configured and not replace_existing)
                ):
                    raise TelegramCredentialError(
                        "Telegram credential status changed. Reload and try again.",
                        error_code="credential_state_changed", status_code=409,
                    )
                context = refreshed
            except TelegramCredentialError:
                raise
            except ConfigConflictError:
                raise TelegramCredentialError(
                    "Agent configuration changed. Reload and try again.",
                    error_code="credential_state_changed", status_code=409,
                ) from None
            except (OSError, ValueError, RuntimeError, TypeError):
                raise TelegramCredentialError(
                    "Telegram draft could not be saved.",
                    error_code="telegram_credential_save_failed", status_code=500,
                ) from None

        if save_unverified:
            bot = None
        elif self.verify_bot is verify_telegram_bot_token:
            bot = await self.verify_bot(token, api_root=context.telegram_api_root)
        else:
            bot = await self.verify_bot(token)

        def publish():
            durability_warning = None
            # Freeze the Agent-to-secret mapping while the credential document
            # performs its own optimistic, serialized publication.
            with telegram_configuration_lock(self.secrets_path), file_write_lock(self.config_path):
                latest_config = read_config_json(self.config_path)
                if latest_config.revision != context.config_revision:
                    raise TelegramCredentialError(
                        "Agent configuration changed. Reload and try again.",
                        error_code="credential_state_changed",
                        status_code=409,
                    )
                latest_secrets = self._read_secrets()
                latest_context = self._context_from_documents(
                    agent_id=agent_id,
                    config=latest_config,
                    secrets=latest_secrets,
                )
                if (
                    latest_context.token_key != context.token_key
                    or latest_context.shared_reference
                    or latest_context.credential_revision
                    != context.credential_revision
                ):
                    raise TelegramCredentialError(
                        "Telegram credential status changed. Reload and try again.",
                        error_code="credential_state_changed",
                        status_code=409,
                    )
                additional_sids = ()
                if self.private_full_control_sids_resolver is not None:
                    additional_sids = self.private_full_control_sids_resolver(latest_secrets)
                    if isinstance(additional_sids, (str, bytes)):
                        raise TypeError("Remote principal resolver must return an iterable")
                private_sids = tuple(dict.fromkeys((*self.private_full_control_sids, *additional_sids)))
                pending = _pending_entries(latest_secrets)
                if save_unverified:
                    pending[context.agent_id] = {
                        "token_key": context.token_key, "token": token,
                        "agent_lifecycle_id": context.agent_lifecycle_id,
                    }
                    latest_secrets[_PENDING_SECRET_KEY] = pending
                else:
                    latest_secrets[context.token_key] = token
                    pending.pop(context.agent_id, None)
                    if pending:
                        latest_secrets[_PENDING_SECRET_KEY] = pending
                    else:
                        latest_secrets.pop(_PENDING_SECRET_KEY, None)
                try:
                    write_config_json(
                        self.secrets_path,
                        latest_secrets,
                        private_full_control_sids=private_sids,
                    )
                except ConfigDurabilityError:
                    durability_warning = "credential_saved_durability_unconfirmed"
            return latest_config, latest_secrets, durability_warning

        try:
            latest_config, latest_secrets, durability_warning = await asyncio.to_thread(publish)
        except TelegramCredentialError:
            raise
        except ConfigConflictError:
            raise TelegramCredentialError(
                "Telegram credential status changed. Reload and try again.",
                error_code="credential_state_changed",
                status_code=409,
            ) from None
        except (OSError, RuntimeError, TypeError):
            raise TelegramCredentialError(
                "Telegram credential could not be saved.",
                error_code="telegram_credential_save_failed",
                status_code=500,
            ) from None

        published_context = self._context_from_documents(
            agent_id=agent_id,
            config=latest_config,
            secrets=latest_secrets,
        )
        result = self._status_payload(
            published_context,
            runtime_active=runtime_active,
            runtime_metadata=runtime_metadata if save_unverified else None,
        )
        if save_unverified:
            # A draft never changes the active credential or requests adoption.
            result.update(replaced=False)
        else:
            result.update(
                activation_required=(
                    "agent_restart"
                    if runtime_active and published_context.authorized_user_configured else None
                ),
                replaced=context.configured,
                verified_bot=bot,
            )
        if durability_warning:
            result["warning_code"] = durability_warning
        return result
    async def verify_pending(
        self, agent_id: str, *, expected_revision: str, runtime_active: bool = False,
    ) -> dict:
        """Verify the exact persisted draft, then publish with normal race checks."""
        context = self.context(agent_id)
        if expected_revision != context.credential_revision:
            raise TelegramCredentialError(
                "Telegram credential status changed. Reload and try again.",
                error_code="credential_state_changed", status_code=409,
            )
        if context.shared_reference:
            raise TelegramCredentialError(
                "This Telegram credential is shared by multiple Agents.",
                error_code="telegram_credential_shared", status_code=409,
            )
        entry = _pending_entries(context.secrets).get(context.agent_id)
        if (not isinstance(entry, dict) or entry.get("token_key") != context.token_key
                or not context.agent_lifecycle_id
                or entry.get("agent_lifecycle_id") != context.agent_lifecycle_id
                or not isinstance(entry.get("token"), str)):
            raise TelegramCredentialError(
                "Telegram draft is unavailable or its Agent reference changed.",
                error_code="credential_state_changed", status_code=409,
            )
        return await self.replace(
            context.agent_id, token=entry["token"], expected_revision=expected_revision,
            replace_existing=context.configured, runtime_active=runtime_active,
        )
# HASHI-Fingerprint: hfp_beaf162634a04faea4a8bc16635b9a1a
