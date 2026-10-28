"""Safe, owner-authenticated Telegram Bot credential replacement for one Agent.

The public contract never reads a token back.  A caller first observes an
opaque credential revision, then supplies that revision with a new token.
Telegram verifies the token before an optimistic, private-file publication.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from orchestrator.agent_delivery_policy import (
    normalize_telegram_mirror_policy,
    resolve_telegram_mirror_policy,
    telegram_mirror_policy_from_document,
    update_agent_telegram_mirror_policy,
)
from orchestrator.connector_delivery_preferences import get_connector_preference
from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDocument,
    ConfigDurabilityError,
    file_write_lock,
    new_config_json,
    read_config_json,
    write_config_json,
)
from orchestrator.telegram_identity import (
    resolve_owner_scope_id,
    resolve_telegram_owner_id,
)


_TOKEN_PATTERN = re.compile(r"^[1-9][0-9]{4,19}:[A-Za-z0-9_-]{20,200}$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROTECTED_SECRET_KEYS = frozenset(
    {
        "authorized_telegram_id",
        "workbench_admin_token",
        "hashi_remote_shared_token",
    }
)


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


async def verify_telegram_bot_token(token: str) -> dict:
    """Verify a Bot token without returning or logging the credential."""

    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
            async with session.get(
                f"https://api.telegram.org/bot{token}/getMe",
                allow_redirects=False,
            ) as response:
                payload = await response.json()
                bot = payload.get("result") if isinstance(payload, dict) else None
                if (
                    response.status != 200
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
            owner_scope_id = resolve_owner_scope_id(
                global_config,
                authorized_owner_id,
            )
        except ValueError:
            raise TelegramCredentialError(
                "Telegram owner configuration is invalid.",
                error_code="telegram_credential_config_invalid",
                status_code=409,
            ) from None
        return TelegramCredentialContext(
            agent_id=canonical_id,
            token_key=token_key,
            configured=configured,
            authorized_user_configured=authorized_owner_id is not None,
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

    def status(self, agent_id: str, *, runtime_active: bool = False) -> dict:
        context = self.context(agent_id)
        return self._status_payload(context, runtime_active=runtime_active)

    def _status_payload(
        self,
        context: TelegramCredentialContext,
        *,
        runtime_active: bool,
    ) -> dict:
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
            "credential_revision": context.credential_revision,
            "runtime_active": bool(runtime_active),
            "activation_required": None,
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
                write_config_json(self.config_path, config)
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
        )
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
    ) -> dict:
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
        if not context.authorized_user_configured:
            raise TelegramCredentialError(
                "Configure the authorized Telegram user locally first.",
                error_code="telegram_owner_not_configured",
                status_code=409,
            )
        if context.configured and not replace_existing:
            raise TelegramCredentialError(
                "Confirm replacement of the existing Telegram credential.",
                error_code="telegram_replace_confirmation_required",
                status_code=409,
            )

        bot = await self.verify_bot(token)
        durability_warning = None
        try:
            # Freeze the Agent-to-secret mapping while the credential document
            # performs its own optimistic, serialized publication.
            with file_write_lock(self.config_path):
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
                latest_secrets[context.token_key] = token
                try:
                    write_config_json(
                        self.secrets_path,
                        latest_secrets,
                        private_full_control_sids=self.private_full_control_sids,
                    )
                except ConfigDurabilityError:
                    durability_warning = "credential_saved_durability_unconfirmed"
        except TelegramCredentialError:
            raise
        except ConfigConflictError:
            raise TelegramCredentialError(
                "Telegram credential status changed. Reload and try again.",
                error_code="credential_state_changed",
                status_code=409,
            ) from None
        except OSError:
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
        )
        result.update(
            activation_required="agent_restart" if runtime_active else None,
            replaced=context.configured,
            verified_bot=bot,
        )
        if durability_warning:
            result["warning_code"] = durability_warning
        return result
