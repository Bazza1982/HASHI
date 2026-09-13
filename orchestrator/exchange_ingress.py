"""Authenticated Exchange delivery acceptance and PAO scheduling bridge."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.exchange_config import ExchangeConfig, load_exchange_config
from orchestrator.message_context import (
    CONNECTOR_EVIDENCE_METADATA_KEY,
    seal_connector_evidence,
    verify_connector_evidence,
)
from orchestrator.session_store import SessionNotFound
from remote.exchange_protocol import (
    canonical_json,
    delivery_identity_key,
    delivery_payload_digest,
    validate_delivery_deadline,
    validate_delivery_frame,
)
from remote.exchange_outbox import ExchangeOutbox
from remote.internet_address import ExchangeAddress, PublicAddress


EXCHANGE_INGRESS_CLAIMS_TYPE = "hashi.exchange-ingress-claims"
EXCHANGE_INGRESS_CLAIMS_VERSION = 1
EXCHANGE_SOURCE = "hchat-exchange"
SCHEDULING_LEASE_SECONDS = 30.0
INBOX_DEDUP_RETENTION_SECONDS = 30 * 24 * 60 * 60


logger = logging.getLogger(__name__)


class ExchangeIngressError(ValueError):
    code = "INVALID_MESSAGE"
    status = 400


class ExchangeIngressAuthenticationError(ExchangeIngressError):
    code = "AUTH_FAILED"
    status = 401


class ExchangeIngressPermissionError(ExchangeIngressError):
    code = "PERMISSION_DENIED"
    status = 403


class ExchangeIngressConflict(ExchangeIngressError):
    code = "IDEMPOTENCY_CONFLICT"
    status = 409


class ExchangeIngressUnavailable(ExchangeIngressError):
    code = "RECIPIENT_UNAVAILABLE"
    status = 503


@dataclass(frozen=True, slots=True, init=False)
class VerifiedRemotePrincipal:
    """Identity facts constructible only after connector-evidence verification."""

    authority_id: str
    actor_id: str
    registered_instance_id: str
    instance_alias: str
    username: str
    agent_id: str
    address: str
    connection_epoch: str
    delivery_id: str
    grant_revision: int

    @classmethod
    def _verified(
        cls,
        *,
        sender: ExchangeAddress,
        connection_epoch: str,
        delivery_id: str,
        grant_revision: int,
    ) -> "VerifiedRemotePrincipal":
        value = object.__new__(cls)
        for field, item in (
            ("authority_id", sender.authority_id),
            ("actor_id", sender.actor_id),
            ("registered_instance_id", sender.registered_instance_id),
            ("instance_alias", sender.public_address.instance_alias),
            ("username", sender.public_address.username),
            ("agent_id", sender.agent_id),
            ("address", sender.address),
            ("connection_epoch", connection_epoch),
            ("delivery_id", delivery_id),
            ("grant_revision", int(grant_revision)),
        ):
            object.__setattr__(value, field, item)
        return value

    def public_snapshot(self) -> dict[str, Any]:
        return {
            "authority_id": self.authority_id,
            "actor_id": self.actor_id,
            "registered_instance_id": self.registered_instance_id,
            "instance_alias": self.instance_alias,
            "username": self.username,
            "agent_id": self.agent_id,
            "address": self.address,
            "connection_epoch": self.connection_epoch,
            "delivery_id": self.delivery_id,
            "grant_revision": self.grant_revision,
            "assurance": "exchange_verified",
        }


def render_exchange_hchat_prompt(delivery: Mapping[str, Any]) -> str:
    """Build the legacy-compatible display envelope from typed identity."""

    normalized = validate_delivery_frame(dict(delivery))
    sender = ExchangeAddress.from_mapping(normalized["sender"])
    text = str(normalized["content"]["text"])
    from tools.hchat_send import (
        format_hchat_message,
        format_hchat_terminal_reply,
    )

    if normalized["message_type"] == "agent_reply":
        if not text.lstrip().casefold().startswith("[hchat reply from "):
            text = format_hchat_terminal_reply(sender.agent_id, text)
        return format_hchat_message(
            sender.agent_id,
            sender.public_address.instance_address,
            text,
            include_autoreply_instruction=False,
        )
    return format_hchat_message(
        sender.agent_id,
        sender.public_address.instance_address,
        text,
    )


def build_exchange_ingress_claims(
    *,
    delivery: Mapping[str, Any],
    welcome: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = validate_delivery_frame(dict(delivery))
    return {
        "type": EXCHANGE_INGRESS_CLAIMS_TYPE,
        "version": EXCHANGE_INGRESS_CLAIMS_VERSION,
        "connection_epoch": str(welcome.get("epoch") or ""),
        "local_actor_id": str(welcome.get("actor_id") or ""),
        "local_instance_address": str(welcome.get("instance_address") or ""),
        "delivery": normalized,
    }


def _principal_from_claims(
    config: ExchangeConfig,
    claims: Mapping[str, Any],
    *,
    prompt: str,
    now: float,
    require_published: bool = True,
    require_deadline: bool = True,
) -> tuple[VerifiedRemotePrincipal, dict[str, Any]]:
    if (
        claims.get("type") != EXCHANGE_INGRESS_CLAIMS_TYPE
        or claims.get("version") != EXCHANGE_INGRESS_CLAIMS_VERSION
    ):
        raise ExchangeIngressAuthenticationError("invalid Exchange ingress claims")
    if not config.enabled:
        raise ExchangeIngressPermissionError("Exchange is disabled")
    try:
        delivery = validate_delivery_frame(dict(claims.get("delivery") or {}))
        sender = ExchangeAddress.from_mapping(delivery["sender"])
        recipient = ExchangeAddress.from_mapping(delivery["recipient"])
        local_instance = PublicAddress.parse(
            f"x@{str(claims.get('local_instance_address') or '')}"
        )
    except (TypeError, ValueError) as exc:
        raise ExchangeIngressError("invalid Exchange delivery") from exc
    connection_epoch = str(claims.get("connection_epoch") or "")
    local_actor_id = str(claims.get("local_actor_id") or "")
    if (
        not connection_epoch
        or delivery["recipient_epoch"] != connection_epoch
        or sender.authority_id != config.authority_id
        or recipient.authority_id != config.authority_id
        or recipient.registered_instance_id != config.registered_instance_id
        or recipient.actor_id != local_actor_id
        or local_instance.instance_alias != config.instance_alias
        or recipient.public_address.instance_address
        != local_instance.instance_address
    ):
        raise ExchangeIngressPermissionError(
            "Exchange recipient identity binding mismatch"
        )
    if require_published and recipient.agent_id not in config.published_agents:
        raise ExchangeIngressPermissionError("recipient is not published")
    if require_deadline:
        try:
            validate_delivery_deadline(delivery, now=now)
        except ValueError as exc:
            raise ExchangeIngressError("Exchange delivery expired") from exc
    rendered = render_exchange_hchat_prompt(delivery)
    if rendered != str(prompt):
        raise ExchangeIngressAuthenticationError(
            "Exchange prompt/evidence binding mismatch"
        )
    principal = VerifiedRemotePrincipal._verified(
        sender=sender,
        connection_epoch=connection_epoch,
        delivery_id=delivery["delivery_id"],
        grant_revision=delivery["grant_revision"],
    )
    return principal, delivery


@dataclass(frozen=True, slots=True)
class InboxRecord:
    inbox_key: str
    idempotency_key: str
    payload_digest: str
    delivery: dict[str, Any]
    state: str
    request_id: str | None
    run_id: str | None
    session_id: str | None
    message_id: str | None
    accepted_at: float
    updated_at: float
    replayed: bool = False


class ExchangeInbox:
    """Minimal PAO-owned acceptance ledger; not a second conversation history."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS exchange_inbox (
                    inbox_key TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_digest TEXT NOT NULL,
                    delivery_json TEXT NOT NULL,
                    target_agent TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_id TEXT,
                    run_id TEXT,
                    session_id TEXT,
                    message_id TEXT,
                    accepted_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS exchange_inbox_pending
                ON exchange_inbox(state, updated_at)
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _record(row: sqlite3.Row, *, replayed: bool = False) -> InboxRecord:
        return InboxRecord(
            inbox_key=str(row["inbox_key"]),
            idempotency_key=str(row["idempotency_key"]),
            payload_digest=str(row["payload_digest"]),
            delivery=json.loads(str(row["delivery_json"])),
            state=str(row["state"]),
            request_id=(
                str(row["request_id"]) if row["request_id"] is not None else None
            ),
            run_id=str(row["run_id"]) if row["run_id"] is not None else None,
            session_id=(
                str(row["session_id"]) if row["session_id"] is not None else None
            ),
            message_id=(
                str(row["message_id"]) if row["message_id"] is not None else None
            ),
            accepted_at=float(row["accepted_at"]),
            updated_at=float(row["updated_at"]),
            replayed=replayed,
        )

    def accept(self, delivery: Mapping[str, Any]) -> InboxRecord:
        normalized = validate_delivery_frame(dict(delivery))
        identity = delivery_identity_key(normalized)
        inbox_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        idempotency_key = f"exchange-{inbox_key}"
        digest = delivery_payload_digest(normalized)
        recipient = ExchangeAddress.from_mapping(normalized["recipient"])
        rendered = canonical_json(normalized).decode("ascii")
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM exchange_inbox WHERE inbox_key=?",
                (inbox_key,),
            ).fetchone()
            if row is not None:
                if str(row["payload_digest"]) != digest:
                    raise ExchangeIngressConflict(
                        "Exchange message ID conflicts with accepted payload"
                    )
                return self._record(row, replayed=True)
            connection.execute(
                """
                INSERT INTO exchange_inbox(
                    inbox_key, idempotency_key, payload_digest, delivery_json,
                    target_agent, state, accepted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?)
                """,
                (
                    inbox_key,
                    idempotency_key,
                    digest,
                    rendered,
                    recipient.agent_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM exchange_inbox WHERE inbox_key=?",
                (inbox_key,),
            ).fetchone()
        return self._record(row)

    def get(self, inbox_key: str) -> InboxRecord | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM exchange_inbox WHERE inbox_key=?",
                (str(inbox_key),),
            ).fetchone()
        return self._record(row) if row is not None else None

    def claim(
        self,
        inbox_key: str,
        *,
        now: float | None = None,
        recover_scheduled: bool = False,
        recover_leased: bool = False,
    ) -> InboxRecord | None:
        timestamp = time.time() if now is None else float(now)
        stale_before = timestamp - SCHEDULING_LEASE_SECONDS
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM exchange_inbox WHERE inbox_key=?",
                (str(inbox_key),),
            ).fetchone()
            if row is None:
                return None
            if str(row["state"]) == "scheduled" and not recover_scheduled:
                return None
            if (
                str(row["state"]) == "scheduling"
                and float(row["updated_at"]) > stale_before
                and not recover_leased
            ):
                return None
            connection.execute(
                """
                UPDATE exchange_inbox
                SET state='scheduling', updated_at=?
                WHERE inbox_key=?
                """,
                (timestamp, str(inbox_key)),
            )
            row = connection.execute(
                "SELECT * FROM exchange_inbox WHERE inbox_key=?",
                (str(inbox_key),),
            ).fetchone()
        return self._record(row)

    def release(self, inbox_key: str) -> None:
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE exchange_inbox
                SET state='accepted', updated_at=?
                WHERE inbox_key=? AND state='scheduling'
                """,
                (now, str(inbox_key)),
            )

    def mark_scheduled(
        self,
        inbox_key: str,
        *,
        request_id: str,
        run_id: str | None,
        session_id: str | None,
        message_id: str | None,
    ) -> InboxRecord:
        now = time.time()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE exchange_inbox
                SET state='scheduled', request_id=?, run_id=?, session_id=?,
                    message_id=?, updated_at=?
                WHERE inbox_key=?
                """,
                (
                    str(request_id),
                    str(run_id) if run_id else None,
                    str(session_id) if session_id else None,
                    str(message_id) if message_id else None,
                    now,
                    str(inbox_key),
                ),
            )
            row = connection.execute(
                "SELECT * FROM exchange_inbox WHERE inbox_key=?",
                (str(inbox_key),),
            ).fetchone()
        if row is None:
            raise ExchangeIngressError("Exchange inbox record disappeared")
        return self._record(row)

    def redact_scheduled(self, inbox_key: str) -> None:
        """Drop the duplicate body after PAO no longer needs queue recovery."""

        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE exchange_inbox
                SET delivery_json='{}'
                WHERE inbox_key=? AND state='scheduled'
                """,
                (str(inbox_key),),
            )

    def purge(
        self,
        *,
        now: float | None = None,
        retention_seconds: float = INBOX_DEDUP_RETENTION_SECONDS,
    ) -> int:
        """Remove only old scheduled markers; pending bodies remain recoverable."""

        timestamp = time.time() if now is None else float(now)
        cutoff = timestamp - max(0.0, float(retention_seconds))
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM exchange_inbox
                WHERE state='scheduled' AND delivery_json='{}'
                  AND updated_at<=?
                """,
                (cutoff,),
            )
            return int(cursor.rowcount)

    def pending(
        self,
        *,
        limit: int = 100,
        include_leased: bool = False,
    ) -> list[InboxRecord]:
        cutoff = time.time() - SCHEDULING_LEASE_SECONDS
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exchange_inbox
                WHERE state='accepted'
                   OR (state='scheduling' AND (? OR updated_at <= ?))
                ORDER BY accepted_at, inbox_key
                LIMIT ?
                """,
                (
                    1 if include_leased else 0,
                    cutoff,
                    max(1, min(int(limit), 100)),
                ),
            ).fetchall()
        return [self._record(row) for row in rows]

    def scheduled(self, *, limit: int = 100) -> list[InboxRecord]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM exchange_inbox
                WHERE state='scheduled' AND delivery_json!='{}'
                ORDER BY accepted_at, inbox_key
                LIMIT ?
                """,
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [self._record(row) for row in rows]


class ExchangeIngressService:
    def __init__(
        self,
        *,
        hashi_root: Path | str,
        runtime_map: Callable[[], Mapping[str, Any]],
        session_store: Any,
    ):
        self.hashi_root = Path(hashi_root)
        self._runtime_map = runtime_map
        self.session_store = session_store
        authoritative_db = getattr(
            session_store,
            "db_path",
            self.hashi_root / "state" / "sessions.sqlite3",
        )
        self.inbox = ExchangeInbox(
            authoritative_db
        )
        self.outbox = ExchangeOutbox(
            self.hashi_root / "state" / "exchange_outbox.sqlite3"
        )

    def _verified(
        self,
        *,
        evidence: Any,
        prompt: str,
        require_published: bool = True,
        require_deadline: bool = True,
    ) -> tuple[VerifiedRemotePrincipal, dict[str, Any]]:
        claims = verify_connector_evidence(
            self.hashi_root,
            evidence=evidence,
            prompt=prompt,
        )
        if not isinstance(claims, Mapping):
            raise ExchangeIngressAuthenticationError(
                "Exchange connector evidence is missing or invalid"
            )
        try:
            config = load_exchange_config(self.hashi_root)
        except (OSError, ValueError) as exc:
            raise ExchangeIngressUnavailable(
                "Exchange configuration is unavailable"
            ) from exc
        return _principal_from_claims(
            config,
            claims,
            prompt=prompt,
            now=time.time(),
            require_published=require_published,
            require_deadline=require_deadline,
        )

    def accept(self, *, evidence: Any, prompt: str) -> InboxRecord:
        _principal, delivery = self._verified(
            evidence=evidence,
            prompt=prompt,
        )
        self._verify_reply_correlation(delivery)
        recipient = ExchangeAddress.from_mapping(delivery["recipient"])
        runtime = self._runtime_map().get(recipient.agent_id)
        if runtime is None or getattr(runtime, "startup_success", True) is False:
            raise ExchangeIngressPermissionError(
                "Exchange recipient runtime is unavailable"
            )
        return self.inbox.accept(delivery)

    def _verify_reply_correlation(
        self,
        delivery: Mapping[str, Any],
    ) -> None:
        """Require an inbound reply to match the original local outbound tuple."""

        if delivery.get("message_type") != "agent_reply":
            return
        correlation = self.outbox.get_correlation(
            str(delivery.get("in_reply_to") or "")
        )
        sender = ExchangeAddress.from_mapping(delivery["sender"])
        recipient = ExchangeAddress.from_mapping(delivery["recipient"])
        if (
            correlation is None
            or correlation.message_type != "agent_message"
            or correlation.accepted_at is None
            or correlation.delivery_state in {"rejected", "expired"}
            or correlation.from_agent != recipient.agent_id
            or correlation.conversation_id != delivery["conversation_id"]
            or correlation.recipient != sender.to_mapping()
        ):
            raise ExchangeIngressPermissionError(
                "Exchange reply does not match a local outbound request"
            )

    async def schedule(
        self,
        *,
        evidence: Any,
        prompt: str,
    ) -> InboxRecord:
        _principal, delivery = self._verified(
            evidence=evidence,
            prompt=prompt,
        )
        identity = delivery_identity_key(delivery)
        inbox_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return await self._schedule_key(inbox_key)

    @staticmethod
    def _runtime_claims(
        principal: VerifiedRemotePrincipal,
        delivery: Mapping[str, Any],
    ) -> dict[str, Any]:
        recipient = ExchangeAddress.from_mapping(delivery["recipient"])
        return {
            "_message_source_reserved": "hchat",
            "_hchat_context": {
                "from_agent": principal.agent_id,
                "from_instance": principal.registered_instance_id,
                "from_address": principal.address,
                "to_agent": recipient.agent_id,
                "to_instance": recipient.registered_instance_id,
                "to_address": recipient.address,
                "sender_assurance": "exchange_verified",
                "network_authentication": "exchange_wss",
                "authenticated_peer": principal.registered_instance_id,
                "relay_chain": [principal.address],
                "origin_instance": {
                    "id": principal.registered_instance_id,
                    "assurance": "exchange_verified",
                },
                "remote_principal": principal.public_snapshot(),
                "exchange_message": {
                    "message_id": delivery["message_id"],
                    "conversation_id": delivery["conversation_id"],
                    "message_type": delivery["message_type"],
                    "in_reply_to": delivery["in_reply_to"],
                    "expires_at": delivery["expires_at"],
                    "authorization_expires_at": delivery[
                        "authorization_expires_at"
                    ],
                },
            },
        }

    async def _schedule_key(
        self,
        inbox_key: str,
        *,
        recover_scheduled: bool = False,
        recover_leased: bool = False,
    ) -> InboxRecord:
        current = self.inbox.get(inbox_key)
        if current is None:
            raise ExchangeIngressError("Exchange inbox record not found")
        if current.state == "scheduled" and not recover_scheduled:
            return current
        record = self.inbox.claim(
            inbox_key,
            recover_scheduled=recover_scheduled,
            recover_leased=recover_leased,
        )
        if record is None:
            current = self.inbox.get(inbox_key)
            if current is not None:
                return current
            raise ExchangeIngressError("Exchange inbox record not found")
        try:
            config = load_exchange_config(self.hashi_root)
            prompt = render_exchange_hchat_prompt(record.delivery)
            # The message was already accepted under a live publish decision.
            # A later unpublish blocks new acceptance but does not discard this
            # committed message.
            claims = build_exchange_ingress_claims(
                delivery=record.delivery,
                welcome={
                    "epoch": record.delivery["recipient_epoch"],
                    "actor_id": record.delivery["recipient"]["actor_id"],
                    "instance_address": PublicAddress.parse(
                        record.delivery["recipient"]["address"]
                    ).instance_address,
                },
            )
            principal, delivery = _principal_from_claims(
                config,
                claims,
                prompt=prompt,
                now=time.time(),
                require_published=False,
                require_deadline=False,
            )
            recipient = ExchangeAddress.from_mapping(delivery["recipient"])
            runtime = self._runtime_map().get(recipient.agent_id)
            if runtime is None or getattr(runtime, "startup_success", True) is False:
                raise ExchangeIngressUnavailable(
                    "Exchange recipient runtime is unavailable"
                )
            connector_evidence = seal_connector_evidence(
                self.hashi_root,
                claims=self._runtime_claims(principal, delivery),
                prompt=prompt,
            )
            if connector_evidence is None:
                raise ExchangeIngressUnavailable(
                    "local Exchange ingress authentication is unavailable"
                )
            metadata = {
                CONNECTOR_EVIDENCE_METADATA_KEY: connector_evidence,
                "owner_id": (
                    f"exchange:{principal.authority_id}:{principal.actor_id}"
                ),
                "session_surface": "hchat",
                "session_channel_key": (
                    f"{principal.registered_instance_id}:"
                    f"{delivery['conversation_id']}"
                ),
            }
            request_id = await runtime.enqueue_api_text(
                prompt,
                source=EXCHANGE_SOURCE,
                deliver_to_telegram=True,
                request_metadata=metadata,
                idempotency_key=record.idempotency_key,
            )
            if not request_id:
                raise ExchangeIngressUnavailable(
                    "PAO did not accept the Exchange message"
                )
            run_id = session_id = message_id = None
            try:
                run = self.session_store.get_run_by_request(str(request_id))
                run_id = str(run.get("run_id") or "") or None
                session_id = str(run.get("session_id") or "") or None
                message_id = str(run.get("user_message_id") or "") or None
            except SessionNotFound:
                pass
            return self.inbox.mark_scheduled(
                inbox_key,
                request_id=str(request_id),
                run_id=run_id,
                session_id=session_id,
                message_id=message_id,
            )
        except Exception:
            self.inbox.release(inbox_key)
            raise

    def _run_state(self, record: InboxRecord) -> str | None:
        if not record.request_id:
            return None
        try:
            run = self.session_store.get_run_by_request(record.request_id)
        except SessionNotFound:
            return None
        return str(run.get("state") or "") or None

    async def recover_pending(
        self,
        *,
        limit: int = 100,
        recover_scheduled: bool = False,
    ) -> list[InboxRecord]:
        recovered: list[InboxRecord] = []
        self.inbox.purge()
        scheduled = self.inbox.scheduled(limit=limit)
        scheduled_states = {
            record.inbox_key: self._run_state(record)
            for record in scheduled
        }
        for record in scheduled:
            state = scheduled_states[record.inbox_key]
            if state is not None and state != "queued":
                self.inbox.redact_scheduled(record.inbox_key)
        try:
            config = load_exchange_config(self.hashi_root)
        except (OSError, ValueError):
            return recovered
        if not config.enabled:
            return recovered
        for record in scheduled:
            state = scheduled_states[record.inbox_key]
            if recover_scheduled and state == "queued":
                try:
                    recovered.append(
                        await self._schedule_key(
                            record.inbox_key,
                            recover_scheduled=True,
                            recover_leased=True,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Exchange scheduled Run recovery deferred (%s)",
                        type(exc).__name__,
                    )
                continue
        for record in self.inbox.pending(
            limit=limit,
            include_leased=recover_scheduled,
        ):
            try:
                recovered.append(
                    await self._schedule_key(
                        record.inbox_key,
                        recover_leased=recover_scheduled,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Exchange inbox item recovery deferred (%s)",
                    type(exc).__name__,
                )
                continue
        return recovered


__all__ = [
    "EXCHANGE_INGRESS_CLAIMS_TYPE",
    "EXCHANGE_SOURCE",
    "INBOX_DEDUP_RETENTION_SECONDS",
    "ExchangeInbox",
    "ExchangeIngressAuthenticationError",
    "ExchangeIngressConflict",
    "ExchangeIngressError",
    "ExchangeIngressPermissionError",
    "ExchangeIngressService",
    "ExchangeIngressUnavailable",
    "InboxRecord",
    "VerifiedRemotePrincipal",
    "build_exchange_ingress_claims",
    "render_exchange_hchat_prompt",
]
