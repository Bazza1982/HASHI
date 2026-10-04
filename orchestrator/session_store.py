from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.audio_assets import (
    DEFAULT_RETENTION_SECONDS,
    MIN_RETENTION_SECONDS,
    AudioAssetError,
    AudioAssetNotFound,
    AudioAssetStore,
    normalize_audio_format,
)
from orchestrator.hchat_attachment_contract import (
    HCHAT_MAX_ATTACHMENT_BYTES,
    HCHAT_MAX_ATTACHMENTS_PER_MESSAGE,
    HCHAT_MAX_TOTAL_ATTACHMENT_BYTES,
)
from orchestrator.multimodal_contract import (
    contains_persistent_inline_media,
    modality_for_attachment,
)
from orchestrator.frontend_contracts import (
    normalize_delivery_receipt,
    normalize_media_group,
)
from orchestrator.storage_profile import removable_storage_profile

TERMINAL_RUN_STATES = frozenset(
    {"completed", "failed", "stopped", "superseded", "interrupted"}
)
CONVERSATION_CONTINUITY_TYPE = "hashi.conversation-continuity"
CONVERSATION_CONTINUITY_VERSION = 1
CONVERSATION_HISTORY_MODES = frozenset({"move", "copy", "inherit_read_only"})
MAX_CONTINUITY_SESSIONS = 500
MAX_CONTINUITY_MESSAGES = 100_000
MAX_SESSION_MESSAGE_CHARS = 200_000
PRIMARY_CONVERSATION_SURFACE = "conversation"
PRIMARY_CONVERSATION_CHANNEL = "main"
SESSION_KIND_CONVERSATION = "conversation"
SESSION_KIND_AGENT_ACTIVITY = "agent_activity"
SESSION_KINDS = frozenset(
    {SESSION_KIND_CONVERSATION, SESSION_KIND_AGENT_ACTIVITY}
)
MAX_SESSION_ATTACHMENTS_PER_MESSAGE = 16
MAX_SESSION_ATTACHMENT_BYTES = 64 * 1024 * 1024
MAX_SESSION_ATTACHMENT_TOTAL_BYTES = 64 * 1024 * 1024
HCHAT_SESSION_ATTACHMENTS_PER_MESSAGE = HCHAT_MAX_ATTACHMENTS_PER_MESSAGE
HCHAT_SESSION_ATTACHMENT_BYTES = HCHAT_MAX_ATTACHMENT_BYTES
HCHAT_SESSION_ATTACHMENT_TOTAL_BYTES = HCHAT_MAX_TOTAL_ATTACHMENT_BYTES


def _attachment_policy_limits(policy: str) -> tuple[int, int, int]:
    normalized = str(policy or "standard").strip().casefold()
    if normalized == "standard":
        return (
            MAX_SESSION_ATTACHMENTS_PER_MESSAGE,
            MAX_SESSION_ATTACHMENT_BYTES,
            MAX_SESSION_ATTACHMENT_TOTAL_BYTES,
        )
    if normalized == "hchat":
        return (
            HCHAT_SESSION_ATTACHMENTS_PER_MESSAGE,
            HCHAT_SESSION_ATTACHMENT_BYTES,
            HCHAT_SESSION_ATTACHMENT_TOTAL_BYTES,
        )
    raise ValueError("unknown attachment policy")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def conversation_continuity_digest(payload: Mapping[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "capsule_digest"}
    return hashlib.sha256(_json(body).encode("utf-8")).hexdigest()


def _continuity_origin_ref(
    *,
    source_instance: str,
    source_session_id: str,
    source_message_id: str,
    source_ordinal: int,
    content_hash: str,
) -> str:
    basis = "\n".join(
        (
            str(source_instance).strip().upper(),
            str(source_session_id),
            str(source_message_id),
            str(int(source_ordinal)),
            str(content_hash).lower(),
        )
    )
    return "origin:" + hashlib.sha256(basis.encode("utf-8")).hexdigest()


class SessionStoreError(RuntimeError):
    code = "session_store_error"


class SessionNotFound(SessionStoreError):
    code = "session_not_found"


class SessionConflict(SessionStoreError):
    code = "session_conflict"


class IdempotencyConflict(SessionConflict):
    code = "idempotency_conflict"


class StaleFencingToken(SessionConflict):
    code = "stale_fencing_token"


def validate_conversation_continuity_capsule(
    capsule: Mapping[str, Any],
    *,
    owner_id: str | None = None,
    source_agent_id: str | None = None,
    transfer_id: str | None = None,
    history_mode: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize one portable, execution-free history capsule."""

    if not isinstance(capsule, Mapping):
        raise SessionConflict("conversation continuity capsule must be an object")
    payload = dict(capsule)
    if payload.get("type") != CONVERSATION_CONTINUITY_TYPE:
        raise SessionConflict("conversation continuity capsule type is invalid")
    if payload.get("schema_version") != CONVERSATION_CONTINUITY_VERSION:
        raise SessionConflict("conversation continuity capsule version is unsupported")

    capsule_owner = str(payload.get("owner_id") or "").strip()
    capsule_agent = str(payload.get("agent_id") or "").strip().lower()
    capsule_transfer = str(payload.get("transfer_id") or "").strip()
    capsule_mode = str(payload.get("history_mode") or "").strip().lower()
    source_instance = str(payload.get("source_instance") or "").strip().upper()
    if not all((capsule_owner, capsule_agent, capsule_transfer, source_instance)):
        raise SessionConflict("conversation continuity identity is incomplete")
    if capsule_mode not in CONVERSATION_HISTORY_MODES:
        raise SessionConflict("conversation continuity history mode is invalid")
    if owner_id is not None and capsule_owner != str(owner_id).strip():
        raise SessionConflict("conversation continuity owner does not match target owner")
    if (
        source_agent_id is not None
        and capsule_agent != str(source_agent_id).strip().lower()
    ):
        raise SessionConflict("conversation continuity source Agent does not match")
    if transfer_id is not None and capsule_transfer != str(transfer_id).strip():
        raise SessionConflict("conversation continuity transfer identity does not match")
    if history_mode is not None and capsule_mode != str(history_mode).strip().lower():
        raise SessionConflict("conversation history mode does not match the capsule")

    expected_digest = conversation_continuity_digest(payload)
    capsule_digest = str(payload.get("capsule_digest") or "").lower()
    if not capsule_digest or not hmac.compare_digest(expected_digest, capsule_digest):
        raise SessionConflict("conversation continuity capsule digest does not match")

    sessions = payload.get("sessions")
    if not isinstance(sessions, list) or len(sessions) > MAX_CONTINUITY_SESSIONS:
        raise SessionConflict("conversation continuity session count is invalid")
    normalized_sessions: list[dict[str, Any]] = []
    seen_sessions: set[str] = set()
    seen_origins: set[str] = set()
    message_count = 0
    for raw_session in sessions:
        if not isinstance(raw_session, Mapping):
            raise SessionConflict("conversation continuity session is invalid")
        source_session_id = str(raw_session.get("source_session_id") or "")
        if not source_session_id or source_session_id in seen_sessions:
            raise SessionConflict("conversation continuity source Session is duplicated")
        seen_sessions.add(source_session_id)
        raw_messages = raw_session.get("messages")
        raw_bindings = raw_session.get("bindings")
        if not isinstance(raw_messages, list) or not isinstance(raw_bindings, list):
            raise SessionConflict("conversation continuity Session members are invalid")
        normalized_messages: list[dict[str, Any]] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, Mapping):
                raise SessionConflict("conversation continuity message is invalid")
            item = dict(raw_message)
            role = str(item.get("role") or "").lower()
            content = item.get("content")
            if role not in {"user", "assistant"}:
                raise SessionConflict("conversation continuity message role is invalid")
            if (
                not isinstance(content, list)
                or not all(
                    isinstance(part, Mapping)
                    and str(part.get("type") or "").casefold() == "text"
                    and isinstance(part.get("text"), str)
                    for part in content
                )
                or contains_persistent_inline_media(content)
            ):
                raise SessionConflict(
                    "conversation continuity contains unsupported attachments"
                )
            item_source_instance = str(item.get("source_instance") or "").upper()
            item_source_session = str(item.get("source_session_id") or "")
            source_message_id = str(item.get("source_message_id") or "")
            source_created_at = str(item.get("source_created_at") or "")
            try:
                source_ordinal = int(item.get("source_ordinal"))
            except (TypeError, ValueError) as exc:
                raise SessionConflict("conversation continuity ordinal is invalid") from exc
            if not all(
                (
                    item_source_instance,
                    item_source_session,
                    source_message_id,
                    source_created_at,
                )
            ) or source_ordinal < 1:
                raise SessionConflict("conversation continuity message identity is incomplete")
            content_json = _json([dict(part) for part in content])
            content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
            if not hmac.compare_digest(
                content_hash,
                str(item.get("content_hash") or "").lower(),
            ):
                raise SessionConflict("conversation continuity message content was changed")
            origin_ref = _continuity_origin_ref(
                source_instance=item_source_instance,
                source_session_id=item_source_session,
                source_message_id=source_message_id,
                source_ordinal=source_ordinal,
                content_hash=content_hash,
            )
            if not hmac.compare_digest(origin_ref, str(item.get("origin_ref") or "")):
                raise SessionConflict("conversation continuity origin reference is invalid")
            if origin_ref in seen_origins:
                raise SessionConflict("conversation continuity message is duplicated")
            seen_origins.add(origin_ref)
            display_text = item.get("display_text")
            if display_text is not None and (
                not isinstance(display_text, str)
                or len(display_text) > MAX_SESSION_MESSAGE_CHARS
            ):
                raise SessionConflict(
                    "conversation continuity display text is invalid"
                )
            item.update(
                {
                    "role": role,
                    "content": [dict(part) for part in content],
                    "content_json": content_json,
                    "content_hash": content_hash,
                    "origin_ref": origin_ref,
                    "source_instance": item_source_instance,
                    "source_session_id": item_source_session,
                    "source_message_id": source_message_id,
                    "source_created_at": source_created_at,
                    "source_ordinal": source_ordinal,
                    "text": str(item.get("text") or ""),
                    "display_text": display_text,
                    "source": str(item.get("source") or "unknown"),
                }
            )
            normalized_messages.append(item)
            message_count += 1
            if message_count > MAX_CONTINUITY_MESSAGES:
                raise SessionConflict("conversation continuity message count is too large")
        bindings: list[dict[str, str]] = []
        seen_bindings: set[tuple[str, str]] = set()
        for raw_binding in raw_bindings:
            if not isinstance(raw_binding, Mapping):
                raise SessionConflict("conversation continuity binding is invalid")
            surface = str(raw_binding.get("surface") or "").strip().lower()
            channel_key = str(raw_binding.get("channel_key") or "").strip()
            key = (surface, channel_key)
            if not surface or not channel_key or key in seen_bindings:
                raise SessionConflict(
                    "conversation continuity binding is invalid or duplicated"
                )
            seen_bindings.add(key)
            bindings.append({"surface": surface, "channel_key": channel_key})
        normalized_sessions.append(
            {
                "source_session_id": source_session_id,
                "title": str(raw_session.get("title") or "Imported history")[:500],
                "title_source": str(raw_session.get("title_source") or "system"),
                "status": str(raw_session.get("status") or "active"),
                "is_default": bool(raw_session.get("is_default")),
                "created_at": str(raw_session.get("created_at") or ""),
                "bindings": bindings,
                "messages": normalized_messages,
            }
        )

    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        raise SessionConflict("conversation continuity summary is invalid")
    if (
        summary.get("session_count") != len(normalized_sessions)
        or summary.get("eligible_message_count") != message_count
        or summary.get("attachments_included") != 0
        or not isinstance(summary.get("excluded_message_count"), int)
        or isinstance(summary.get("excluded_message_count"), bool)
        or int(summary.get("excluded_message_count")) < 0
    ):
        raise SessionConflict("conversation continuity summary does not match payload")
    return {
        "payload": payload,
        "capsule_digest": capsule_digest,
        "sessions": normalized_sessions,
        "message_count": message_count,
        "owner_id": capsule_owner,
        "source_agent_id": capsule_agent,
        "transfer_id": capsule_transfer,
        "history_mode": capsule_mode,
    }


@dataclass(frozen=True)
class AcceptedRun:
    session_id: str
    run_id: str
    message_id: str
    request_id: str
    context_generation: int
    replayed: bool = False


class SessionStore:
    """Transactional HASHI-owned conversation Session repository.

    The personal/local implementation deliberately lives under instance state,
    outside replaceable Agent workspaces. Raw Session records are canonical;
    per-Session working files are derived state used by Memory+ and Compact.
    """

    SCHEMA_VERSION = 23

    def __init__(
        self,
        db_path: str | Path,
        *,
        instance_id: str = "HASHI",
        attachment_root: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.instance_id = str(instance_id or "HASHI").upper()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspaces_root = self.db_path.parent / "session_workspaces"
        self.workspaces_root.mkdir(parents=True, exist_ok=True)
        self.audio_assets = AudioAssetStore(
            self.db_path.parent / "native_audio_assets"
        )
        self.attachment_files_root = Path(
            attachment_root or self.db_path.parent / "session_attachments"
        ).expanduser().resolve()
        self.attachment_files_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.attachment_files_root, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()
        self._initialize()

    @classmethod
    def from_global_config(cls, global_config: Any) -> SessionStore:
        bridge_home = Path(
            getattr(global_config, "bridge_home", None)
            or getattr(global_config, "project_root", None)
            or "."
        )
        base_media_dir = getattr(global_config, "base_media_dir", None)
        attachment_root = (
            Path(base_media_dir) / "session_attachments"
            if base_media_dir
            else bridge_home / "media" / "session_attachments"
        )
        return cls(
            bridge_home / "state" / "sessions.sqlite3",
            instance_id=str(getattr(global_config, "instance_id", "HASHI") or "HASHI"),
            attachment_root=attachment_root,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path, timeout=30.0, check_same_thread=False
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        if removable_storage_profile():
            connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Commit or roll back, then deterministically release the file handle."""

        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    session_kind TEXT NOT NULL DEFAULT 'conversation',
                    title_source TEXT NOT NULL DEFAULT 'system',
                    status TEXT NOT NULL DEFAULT 'active',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    context_generation INTEGER NOT NULL DEFAULT 1,
                    memory_policy TEXT NOT NULL DEFAULT 'promote',
                    workzone TEXT,
                    workzone_revision INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL DEFAULT 1,
                    history_generation INTEGER NOT NULL DEFAULT 1,
                    next_message_ordinal INTEGER NOT NULL DEFAULT 1,
                    next_event_sequence INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    deleted_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_default_session_per_agent
                    ON sessions(instance_id, owner_id, agent_id)
                    WHERE is_default = 1;
                CREATE INDEX IF NOT EXISTS sessions_owner_agent_updated
                    ON sessions(instance_id, owner_id, agent_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS session_workzones (
                    session_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, slot_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS session_workzones_session_enabled
                    ON session_workzones(session_id, enabled, slot_id);

                CREATE TABLE IF NOT EXISTS agent_workzone_profiles (
                    instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(instance_id, owner_id, agent_id)
                );

                CREATE TABLE IF NOT EXISTS agent_workzones (
                    instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(instance_id, owner_id, agent_id, slot_id),
                    FOREIGN KEY(instance_id, owner_id, agent_id)
                        REFERENCES agent_workzone_profiles(instance_id, owner_id, agent_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS agent_workzones_enabled
                    ON agent_workzones(instance_id, owner_id, agent_id, enabled, slot_id);

                CREATE TABLE IF NOT EXISTS agent_workzone_events (
                    event_id TEXT PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_workzone_events_owner_agent
                    ON agent_workzone_events(instance_id, owner_id, agent_id, created_at);

                CREATE TABLE IF NOT EXISTS session_participants (
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'presentation',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, agent_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS session_context_generations (
                    session_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, generation),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    ordinal INTEGER NOT NULL,
                    context_generation INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message_context_json TEXT NOT NULL DEFAULT '{}',
                    content_json TEXT NOT NULL,
                    text TEXT NOT NULL,
                    display_text TEXT,
                    visibility TEXT NOT NULL DEFAULT 'visible',
                    history_eligible INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, ordinal),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS messages_session_generation
                    ON messages(session_id, context_generation, ordinal);

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_message_id TEXT NOT NULL,
                    final_message_id TEXT,
                    agent_id TEXT NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message_context_json TEXT NOT NULL DEFAULT '{}',
                    delivery_route_json TEXT NOT NULL DEFAULT '{}',
                    requested_mode TEXT,
                    effective_mode TEXT,
                    response_preferences_json TEXT NOT NULL DEFAULT '{}',
                    context_generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    worker_id TEXT,
                    error_code TEXT,
                    error_text TEXT,
                    parent_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(session_id, idempotency_key),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(user_message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(final_message_id) REFERENCES messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS runs_session_created
                    ON runs(session_id, created_at, run_id);

                CREATE TABLE IF NOT EXISTS run_attempts (
                    run_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    worker_id TEXT NOT NULL,
                    authorization_json TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    state TEXT NOT NULL,
                    PRIMARY KEY(run_id, attempt),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT,
                    phase TEXT,
                    delivery_class TEXT NOT NULL DEFAULT 'durable',
                    summary TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, sequence),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS run_projection_records (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    projection_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS idempotency_records (
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, idempotency_key),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS frontend_command_invocations (
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    invocation_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    response_json TEXT,
                    event_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, client_id, request_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS channel_bindings (
                    instance_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    channel_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(instance_id, owner_id, agent_id, surface, channel_key),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS backend_bindings (
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    context_generation INTEGER NOT NULL,
                    backend_id TEXT NOT NULL,
                    backend_thread_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, session_id, context_generation, backend_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS session_capsules (
                    capsule_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    context_generation INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_continuity_imports (
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    origin_ref TEXT NOT NULL,
                    transfer_id TEXT NOT NULL,
                    target_session_id TEXT NOT NULL,
                    target_message_id TEXT NOT NULL UNIQUE,
                    capsule_digest TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, agent_id, origin_ref),
                    FOREIGN KEY(target_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(target_message_id) REFERENCES messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS continuity_imports_transfer
                    ON conversation_continuity_imports(transfer_id);

                CREATE TABLE IF NOT EXISTS conversation_continuity_batches (
                    transfer_id TEXT PRIMARY KEY,
                    capsule_digest TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    history_mode TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_continuity_origin_claims (
                    transfer_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    origin_ref TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY(transfer_id, owner_id, agent_id, origin_ref)
                );
                CREATE INDEX IF NOT EXISTS continuity_origin_claims_origin
                    ON conversation_continuity_origin_claims(
                        owner_id, agent_id, origin_ref
                    );

                CREATE TABLE IF NOT EXISTS conversation_continuity_retirements (
                    transfer_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    session_ids_json TEXT NOT NULL,
                    retired_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_memory_records (
                    promotion_record_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    user_message_id TEXT NOT NULL,
                    assistant_message_id TEXT NOT NULL,
                    memory_origin_ref TEXT NOT NULL UNIQUE,
                    promoted_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS memory_promotion_watermarks (
                    agent_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    promoted_through_ordinal INTEGER NOT NULL DEFAULT 0,
                    promoted_at TEXT NOT NULL,
                    PRIMARY KEY(agent_id, session_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS memory_promotion_jobs (
                    job_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    session_id TEXT,
                    trigger_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    promoted_count INTEGER NOT NULL DEFAULT 0,
                    error_text TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS memory_promotion_schedules (
                    agent_id TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    local_time TEXT NOT NULL DEFAULT '00:00',
                    timezone TEXT NOT NULL DEFAULT 'local',
                    last_local_date TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_consumers (
                    consumer_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    acknowledged_sequence INTEGER NOT NULL DEFAULT 0,
                    issued_through_sequence INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    event_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id)
                );

                CREATE TABLE IF NOT EXISTS connector_delivery_tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    event_id TEXT NOT NULL,
                    connector_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content_modes_json TEXT NOT NULL DEFAULT '[]',
                    retry_class TEXT NOT NULL DEFAULT 'query_before_retry',
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    completed_at TEXT,
                    last_claim_token TEXT,
                    last_claim_status TEXT,
                    UNIQUE(event_id, endpoint_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id)
                );
                CREATE INDEX IF NOT EXISTS connector_delivery_tasks_claimable
                    ON connector_delivery_tasks(
                        session_id, connector_id, endpoint_id, state, created_at
                    );

                CREATE TABLE IF NOT EXISTS connector_delivery_receipts (
                    event_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proof_json TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(event_id, endpoint_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id)
                );

                CREATE TABLE IF NOT EXISTS frontend_message_events (
                    message_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS frontend_transport_references (
                    connector_id TEXT NOT NULL,
                    endpoint_id TEXT NOT NULL,
                    transport_message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(connector_id, endpoint_id, transport_message_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id) ON DELETE CASCADE,
                    FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS frontend_transport_references_session
                    ON frontend_transport_references(session_id, owner_id, event_id);

                CREATE TABLE IF NOT EXISTS session_attachments (
                    attachment_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'staged',
                    semantic_role TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER,
                    retention_seconds INTEGER,
                    retention_indefinite INTEGER NOT NULL DEFAULT 0,
                    upload_required INTEGER NOT NULL DEFAULT 0,
                    asset_id TEXT,
                    uploaded_at TEXT,
                    created_at TEXT NOT NULL,
                    committed_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );

                CREATE TABLE IF NOT EXISTS attachment_stage_idempotency (
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, owner_id, idempotency_key),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(attachment_id) REFERENCES session_attachments(attachment_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS run_audio_assets (
                    run_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'input',
                    lease_released INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    released_at TEXT,
                    PRIMARY KEY(run_id, asset_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(attachment_id) REFERENCES session_attachments(attachment_id)
                );

                CREATE TABLE IF NOT EXISTS run_output_attachments (
                    run_id TEXT NOT NULL,
                    attachment_id TEXT NOT NULL,
                    output_index INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    publication_id TEXT,
                    group_index INTEGER NOT NULL,
                    request_digest TEXT NOT NULL,
                    caption TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, attachment_id),
                    UNIQUE(run_id, output_index),
                    UNIQUE(run_id, idempotency_key, group_index),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(attachment_id) REFERENCES session_attachments(attachment_id)
                );
                CREATE TABLE IF NOT EXISTS run_deliverable_publications (
                    run_id TEXT NOT NULL,
                    publication_id TEXT NOT NULL,
                    publication_digest TEXT NOT NULL,
                    attachment_digest TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    message_id TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, publication_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id)
                );

                CREATE TABLE IF NOT EXISTS voice_transcripts (
                    transcript_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    message_id TEXT,
                    attachment_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    safe_voice_state TEXT NOT NULL DEFAULT 'released',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(message_id) REFERENCES messages(message_id),
                    FOREIGN KEY(attachment_id) REFERENCES session_attachments(attachment_id)
                );

                CREATE TABLE IF NOT EXISTS runtime_event_correlations (
                    source_event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(event_id) REFERENCES run_events(event_id)
                );

                CREATE TABLE IF NOT EXISTS run_approvals (
                    approval_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    scope_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    decision TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS live_call_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    instance_generation TEXT NOT NULL,
                    context_generation INTEGER NOT NULL,
                    request_digest TEXT NOT NULL,
                    phone_config_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'reserved',
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    provider_id TEXT,
                    call_id TEXT,
                    outcome_json TEXT,
                    cleanup_state TEXT NOT NULL DEFAULT 'none',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS live_call_attempts_session
                    ON live_call_attempts(session_id, created_at);

                CREATE TABLE IF NOT EXISTS live_calls (
                    call_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    instance_generation TEXT NOT NULL,
                    context_generation INTEGER NOT NULL,
                    call_epoch INTEGER NOT NULL DEFAULT 1,
                    provider_session_id TEXT NOT NULL,
                    phase TEXT NOT NULL DEFAULT 'connecting',
                    foreground INTEGER NOT NULL DEFAULT 1,
                    controller_lease TEXT NOT NULL,
                    lease_expiry TEXT NOT NULL,
                    latest_session_event_sequence INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    max_ends_at TEXT NOT NULL,
                    ended_at TEXT,
                    provider_close_state TEXT NOT NULL DEFAULT 'pending',
                    usage_json TEXT,
                    phone_config_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS live_calls_owner_agent_active
                    ON live_calls(owner_id, agent_id, phase);
                CREATE TABLE IF NOT EXISTS live_foreground_inbox (
                    inbox_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL,
                    source_session_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(call_id, source_message_id),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id),
                    FOREIGN KEY(source_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(source_message_id) REFERENCES messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS live_foreground_inbox_pending
                    ON live_foreground_inbox(call_id, state, created_at);
                CREATE TABLE IF NOT EXISTS live_foreground_event_inbox (
                    inbox_id TEXT PRIMARY KEY,
                    call_id TEXT NOT NULL,
                    source_session_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(call_id, source_event_id),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id),
                    FOREIGN KEY(source_session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY(source_event_id) REFERENCES run_events(event_id)
                );
                CREATE INDEX IF NOT EXISTS live_foreground_event_inbox_pending
                    ON live_foreground_event_inbox(call_id, state, created_at);
                CREATE TABLE IF NOT EXISTS live_delegations (
                    call_id TEXT NOT NULL,
                    call_epoch INTEGER NOT NULL,
                    delegation_id TEXT NOT NULL,
                    offset_ms INTEGER NOT NULL,
                    after_ms INTEGER NOT NULL DEFAULT 0,
                    cutoff_ms INTEGER NOT NULL,
                    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
                    proposal_version INTEGER NOT NULL DEFAULT 1,
                    proposal_digest TEXT NOT NULL DEFAULT '',
                    proposal_text TEXT NOT NULL DEFAULT '',
                    ambiguous INTEGER NOT NULL DEFAULT 0,
                    proposal_state TEXT NOT NULL DEFAULT 'pending',
                    proposal_ready_after TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decision TEXT,
                    decision_key TEXT,
                    decision_digest TEXT,
                    accepted_message_id TEXT,
                    accepted_run_id TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    PRIMARY KEY(call_id, call_epoch, delegation_id),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id)
                );

                CREATE TABLE IF NOT EXISTS live_actions (
                    call_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    delegation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    request TEXT NOT NULL,
                    target_action_id TEXT,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    receipt TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(call_id, action_id),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id)
                );
                CREATE INDEX IF NOT EXISTS live_actions_delegation
                    ON live_actions(call_id, delegation_id);

                CREATE TABLE IF NOT EXISTS live_control_receipts (
                    call_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(call_id, idempotency_key),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id)
                );

                CREATE TABLE IF NOT EXISTS live_provider_delegation_inbox (
                    owner_id TEXT NOT NULL,
                    provider_event_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    call_epoch INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    delegation_id TEXT NOT NULL,
                    offset_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, session_id, call_id, call_epoch, provider_event_id),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS live_provider_delegation_inbox_pending
                    ON live_provider_delegation_inbox(call_id, call_epoch, created_at);
                CREATE TABLE IF NOT EXISTS live_provider_fragment_inbox (
                    owner_id TEXT NOT NULL,
                    provider_event_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    call_epoch INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    text TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, session_id, call_id, call_epoch, provider_event_id),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS live_provider_fragment_inbox_pending
                    ON live_provider_fragment_inbox(call_id, call_epoch, created_at);
                CREATE TABLE IF NOT EXISTS live_provider_event_inbox (
                    queue_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    provider_event_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    call_epoch INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL CHECK(event_type IN ('transcript', 'delegation')),
                    speaker TEXT,
                    text TEXT,
                    start_ms INTEGER,
                    end_ms INTEGER,
                    delegation_id TEXT,
                    offset_ms INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_id, session_id, call_id, call_epoch, provider_event_id),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS live_provider_event_inbox_pending
                    ON live_provider_event_inbox(call_id, call_epoch, queue_sequence);
                CREATE TABLE IF NOT EXISTS live_fragments (
                    owner_id TEXT NOT NULL,
                    provider_event_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    call_epoch INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id, session_id, call_id, call_epoch, provider_event_id),
                    FOREIGN KEY(call_id) REFERENCES live_calls(call_id),
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                );
                CREATE INDEX IF NOT EXISTS live_fragments_call_epoch
                    ON live_fragments(call_id, call_epoch, start_ms);
                """
            )
            from orchestrator.phone_context_handoff import initialize_schema as initialize_phone_context_schema
            initialize_phone_context_schema(connection)
            # Upgrade older split inboxes into one sequence so transcript and
            # delegation replay keeps the Provider's per-call receive order.
            connection.execute(
                """
                INSERT OR IGNORE INTO live_provider_event_inbox(
                    owner_id, provider_event_id, call_id, call_epoch, session_id,
                    event_type, speaker, text, start_ms, end_ms, delegation_id,
                    offset_ms, created_at
                )
                SELECT owner_id, provider_event_id, call_id, call_epoch, session_id,
                       event_type, speaker, text, start_ms, end_ms, delegation_id,
                       offset_ms, created_at
                FROM (
                    SELECT owner_id, provider_event_id, call_id, call_epoch, session_id,
                           'transcript' AS event_type, speaker, text, start_ms, end_ms,
                           NULL AS delegation_id, NULL AS offset_ms, created_at
                    FROM live_provider_fragment_inbox
                    UNION ALL
                    SELECT owner_id, provider_event_id, call_id, call_epoch, session_id,
                           'delegation' AS event_type, NULL AS speaker, NULL AS text,
                           NULL AS start_ms, NULL AS end_ms, delegation_id, offset_ms,
                           created_at
                    FROM live_provider_delegation_inbox
                )
                ORDER BY created_at, call_id, call_epoch, provider_event_id
                """
            )
            connection.execute("DELETE FROM live_provider_fragment_inbox")
            connection.execute("DELETE FROM live_provider_delegation_inbox")
            attempt_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(live_call_attempts)"
                ).fetchall()
            }
            if "instance_generation" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE live_call_attempts ADD COLUMN "
                    "instance_generation TEXT NOT NULL DEFAULT '1'"
                )
            if "phone_config_json" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE live_call_attempts ADD COLUMN "
                    "phone_config_json TEXT NOT NULL DEFAULT '{}'"
                )
            call_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(live_calls)").fetchall()
            }
            foreground_migration_needed = "foreground" not in call_columns
            for column, declaration in {
                "max_ends_at": "TEXT",
                "provider_close_state": "TEXT NOT NULL DEFAULT 'pending'",
                "usage_json": "TEXT",
                "phone_config_json": "TEXT NOT NULL DEFAULT '{}'",
                "termination_initiator": "TEXT",
                "termination_reason": "TEXT",
                "foreground": "INTEGER NOT NULL DEFAULT 1",
            }.items():
                if column not in call_columns:
                    connection.execute(
                        f"ALTER TABLE live_calls ADD COLUMN {column} {declaration}"
                    )
            delegation_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(live_delegations)"
                ).fetchall()
            }
            for column, declaration in {
                "proposal_state": "TEXT NOT NULL DEFAULT 'ready'",
                "proposal_ready_after": "TEXT",
                "decision_key": "TEXT",
                "decision_digest": "TEXT",
            }.items():
                if column not in delegation_columns:
                    connection.execute(
                        f"ALTER TABLE live_delegations ADD COLUMN {column} {declaration}"
                    )
            fragment_info = connection.execute(
                "PRAGMA table_info(live_fragments)"
            ).fetchall()
            fragment_columns = {str(row["name"]) for row in fragment_info}
            fragment_pk = tuple(
                str(row["name"])
                for row in sorted(fragment_info, key=lambda item: int(item["pk"]))
                if int(row["pk"])
            )
            expected_fragment_pk = (
                "owner_id", "session_id", "call_id", "call_epoch", "provider_event_id"
            )
            if "text" in fragment_columns or fragment_pk != expected_fragment_pk:
                connection.execute("ALTER TABLE live_fragments RENAME TO live_fragments_v15")
                connection.executescript(
                    """
                    CREATE TABLE live_fragments (
                        owner_id TEXT NOT NULL,
                        provider_event_id TEXT NOT NULL,
                        call_id TEXT NOT NULL,
                        call_epoch INTEGER NOT NULL,
                        session_id TEXT NOT NULL,
                        speaker TEXT NOT NULL,
                        start_ms INTEGER NOT NULL,
                        end_ms INTEGER NOT NULL,
                        event_id TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(owner_id, session_id, call_id, call_epoch, provider_event_id),
                        FOREIGN KEY(call_id) REFERENCES live_calls(call_id),
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                    );
                    INSERT OR IGNORE INTO live_fragments(
                        owner_id, provider_event_id, call_id, call_epoch, session_id,
                        speaker, start_ms, end_ms, event_id, sequence, created_at
                    )
                    SELECT calls.owner_id, old.provider_event_id, old.call_id,
                           old.call_epoch, old.session_id, old.speaker, old.start_ms,
                           old.end_ms, old.event_id, old.sequence, old.created_at
                    FROM live_fragments_v15 AS old
                    JOIN live_calls AS calls ON calls.call_id = old.call_id
                    WHERE old.event_id IS NOT NULL AND old.sequence IS NOT NULL;
                    DROP TABLE live_fragments_v15;
                    CREATE INDEX live_fragments_call_epoch
                        ON live_fragments(call_id, call_epoch, start_ms);
                    """
                )
            schema_version_row = connection.execute(
                "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
            ).fetchone()
            try:
                stored_schema_version = int(schema_version_row["value"]) if schema_version_row else 0
            except (TypeError, ValueError):
                stored_schema_version = 0
            if stored_schema_version < self.SCHEMA_VERSION:
                legacy_terminal_rows = connection.execute(
                    """SELECT rowid, * FROM live_calls
                       WHERE phase IN ('ended', 'failed', 'interrupted')"""
                ).fetchall()
                for row in legacy_terminal_rows:
                    explicit_user_hangup = (
                        str(row["termination_initiator"] or "") == "user"
                        and str(row["termination_reason"] or "") == "user_hangup"
                    )
                    if not explicit_user_hangup:
                        event_rows = connection.execute(
                            "SELECT detail_json FROM run_events WHERE session_id = ? "
                            "AND kind = 'voice.live.call.state'",
                            (str(row["session_id"]),),
                        ).fetchall()
                        for event_row in event_rows:
                            try:
                                detail = json.loads(event_row["detail_json"] or "{}")
                            except (TypeError, ValueError):
                                continue
                            call_scope = detail.get("scope") if isinstance(detail, Mapping) else None
                            if not isinstance(call_scope, Mapping) or str(call_scope.get("call_id") or "") != str(row["call_id"]):
                                continue
                            if ((detail.get("termination_initiator") == "user"
                                 and detail.get("termination_reason") == "user_hangup")
                                    or detail.get("reason") == "user_hangup"):
                                explicit_user_hangup = True
                                break
                    if explicit_user_hangup:
                        continue
                    legacy_phase = str(row["phase"])
                    prior_close_state = str(row["provider_close_state"] or "unconfirmed")
                    connection.execute(
                        """UPDATE live_calls
                           SET phase = 'recovering', ended_at = NULL,
                               provider_close_state = CASE WHEN provider_close_state = 'confirmed'
                                                           THEN 'confirmed' ELSE 'unconfirmed' END
                           WHERE rowid = ?""",
                        (int(row["rowid"]),),
                    )
                    self._append_event(
                        connection, session_id=str(row["session_id"]), run_id=None,
                        kind="voice.live.call.state",
                        summary="Legacy call retained for explicit recovery",
                        detail={
                            "scope": {
                                "instance_id": str(row["instance_id"]),
                                "instance_generation": str(row["instance_generation"]),
                                "agent_id": str(row["agent_id"]),
                                "session_id": str(row["session_id"]),
                                "context_generation": int(row["context_generation"]),
                                "call_id": str(row["call_id"]),
                                "call_epoch": int(row["call_epoch"]),
                            },
                            "phase": "recovering",
                            "reason": "legacy_terminal_recovery",
                            "legacy_phase": legacy_phase,
                            "provider_close_state": prior_close_state,
                        },
                    )
            # A pre-qualified prototype could leave multiple live calls per owner.
            # Keep one foreground call and preserve every other call as recoverable
            # background state before installing the owner-level foreground invariant.
            active_live_rows = connection.execute(
                """SELECT rowid, * FROM live_calls
                WHERE phase IN ('connecting', 'active', 'ending', 'recovering')
                  AND (? = 1 OR foreground = 1)
                ORDER BY owner_id, foreground DESC,
                    CASE WHEN phase IN ('connecting', 'active', 'ending') THEN 0 ELSE 1 END,
                    started_at DESC, rowid DESC""",
                (int(foreground_migration_needed),),
            ).fetchall()
            active_live_keys: set[str] = set()
            for row in active_live_rows:
                key = str(row["owner_id"])
                if key in active_live_keys:
                    connection.execute(
                        """UPDATE live_calls
                        SET phase = 'recovering', ended_at = NULL,
                            foreground = 0, provider_close_state = 'unconfirmed'
                        WHERE rowid = ?""",
                        (int(row["rowid"]),),
                    )
                    self._append_event(
                        connection, session_id=str(row["session_id"]), run_id=None,
                        kind="voice.live.call.state",
                        summary="Older live call retained for explicit recovery",
                        detail={
                            "scope": {
                                "instance_id": str(row["instance_id"]),
                                "instance_generation": str(row["instance_generation"]),
                                "agent_id": str(row["agent_id"]),
                                "session_id": str(row["session_id"]),
                                "context_generation": int(row["context_generation"]),
                                "call_id": str(row["call_id"]),
                                "call_epoch": int(row["call_epoch"]),
                            },
                            "phase": "recovering",
                            "reason": "owner_singleton_migration",
                            "foreground": False,
                        },
                    )
                else:
                    active_live_keys.add(key)
                    if foreground_migration_needed:
                        connection.execute(
                            "UPDATE live_calls SET foreground = 1 WHERE rowid = ?",
                            (int(row["rowid"]),),
                        )
            connection.execute("DROP INDEX IF EXISTS one_live_call_per_owner_agent")
            foreground_index = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' "
                "AND name = 'one_live_call_per_owner'"
            ).fetchone()
            if foreground_index and "foreground" not in str(foreground_index["sql"] or "").lower():
                connection.execute("DROP INDEX one_live_call_per_owner")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_live_call_per_owner "
                "ON live_calls(owner_id) "
                "WHERE foreground = 1 AND phase IN ('connecting', 'active', 'ending', 'recovering')"
            )
            consumer_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(event_consumers)"
                ).fetchall()
            }
            if "issued_through_sequence" not in consumer_columns:
                connection.execute(
                    "ALTER TABLE event_consumers ADD COLUMN "
                    "issued_through_sequence INTEGER NOT NULL DEFAULT 0"
                )
            outbox_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(delivery_outbox)"
                ).fetchall()
            }
            outbox_migrations = {
                "lease_owner": "TEXT",
                "lease_token": "TEXT",
                "lease_expires_at": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "last_error_code": "TEXT",
                "completed_at": "TEXT",
                "last_claim_token": "TEXT",
                "last_claim_status": "TEXT",
            }
            for column, declaration in outbox_migrations.items():
                if column not in outbox_columns:
                    connection.execute(
                        f"ALTER TABLE delivery_outbox ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS delivery_outbox_claimable "
                "ON delivery_outbox(session_id, state, created_at)"
            )
            attachment_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(session_attachments)"
                ).fetchall()
            }
            attachment_migrations = {
                "semantic_role": "TEXT NOT NULL DEFAULT ''",
                "duration_ms": "INTEGER",
                "retention_seconds": "INTEGER",
                "retention_indefinite": "INTEGER NOT NULL DEFAULT 0",
                "upload_required": "INTEGER NOT NULL DEFAULT 0",
                "asset_id": "TEXT",
                "uploaded_at": "TEXT",
            }
            for column, declaration in attachment_migrations.items():
                if column not in attachment_columns:
                    connection.execute(
                        f"ALTER TABLE session_attachments ADD COLUMN {column} {declaration}"
                    )
            output_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(run_output_attachments)"
                ).fetchall()
            }
            if "publication_id" not in output_columns:
                connection.execute(
                    "ALTER TABLE run_output_attachments ADD COLUMN publication_id TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS run_output_publications "
                "ON run_output_attachments(run_id, publication_id)"
            )
            run_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "response_preferences_json" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN "
                    "response_preferences_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "message_context_json" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN "
                    "message_context_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "delivery_route_json" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN "
                    "delivery_route_json TEXT NOT NULL DEFAULT '{}'"
                )
            message_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "message_context_json" not in message_columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN "
                    "message_context_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "display_text" not in message_columns:
                connection.execute(
                    "ALTER TABLE messages ADD COLUMN display_text TEXT"
                )
            session_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "workzone_revision" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN "
                    "workzone_revision INTEGER NOT NULL DEFAULT 0"
                )
            if "history_generation" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN "
                    "history_generation INTEGER NOT NULL DEFAULT 1"
                )
            if "session_kind" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN "
                    "session_kind TEXT NOT NULL DEFAULT 'conversation'"
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS one_active_agent_activity_session "
                "ON sessions(instance_id, owner_id, agent_id, session_kind) "
                "WHERE session_kind = 'agent_activity' AND status = 'active'"
            )
            # One-time compatibility projection.  The former scalar Workzone
            # becomes the enabled ``main`` slot without changing the Session's
            # effective working directory.
            connection.execute(
                """
                INSERT OR IGNORE INTO session_workzones(
                    session_id, slot_id, path, enabled, label, created_at, updated_at
                )
                SELECT session_id, 'main', workzone, 1, '', created_at, updated_at
                FROM sessions
                WHERE workzone IS NOT NULL AND TRIM(workzone) != ''
                """
            )
            # Schema 7 originally recorded only the transfer that materialized
            # an origin.  Preserve those installations as the first claimant
            # when opening a database created by an earlier qualified build.
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_continuity_origin_claims(
                    transfer_id, owner_id, agent_id, origin_ref, claimed_at
                )
                SELECT transfer_id, owner_id, agent_id, origin_ref, imported_at
                FROM conversation_continuity_imports
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES('schema_version', ?)",
                (str(self.SCHEMA_VERSION),),
            )

    @staticmethod
    def owner_id_for(global_config: Any, explicit: str | None = None) -> str:
        if explicit and str(explicit).strip():
            return str(explicit).strip()
        return f"user:{int(getattr(global_config, 'authorized_id', 0) or 0)}"

    @staticmethod
    def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["is_default"] = bool(result.get("is_default"))
        return result

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["content"] = json.loads(result.pop("content_json") or "[]")
        if "message_context_json" in result:
            result["message_context"] = _json_object(
                result.pop("message_context_json")
            )
        result["history_eligible"] = bool(result.get("history_eligible"))
        return result

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        if "response_preferences_json" in result:
            result["response_preferences"] = _json_object(
                result.pop("response_preferences_json")
            )
        if "message_context_json" in result:
            result["message_context"] = _json_object(
                result.pop("message_context_json")
            )
        if "delivery_route_json" in result:
            result["delivery_route"] = _json_object(
                result.pop("delivery_route_json")
            )
        return result

    def _next_ordinal(self, connection: sqlite3.Connection, session_id: str) -> int:
        row = connection.execute(
            """UPDATE sessions
            SET next_message_ordinal = next_message_ordinal + 1
            WHERE session_id = ?
            RETURNING next_message_ordinal - 1 AS ordinal""",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return int(row["ordinal"])

    def _queue_foreground_message(
        self, connection: sqlite3.Connection, *, session_id: str, message_id: str
    ) -> None:
        """Route canonical visible conversation messages to the active phone inbox."""
        row = connection.execute(
            """SELECT m.role, m.source, m.text, m.visibility, m.history_eligible,
                      m.message_context_json, m.run_id,
                      r.message_context_json AS run_message_context_json,
                      s.owner_id, s.agent_id
               FROM messages AS m JOIN sessions AS s ON s.session_id = m.session_id
               LEFT JOIN runs AS r ON r.run_id = m.run_id
               WHERE m.message_id = ? AND m.session_id = ?""",
            (str(message_id), str(session_id)),
        ).fetchone()
        if row is None or row["role"] not in {"user", "assistant"}:
            return
        if str(row["source"] or "") == "live-phone" or row["visibility"] != "visible":
            return
        if not bool(row["history_eligible"]):
            return
        if not str(row["text"] or "").strip():
            return
        context = _json_object(row["message_context_json"])
        run_context = _json_object(row["run_message_context_json"])
        if (
            context.get("live_voice")
            or context.get("live_call_record")
            or context.get("presentation_only")
            or run_context.get("live_voice")
        ):
            return
        calls = connection.execute(
            """SELECT call_id FROM live_calls
               WHERE owner_id = ? AND foreground = 1
                 AND phase IN ('connecting', 'active', 'ending', 'recovering')
               ORDER BY CASE WHEN phase IN ('connecting', 'active', 'ending') THEN 0 ELSE 1 END,
                        started_at DESC, rowid DESC
               LIMIT 1""",
            (str(row["owner_id"]),),
        ).fetchall()
        now = _utc_now()
        for call in calls:
            connection.execute(
                """INSERT OR IGNORE INTO live_foreground_inbox(
                       inbox_id, call_id, source_session_id, source_message_id,
                       state, created_at
                   ) VALUES (?, ?, ?, ?, 'pending', ?)""",
                (_new_id("fg"), str(call["call_id"]), str(session_id), str(message_id), now),
            )

    def _queue_foreground_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        event_id: str,
        kind: str,
        run_id: str | None,
    ) -> None:
        """Route PAO activity events (including failures) without copying event payloads."""
        normalized_kind = str(kind or "")
        if not (normalized_kind.startswith("run.") or normalized_kind.startswith("assistant.output.")):
            return
        if normalized_kind == "run.completed":
            return
        source = connection.execute(
            """SELECT owner_id, agent_id FROM sessions WHERE session_id = ?""",
            (str(session_id),),
        ).fetchone()
        if source is None:
            return
        if run_id:
            run = connection.execute(
                "SELECT message_context_json FROM runs WHERE run_id = ? AND session_id = ?",
                (str(run_id), str(session_id)),
            ).fetchone()
            if run is not None and _json_object(run["message_context_json"]).get("live_voice"):
                return
        calls = connection.execute(
            """SELECT call_id FROM live_calls
               WHERE owner_id = ? AND foreground = 1
                 AND phase IN ('connecting', 'active', 'ending', 'recovering')
               ORDER BY CASE WHEN phase IN ('connecting', 'active', 'ending') THEN 0 ELSE 1 END,
                        started_at DESC, rowid DESC
               LIMIT 1""",
            (str(source["owner_id"]),),
        ).fetchall()
        now = _utc_now()
        for call in calls:
            connection.execute(
                """INSERT OR IGNORE INTO live_foreground_event_inbox(
                       inbox_id, call_id, source_session_id, source_event_id, state, created_at
                   ) VALUES (?, ?, ?, ?, 'pending', ?)""",
                (_new_id("fge"), str(call["call_id"]), str(session_id), str(event_id), now),
            )

    def resolve_live_voice_origin(
        self,
        *,
        owner_id: str,
        session_id: str,
        agent_id: str,
        context_generation: int,
        candidate: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Validate and normalize an internal Live delegation origin marker."""

        value = dict(candidate or {})
        try:
            call_id = str(value["call_id"])
            call_epoch = int(value["call_epoch"])
            delegation_id = str(value["delegation_id"])
            proposal_version = int(value["proposal_version"])
            proposal_digest = str(value["proposal_digest"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionConflict("live_voice_origin_invalid") from exc
        if not all((call_id, delegation_id, proposal_digest)):
            raise SessionConflict("live_voice_origin_invalid")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT c.call_id
                   FROM live_calls AS c
                   JOIN live_delegations AS d
                     ON d.call_id = c.call_id AND d.call_epoch = c.call_epoch
                   WHERE c.call_id = ? AND c.call_epoch = ?
                     AND c.owner_id = ? AND c.session_id = ? AND c.agent_id = ?
                     AND c.context_generation = ?
                     AND c.phase IN ('connecting', 'active', 'ending', 'recovering')
                     AND d.delegation_id = ? AND d.proposal_version = ?
                     AND d.proposal_digest = ?
                     AND d.decision IN ('admitting', 'admitted')""",
                (
                    call_id,
                    call_epoch,
                    str(owner_id),
                    str(session_id),
                    str(agent_id).lower(),
                    int(context_generation),
                    delegation_id,
                    proposal_version,
                    proposal_digest,
                ),
            ).fetchone()
        if row is None:
            raise SessionConflict("live_voice_origin_invalid")
        return {
            "call_id": call_id,
            "call_epoch": call_epoch,
            "delegation_id": delegation_id,
            "proposal_version": proposal_version,
            "proposal_digest": proposal_digest,
        }

    def stage_live_provider_fragment(
        self, *, owner_id: str, provider_event_id: str, call_id: str,
        call_epoch: int, session_id: str, speaker: str, text: str,
        start_ms: int, end_ms: int,
    ) -> None:
        """Durably stage a transcript in the shared Provider event order."""
        identity = (
            str(owner_id), str(session_id), str(call_id), int(call_epoch),
            str(provider_event_id),
        )
        values = (
            str(speaker), str(text), int(start_ms), int(end_ms),
        )
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                """SELECT 1 FROM live_fragments
                   WHERE owner_id = ? AND session_id = ? AND call_id = ?
                     AND call_epoch = ? AND provider_event_id = ?""",
                identity,
            ).fetchone()
            if existing is not None:
                return
            existing = connection.execute(
                """SELECT event_type, speaker, text, start_ms, end_ms
                   FROM live_provider_event_inbox
                   WHERE owner_id = ? AND session_id = ? AND call_id = ?
                     AND call_epoch = ? AND provider_event_id = ?""",
                identity,
            ).fetchone()
            if existing is not None:
                if tuple(existing) != ("transcript", *values):
                    raise SessionConflict("provider transcript identity conflicts with pending fragment")
                return
            connection.execute(
                """INSERT INTO live_provider_event_inbox(
                       owner_id, provider_event_id, call_id, call_epoch, session_id,
                       event_type, speaker, text, start_ms, end_ms, created_at
                   ) VALUES (?, ?, ?, ?, ?, 'transcript', ?, ?, ?, ?, ?)""",
                (*identity[:1], identity[4], identity[2], identity[3], identity[1],
                 *values, _utc_now()),
            )

    def pending_live_provider_fragments(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT i.*, c.agent_id, c.instance_id, c.instance_generation,
                          c.context_generation, c.provider_session_id
                   FROM live_provider_event_inbox AS i
                   JOIN live_calls AS c ON c.call_id = i.call_id
                   WHERE i.event_type = 'transcript'
                   ORDER BY i.queue_sequence"""
            ).fetchall()
        return [dict(row) for row in rows]

    def stage_live_provider_delegation(
        self, *, owner_id: str, provider_event_id: str, call_id: str,
        call_epoch: int, session_id: str, delegation_id: str, offset_ms: int,
    ) -> None:
        """Durably stage a typed delegation in the shared Provider event order."""
        identity = (
            str(owner_id), str(session_id), str(call_id), int(call_epoch),
            str(provider_event_id),
        )
        values = (str(delegation_id), int(offset_ms))
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                """SELECT event_type, delegation_id, offset_ms
                   FROM live_provider_event_inbox
                   WHERE owner_id = ? AND session_id = ? AND call_id = ?
                     AND call_epoch = ? AND provider_event_id = ?""",
                identity,
            ).fetchone()
            if existing is not None:
                if tuple(existing) != ("delegation", *values):
                    raise SessionConflict("provider delegation identity conflicts with pending event")
                return
            connection.execute(
                """INSERT INTO live_provider_event_inbox(
                       owner_id, provider_event_id, call_id, call_epoch, session_id,
                       event_type, delegation_id, offset_ms, created_at
                   ) VALUES (?, ?, ?, ?, ?, 'delegation', ?, ?, ?)""",
                (*identity[:1], identity[4], identity[2], identity[3], identity[1],
                 *values, _utc_now()),
            )

    def pending_live_provider_delegations(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT i.*, c.agent_id, c.instance_id, c.instance_generation,
                          c.context_generation, c.provider_session_id
                   FROM live_provider_event_inbox AS i
                   JOIN live_calls AS c ON c.call_id = i.call_id
                   WHERE i.event_type = 'delegation'
                   ORDER BY i.queue_sequence"""
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_live_provider_events(self) -> list[dict[str, Any]]:
        """Return all staged typed Provider events in their durable receive order."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT i.*, c.agent_id, c.instance_id, c.instance_generation,
                          c.context_generation, c.provider_session_id
                   FROM live_provider_event_inbox AS i
                   JOIN live_calls AS c ON c.call_id = i.call_id
                   ORDER BY i.queue_sequence"""
            ).fetchall()
        return [dict(row) for row in rows]

    def has_pending_live_provider_fragments(self, call_id: str) -> bool:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM live_provider_event_inbox WHERE call_id = ? AND event_type = 'transcript' LIMIT 1",
                (str(call_id),),
            ).fetchone()
        return row is not None

    def clear_staged_live_provider_fragment(
        self, *, owner_id: str, provider_event_id: str, call_id: str,
        call_epoch: int, session_id: str,
    ) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """DELETE FROM live_provider_event_inbox
                   WHERE owner_id = ? AND session_id = ? AND call_id = ?
                     AND call_epoch = ? AND provider_event_id = ?""",
                (
                    str(owner_id), str(session_id), str(call_id),
                    int(call_epoch), str(provider_event_id),
                ),
            )

    def pending_live_foreground_events(
        self, call_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT i.inbox_id, i.call_id, i.source_session_id, i.source_event_id,
                          e.kind, e.status, e.phase, e.summary, e.created_at
                   FROM live_foreground_event_inbox AS i
                   JOIN run_events AS e ON e.event_id = i.source_event_id
                   WHERE i.call_id = ? AND i.state = 'pending'
                   ORDER BY i.created_at, i.inbox_id LIMIT ?""",
                (str(call_id), bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_live_foreground_event_delivered(
        self, call_id: str, inbox_id: str
    ) -> bool:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE live_foreground_event_inbox
                   SET state = 'delivered', delivered_at = ?
                   WHERE call_id = ? AND inbox_id = ? AND state = 'pending'""",
                (_utc_now(), str(call_id), str(inbox_id)),
            )
            return cursor.rowcount == 1

    def live_foreground_history(self, call_id: str, *, limit: int = 256) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 512))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT i.created_at AS routed_at, i.source_session_id,
                          m.role, m.source, m.text AS content,
                          'message' AS item_type, '' AS event_kind
                   FROM live_foreground_inbox AS i
                   JOIN messages AS m ON m.message_id = i.source_message_id
                   WHERE i.call_id = ?
                   UNION ALL
                   SELECT i.created_at AS routed_at, i.source_session_id,
                          'assistant' AS role, e.kind AS source, e.summary AS content,
                          'event' AS item_type, e.kind AS event_kind
                   FROM live_foreground_event_inbox AS i
                   JOIN run_events AS e ON e.event_id = i.source_event_id
                   WHERE i.call_id = ?
                   ORDER BY routed_at, source_session_id LIMIT ?""",
                (str(call_id), str(call_id), bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_live_foreground_messages(
        self, call_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT i.inbox_id, i.call_id, i.source_session_id,
                          i.source_message_id, m.role, m.source, m.text, m.created_at
                   FROM live_foreground_inbox AS i
                   JOIN messages AS m ON m.message_id = i.source_message_id
                   WHERE i.call_id = ? AND i.state = 'pending'
                   ORDER BY i.created_at, i.inbox_id LIMIT ?""",
                (str(call_id), bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_live_foreground_items(
        self, call_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return one globally ordered page from both foreground inboxes."""
        bounded = max(1, min(int(limit), 100))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT * FROM (
                       SELECT 'message' AS item_type, i.inbox_id,
                              i.source_session_id, i.source_message_id,
                              '' AS source_event_id, m.role, m.source, '' AS kind,
                              m.text, '' AS summary, m.created_at
                       FROM live_foreground_inbox AS i
                       JOIN messages AS m ON m.message_id = i.source_message_id
                       WHERE i.call_id = ? AND i.state = 'pending'
                       UNION ALL
                       SELECT 'event' AS item_type, i.inbox_id,
                              i.source_session_id, '' AS source_message_id,
                              i.source_event_id, 'assistant' AS role, e.kind AS source,
                              e.kind, '' AS text, e.summary, e.created_at
                       FROM live_foreground_event_inbox AS i
                       JOIN run_events AS e ON e.event_id = i.source_event_id
                       WHERE i.call_id = ? AND i.state = 'pending'
                   )
                   ORDER BY created_at, inbox_id LIMIT ?""",
                (str(call_id), str(call_id), bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_live_foreground_message_delivered(
        self, call_id: str, inbox_id: str
    ) -> bool:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE live_foreground_inbox
                   SET state = 'delivered', delivered_at = ?
                   WHERE call_id = ? AND inbox_id = ? AND state = 'pending'""",
                (_utc_now(), str(call_id), str(inbox_id)),
            )
            return cursor.rowcount == 1

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        run_id: str | None,
        kind: str,
        status: str = "",
        phase: str = "",
        summary: str = "",
        detail: Mapping[str, Any] | None = None,
        outbox: bool = False,
        delivery_route: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = connection.execute(
            """UPDATE sessions
            SET next_event_sequence = next_event_sequence + 1
            WHERE session_id = ?
            RETURNING next_event_sequence - 1 AS sequence""",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        sequence = int(row["sequence"])
        event_id = _new_id("evt")
        created_at = _utc_now()
        connection.execute(
            """
            INSERT INTO run_events(
                event_id, session_id, run_id, sequence, kind, status, phase,
                summary, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                session_id,
                run_id,
                sequence,
                kind,
                status or None,
                phase or None,
                summary,
                _json(dict(detail or {})),
                created_at,
            ),
        )
        self._queue_foreground_event(
            connection,
            session_id=session_id,
            event_id=event_id,
            kind=kind,
            run_id=run_id,
        )
        if outbox:
            connection.execute(
                """
                INSERT INTO delivery_outbox(
                    outbox_id, session_id, run_id, event_id, state, created_at
                ) VALUES (?, ?, ?, ?, 'delegated', ?)
                """,
                (_new_id("out"), session_id, run_id, event_id, created_at),
            )
            resolved_route = dict(delivery_route or {})
            route_was_resolved = delivery_route is not None
            if not resolved_route and run_id is not None:
                run_row = connection.execute(
                    "SELECT delivery_route_json FROM runs WHERE run_id=?",
                    (str(run_id),),
                ).fetchone()
                if run_row is not None:
                    resolved_route = _json_object(
                        run_row["delivery_route_json"] or "{}"
                    )
                    route_was_resolved = bool(resolved_route)
            destinations: list[dict[str, Any]] = []
            invalid_route = False
            if resolved_route:
                try:
                    from orchestrator.frontend_delivery import (
                        delivery_intent_from_run_route,
                    )

                    intent = delivery_intent_from_run_route(
                        resolved_route,
                        event_id=event_id,
                        session_id=session_id,
                        idempotency_key=f"{event_id}:{kind}",
                        content_modes=(
                            tuple(
                                str(item).strip().casefold()
                                for item in (detail or {}).get(
                                    "content_modes", ()
                                )
                                if str(item).strip()
                            )
                            or (
                                ("text", "media")
                                if kind == "assistant.output.available"
                                else ("text", "card")
                            )
                        ),
                    )
                    destinations = [
                        dict(item) for item in intent.get("destinations", ())
                    ]
                except ValueError:
                    invalid_route = True
            if not destinations and not route_was_resolved:
                from orchestrator.frontend_connector_registry import endpoint_id_for

                destinations = [
                    {
                        "connector_id": "session_api",
                        "endpoint_id": endpoint_id_for(
                            "session_api",
                            ingress_transport="session-api",
                            channel_key="default",
                        ),
                        "role": "primary",
                        "content_modes": ["text", "card", "media"],
                        "retry_class": "query_before_retry",
                    }
                ]
            for destination in destinations:
                connection.execute(
                    """
                    INSERT INTO connector_delivery_tasks(
                        task_id, session_id, run_id, event_id, connector_id,
                        endpoint_id, role, content_modes_json, retry_class,
                        state, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    ON CONFLICT(event_id, endpoint_id) DO NOTHING
                    """,
                    (
                        _new_id("delivery"),
                        session_id,
                        run_id,
                        event_id,
                        str(destination["connector_id"]),
                        str(destination["endpoint_id"]),
                        str(destination.get("role") or "primary"),
                        _json(list(destination.get("content_modes") or [])),
                        str(
                            destination.get("retry_class")
                            or "query_before_retry"
                        ),
                        created_at,
                    ),
                    )
            if invalid_route:
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET state='failed', completed_at=?,
                        last_error_code='invalid_delivery_route'
                    WHERE event_id=?
                    """,
                    (created_at, event_id),
                )
            elif not destinations:
                connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET state='suppressed', completed_at=?
                    WHERE event_id=?
                    """,
                    (created_at, event_id),
                )
        return {
            "event_id": event_id,
            "session_id": session_id,
            "run_id": run_id,
            "sequence": sequence,
            "kind": kind,
            "status": status or None,
            "phase": phase or None,
            "summary": summary,
            "detail": dict(detail or {}),
            "created_at": created_at,
        }

    def reserve_frontend_command_invocation(
        self,
        *,
        session_id: str,
        owner_id: str,
        client_id: str,
        request_id: str,
        request_digest: str,
        context_generation: int,
        invocation: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Durably fence one command before any command handler can mutate state."""

        identity = tuple(
            str(value or "").strip()
            for value in (session_id, owner_id, client_id, request_id)
        )
        if any(not value for value in identity):
            raise ValueError("command invocation identity is required")
        digest = str(request_digest or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("command invocation digest is invalid")
        encoded_invocation = _json(dict(invocation))
        if len(encoded_invocation.encode("utf-8")) > 32768:
            raise ValueError("command invocation exceeds the storage limit")
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT context_generation FROM sessions
                WHERE session_id=? AND owner_id=? AND deleted_at IS NULL
                """,
                (identity[0], identity[1]),
            ).fetchone()
            if session is None:
                raise SessionNotFound(identity[0])
            if int(session["context_generation"]) != int(context_generation):
                raise SessionConflict("session context changed before command admission")
            existing = connection.execute(
                """
                SELECT request_digest, state, response_json, event_id
                FROM frontend_command_invocations
                WHERE session_id=? AND client_id=? AND request_id=?
                """,
                (identity[0], identity[2], identity[3]),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise IdempotencyConflict(
                        "command request id is already bound to different content"
                    )
                if str(existing["state"]) == "completed":
                    response = json.loads(str(existing["response_json"] or "{}"))
                    return {
                        "state": "completed",
                        "response": response,
                        "event_id": existing["event_id"],
                        "replayed": True,
                    }
                return {"state": "pending", "replayed": True}
            connection.execute(
                """
                INSERT INTO frontend_command_invocations(
                    session_id, owner_id, client_id, request_id, request_digest,
                    invocation_json, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    identity[0], identity[1], identity[2], identity[3], digest,
                    encoded_invocation, now, now,
                ),
            )
        return {"state": "reserved", "replayed": False}

    def complete_frontend_command_invocation(
        self,
        *,
        session_id: str,
        owner_id: str,
        client_id: str,
        request_id: str,
        request_digest: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist a command result and its canonical Session event atomically."""

        identity = tuple(
            str(value or "").strip()
            for value in (session_id, owner_id, client_id, request_id)
        )
        if any(not value for value in identity):
            raise ValueError("command invocation identity is required")
        digest = str(request_digest or "").strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("command invocation digest is invalid")
        response_json = _json(dict(response))
        if len(response_json.encode("utf-8")) > 262144:
            raise ValueError("command result exceeds the storage limit")
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT request_digest, invocation_json, state, response_json, event_id
                FROM frontend_command_invocations
                WHERE session_id=? AND owner_id=? AND client_id=? AND request_id=?
                """,
                identity,
            ).fetchone()
            if row is None:
                raise SessionConflict("command invocation was not reserved")
            if str(row["request_digest"]) != digest:
                raise IdempotencyConflict(
                    "command request id is already bound to different content"
                )
            if str(row["state"]) == "completed":
                return {
                    "state": "completed",
                    "response": json.loads(str(row["response_json"] or "{}")),
                    "event_id": row["event_id"],
                    "replayed": True,
                }
            invocation = json.loads(str(row["invocation_json"]))
            event_status = "completed" if response.get("ok") is True else "failed"
            event = self._append_event(
                connection,
                session_id=identity[0],
                run_id=None,
                kind="frontend.command_result",
                status=event_status,
                phase="command",
                summary=f"/{str(invocation.get('command') or 'command')} result",
                detail={"command_invocation": invocation, "result": dict(response)},
            )
            connection.execute(
                """
                UPDATE frontend_command_invocations
                SET state='completed', response_json=?, event_id=?, updated_at=?
                WHERE session_id=? AND owner_id=? AND client_id=? AND request_id=?
                """,
                (response_json, event["event_id"], now, *identity),
            )
        return {
            "state": "completed",
            "response": dict(response),
            "event_id": event["event_id"],
            "replayed": False,
        }

    def create_session(
        self,
        *,
        owner_id: str,
        agent_id: str,
        title: str | None = None,
        is_default: bool = False,
        session_kind: str = SESSION_KIND_CONVERSATION,
    ) -> dict[str, Any]:
        owner_id = str(owner_id).strip()
        agent_id = str(agent_id).strip().lower()
        session_kind = str(session_kind or "").strip().casefold()
        if not owner_id or not agent_id:
            raise ValueError("owner_id and agent_id are required")
        if session_kind not in SESSION_KINDS:
            raise ValueError("unsupported Session kind")
        if is_default and session_kind != SESSION_KIND_CONVERSATION:
            raise ValueError("only a conversation Session can be the default")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if is_default:
                row = connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE instance_id = ? AND owner_id = ? AND agent_id = ? AND is_default = 1
                    """,
                    (self.instance_id, owner_id, agent_id),
                ).fetchone()
                if row is not None:
                    return self._session_dict(row)
            if session_kind == SESSION_KIND_AGENT_ACTIVITY:
                row = connection.execute(
                    """
                    SELECT * FROM sessions
                    WHERE instance_id = ? AND owner_id = ? AND agent_id = ?
                      AND session_kind = ? AND status = 'active'
                    """,
                    (
                        self.instance_id,
                        owner_id,
                        agent_id,
                        SESSION_KIND_AGENT_ACTIVITY,
                    ),
                ).fetchone()
                if row is not None:
                    return self._session_dict(row)
            session_id = _new_id("ses")
            now = _utc_now()
            resolved_title = str(
                title
                or (
                    f"{agent_id} activity"
                    if session_kind == SESSION_KIND_AGENT_ACTIVITY
                    else f"{agent_id} default"
                    if is_default
                    else "New session"
                )
            ).strip()
            connection.execute(
                """
                INSERT INTO sessions(
                    session_id, instance_id, owner_id, agent_id, title,
                    session_kind, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    self.instance_id,
                    owner_id,
                    agent_id,
                    resolved_title,
                    session_kind,
                    int(is_default),
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO session_participants(session_id, agent_id, created_at) VALUES (?, ?, ?)",
                (session_id, agent_id, now),
            )
            connection.execute(
                """
                INSERT INTO session_context_generations(session_id, generation, reason, created_at)
                VALUES (?, 1, 'session_created', ?)
                """,
                (session_id, now),
            )
            self._append_event(
                connection,
                session_id=session_id,
                run_id=None,
                kind="session.created",
                status="active",
                summary="Session created",
                detail={
                    "is_default": bool(is_default),
                    "agent_id": agent_id,
                    "session_kind": session_kind,
                },
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self._session_dict(row)

    def ensure_default_session(self, *, owner_id: str, agent_id: str) -> dict[str, Any]:
        return self.create_session(
            owner_id=owner_id,
            agent_id=agent_id,
            title=f"{str(agent_id).strip().lower()} default",
            is_default=True,
        )

    def ensure_agent_activity_session(
        self,
        *,
        owner_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        """Return the Agent-owned internal Session used for independent Runs."""

        return self.create_session(
            owner_id=owner_id,
            agent_id=agent_id,
            title=f"{str(agent_id).strip().lower()} activity",
            session_kind=SESSION_KIND_AGENT_ACTIVITY,
        )

    def get_session(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        agent_id: str | None = None,
        include_deleted: bool = True,
    ) -> dict[str, Any]:
        clauses = ["session_id = ?", "instance_id = ?"]
        params: list[Any] = [str(session_id), self.instance_id]
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(str(owner_id))
        if agent_id is not None:
            clauses.append("agent_id = ?")
            params.append(str(agent_id).lower())
        if not include_deleted:
            clauses.append("status != 'deleted'")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM sessions WHERE {' AND '.join(clauses)}", params
            ).fetchone()
        if row is None:
            raise SessionNotFound(str(session_id))
        return self._session_dict(row)

    def list_sessions(
        self,
        *,
        owner_id: str,
        agent_id: str | None = None,
        include_archived: bool = False,
        include_internal: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["instance_id = ?", "owner_id = ?", "status != 'deleted'"]
        params: list[Any] = [self.instance_id, str(owner_id)]
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(str(agent_id).lower())
        if not include_archived:
            clauses.append("status = 'active'")
        if not include_internal:
            clauses.append("session_kind = 'conversation'")
        params.append(max(1, min(int(limit), 500)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM sessions WHERE {" AND ".join(clauses)}
                ORDER BY is_default DESC, updated_at DESC, session_id ASC LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._session_dict(row) for row in rows]

    def set_memory_policy(
        self,
        session_id: str,
        *,
        owner_id: str,
        policy: str,
    ) -> dict[str, Any]:
        """Set the Session memory policy after an ownership check."""
        normalized = str(policy or "").strip().casefold()
        if normalized not in {"promote", "disabled"}:
            raise ValueError("memory policy must be promote or disabled")
        self.get_session(session_id, owner_id=owner_id, include_deleted=False)
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE sessions SET memory_policy=?, updated_at=? WHERE session_id=?",
                (normalized, _utc_now(), str(session_id)),
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
        return self._session_dict(row)

    def find_run_by_idempotency(
        self,
        *,
        session_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self.get_session(session_id, owner_id=owner_id, include_deleted=False)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT r.*, m.text AS user_text FROM runs AS r
                JOIN messages AS m ON m.message_id=r.user_message_id
                WHERE r.session_id=? AND r.idempotency_key=?
                """,
                (str(session_id), str(idempotency_key)),
            ).fetchone()
        return None if row is None else self._run_dict(row)

    def list_active_runs(
        self,
        *,
        owner_id: str,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = [
            "s.instance_id=?",
            "s.owner_id=?",
            "r.state NOT IN ('completed','failed','stopped','superseded','interrupted')",
        ]
        params: list[Any] = [self.instance_id, str(owner_id)]
        if session_id is not None:
            clauses.append("r.session_id=?")
            params.append(str(session_id))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.* FROM runs AS r
                JOIN sessions AS s ON s.session_id=r.session_id
                WHERE {" AND ".join(clauses)}
                ORDER BY r.created_at, r.run_id
                """,
                params,
            ).fetchall()
        return [self._run_dict(row) for row in rows]

    def purge_owner(
        self,
        *,
        owner_id: str,
        agent_id: str | None = None,
    ) -> dict[str, int]:
        """Permanently remove Session-owned demo data without archive/quarantine.

        This is intentionally lower level than ordinary Agent deletion. Callers
        must revoke execution and stop the owned Worker before invoking it.
        """
        owner = str(owner_id)
        agent = None if agent_id is None else str(agent_id).lower()
        removed_files: list[Path] = []
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            clauses = ["instance_id=?", "owner_id=?"]
            params: list[Any] = [self.instance_id, owner]
            if agent is not None:
                clauses.append("agent_id=?")
                params.append(agent)
            session_rows = connection.execute(
                f"SELECT session_id, agent_id FROM sessions WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()
            session_ids = [str(row["session_id"]) for row in session_rows]
            agent_ids = sorted({str(row["agent_id"]).lower() for row in session_rows})
            if agent is not None and agent not in agent_ids:
                agent_ids.append(agent)
            if not session_ids:
                for table in (
                    "conversation_continuity_imports",
                    "conversation_continuity_batches",
                    "conversation_continuity_origin_claims",
                    "conversation_continuity_retirements",
                ):
                    connection.execute(f"DELETE FROM {table} WHERE owner_id=?", (owner,))
                return {"sessions": 0, "attachments": 0}

            marks = ",".join("?" for _ in session_ids)
            attachments = connection.execute(
                f"""
                SELECT attachment_id, filename FROM session_attachments
                WHERE session_id IN ({marks})
                """,
                session_ids,
            ).fetchall()
            for row in attachments:
                removed_files.append(
                    self._attachment_file_path(
                        str(row["attachment_id"]), str(row["filename"])
                    )
                )

            run_rows = connection.execute(
                f"SELECT run_id FROM runs WHERE session_id IN ({marks})",
                session_ids,
            ).fetchall()
            run_ids = [str(row["run_id"]) for row in run_rows]
            run_marks = ",".join("?" for _ in run_ids)

            if run_ids:
                connection.execute(
                    f"DELETE FROM run_audio_assets WHERE run_id IN ({run_marks})",
                    run_ids,
                )
                connection.execute(
                    f"DELETE FROM run_output_attachments WHERE run_id IN ({run_marks})",
                    run_ids,
                )
                connection.execute(
                    f"DELETE FROM run_attempts WHERE run_id IN ({run_marks})",
                    run_ids,
                )
            for table in (
                "phone_context_handoffs",
                "phone_context_consumption",
                "voice_transcripts",
                "runtime_event_correlations",
                "run_approvals",
                "delivery_outbox",
                "connector_delivery_receipts",
                "frontend_message_events",
                "event_consumers",
                "agent_memory_records",
                "memory_promotion_watermarks",
                "idempotency_records",
                "frontend_command_invocations",
                "run_projection_records",
                "backend_bindings",
                "channel_bindings",
                "session_capsules",
                "session_attachments",
            ):
                column = "target_session_id" if table == "conversation_continuity_imports" else "session_id"
                connection.execute(
                    f"DELETE FROM {table} WHERE {column} IN ({marks})",
                    session_ids,
                )

            connection.execute(
                "DELETE FROM conversation_continuity_imports WHERE owner_id=?",
                (owner,),
            )
            connection.execute(
                "DELETE FROM conversation_continuity_batches WHERE owner_id=?",
                (owner,),
            )
            connection.execute(
                "DELETE FROM conversation_continuity_origin_claims WHERE owner_id=?",
                (owner,),
            )
            connection.execute(
                "DELETE FROM conversation_continuity_retirements WHERE owner_id=?",
                (owner,),
            )
            for agent_name in agent_ids:
                connection.execute(
                    "DELETE FROM memory_promotion_jobs WHERE agent_id=?",
                    (agent_name,),
                )
                connection.execute(
                    "DELETE FROM memory_promotion_schedules WHERE agent_id=?",
                    (agent_name,),
                )

            # Delete event/message/run graph only after every dependent projection.
            connection.execute(
                f"DELETE FROM run_events WHERE session_id IN ({marks})",
                session_ids,
            )
            connection.execute(
                f"DELETE FROM runs WHERE session_id IN ({marks})",
                session_ids,
            )
            connection.execute(
                f"DELETE FROM messages WHERE session_id IN ({marks})",
                session_ids,
            )
            connection.execute(
                f"DELETE FROM session_workzones WHERE session_id IN ({marks})",
                session_ids,
            )
            connection.execute(
                f"DELETE FROM session_participants WHERE session_id IN ({marks})",
                session_ids,
            )
            connection.execute(
                f"DELETE FROM session_context_generations WHERE session_id IN ({marks})",
                session_ids,
            )
            connection.execute(
                f"DELETE FROM sessions WHERE session_id IN ({marks})",
                session_ids,
            )

        for file_path in removed_files:
            try:
                file_path.unlink(missing_ok=True)
            except OSError:
                # Database authority is already removed. A later media sweeper can
                # retry orphan bytes; never restore the purged Session graph.
                pass
        for session_id in session_ids:
            workspace = (self.workspaces_root / session_id).resolve()
            if workspace.parent == self.workspaces_root.resolve() and workspace.exists():
                shutil.rmtree(workspace, ignore_errors=True)
        return {"sessions": len(session_ids), "attachments": len(removed_files)}

    def bind_channel(
        self,
        *,
        owner_id: str,
        agent_id: str,
        surface: str,
        channel_key: str,
        session_id: str,
    ) -> dict[str, Any]:
        session = self.get_session(
            session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            include_deleted=False,
        )
        if session["status"] != "active":
            raise SessionConflict("only active Sessions can be selected")
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO channel_bindings(
                    instance_id, owner_id, agent_id, surface, channel_key,
                    session_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id, owner_id, agent_id, surface, channel_key)
                DO UPDATE SET session_id = excluded.session_id, updated_at = excluded.updated_at
                """,
                (
                    self.instance_id,
                    str(owner_id),
                    str(agent_id).lower(),
                    str(surface).lower(),
                    str(channel_key),
                    str(session_id),
                    now,
                ),
            )
        return session

    def bind_primary_session(
        self,
        *,
        owner_id: str,
        agent_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Select one shared personal conversation for interactive frontends."""

        selected = self.get_session(
            session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            include_deleted=False,
        )
        if selected.get("session_kind") != SESSION_KIND_CONVERSATION:
            raise SessionConflict(
                "the primary frontend can select only a conversation Session"
            )

        return self.bind_channel(
            owner_id=owner_id,
            agent_id=agent_id,
            surface=PRIMARY_CONVERSATION_SURFACE,
            channel_key=PRIMARY_CONVERSATION_CHANNEL,
            session_id=session_id,
        )

    def resolve_primary_session(
        self,
        *,
        owner_id: str,
        agent_id: str,
        establish: bool = False,
    ) -> dict[str, Any]:
        """Resolve the Session shared by Telegram and Workbench.

        Existing installations may have independent legacy bindings.  On the
        first read, preserve the most recently active one as the shared main
        conversation instead of silently falling back to an older default.
        """

        owner = str(owner_id)
        agent = str(agent_id).lower()
        default = self.ensure_default_session(owner_id=owner, agent_id=agent)
        with self._lock, self._connection() as connection:
            current = connection.execute(
                """
                SELECT s.* FROM channel_bindings AS b
                JOIN sessions AS s ON s.session_id = b.session_id
                WHERE b.instance_id = ? AND b.owner_id = ? AND b.agent_id = ?
                  AND b.surface = ? AND b.channel_key = ?
                  AND s.session_kind = 'conversation'
                  AND s.status = 'active'
                """,
                (
                    self.instance_id,
                    owner,
                    agent,
                    PRIMARY_CONVERSATION_SURFACE,
                    PRIMARY_CONVERSATION_CHANNEL,
                ),
            ).fetchone()
            if current is not None:
                return self._session_dict(current)
            legacy = connection.execute(
                """
                SELECT s.* FROM channel_bindings AS b
                JOIN sessions AS s ON s.session_id = b.session_id
                WHERE b.instance_id = ? AND b.owner_id = ? AND b.agent_id = ?
                  AND (
                    b.surface = 'telegram'
                    OR (b.surface = 'workbench' AND b.channel_key = 'default')
                  )
                  AND s.session_kind = 'conversation'
                  AND s.status = 'active'
                ORDER BY s.updated_at DESC, b.updated_at DESC, s.session_id ASC
                LIMIT 1
                """,
                (self.instance_id, owner, agent),
            ).fetchone()
            selected = self._session_dict(legacy) if legacy is not None else default
        if not establish:
            return selected

        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT s.* FROM channel_bindings AS b
                JOIN sessions AS s ON s.session_id = b.session_id
                WHERE b.instance_id = ? AND b.owner_id = ? AND b.agent_id = ?
                  AND b.surface = ? AND b.channel_key = ?
                  AND s.session_kind = 'conversation'
                  AND s.status = 'active'
                """,
                (
                    self.instance_id,
                    owner,
                    agent,
                    PRIMARY_CONVERSATION_SURFACE,
                    PRIMARY_CONVERSATION_CHANNEL,
                ),
            ).fetchone()
            if current is not None:
                return self._session_dict(current)
            connection.execute(
                """
                INSERT INTO channel_bindings(
                    instance_id, owner_id, agent_id, surface, channel_key,
                    session_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id, owner_id, agent_id, surface, channel_key)
                DO NOTHING
                """,
                (
                    self.instance_id,
                    owner,
                    agent,
                    PRIMARY_CONVERSATION_SURFACE,
                    PRIMARY_CONVERSATION_CHANNEL,
                    selected["session_id"],
                    now,
                ),
            )
            resolved = connection.execute(
                """
                SELECT s.* FROM channel_bindings AS b
                JOIN sessions AS s ON s.session_id = b.session_id
                WHERE b.instance_id = ? AND b.owner_id = ? AND b.agent_id = ?
                  AND b.surface = ? AND b.channel_key = ?
                  AND s.session_kind = 'conversation'
                  AND s.status = 'active'
                """,
                (
                    self.instance_id,
                    owner,
                    agent,
                    PRIMARY_CONVERSATION_SURFACE,
                    PRIMARY_CONVERSATION_CHANNEL,
                ),
            ).fetchone()
        return self._session_dict(resolved) if resolved is not None else selected

    def resolve_session(
        self,
        *,
        owner_id: str,
        agent_id: str,
        surface: str,
        channel_key: str,
        explicit_session_id: str | None = None,
        default_only: bool = False,
    ) -> dict[str, Any]:
        agent_id = str(agent_id).lower()
        default = self.ensure_default_session(owner_id=owner_id, agent_id=agent_id)
        if default_only:
            return default
        if explicit_session_id:
            return self.get_session(
                explicit_session_id,
                owner_id=owner_id,
                agent_id=agent_id,
                include_deleted=False,
            )
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT s.* FROM channel_bindings AS b
                JOIN sessions AS s ON s.session_id = b.session_id
                WHERE b.instance_id = ? AND b.owner_id = ? AND b.agent_id = ?
                  AND b.surface = ? AND b.channel_key = ? AND s.status = 'active'
                """,
                (
                    self.instance_id,
                    str(owner_id),
                    agent_id,
                    str(surface).lower(),
                    str(channel_key),
                ),
            ).fetchone()
        if row is not None:
            return self._session_dict(row)
        self.bind_channel(
            owner_id=owner_id,
            agent_id=agent_id,
            surface=surface,
            channel_key=channel_key,
            session_id=default["session_id"],
        )
        return default

    def accept_run(
        self,
        *,
        session_id: str,
        owner_id: str,
        agent_id: str,
        request_id: str,
        text: str,
        source: str,
        idempotency_key: str,
        display_text: str | None = None,
        execution_mode: str | None = None,
        content: Iterable[Mapping[str, Any]] | None = None,
        parent_run_id: str | None = None,
        response_preferences: Mapping[str, Any] | None = None,
        message_context: Mapping[str, Any] | None = None,
        delivery_route: Mapping[str, Any] | None = None,
        expected_context_generation: int | None = None,
    ) -> AcceptedRun:
        clean = str(text or "").strip()
        if display_text is not None and not isinstance(display_text, str):
            raise ValueError("display_text must be a string")
        if display_text is not None and len(display_text) > MAX_SESSION_MESSAGE_CHARS:
            raise ValueError("display_text exceeds the configured message limit")
        blocks = list(content or ({"type": "text", "text": clean},))
        if contains_persistent_inline_media(blocks):
            raise SessionConflict("Session content cannot contain inline media bytes")
        if not all(isinstance(block, Mapping) for block in blocks):
            raise ValueError("message content parts must be objects")
        if not clean:
            clean = "\n".join(
                str(block.get("text") or "").strip()
                for block in blocks
                if str(block.get("type") or "").strip().casefold() == "text"
                and str(block.get("text") or "").strip()
            ).strip()
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND owner_id = ?
                  AND agent_id = ? AND status = 'active'
                """,
                (session_id, self.instance_id, str(owner_id), str(agent_id).lower()),
            ).fetchone()
            if session is None:
                raise SessionNotFound(session_id)
            if expected_context_generation is not None and int(session["context_generation"]) != int(expected_context_generation):
                raise SessionConflict("session_context_generation_changed")
            audio_rows: list[sqlite3.Row] = []
            attachment_rows: list[sqlite3.Row] = []
            attachment_fingerprints: list[dict[str, Any]] = []
            normalized_blocks: list[dict[str, Any]] = []
            attachment_ids: set[str] = set()
            total_attachment_bytes = 0
            for raw_block in blocks:
                block = dict(raw_block)
                block_type = str(block.get("type") or "").strip().casefold()
                if block_type == "text":
                    if not isinstance(block.get("text"), str):
                        raise ValueError("text content parts require text")
                elif block_type in {"attachment", "audio"}:
                    attachment_id = str(block.get("attachment_id") or "").strip()
                    if not attachment_id:
                        raise SessionConflict(
                            "attachment content requires a committed attachment"
                        )
                    if attachment_id in attachment_ids:
                        raise SessionConflict("attachment cannot be repeated in one message")
                    attachment_ids.add(attachment_id)
                    if len(attachment_ids) > MAX_SESSION_ATTACHMENTS_PER_MESSAGE:
                        raise SessionConflict(
                            "message exceeds the configured attachment count limit"
                        )
                    attachment = connection.execute(
                        """SELECT * FROM session_attachments
                           WHERE attachment_id=? AND session_id=? AND owner_id=?""",
                        (attachment_id, str(session_id), str(owner_id)),
                    ).fetchone()
                    if attachment is None:
                        raise SessionConflict(
                            "attachment is not authorized for this Session"
                        )
                    if str(attachment["state"]) != "committed":
                        raise SessionConflict("attachment is not committed")
                    asset_id = str(attachment["asset_id"] or "")
                    if not asset_id:
                        raise SessionConflict("attachment bytes are unavailable")
                    is_audio = str(attachment["media_type"]).casefold().startswith(
                        "audio/"
                    )
                    if block_type == "audio" and not is_audio:
                        raise SessionConflict("attachment is not audio")
                    semantic_role = str(
                        block.get("semantic_role")
                        or ("audio_attachment" if is_audio else "")
                    ).strip().casefold()
                    if is_audio and semantic_role not in {
                        "voice_message",
                        "audio_attachment",
                    }:
                        raise SessionConflict("invalid audio semantic role")
                    if not is_audio and semantic_role:
                        raise SessionConflict(
                            "semantic_role is only supported for audio attachments"
                        )
                    declared_mime = str(block.get("mime_type") or "").casefold()
                    if declared_mime and declared_mime != str(
                        attachment["media_type"]
                    ).casefold():
                        raise SessionConflict(
                            "attachment MIME does not match committed metadata"
                        )
                    if is_audio:
                        self.audio_assets.describe(
                            asset_id, owner_id=owner_id, session_id=session_id
                        )
                        block["semantic_role"] = semantic_role
                        audio_rows.append(attachment)
                    else:
                        self._validated_attachment_file(dict(attachment))
                    block.setdefault("mime_type", str(attachment["media_type"]))
                    attachment_rows.append(attachment)
                    total_attachment_bytes += int(attachment["size_bytes"])
                    if total_attachment_bytes > MAX_SESSION_ATTACHMENT_TOTAL_BYTES:
                        raise SessionConflict(
                            "message exceeds the configured total attachment size limit"
                        )
                    attachment_fingerprints.append(
                        {
                            "attachment_id": attachment_id,
                            "sha256": str(attachment["sha256"]),
                            "size_bytes": int(attachment["size_bytes"]),
                            **(
                                {"semantic_role": semantic_role}
                                if semantic_role
                                else {}
                            ),
                        }
                    )
                elif block_type == "media":
                    # Trusted Connector ingress already supplies a canonical
                    # media part.  Keep that established internal contract
                    # while the public Frontend Connector uses staged
                    # ``attachment`` references.
                    if not str(block.get("attachment_id") or "").strip():
                        raise SessionConflict(
                            "media content requires attachment identity"
                        )
                elif block_type == "reply_ref":
                    event_id = str(block.get("event_id") or "").strip()
                    if not event_id or len(event_id) > 512:
                        raise ValueError("reply references require a valid Event ID")
                    reference = connection.execute(
                        """
                        SELECT message.message_id
                        FROM run_events AS e
                        JOIN sessions AS s ON s.session_id=e.session_id
                        LEFT JOIN frontend_message_events AS fme
                          ON fme.event_id=e.event_id AND fme.session_id=e.session_id
                        LEFT JOIN runs AS target_run
                          ON target_run.run_id=e.run_id
                         AND target_run.session_id=e.session_id
                        JOIN messages AS message
                          ON message.message_id=COALESCE(
                              fme.message_id, target_run.final_message_id
                          )
                         AND message.session_id=e.session_id
                        WHERE e.event_id=? AND e.session_id=?
                          AND s.owner_id=? AND s.instance_id=?
                          AND message.role='assistant'
                          AND message.visibility='visible'
                        """,
                        (event_id, str(session_id), str(owner_id), self.instance_id),
                    ).fetchone()
                    if reference is None:
                        raise SessionConflict(
                            "reply reference is not a visible assistant Event in this Session"
                        )
                    block = {"type": "reply_ref", "event_id": event_id}
                elif not block_type:
                    raise ValueError("message content parts require a type")
                else:
                    raise ValueError(f"unsupported message content type {block_type!r}")
                normalized_blocks.append(block)
            blocks = normalized_blocks
            if not clean and not attachment_rows and not any(
                str(block.get("type") or "").casefold() == "media"
                for block in normalized_blocks
            ):
                raise ValueError("message requires text or a committed attachment")
            digest_payload = {
                "content": blocks,
                "attachments": attachment_fingerprints,
                "display_text": display_text,
                "execution_mode": str(execution_mode or ""),
                "parent_run_id": str(parent_run_id or ""),
                "response_preferences": dict(response_preferences or {}),
                "message_context": dict(message_context or {}),
                "delivery_route": dict(delivery_route or {}),
            }
            digest = hashlib.sha256(
                _json(digest_payload).encode("utf-8")
            ).hexdigest()
            existing = connection.execute(
                """
                SELECT i.request_digest, r.* FROM idempotency_records AS i
                JOIN runs AS r ON r.run_id = i.run_id
                WHERE i.session_id = ? AND i.idempotency_key = ?
                """,
                (session_id, str(idempotency_key)),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != digest:
                    raise IdempotencyConflict(
                        "idempotency key is already bound to a different request"
                    )
                return AcceptedRun(
                    session_id=session_id,
                    run_id=str(existing["run_id"]),
                    message_id=str(existing["user_message_id"]),
                    request_id=str(existing["request_id"]),
                    context_generation=int(existing["context_generation"]),
                    replayed=True,
                )
            run_id = _new_id("run")
            message_id = _new_id("msg")
            generation = int(session["context_generation"])
            ordinal = self._next_ordinal(connection, session_id)
            content_json = _json(blocks)
            content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO messages(
                    message_id, session_id, run_id, ordinal, context_generation,
                    role, author_id, source, message_context_json, content_json,
                    text, display_text, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, 'user', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    run_id,
                    ordinal,
                    generation,
                    str(owner_id),
                    str(source),
                    _json(dict(message_context or {})),
                    content_json,
                    clean,
                    display_text,
                    content_hash,
                    now,
                ),
            )
            self._queue_foreground_message(
                connection, session_id=session_id, message_id=message_id
            )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, session_id, user_message_id, agent_id, request_id,
                    idempotency_key, request_digest, source, requested_mode,
                    effective_mode, response_preferences_json, message_context_json,
                    delivery_route_json,
                    context_generation, state, parent_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    message_id,
                    str(agent_id).lower(),
                    str(request_id),
                    str(idempotency_key),
                    digest,
                    str(source),
                    execution_mode,
                    execution_mode,
                    _json(dict(response_preferences or {})),
                    _json(dict(message_context or {})),
                    _json(dict(delivery_route or {})),
                    generation,
                    parent_run_id,
                    now,
                    now,
                ),
            )
            if attachment_rows:
                connection.executemany(
                    """UPDATE session_attachments
                       SET retention_seconds=NULL, retention_indefinite=1
                       WHERE attachment_id=? AND session_id=? AND owner_id=?""",
                    [
                        (
                            str(attachment["attachment_id"]),
                            str(session_id),
                            str(owner_id),
                        )
                        for attachment in attachment_rows
                    ],
                )
            for attachment in audio_rows:
                asset_id = str(attachment["asset_id"])
                self.audio_assets.set_indefinite(
                    asset_id, owner_id=owner_id, session_id=session_id
                )
                self.audio_assets.acquire(
                    asset_id, owner_id=owner_id, session_id=session_id
                )
                connection.execute(
                    """INSERT INTO run_audio_assets(
                           run_id, attachment_id, asset_id, direction, created_at
                       ) VALUES (?, ?, ?, 'input', ?)""",
                    (
                        run_id,
                        str(attachment["attachment_id"]),
                        asset_id,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO idempotency_records(
                    session_id, owner_id, idempotency_key, request_digest,
                    run_id, message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(owner_id),
                    str(idempotency_key),
                    digest,
                    run_id,
                    message_id,
                    now,
                ),
            )
            self._append_event(
                connection,
                session_id=session_id,
                run_id=run_id,
                kind="run.accepted",
                status="queued",
                phase="admission",
                summary="User message accepted",
                detail={"message_id": message_id, "request_id": request_id},
                outbox=True,
            )
            connection.execute(
                """
                UPDATE sessions SET updated_at = ?, revision = revision + 1
                WHERE session_id = ?
                """,
                (now, session_id),
            )
        return AcceptedRun(
            session_id=session_id,
            run_id=run_id,
            message_id=message_id,
            request_id=request_id,
            context_generation=generation,
        )

    def append_presentation_message(
        self,
        *,
        session_id: str,
        owner_id: str,
        agent_id: str,
        role: str,
        text: str,
        source: str,
        idempotency_key: str,
        content_format: str = "plain-text",
        presentation_channel: str = "command",
        history_eligible: bool = False,
        message_context: Mapping[str, Any] | None = None,
        content: Iterable[Mapping[str, Any]] | None = None,
        outbox: bool = False,
        delivery_route: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist frontend-visible standard content outside model history."""

        clean = str(text or "").strip()
        normalized_role = str(role or "").strip().casefold()
        normalized_source = str(source or "").strip()
        stable_key = str(idempotency_key or "").strip()
        normalized_format = str(content_format or "plain-text").strip().casefold()
        normalized_channel = str(presentation_channel or "command").strip().casefold()
        if not normalized_source or not stable_key:
            raise ValueError("presentation message source and idempotency key are required")
        if normalized_role not in {"user", "assistant"}:
            raise ValueError("presentation message role must be user or assistant")
        if normalized_format not in {"plain-text", "markdown", "telegram-html"}:
            raise ValueError("unsupported presentation content format")
        context = dict(message_context or {})
        context.update(
            {
                "content_format": normalized_format,
                "presentation_channel": normalized_channel,
                "presentation_only": not bool(history_eligible),
            }
        )
        supplied_content = list(content or ())
        normalized_content: list[dict[str, Any]] = []
        if supplied_content:
            if contains_persistent_inline_media(supplied_content):
                raise SessionConflict(
                    "presentation content cannot contain inline media bytes"
                )
            for raw_part in supplied_content:
                if not isinstance(raw_part, Mapping):
                    raise ValueError("presentation content parts must be objects")
                part = dict(raw_part)
                part_type = str(part.get("type") or "").strip().casefold()
                if part_type == "text":
                    part_text = str(part.get("text") or "")
                    if not part_text:
                        raise ValueError("presentation text parts require text")
                    normalized_content.append(
                        {"type": "text", "text": part_text}
                    )
                    if not clean and part_text.strip():
                        clean = part_text.strip()
                elif part_type in {"attachment", "media"}:
                    attachment_id = str(
                        part.get("attachment_id") or ""
                    ).strip()
                    if not attachment_id:
                        raise ValueError(
                            "presentation media requires attachment_id"
                        )
                    normalized_content.append(
                        self.attachment_canonical_part(
                            session_id=str(session_id),
                            owner_id=str(owner_id),
                            attachment_id=attachment_id,
                            item_index=len(normalized_content) + 1,
                            semantic_role=str(
                                part.get("semantic_role") or ""
                            )
                            or None,
                            presentation_role=str(
                                part.get("presentation_role") or ""
                            )
                            or None,
                            caption=str(part.get("caption") or ""),
                            detail=str(part.get("detail") or ""),
                        )
                    )
                else:
                    raise ValueError(
                        f"unsupported presentation content type {part_type!r}"
                    )
        elif clean:
            normalized_content = [{"type": "text", "text": clean}]
        if not normalized_content:
            raise ValueError("presentation message requires text or media")
        content_json = _json(normalized_content)
        content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        identity = "\n".join(
            (
                "presentation-message-v1",
                self.instance_id,
                str(session_id),
                stable_key,
            )
        )
        message_id = "msg_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND owner_id = ?
                  AND agent_id = ? AND status = 'active'
                """,
                (str(session_id), self.instance_id, str(owner_id), str(agent_id).lower()),
            ).fetchone()
            if session is None:
                raise SessionNotFound(str(session_id))
            existing = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["session_id"]) != str(session_id)
                    or str(existing["role"]) != normalized_role
                    or str(existing["source"]) != normalized_source
                    or str(existing["content_hash"]) != content_hash
                    or _json_object(existing["message_context_json"]) != context
                    or bool(existing["history_eligible"]) != bool(history_eligible)
                ):
                    raise IdempotencyConflict(
                        "presentation idempotency key is bound to different content"
                    )
                result = self._message_dict(existing)
                event_link = connection.execute(
                    "SELECT event_id FROM frontend_message_events WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if event_link is not None:
                    result["delivery_event_id"] = str(event_link["event_id"])
                return result
            ordinal = self._next_ordinal(connection, str(session_id))
            connection.execute(
                """
                INSERT INTO messages(
                    message_id, session_id, run_id, ordinal,
                    context_generation, role, author_id, source,
                    message_context_json, content_json, text, visibility,
                    history_eligible, content_hash, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'visible', ?, ?, ?)
                """,
                (
                    message_id,
                    str(session_id),
                    ordinal,
                    int(session["context_generation"]),
                    normalized_role,
                    str(owner_id) if normalized_role == "user" else str(agent_id).lower(),
                    normalized_source,
                    _json(context),
                    content_json,
                    clean,
                    int(bool(history_eligible)),
                    content_hash,
                    now,
                ),
            )
            self._queue_foreground_message(
                connection, session_id=str(session_id), message_id=message_id
            )
            presentation_event = self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind="frontend.message.recorded",
                status="recorded",
                phase="presentation",
                summary="Frontend-visible message recorded",
                detail={
                    "message_id": message_id,
                    "source": normalized_source,
                    "presentation_channel": normalized_channel,
                    "content_modes": sorted(
                        {
                            "media"
                            if str(item.get("type") or "").casefold()
                            in {"attachment", "media", "audio"}
                            else "text"
                            for item in normalized_content
                        }
                    ),
                },
                outbox=outbox,
                delivery_route=delivery_route,
            )
            connection.execute(
                """
                INSERT INTO frontend_message_events(message_id, event_id, session_id)
                VALUES (?, ?, ?)
                """,
                (message_id, presentation_event["event_id"], str(session_id)),
            )
            connection.execute(
                """
                UPDATE sessions SET updated_at = ?, revision = revision + 1
                WHERE session_id = ?
                """,
                (now, str(session_id)),
            )
            inserted = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (message_id,)
            ).fetchone()
        result = self._message_dict(inserted)
        result["delivery_event_id"] = presentation_event["event_id"]
        return result

    def update_presentation_message(
        self,
        *,
        session_id: str,
        owner_id: str,
        agent_id: str,
        message_id: str,
        text: str,
        message_context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update one non-semantic UI row and make its revision poll-visible."""

        clean = str(text or "").strip()
        if not clean:
            raise ValueError("presentation message text is required")
        supplied_context = dict(message_context or {})
        if (
            not supplied_context
            or set(supplied_context) - {"command_ui", "frontend_presentation"}
            or not isinstance(supplied_context.get("command_ui"), Mapping)
            or (
                "frontend_presentation" in supplied_context
                and not isinstance(
                    supplied_context.get("frontend_presentation"), Mapping
                )
            )
        ):
            raise ValueError(
                "only command_ui and its standard presentation may be updated"
            )
        content = [{"type": "text", "text": clean}]
        content_json = _json(content)
        content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND owner_id = ?
                  AND agent_id = ? AND status = 'active'
                """,
                (
                    str(session_id),
                    self.instance_id,
                    str(owner_id),
                    str(agent_id).lower(),
                ),
            ).fetchone()
            if session is None:
                raise SessionNotFound(str(session_id))
            existing = connection.execute(
                """
                SELECT * FROM messages
                WHERE message_id = ? AND session_id = ?
                  AND context_generation = ? AND role = 'assistant'
                  AND run_id IS NULL AND history_eligible = 0
                  AND visibility = 'visible' AND author_id = ?
                """,
                (
                    str(message_id),
                    str(session_id),
                    int(session["context_generation"]),
                    str(agent_id).lower(),
                ),
            ).fetchone()
            if existing is None:
                raise SessionNotFound("presentation message not found")
            context = _json_object(existing["message_context_json"])
            context.update(supplied_context)
            if "frontend_presentation" not in supplied_context and isinstance(
                context.get("frontend_presentation"), Mapping
            ):
                presentation = dict(context["frontend_presentation"])
                blocks = [
                    dict(item)
                    for item in presentation.get("content_blocks") or []
                    if isinstance(item, Mapping)
                ]
                for block in blocks:
                    if block.get("type") == "text":
                        block["text"] = clean
                        break
                presentation["content_blocks"] = blocks
                context["frontend_presentation"] = presentation
            # These invariants keep the row outside model history regardless of
            # what a Connector callback attempted to supply.
            context["presentation_only"] = True
            context_json = _json(context)
            if (
                str(existing["text"]) == clean
                and str(existing["content_json"]) == content_json
                and str(existing["message_context_json"]) == context_json
            ):
                return self._message_dict(existing)
            ordinal = self._next_ordinal(connection, str(session_id))
            connection.execute(
                """
                UPDATE messages
                SET ordinal = ?, message_context_json = ?, content_json = ?,
                    text = ?, content_hash = ?
                WHERE message_id = ?
                """,
                (
                    ordinal,
                    context_json,
                    content_json,
                    clean,
                    content_hash,
                    str(message_id),
                ),
            )
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind="frontend.message.updated",
                status="recorded",
                phase="presentation",
                summary="Frontend-visible command menu updated",
                detail={"message_id": str(message_id)},
            )
            connection.execute(
                """
                UPDATE sessions SET updated_at = ?, revision = revision + 1
                WHERE session_id = ?
                """,
                (now, str(session_id)),
            )
            updated = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?", (str(message_id),)
            ).fetchone()
        return self._message_dict(updated)

    def update_live_call_record(
        self,
        *,
        session_id: str,
        owner_id: str,
        agent_id: str,
        call_id: str,
        call_epoch: int,
        text: str,
        segment_count: int,
    ) -> dict[str, Any]:
        """Reproject one owned call record from its durable transcript fragments."""

        clean = str(text or "").strip()
        if (
            not clean
            or isinstance(segment_count, bool)
            or not isinstance(segment_count, int)
            or segment_count < 0
        ):
            raise ValueError("live call record requires text and a segment count")
        identity = "\n".join(
            (
                "presentation-message-v1", self.instance_id, str(session_id),
                f"live-call-record:{call_id}:{call_epoch}",
            )
        )
        message_id = "msg_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        content_json = _json([{"type": "text", "text": clean}])
        content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """SELECT 1 FROM sessions WHERE session_id=? AND instance_id=?
                   AND owner_id=? AND agent_id=?""",
                (str(session_id), self.instance_id, str(owner_id), str(agent_id).lower()),
            ).fetchone()
            call = connection.execute(
                """SELECT started_at, ended_at FROM live_calls WHERE call_id=? AND call_epoch=?
                   AND session_id=? AND owner_id=? AND agent_id=?""",
                (str(call_id), int(call_epoch), str(session_id), str(owner_id), str(agent_id).lower()),
            ).fetchone()
            existing = connection.execute(
                """SELECT * FROM messages WHERE message_id=? AND session_id=?
                   AND role='assistant' AND source='live-phone' AND run_id IS NULL
                   AND history_eligible=0 AND visibility='visible' AND author_id=?""",
                (message_id, str(session_id), str(agent_id).lower()),
            ).fetchone()
            if session is None or call is None or existing is None:
                raise SessionConflict("live call record scope changed")
            context = _json_object(existing["message_context_json"])
            record = context.get("live_call_record")
            if (
                not isinstance(record, Mapping)
                or record.get("call_id") != call_id
                or record.get("call_epoch") != call_epoch
            ):
                raise SessionConflict("live call record identity changed")
            context["live_call_record"] = {
                **record, "schema": "hashi.live_voice.transcript.v2",
                "started_at": str(call["started_at"] or ""),
                "ended_at": str(call["ended_at"] or ""),
                "segment_count": segment_count,
            }
            context_json = _json(context)
            if (
                str(existing["text"]) == clean
                and str(existing["content_json"]) == content_json
                and str(existing["message_context_json"]) == context_json
            ):
                return self._message_dict(existing)
            connection.execute(
                """UPDATE messages SET message_context_json=?, content_json=?,
                   text=?, content_hash=? WHERE message_id=?""",
                (context_json, content_json, clean, content_hash, message_id),
            )
            self._append_event(
                connection, session_id=str(session_id), run_id=None,
                kind="frontend.message.updated", status="recorded", phase="presentation",
                summary="Live call transcript updated", detail={"message_id": message_id},
            )
            connection.execute(
                """UPDATE sessions SET updated_at=?, revision=revision+1,
                   history_generation=history_generation+1 WHERE session_id=?""",
                (_utc_now(), str(session_id)),
            )
            updated = connection.execute(
                "SELECT * FROM messages WHERE message_id=?", (message_id,),
            ).fetchone()
        return self._message_dict(updated)

    def mark_request_running(
        self,
        request_id: str,
        *,
        worker_id: str,
        message_context: Mapping[str, Any] | None = None,
    ) -> int | None:
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM runs WHERE request_id = ?", (str(request_id),)
            ).fetchone()
            if row is None or str(row["state"]) != "queued":
                return None
            attempt = int(row["attempt"]) + 1
            token = int(row["fencing_token"]) + 1
            current_message_context = (
                dict(message_context)
                if isinstance(message_context, Mapping)
                else _json_object(row["message_context_json"])
            )
            message_context_json = _json(current_message_context)
            updated = connection.execute(
                """
                UPDATE runs SET state = 'running', attempt = ?, fencing_token = ?,
                    worker_id = ?, message_context_json = ?,
                    started_at = ?, updated_at = ?
                WHERE run_id = ? AND state = 'queued' AND fencing_token = ?
                """,
                (
                    attempt,
                    token,
                    str(worker_id),
                    message_context_json,
                    now,
                    now,
                    row["run_id"],
                    int(row["fencing_token"]),
                ),
            )
            if updated.rowcount != 1:
                return None
            connection.execute(
                """UPDATE messages SET message_context_json = ?
                   WHERE message_id = ? AND run_id = ?""",
                (message_context_json, row["user_message_id"], row["run_id"]),
            )
            authorization_snapshot = {
                "scope": "current_message",
                "state": str(
                    current_message_context.get("private_authorization_state")
                    or "none"
                ),
                "private_authorizations": list(
                    current_message_context.get("private_authorizations") or []
                ),
            }
            connection.execute(
                """
                INSERT INTO run_attempts(
                    run_id, attempt, fencing_token, worker_id,
                    authorization_json, started_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, 'running')
                """,
                (
                    row["run_id"],
                    attempt,
                    token,
                    str(worker_id),
                    _json(authorization_snapshot),
                    now,
                ),
            )
            self._append_event(
                connection,
                session_id=str(row["session_id"]),
                run_id=str(row["run_id"]),
                kind="run.started",
                status="running",
                phase="execution",
                summary="Run started",
                detail={"attempt": attempt, "fencing_token": token},
            )
            return token

    def _release_run_audio_leases(
        self, connection: sqlite3.Connection, *, run_id: str, released_at: str
    ) -> None:
        rows = connection.execute(
            """SELECT ra.asset_id, a.owner_id, a.session_id
               FROM run_audio_assets AS ra
               JOIN session_attachments AS a
                 ON a.attachment_id = ra.attachment_id
               WHERE ra.run_id=? AND ra.lease_released=0""",
            (str(run_id),),
        ).fetchall()
        for row in rows:
            try:
                self.audio_assets.release(
                    str(row["asset_id"]),
                    owner_id=str(row["owner_id"]),
                    session_id=str(row["session_id"]),
                )
            except AudioAssetNotFound:
                pass
        connection.execute(
            """UPDATE run_audio_assets SET lease_released=1, released_at=?
               WHERE run_id=? AND lease_released=0""",
            (released_at, str(run_id)),
        )

    def finish_request(
        self,
        request_id: str,
        *,
        success: bool,
        assistant_text: str | None = None,
        assistant_content: Iterable[Mapping[str, Any]] | None = None,
        assistant_source: str = "",
        error_text: str | None = None,
        error_context: Mapping[str, Any] | None = None,
        failure_state: str = "failed",
        fencing_token: int | None = None,
    ) -> dict[str, Any] | None:
        now = _utc_now()
        public_error_context: dict[str, Any] = {}
        if isinstance(error_context, Mapping):
            for key in ("error_code", "provider_request_id", "backend", "diagnostic_log"):
                value = error_context.get(key)
                if isinstance(value, str) and value.strip():
                    public_error_context[key] = value.strip()[:4096 if key == "diagnostic_log" else 160]
            for key in ("error_retryable", "side_effects_possible"):
                value = error_context.get(key)
                if isinstance(value, bool):
                    public_error_context[key] = value
            for key in ("http_status", "retry_after_s"):
                value = error_context.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 86400:
                    public_error_context[key] = value
            reconciliation = error_context.get("effect_reconciliation")
            if isinstance(reconciliation, Mapping):
                safe_summary: dict[str, Any] = {}
                for key in (
                    "confirmed_write_count",
                    "observed_tool_count",
                    "unverified_action_count",
                    "completed_background_job_count",
                ):
                    value = reconciliation.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100_000:
                        safe_summary[key] = value
                if isinstance(reconciliation.get("evidence_limited"), bool):
                    safe_summary["evidence_limited"] = reconciliation["evidence_limited"]
                if safe_summary:
                    public_error_context["effect_reconciliation"] = safe_summary
        clean = str(assistant_text or "").strip()
        supplied_content = list(assistant_content or ())
        if contains_persistent_inline_media(supplied_content):
            raise SessionConflict("assistant content cannot contain inline media bytes")
        normalized_content: list[dict[str, Any]] = []
        for raw_part in supplied_content:
            if not isinstance(raw_part, Mapping):
                raise ValueError("assistant content parts must be objects")
            part = dict(raw_part)
            part_type = str(part.get("type") or "").strip().casefold()
            if part_type == "text":
                if not isinstance(part.get("text"), str):
                    raise ValueError("assistant text parts require text")
                if not clean and str(part.get("text") or "").strip():
                    clean = str(part["text"]).strip()
            elif part_type == "audio":
                asset_id = str(part.get("asset_id") or "").strip()
                digest = str(part.get("sha256") or "").strip().casefold()
                if not asset_id:
                    raise ValueError("assistant audio parts require asset_id")
                if digest and (
                    len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise ValueError("assistant audio sha256 is invalid")
            else:
                raise ValueError(f"unsupported assistant content type {part_type!r}")
            normalized_content.append(part)
        if not normalized_content and clean:
            normalized_content = [{"type": "text", "text": clean}]
        terminal_content_already_published = False
        incremental_only_complete = False
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM runs WHERE request_id = ?", (str(request_id),)
            ).fetchone()
            if run is None:
                return None
            current_state = str(run["state"])
            if current_state in TERMINAL_RUN_STATES:
                return self._run_dict(run)
            if fencing_token is not None and int(run["fencing_token"]) != int(
                fencing_token
            ):
                raise StaleFencingToken("executor fencing token is stale")
            session_id = str(run["session_id"])
            unpublished_rows = connection.execute(
                """SELECT o.*, r.session_id, r.request_id, s.owner_id, r.agent_id
                   FROM run_output_attachments AS o
                   JOIN runs AS r ON r.run_id=o.run_id
                   JOIN sessions AS s ON s.session_id=r.session_id
                   WHERE o.run_id=? AND o.publication_id IS NULL
                   ORDER BY o.output_index ASC""",
                (str(run["run_id"]),),
            ).fetchall()
            for item_index, raw_part in enumerate(
                self._canonical_run_output_attachments(unpublished_rows),
                start=len(normalized_content) + 1,
            ):
                part = dict(raw_part)
                part["item_index"] = item_index
                normalized_content.append(part)
            deliverable_audio = any(
                part.get("type") == "audio" and str(part.get("asset_id") or "").strip()
                for part in normalized_content
            )
            deliverable_attachment = any(
                str(part.get("type") or "").strip().casefold()
                in {"attachment", "media"}
                and str(part.get("attachment_id") or "").strip()
                for part in normalized_content
            )
            final_message_id = None
            if success:
                if not clean and not deliverable_audio and not deliverable_attachment:
                    incremental_only_complete = connection.execute(
                        """SELECT 1 FROM run_deliverable_publications
                           WHERE run_id=? LIMIT 1""",
                        (str(run["run_id"]),),
                    ).fetchone() is not None
                    if not incremental_only_complete:
                        success = False
                        error_text = (
                            error_text or "backend returned no visible final response"
                        )
                if success and not incremental_only_complete:
                    content = normalized_content
                    content_json = _json(content)
                    content_hash = hashlib.sha256(
                        content_json.encode("utf-8")
                    ).hexdigest()
                    existing_output = connection.execute(
                        """SELECT message_id FROM messages
                           WHERE run_id=? AND role='assistant' AND content_hash=?
                           ORDER BY ordinal ASC LIMIT 1""",
                        (str(run["run_id"]), content_hash),
                    ).fetchone()
                    if existing_output is not None:
                        # Direct native audio was already projected at
                        # first-ready time.  Reuse that canonical Message when
                        # the Run later settles instead of duplicating it.
                        final_message_id = str(existing_output["message_id"])
                        terminal_content_already_published = True
                    else:
                        final_message_id = _new_id("msg")
                        ordinal = self._next_ordinal(connection, session_id)
                        connection.execute(
                            """
                            INSERT INTO messages(
                                message_id, session_id, run_id, ordinal,
                                context_generation, role, author_id, source,
                                content_json, text, content_hash, created_at
                            ) VALUES (?, ?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                final_message_id,
                                session_id,
                                run["run_id"],
                                ordinal,
                                int(run["context_generation"]),
                                str(run["agent_id"]),
                                str(assistant_source or run["agent_id"]),
                                content_json,
                                clean,
                                content_hash,
                                now,
                            ),
                        )
                    if (deliverable_audio or deliverable_attachment) and existing_output is None:
                        self._append_event(
                            connection,
                            session_id=session_id,
                            run_id=str(run["run_id"]),
                            kind="assistant.output.available",
                            status="available",
                            phase="final",
                            summary=(
                                "Assistant attachment output available"
                                if deliverable_attachment
                                else "Assistant audio output available"
                            ),
                            detail={
                                "message_id": final_message_id,
                                "request_id": str(request_id),
                                "disposition": "final",
                                "content": content,
                            },
                            # The terminal run.completed Event owns delivery of
                            # final Message content.  Keep this availability
                            # Event in the feed, but do not create a second
                            # destination task for the same attachment bytes.
                            outbox=False,
                        )
            state = "completed" if success else str(failure_state or "failed")
            if state not in TERMINAL_RUN_STATES:
                state = "failed"
            error_code = None if success else "run_failed"
            connection.execute(
                """
                UPDATE runs SET state = ?, final_message_id = ?, error_code = ?,
                    error_text = ?, completed_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    state,
                    final_message_id,
                    error_code,
                    None if success else str(error_text or "run failed"),
                    now,
                    now,
                    run["run_id"],
                ),
            )
            connection.execute(
                """
                UPDATE run_attempts SET state = ?, finished_at = ?
                WHERE run_id = ? AND attempt = ?
                """,
                (state, now, run["run_id"], int(run["attempt"])),
            )
            event = self._append_event(
                connection,
                session_id=session_id,
                run_id=str(run["run_id"]),
                kind="run.completed" if success else f"run.{state}",
                status=state,
                phase="terminal",
                summary="Assistant response completed"
                if success
                else str(error_text or "Run failed"),
                detail={"message_id": final_message_id}
                if success
                else {
                    "error": str(error_text or "run failed"),
                    "error_context": public_error_context,
                },
                # A first-ready native output Event already owns delivery of
                # a reused Message.  The terminal Event remains in the feed,
                # but must not enqueue the same text/audio a second time.
                outbox=not (terminal_content_already_published or incremental_only_complete),
            )
            if success and final_message_id:
                self._queue_foreground_message(
                    connection, session_id=session_id, message_id=str(final_message_id)
                )
            projection = {
                "run_id": str(run["run_id"]),
                "session_id": session_id,
                "state": state,
                "user_message_id": str(run["user_message_id"]),
                "final_message_id": final_message_id,
                "error": None if success else str(error_text or "run failed"),
                "latest_event_sequence": event["sequence"],
            }
            connection.execute(
                """
                INSERT INTO run_projection_records(run_id, session_id, projection_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    projection_json = excluded.projection_json,
                    updated_at = excluded.updated_at
                """,
                (run["run_id"], session_id, _json(projection), now),
            )
            connection.execute(
                """
                UPDATE sessions SET updated_at = ?, revision = revision + 1
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            self._release_run_audio_leases(
                connection, run_id=str(run["run_id"]), released_at=now
            )
            result = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run["run_id"],)
            ).fetchone()
            return self._run_dict(result)

    def record_assistant_delivery(
        self,
        request_id: str,
        *,
        delivered: bool,
        assistant_text: str | None = None,
        surface: str,
        channel_key: str,
        transport: str,
        completion_path: str,
        disposition: str = "",
        outcome_state: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist one final-response delivery outcome for a Session Run."""

        normalized_surface = str(surface or "").strip().lower()
        normalized_channel = str(channel_key or "").strip()
        if not normalized_surface or not normalized_channel:
            return None
        route_phase = f"transport:{normalized_surface}:{normalized_channel}"
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT r.*, m.text AS canonical_assistant_text
                FROM runs AS r
                JOIN sessions AS s ON s.session_id = r.session_id
                JOIN messages AS m ON m.message_id = r.final_message_id
                WHERE r.request_id = ? AND r.state = 'completed'
                  AND s.instance_id = ?
                """,
                (str(request_id), self.instance_id),
            ).fetchone()
            if run is None:
                return None

            outcome_status = str(outcome_state or "").strip().casefold() or (
                "delivered" if delivered else "failed"
            )
            if outcome_status not in {
                "queued",
                "accepted",
                "delivered",
                "failed",
                "unknown",
            }:
                raise ValueError("unsupported assistant delivery outcome state")
            if delivered != (outcome_status == "delivered"):
                raise ValueError(
                    "assistant delivery outcome state contradicts delivered"
                )
            existing = connection.execute(
                """
                SELECT * FROM run_events
                WHERE run_id = ? AND kind = 'assistant.delivery.outcome'
                  AND status = ? AND phase = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (str(run["run_id"]), outcome_status, route_phase),
            ).fetchone()
            if existing is not None:
                detail = _json_object(existing["detail_json"])
                event = dict(existing)
                event["detail"] = detail
                event.pop("detail_json", None)
                return event

            delivered_text = str(assistant_text or "").strip()
            canonical_text = str(run["canonical_assistant_text"] or "").strip()
            detail = {
                "request_id": str(request_id),
                "message_id": str(run["final_message_id"]),
                "surface": normalized_surface,
                "channel_key": normalized_channel,
                "transport": str(transport or "").strip().lower(),
                "completion_path": str(completion_path or "").strip().lower(),
                "disposition": str(disposition or "").strip(),
                "outcome_state": outcome_status,
            }
            if delivered_text and delivered_text != canonical_text:
                detail["text_override"] = delivered_text
            return self._append_event(
                connection,
                session_id=str(run["session_id"]),
                run_id=str(run["run_id"]),
                kind="assistant.delivery.outcome",
                status=outcome_status,
                phase=route_phase,
                summary=(
                    "Assistant final response delivered"
                    if delivered
                    else (
                        "Assistant final response queued"
                        if outcome_status == "queued"
                        else (
                            "Assistant final response accepted by transport"
                            if outcome_status == "accepted"
                            else (
                                "Assistant final response delivery outcome unknown"
                                if outcome_status == "unknown"
                                else "Assistant final response delivery failed"
                            )
                        )
                    )
                ),
                detail=detail,
            )

    def latest_delivered_assistant_text(
        self,
        session_id: str,
        *,
        surface: str,
        channel_key: str,
    ) -> str | None:
        """Return the newest visible assistant text confirmed on one route."""

        texts = self.recent_delivered_assistant_texts(
            session_id, surface=surface, channel_key=channel_key, limit=1
        )
        return texts[0] if texts else None

    def recent_delivered_assistant_texts(
        self,
        session_id: str,
        *,
        surface: str,
        channel_key: str,
        limit: int = 4,
    ) -> list[str]:
        """Return up to four confirmed final replies, newest first, on one route."""

        session = self.get_session(session_id)
        normalized_surface = str(surface or "").strip().lower()
        normalized_channel = str(channel_key or "").strip()
        if not normalized_surface or not normalized_channel:
            return []
        bounded_limit = max(1, min(int(limit), 4))
        route_phase = f"transport:{normalized_surface}:{normalized_channel}"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT e.detail_json, m.text
                FROM run_events AS e
                JOIN runs AS r ON r.run_id = e.run_id
                JOIN messages AS m ON m.message_id = r.final_message_id
                JOIN sessions AS s ON s.session_id = e.session_id
                WHERE e.session_id = ? AND s.instance_id = ?
                  AND e.kind = 'assistant.delivery.outcome'
                  AND e.status = 'delivered'
                  AND e.phase = ?
                  AND r.state = 'completed'
                  AND m.role = 'assistant'
                  AND m.visibility = 'visible'
                  AND m.context_generation = ?
                ORDER BY e.sequence DESC
                LIMIT ?
                """,
                (
                    str(session_id), self.instance_id, route_phase,
                    int(session["context_generation"]), bounded_limit,
                ),
            ).fetchall()
        texts: list[str] = []
        for row in rows:
            detail = _json_object(row["detail_json"])
            text = str(detail.get("text_override") or row["text"] or "").strip()
            if text:
                texts.append(text)
        return texts

    def has_assistant_delivery_outcome(
        self,
        session_id: str,
        *,
        surface: str,
        channel_key: str,
    ) -> bool:
        """Return whether delivery-aware tracking has begun on this route."""

        self.get_session(session_id)
        normalized_surface = str(surface or "").strip().lower()
        normalized_channel = str(channel_key or "").strip()
        if not normalized_surface or not normalized_channel:
            return False
        route_phase = f"transport:{normalized_surface}:{normalized_channel}"
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM run_events AS e
                JOIN sessions AS s ON s.session_id = e.session_id
                WHERE e.session_id = ? AND s.instance_id = ?
                  AND e.kind = 'assistant.delivery.outcome'
                  AND e.phase = ?
                LIMIT 1
                """,
                (str(session_id), self.instance_id, route_phase),
            ).fetchone()
        return row is not None

    def cancel_run(
        self, run_id: str, *, owner_id: str, reason: str = "cancelled_by_user"
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """SELECT r.* FROM runs r JOIN sessions s ON s.session_id=r.session_id
                   WHERE r.run_id=? AND s.instance_id=? AND s.owner_id=?""",
                (str(run_id), self.instance_id, str(owner_id)),
            ).fetchone()
            if run is None:
                raise SessionNotFound(str(run_id))
            if str(run["state"]) in TERMINAL_RUN_STATES:
                return self._run_dict(run)
            connection.execute(
                """UPDATE runs SET state='stopped', fencing_token=fencing_token+1,
                   worker_id=NULL, error_code='run_cancelled', error_text=?,
                   completed_at=?, updated_at=? WHERE run_id=?""",
                (str(reason), now, now, str(run_id)),
            )
            connection.execute(
                "UPDATE run_attempts SET state='stopped', finished_at=? WHERE run_id=? AND state='running'",
                (now, str(run_id)),
            )
            self._append_event(
                connection,
                session_id=str(run["session_id"]),
                run_id=str(run_id),
                kind="run.stopped",
                status="stopped",
                phase="control",
                summary=str(reason),
                detail={"reason": str(reason)},
                outbox=True,
            )
            self._release_run_audio_leases(
                connection, run_id=str(run_id), released_at=now
            )
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
        return self._run_dict(row)

    def stage_attachment(
        self,
        *,
        session_id: str,
        owner_id: str,
        filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
        semantic_role: str = "",
        duration_ms: int | None = None,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        retention_indefinite: bool = False,
        upload_required: bool | None = None,
        idempotency_key: str | None = None,
        attachment_policy: str = "standard",
    ) -> dict[str, Any]:
        self.get_session(session_id, owner_id=owner_id, include_deleted=False)
        digest = str(sha256).lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        normalized_media_type = (
            str(media_type or "").split(";", 1)[0].strip().casefold()
        )
        if not normalized_media_type or "/" not in normalized_media_type:
            raise ValueError("media_type must be a MIME type")
        declared_size = int(size_bytes)
        if declared_size < 0:
            raise ValueError("attachment size must be non-negative")
        _max_count, max_attachment_bytes, _max_total = _attachment_policy_limits(
            attachment_policy
        )
        if declared_size > max_attachment_bytes:
            raise ValueError("attachment exceeds the configured size limit")
        is_audio = normalized_media_type.startswith("audio/")
        normalized_role = str(semantic_role or "").strip().casefold()
        if is_audio:
            normalized_role = normalized_role or "audio_attachment"
            if normalized_role not in {"voice_message", "audio_attachment"}:
                raise ValueError(
                    "audio semantic_role must be voice_message or audio_attachment"
                )
            if duration_ms is not None and int(duration_ms) < 0:
                raise ValueError("duration_ms must be non-negative")
            if not retention_indefinite and int(retention_seconds) < MIN_RETENTION_SECONDS:
                raise ValueError("audio retention must be at least 60 seconds")
        else:
            if normalized_role:
                raise ValueError("semantic_role is only supported for audio attachments")
            if not retention_indefinite and int(retention_seconds) < 1:
                raise ValueError("attachment retention must be positive")
        requires_upload = True if upload_required is None else bool(upload_required)
        normalized_duration = int(duration_ms) if duration_ms is not None else None
        normalized_retention_indefinite = bool(retention_indefinite)
        normalized_retention = (
            None if normalized_retention_indefinite else int(retention_seconds)
        )
        normalized_key = str(idempotency_key or "").strip()
        if normalized_key and (
            len(normalized_key) > 256
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in normalized_key)
        ):
            raise ValueError("attachment idempotency key must be bounded printable ASCII")
        request_digest = hashlib.sha256(
            json.dumps(
                {
                    "filename": str(filename),
                    "media_type": normalized_media_type,
                    "size_bytes": declared_size,
                    "sha256": digest,
                    "semantic_role": normalized_role,
                    "duration_ms": normalized_duration,
                    "retention_seconds": normalized_retention,
                    "retention_indefinite": normalized_retention_indefinite,
                    "upload_required": bool(requires_upload),
                    "attachment_policy": str(attachment_policy or "standard")
                    .strip()
                    .casefold(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        attachment_id, now = _new_id("att"), _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_key:
                previous = connection.execute(
                    """SELECT request_digest, attachment_id
                       FROM attachment_stage_idempotency
                       WHERE session_id=? AND owner_id=? AND idempotency_key=?""",
                    (str(session_id), str(owner_id), normalized_key),
                ).fetchone()
                if previous is not None:
                    if str(previous["request_digest"]) != request_digest:
                        raise IdempotencyConflict(
                            "attachment stage idempotency key was reused with different metadata"
                        )
                    row = connection.execute(
                        "SELECT * FROM session_attachments WHERE attachment_id=? "
                        "AND session_id=? AND owner_id=?",
                        (
                            str(previous["attachment_id"]),
                            str(session_id),
                            str(owner_id),
                        ),
                    ).fetchone()
                    if row is None:
                        raise SessionConflict(
                            "idempotent attachment stage is no longer available"
                        )
                    result = dict(row)
                    result["retention_indefinite"] = bool(
                        result["retention_indefinite"]
                    )
                    result["upload_required"] = bool(result["upload_required"])
                    return result
            connection.execute(
                """INSERT INTO session_attachments(attachment_id,session_id,owner_id,filename,
                   media_type,size_bytes,sha256,semantic_role,duration_ms,
                   retention_seconds,retention_indefinite,upload_required,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    attachment_id,
                    str(session_id),
                    str(owner_id),
                    str(filename),
                    normalized_media_type,
                    declared_size,
                    digest,
                    normalized_role,
                    normalized_duration,
                    normalized_retention,
                    int(normalized_retention_indefinite),
                    int(requires_upload),
                    now,
                ),
            )
            if normalized_key:
                connection.execute(
                    """INSERT INTO attachment_stage_idempotency(
                           session_id,owner_id,idempotency_key,request_digest,
                           attachment_id,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        str(session_id),
                        str(owner_id),
                        normalized_key,
                        request_digest,
                        attachment_id,
                        now,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (attachment_id,),
            ).fetchone()
        result = dict(row)
        result["retention_indefinite"] = bool(result["retention_indefinite"])
        result["upload_required"] = bool(result["upload_required"])
        return result

    @staticmethod
    def _safe_attachment_id(attachment_id: str) -> str:
        value = str(attachment_id or "").strip()
        if not value or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in value
        ):
            raise ValueError("invalid attachment id")
        return value

    def _attachment_file_path(self, attachment_id: str, filename: str) -> Path:
        safe_id = self._safe_attachment_id(attachment_id)
        suffix = Path(str(filename or "")).suffix.casefold()
        if (
            not suffix.startswith(".")
            or len(suffix) > 16
            or not suffix[1:].isalnum()
        ):
            suffix = ""
        return self.attachment_files_root / f"{safe_id}{suffix}"

    def _validated_attachment_file(self, attachment: Mapping[str, Any]) -> Path:
        path = self._attachment_file_path(
            str(attachment["attachment_id"]), str(attachment["filename"])
        )
        try:
            observed_size = path.stat().st_size
        except OSError as exc:
            raise SessionConflict("uploaded attachment is unavailable") from exc
        if observed_size != int(attachment["size_bytes"]):
            raise SessionConflict("uploaded attachment size changed after intake")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
        except OSError as exc:
            raise SessionConflict("uploaded attachment is unavailable") from exc
        if digest.hexdigest() != str(attachment["sha256"]):
            raise SessionConflict("uploaded attachment digest changed after intake")
        return path

    def upload_attachment_bytes(
        self,
        *,
        session_id: str,
        owner_id: str,
        attachment_id: str,
        payload: bytes,
        audio_direction: str = "input",
    ) -> dict[str, Any]:
        """Validate and atomically materialize one staged Session attachment."""

        if not isinstance(payload, bytes):
            raise ValueError("attachment payload must be bytes")
        normalized_audio_direction = str(audio_direction or "input").strip().casefold()
        if normalized_audio_direction not in {"input", "output"}:
            raise ValueError("audio_direction must be input or output")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=? AND owner_id=?""",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
        if row is None:
            raise SessionNotFound("attachment not found")
        attachment = dict(row)
        if str(attachment["state"]) not in {"staged", "committed"}:
            raise SessionConflict("attachment is no longer available for upload")
        actual_digest = hashlib.sha256(payload).hexdigest()
        if len(payload) != int(attachment["size_bytes"]):
            raise SessionConflict("attachment size does not match staged metadata")
        if actual_digest != str(attachment["sha256"]):
            raise SessionConflict("attachment digest does not match staged metadata")

        existing_asset = str(attachment.get("asset_id") or "")
        if existing_asset:
            if str(attachment["media_type"]).casefold().startswith("audio/"):
                try:
                    self.audio_assets.describe(
                        existing_asset, owner_id=owner_id, session_id=session_id
                    )
                except AudioAssetError as exc:
                    raise SessionConflict("staged audio asset is unavailable") from exc
            else:
                self._validated_attachment_file(attachment)
            return attachment
        if attachment["state"] != "staged":
            raise SessionConflict("only staged attachments can be uploaded")

        if str(attachment["media_type"]).casefold().startswith("audio/"):
            audio_format = normalize_audio_format(
                Path(str(attachment["filename"])).suffix,
                mime_type=str(attachment["media_type"]),
            )
            asset = self.audio_assets.create(
                payload,
                owner_id=owner_id,
                session_id=session_id,
                direction=normalized_audio_direction,
                mime_type=str(attachment["media_type"]),
                audio_format=audio_format,
                asset_id=str(attachment_id),
                filename=str(attachment["filename"]),
                duration_ms=attachment.get("duration_ms"),
                retention_seconds=int(
                    attachment.get("retention_seconds") or DEFAULT_RETENTION_SECONDS
                ),
                retention_indefinite=bool(attachment.get("retention_indefinite")),
                correlation={"attachment_id": str(attachment_id)},
            )
            stored_asset_id = str(asset["asset_id"])
            observed_duration_ms = asset.get("duration_ms")
        else:
            target = self._attachment_file_path(
                str(attachment_id), str(attachment["filename"])
            )
            partial = target.with_suffix(f"{target.suffix}.{uuid4().hex}.partial")
            try:
                partial.write_bytes(payload)
                try:
                    os.chmod(partial, 0o600)
                except OSError:
                    pass
                partial.replace(target)
            except OSError as exc:
                partial.unlink(missing_ok=True)
                raise SessionConflict("attachment bytes could not be stored") from exc
            stored_asset_id = str(attachment_id)
            observed_duration_ms = None
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """UPDATE session_attachments SET asset_id=?, uploaded_at=?,
                       duration_ms=COALESCE(duration_ms, ?)
                   WHERE attachment_id=? AND state='staged'""",
                (
                    stored_asset_id,
                    now,
                    observed_duration_ms,
                    str(attachment_id),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (str(attachment_id),),
            ).fetchone()
        return dict(updated)

    def upload_attachment_file(
        self,
        *,
        session_id: str,
        owner_id: str,
        attachment_id: str,
        source_path: Path | str,
    ) -> dict[str, Any]:
        """Atomically copy one staged non-audio attachment without whole buffering."""

        source = Path(source_path)
        if source.is_symlink():
            raise ValueError("attachment symlinks are not supported")
        try:
            source_size = source.stat().st_size
        except OSError as exc:
            raise SessionConflict("attachment source is unavailable") from exc
        if not source.is_file():
            raise ValueError("attachment source must be a regular file")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=? AND owner_id=?""",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
        if row is None:
            raise SessionNotFound("attachment not found")
        attachment = dict(row)
        if str(attachment["state"]) not in {"staged", "committed"}:
            raise SessionConflict("attachment is no longer available for upload")
        if str(attachment["media_type"]).casefold().startswith("audio/"):
            raise ValueError("streaming Session upload requires file-backed media")
        existing_asset = str(attachment.get("asset_id") or "")
        if existing_asset:
            self._validated_attachment_file(attachment)
            return attachment
        if attachment["state"] != "staged":
            raise SessionConflict("only staged attachments can be uploaded")
        if source_size != int(attachment["size_bytes"]):
            raise SessionConflict("attachment size does not match staged metadata")

        target = self._attachment_file_path(
            str(attachment_id), str(attachment["filename"])
        )
        partial = target.with_suffix(f"{target.suffix}.{uuid4().hex}.partial")
        digest = hashlib.sha256()
        observed_size = 0
        try:
            with source.open("rb") as input_handle, partial.open("xb") as output_handle:
                while chunk := input_handle.read(8 * 1024 * 1024):
                    observed_size += len(chunk)
                    digest.update(chunk)
                    output_handle.write(chunk)
                output_handle.flush()
            if observed_size != int(attachment["size_bytes"]):
                raise SessionConflict("attachment changed while being copied")
            if digest.hexdigest() != str(attachment["sha256"]):
                raise SessionConflict("attachment digest does not match staged metadata")
            try:
                os.chmod(partial, 0o600)
            except OSError:
                pass
            partial.replace(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """UPDATE session_attachments SET asset_id=?, uploaded_at=?
                   WHERE attachment_id=? AND state='staged'""",
                (str(attachment_id), now, str(attachment_id)),
            )
            updated = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (str(attachment_id),),
            ).fetchone()
        return dict(updated)

    def commit_attachment(
        self, *, session_id: str, owner_id: str, attachment_id: str
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=? AND session_id=? AND owner_id=?",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound("attachment not found")
            if str(row["state"]) not in {"staged", "committed"}:
                raise SessionConflict("attachment is no longer available for commit")
            if bool(row["upload_required"]) and not str(row["asset_id"] or ""):
                raise SessionConflict("attachment bytes must be uploaded before commit")
            if str(row["asset_id"] or ""):
                if str(row["media_type"]).casefold().startswith("audio/"):
                    try:
                        self.audio_assets.describe(
                            str(row["asset_id"]),
                            owner_id=owner_id,
                            session_id=session_id,
                        )
                    except AudioAssetError as exc:
                        raise SessionConflict("uploaded attachment is unavailable") from exc
                else:
                    self._validated_attachment_file(dict(row))
            connection.execute(
                "UPDATE session_attachments SET state='committed', committed_at=COALESCE(committed_at,?) WHERE attachment_id=?",
                (now, str(attachment_id)),
            )
            updated = connection.execute(
                "SELECT * FROM session_attachments WHERE attachment_id=?",
                (str(attachment_id),),
            ).fetchone()
        result = dict(updated)
        result["retention_indefinite"] = bool(result["retention_indefinite"])
        result["upload_required"] = bool(result["upload_required"])
        return result

    def discard_unbound_attachments(
        self,
        *,
        session_id: str,
        owner_id: str,
        attachment_ids: Iterable[str],
    ) -> list[str]:
        """Remove a failed intake batch only while none of it is message-bound."""

        normalized_ids = list(
            dict.fromkeys(
                self._safe_attachment_id(item)
                for item in attachment_ids
                if str(item or "").strip()
            )
        )
        if not normalized_ids:
            return []
        rows: list[dict[str, Any]] = []
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for attachment_id in normalized_ids:
                row = connection.execute(
                    """SELECT * FROM session_attachments
                       WHERE attachment_id=? AND session_id=? AND owner_id=?""",
                    (attachment_id, str(session_id), str(owner_id)),
                ).fetchone()
                if row is None:
                    continue
                reference_pattern = '%"attachment_id":"' + attachment_id + '"%'
                bound = any(
                    connection.execute(query, parameters).fetchone() is not None
                    for query, parameters in (
                        (
                            "SELECT 1 FROM messages WHERE session_id=? "
                            "AND content_json LIKE ? LIMIT 1",
                            (str(session_id), reference_pattern),
                        ),
                        (
                            "SELECT 1 FROM run_audio_assets WHERE attachment_id=? LIMIT 1",
                            (attachment_id,),
                        ),
                        (
                            "SELECT 1 FROM run_output_attachments WHERE attachment_id=? LIMIT 1",
                            (attachment_id,),
                        ),
                        (
                            "SELECT 1 FROM voice_transcripts WHERE attachment_id=? LIMIT 1",
                            (attachment_id,),
                        ),
                    )
                )
                if bound:
                    raise SessionConflict(
                        "message-bound attachments cannot be discarded"
                    )
                rows.append(dict(row))
            for row in rows:
                connection.execute(
                    "DELETE FROM attachment_stage_idempotency WHERE attachment_id=?",
                    (str(row["attachment_id"]),),
                )
                connection.execute(
                    "DELETE FROM session_attachments WHERE attachment_id=?",
                    (str(row["attachment_id"]),),
                )
        for row in rows:
            if str(row.get("asset_id") or "") and not str(
                row.get("media_type") or ""
            ).casefold().startswith("audio/"):
                try:
                    self._attachment_file_path(
                        str(row["attachment_id"]), str(row["filename"])
                    ).unlink(missing_ok=True)
                except OSError:
                    pass
        return [str(row["attachment_id"]) for row in rows]

    def attachment_bytes(
        self, *, session_id: str, owner_id: str, attachment_id: str
    ) -> tuple[dict[str, Any], bytes]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=? AND owner_id=?
                     AND state='committed'""",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
        if row is None or not str(row["asset_id"] or ""):
            raise SessionNotFound("attachment not found")
        attachment = dict(row)
        if str(attachment["media_type"]).casefold().startswith("audio/"):
            return self.audio_assets.read_bytes(
                str(attachment["asset_id"]), owner_id=owner_id, session_id=session_id
            )
        path = self._validated_attachment_file(attachment)
        return attachment, path.read_bytes()

    def attachment_canonical_part(
        self,
        *,
        session_id: str,
        owner_id: str,
        attachment_id: str,
        item_index: int,
        semantic_role: str | None = None,
        presentation_role: str | None = None,
        caption: str = "",
        detail: str = "",
    ) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=? AND owner_id=?
                     AND state='committed'""",
                (str(attachment_id), str(session_id), str(owner_id)),
            ).fetchone()
        if row is None or not str(row["asset_id"] or ""):
            raise SessionNotFound("attachment not found")
        attachment = dict(row)
        media_type = str(attachment["media_type"])
        modality = modality_for_attachment(
            "", mime_type=media_type, filename=str(attachment["filename"])
        )
        if modality == "audio":
            metadata, local_path = self.audio_assets.authorized_path(
                str(attachment["asset_id"]),
                owner_id=owner_id,
                session_id=session_id,
            )
        else:
            local_path = self._validated_attachment_file(attachment)
            metadata = attachment
        role = str(
            semantic_role or attachment["semantic_role"] or "audio_attachment"
        )
        resolved_presentation_role = str(
            presentation_role or ""
        ).strip().casefold()
        if resolved_presentation_role not in {
            "",
            "audio",
            "document",
            "image",
            "photo",
            "video",
            "voice",
        }:
            raise ValueError("attachment presentation_role is invalid")
        part = {
            "type": "media",
            "item_index": int(item_index),
            "attachment_id": str(attachment_id),
            "modality": modality,
            "kind": (
                "voice"
                if modality == "audio" and role == "voice_message"
                else resolved_presentation_role or modality
            ),
            "mime_type": media_type,
            "filename": str(attachment["filename"]),
            "caption": str(caption or ""),
            "local_ref": str(local_path),
            "size_bytes": int(metadata["size_bytes"]),
            "sha256": str(metadata["sha256"]),
            "transport": {},
        }
        if modality == "audio":
            part["semantic_role"] = role
            part["duration_ms"] = attachment["duration_ms"]
        if resolved_presentation_role:
            part["presentation_role"] = resolved_presentation_role
        if detail:
            part["detail"] = str(detail)
        return part

    def _run_output_attachment_rows(
        self,
        *,
        request_id: str,
        session_id: str | None = None,
        owner_id: str | None = None,
        agent_id: str | None = None,
        idempotency_key: str | None = None,
        unpublished_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses = ["r.request_id=?", "s.instance_id=?"]
        params: list[Any] = [str(request_id), self.instance_id]
        if session_id is not None:
            clauses.append("r.session_id=?")
            params.append(str(session_id))
        if owner_id is not None:
            clauses.append("s.owner_id=?")
            params.append(str(owner_id))
        if agent_id is not None:
            clauses.append("r.agent_id=?")
            params.append(str(agent_id).lower())
        if idempotency_key is not None:
            clauses.append("o.idempotency_key=?")
            params.append(str(idempotency_key))
        if unpublished_only:
            clauses.append("o.publication_id IS NULL")
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT o.*, r.session_id, r.request_id, s.owner_id, r.agent_id
                FROM run_output_attachments AS o
                JOIN runs AS r ON r.run_id=o.run_id
                JOIN sessions AS s ON s.session_id=r.session_id
                WHERE {" AND ".join(clauses)}
                ORDER BY o.output_index ASC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def _canonical_run_output_attachments(
        self, rows: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        parts = []
        for raw in rows:
            row = dict(raw)
            parts.append(
                self.attachment_canonical_part(
                    session_id=str(row["session_id"]),
                    owner_id=str(row["owner_id"]),
                    attachment_id=str(row["attachment_id"]),
                    item_index=int(row["output_index"]),
                    caption=str(row.get("caption") or ""),
                    detail=str(row.get("detail") or ""),
                )
            )
        return parts

    def run_output_attachment_group(
        self,
        *,
        request_id: str,
        session_id: str,
        owner_id: str,
        agent_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        rows = self._run_output_attachment_rows(
            request_id=request_id,
            session_id=session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            idempotency_key=idempotency_key,
        )
        if not rows:
            return None
        digests = {str(row["request_digest"]) for row in rows}
        if len(digests) != 1:
            raise SessionConflict("frontend attachment output group is inconsistent")
        attachments = self._canonical_run_output_attachments(rows)
        group_id_material = "\0".join(
            (
                self.instance_id,
                str(rows[0]["session_id"]),
                str(rows[0]["request_id"]),
                str(rows[0]["idempotency_key"]),
            )
        )
        group_id = "grp_" + hashlib.sha256(
            group_id_material.encode("utf-8")
        ).hexdigest()[:32]
        media_group = normalize_media_group(
            {
                "type": "hashi.media-group",
                "version": 1,
                "group_id": group_id,
                "retention_class": "message_bound",
                "attachments": [
                    {
                        "attachment_id": str(item["attachment_id"]),
                        "ordinal": index,
                        "sha256": str(item["sha256"]),
                        "filename": str(item["filename"]),
                    }
                    for index, item in enumerate(attachments)
                ],
            }
        )
        return {
            "request_digest": next(iter(digests)),
            "group_id": group_id,
            "media_group": media_group,
            "attachments": attachments,
        }

    def pending_incremental_telegram_deliveries(
        self, *, agent_id: str, limit: int = 20
    ) -> list[dict[str, str]]:
        """Find this Agent's durable publication tasks needing FC dispatch.

        Expired claims are included so the normal claim path can mark their
        uncertain external outcome unknown without sending them again.
        """
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("incremental delivery limit must be between 1 and 100")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT t.session_id, s.owner_id, r.request_id, p.event_id
                   FROM run_deliverable_publications AS p
                   JOIN runs AS r ON r.run_id=p.run_id
                   JOIN sessions AS s ON s.session_id=r.session_id
                   JOIN connector_delivery_tasks AS t ON t.event_id=p.event_id
                   WHERE s.instance_id=? AND s.agent_id=? AND r.agent_id=?
                     AND t.session_id=s.session_id AND t.run_id=r.run_id
                     AND t.connector_id='telegram'
                     AND (t.state IN ('pending','retry') OR
                          (t.state='claimed' AND t.lease_expires_at<=?))
                   ORDER BY t.created_at, t.task_id LIMIT ?""",
                (self.instance_id, str(agent_id).lower(), str(agent_id).lower(), now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_deliverable_publication(
        self,
        *,
        request_id: str,
        session_id: str,
        owner_id: str,
        agent_id: str,
        publication_id: str,
        publication_digest: str | None = None,
    ) -> dict[str, Any] | None:
        """Read one durable Run result and each frozen endpoint's current state."""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT p.* FROM run_deliverable_publications AS p
                   JOIN runs AS r ON r.run_id=p.run_id
                   JOIN sessions AS s ON s.session_id=r.session_id
                   WHERE r.request_id=? AND r.session_id=? AND s.instance_id=?
                     AND s.owner_id=? AND r.agent_id=? AND p.publication_id=?""",
                (
                    str(request_id), str(session_id), self.instance_id,
                    str(owner_id), str(agent_id).lower(), str(publication_id),
                ),
            ).fetchone()
            if row is None:
                return None
            if (
                publication_digest is not None
                and str(row["publication_digest"]) != str(publication_digest)
            ):
                raise IdempotencyConflict(
                    "deliverable publication ID is bound to different content"
                )
            tasks = connection.execute(
                """SELECT t.connector_id, t.endpoint_id, t.state AS task_state,
                          t.attempt_count, t.last_error_code, c.status,
                          c.proof_json
                   FROM connector_delivery_tasks AS t
                   LEFT JOIN connector_delivery_receipts AS c
                     ON c.event_id=t.event_id AND c.endpoint_id=t.endpoint_id
                   WHERE t.event_id=? ORDER BY t.role, t.endpoint_id""",
                (str(row["event_id"]),),
            ).fetchall()
        group = self.run_output_attachment_group(
            request_id=request_id,
            session_id=session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            idempotency_key=str(row["idempotency_key"]),
        )
        if group is None or str(group["group_id"]) != str(row["group_id"]):
            raise SessionConflict("deliverable publication attachment group is missing")
        deliveries = []
        for task in tasks:
            receipt_status = str(task["status"] or "")
            task_state = str(task["task_state"])
            if receipt_status in {"accepted", "delivered", "failed", "unknown"}:
                state = receipt_status
            elif task_state in {"pending", "retry", "claimed"}:
                state = "queued"
            elif task_state == "failed":
                state = "failed"
            else:
                state = "unknown"
            deliveries.append(
                {
                    "connector_id": str(task["connector_id"]),
                    "endpoint_id": str(task["endpoint_id"]),
                    "state": state,
                    "task_state": task_state,
                    "attempt_count": int(task["attempt_count"]),
                    "last_error_code": task["last_error_code"],
                    "proof": (
                        _json_object(task["proof_json"])
                        if task["proof_json"] else None
                    ),
                }
            )
        return {
            "publication_id": str(row["publication_id"]),
            "publication_digest": str(row["publication_digest"]),
            "request_id": str(request_id),
            "run_id": str(row["run_id"]),
            "session_id": str(session_id),
            "group_id": group["group_id"],
            "media_group": group["media_group"],
            "attachments": group["attachments"],
            "attachment_count": len(group["attachments"]),
            "message_id": str(row["message_id"]),
            "event_id": str(row["event_id"]),
            "persisted": True,
            "deliveries": deliveries,
        }

    def run_output_attachment_content(
        self,
        request_id: str,
        *,
        owner_id: str | None = None,
        agent_id: str | None = None,
        unpublished_only: bool = False,
    ) -> list[dict[str, Any]]:
        return self._canonical_run_output_attachments(
            self._run_output_attachment_rows(
                request_id=request_id,
                owner_id=owner_id,
                agent_id=agent_id,
                unpublished_only=unpublished_only,
            )
        )

    def bind_run_output_attachments(
        self,
        *,
        request_id: str,
        session_id: str,
        owner_id: str,
        agent_id: str,
        idempotency_key: str,
        request_digest: str,
        attachments: Iterable[Mapping[str, Any]],
        attachment_policy: str = "standard",
        publication_id: str | None = None,
        publication_digest: str | None = None,
        publication_text: str = "",
        expected_run_id: str | None = None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 512:
            raise ValueError("frontend attachment idempotency key is invalid")
        digest = str(request_digest or "").strip().casefold()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("frontend attachment request digest is invalid")
        publishing = publication_id is not None
        stable_publication_id = str(publication_id or "").strip()
        publication_hash = str(publication_digest or "").strip().casefold()
        publication_text = str(publication_text or "").strip()
        if publishing:
            if (
                not 1 <= len(stable_publication_id) <= 128
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
                       for c in stable_publication_id)
            ):
                raise ValueError("deliverable publication ID is invalid")
            if len(publication_hash) != 64 or any(
                c not in "0123456789abcdef" for c in publication_hash
            ):
                raise ValueError("deliverable publication digest is invalid")
            if len(publication_text) > 4096:
                raise ValueError("deliverable text exceeds 4096 characters")
            if not expected_run_id or type(fencing_token) is not int or fencing_token <= 0:
                raise SessionConflict("deliverable publication requires the current Run executor")
        bindings = []
        seen: set[str] = set()
        for raw in attachments:
            if not isinstance(raw, Mapping):
                raise ValueError("frontend attachment bindings must be objects")
            attachment_id = str(raw.get("attachment_id") or "").strip()
            caption = str(raw.get("caption") or "").strip()
            detail = str(raw.get("detail") or "").strip()
            if not attachment_id or attachment_id in seen:
                raise ValueError("frontend attachment bindings must be distinct")
            if len(caption) > 4096 or len(detail) > 64:
                raise ValueError("frontend attachment presentation metadata is too long")
            seen.add(attachment_id)
            bindings.append(
                {
                    "attachment_id": attachment_id,
                    "caption": caption,
                    "detail": detail,
                }
            )
        max_count, _max_attachment_bytes, max_total_bytes = _attachment_policy_limits(
            attachment_policy
        )
        if not bindings or len(bindings) > max_count:
            raise ValueError("frontend attachment binding count is invalid")

        now = _utc_now()
        replayed = False
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT r.* FROM runs AS r
                JOIN sessions AS s ON s.session_id=r.session_id
                WHERE r.request_id=? AND r.session_id=? AND s.instance_id=?
                  AND s.owner_id=? AND r.agent_id=?
                """,
                (
                    str(request_id),
                    str(session_id),
                    self.instance_id,
                    str(owner_id),
                    str(agent_id).lower(),
                ),
            ).fetchone()
            if run is None:
                raise SessionNotFound(str(request_id))
            if publishing and str(run["run_id"]) != str(expected_run_id):
                raise SessionConflict("deliverable publication Run identity mismatch")
            prior_publication = (
                connection.execute(
                    """SELECT * FROM run_deliverable_publications
                       WHERE run_id=? AND publication_id=?""",
                    (str(run["run_id"]), stable_publication_id),
                ).fetchone()
                if publishing else None
            )
            if prior_publication is not None and str(
                prior_publication["publication_digest"]
            ) != publication_hash:
                raise IdempotencyConflict(
                    "deliverable publication ID is bound to different content"
                )
            existing = connection.execute(
                """
                SELECT * FROM run_output_attachments
                WHERE run_id=? AND idempotency_key=?
                ORDER BY group_index ASC
                """,
                (str(run["run_id"]), key),
            ).fetchall()
            if existing:
                if any(str(row["request_digest"]) != digest for row in existing):
                    raise IdempotencyConflict(
                        "frontend attachment idempotency key is bound to different files"
                    )
                replayed = True
                if publishing and prior_publication is None:
                    raise SessionConflict("deliverable publication record is missing")
            else:
                if prior_publication is not None:
                    raise SessionConflict("deliverable publication attachment group is missing")
                if str(run["state"]) != "running":
                    raise SessionConflict(
                        "frontend attachments require the current running Session Run"
                    )
                if publishing and int(run["fencing_token"]) != fencing_token:
                    raise StaleFencingToken("deliverable publisher fencing token is stale")
                totals = connection.execute(
                    """
                    SELECT COUNT(*) AS attachment_count,
                           COALESCE(SUM(a.size_bytes),0) AS total_bytes,
                           COALESCE(MAX(o.output_index),0) AS last_index
                    FROM run_output_attachments AS o
                    JOIN session_attachments AS a ON a.attachment_id=o.attachment_id
                    WHERE o.run_id=?
                    """,
                    (str(run["run_id"]),),
                ).fetchone()
                attachment_count = int(totals["attachment_count"])
                total_bytes = int(totals["total_bytes"])
                last_index = int(totals["last_index"])
                if attachment_count + len(bindings) > max_count:
                    raise SessionConflict(
                        "assistant reply exceeds the configured attachment count limit"
                    )
                rows = []
                for binding in bindings:
                    attachment = connection.execute(
                        """
                        SELECT * FROM session_attachments
                        WHERE attachment_id=? AND session_id=? AND owner_id=?
                          AND state='committed'
                        """,
                        (
                            binding["attachment_id"],
                            str(session_id),
                            str(owner_id),
                        ),
                    ).fetchone()
                    if attachment is None or not str(attachment["asset_id"] or ""):
                        raise SessionConflict(
                            "frontend attachment is unavailable or not committed"
                        )
                    total_bytes += int(attachment["size_bytes"])
                    if total_bytes > max_total_bytes:
                        raise SessionConflict(
                            "assistant reply exceeds the configured attachment size limit"
                        )
                    rows.append(dict(attachment))
                for group_index, (binding, attachment) in enumerate(
                    zip(bindings, rows, strict=True), start=1
                ):
                    if str(attachment["media_type"]).casefold().startswith("audio/"):
                        self.audio_assets.describe(
                            str(attachment["asset_id"]),
                            owner_id=owner_id,
                            session_id=session_id,
                        )
                    else:
                        self._validated_attachment_file(attachment)
                    connection.execute(
                        """
                        INSERT INTO run_output_attachments(
                            run_id, attachment_id, output_index, idempotency_key,
                            publication_id, group_index, request_digest, caption,
                            detail, created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            str(run["run_id"]),
                            binding["attachment_id"],
                            last_index + group_index,
                            key,
                            stable_publication_id if publishing else None,
                            group_index,
                            digest,
                            binding["caption"],
                            binding["detail"],
                            now,
                        ),
                    )
                if publishing:
                    group_material = "\0".join(
                        (self.instance_id, str(session_id), str(request_id), key)
                    )
                    group_id = "grp_" + hashlib.sha256(
                        group_material.encode("utf-8")
                    ).hexdigest()[:32]
                    content = (
                        [{"type": "text", "text": publication_text}]
                        if publication_text else []
                    )
                    for index, binding in enumerate(bindings, start=len(content) + 1):
                        part = self.attachment_canonical_part(
                            session_id=str(session_id),
                            owner_id=str(owner_id),
                            attachment_id=binding["attachment_id"],
                            item_index=index,
                            caption=binding["caption"],
                            detail=binding["detail"],
                        )
                        part["group_id"] = group_id
                        content.append(part)
                    content_json = _json(content)
                    message_id = _new_id("msg")
                    connection.execute(
                        """INSERT INTO messages(
                               message_id, session_id, run_id, ordinal,
                               context_generation, role, author_id, source,
                               message_context_json, content_json, text,
                               content_hash, created_at
                           ) VALUES(?, ?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            message_id, str(session_id), str(run["run_id"]),
                            self._next_ordinal(connection, str(session_id)),
                            int(run["context_generation"]), str(run["agent_id"]),
                            "frontend_publish_deliverable",
                            _json({
                                "kind": "deliverable",
                                "publication_id": stable_publication_id,
                                "group_id": group_id,
                            }),
                            content_json, publication_text,
                            hashlib.sha256(content_json.encode("utf-8")).hexdigest(),
                            now,
                        ),
                    )
                    event = self._append_event(
                        connection,
                        session_id=str(session_id),
                        run_id=str(run["run_id"]),
                        kind="assistant.output.available",
                        status="available",
                        phase="incremental",
                        summary=publication_text,
                        detail={
                            "message_id": message_id,
                            "request_id": str(request_id),
                            "publication_id": stable_publication_id,
                            "group_id": group_id,
                            "disposition": "incremental",
                            "content": content,
                        },
                        outbox=True,
                    )
                    from orchestrator.frontend_connector_registry import (
                        get_connector_capabilities,
                        supports_running_media_delivery,
                    )

                    endpoint_tasks = connection.execute(
                        """SELECT task_id, connector_id, endpoint_id
                           FROM connector_delivery_tasks WHERE event_id=?""",
                        (str(event["event_id"]),),
                    ).fetchall()
                    for task in endpoint_tasks:
                        capabilities = get_connector_capabilities(
                            str(task["connector_id"]),
                            endpoint_id=str(task["endpoint_id"]),
                        )
                        media_egress = "media" in capabilities["egress"]
                        if not media_egress or not supports_running_media_delivery(
                            str(task["connector_id"])
                        ):
                            error_code = (
                                "incremental_media_unsupported"
                                if not media_egress
                                else "incremental_media_consumer_unavailable"
                            )
                            connection.execute(
                                """UPDATE connector_delivery_tasks
                                   SET state='failed',
                                       last_error_code=?,
                                       completed_at=? WHERE task_id=?""",
                                (error_code, now, str(task["task_id"])),
                            )
                    self._refresh_delivery_outbox_aggregate(
                        connection, event_id=str(event["event_id"])
                    )
                    connection.execute(
                        """INSERT INTO frontend_message_events(
                               message_id, event_id, session_id) VALUES(?, ?, ?)""",
                        (message_id, str(event["event_id"]), str(session_id)),
                    )
                    connection.execute(
                        """INSERT INTO run_deliverable_publications(
                               run_id, publication_id, publication_digest,
                               attachment_digest, idempotency_key, group_id,
                               message_id, event_id, created_at
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(run["run_id"]), stable_publication_id,
                            publication_hash, digest, key, group_id,
                            message_id, str(event["event_id"]), now,
                        ),
                    )
                    self._queue_foreground_message(
                        connection, session_id=str(session_id), message_id=message_id
                    )
                    connection.execute(
                        "UPDATE sessions SET updated_at=?, revision=revision+1 "
                        "WHERE session_id=?",
                        (now, str(session_id)),
                    )

        group = self.run_output_attachment_group(
            request_id=request_id,
            session_id=session_id,
            owner_id=owner_id,
            agent_id=agent_id,
            idempotency_key=key,
        )
        if group is None:
            raise SessionConflict("frontend attachment output was not persisted")
        durable_attachment_ids = [
            str(part.get("attachment_id") or "")
            for part in group["attachments"]
            if str(part.get("attachment_id") or "")
        ]
        durable_audio_attachment_ids = [
            str(part.get("attachment_id") or "")
            for part in group["attachments"]
            if str(part.get("modality") or "").casefold() == "audio"
            and str(part.get("attachment_id") or "")
        ]
        if durable_attachment_ids:
            # Once bytes are bound to a visible Message, their lifetime follows
            # that Message. This also repairs replayed groups created by older
            # tool versions with preview-only retention.
            durable_audio_assets: list[tuple[str, str]] = []
            with self._lock, self._connection() as connection:
                for attachment_id in durable_audio_attachment_ids:
                    row = connection.execute(
                        """SELECT asset_id FROM session_attachments
                           WHERE attachment_id=? AND session_id=? AND owner_id=?
                             AND state='committed'""",
                        (attachment_id, str(session_id), str(owner_id)),
                    ).fetchone()
                    asset_id = str(row["asset_id"] or "") if row is not None else ""
                    if not asset_id:
                        raise SessionConflict(
                            "frontend audio attachment is unavailable"
                        )
                    durable_audio_assets.append((attachment_id, asset_id))
            for _attachment_id, asset_id in durable_audio_assets:
                self.audio_assets.set_indefinite(
                    asset_id,
                    owner_id=owner_id,
                    session_id=session_id,
                )
            with self._lock, self._connection() as connection:
                connection.executemany(
                    """UPDATE session_attachments
                       SET retention_seconds=NULL, retention_indefinite=1
                       WHERE attachment_id=? AND session_id=? AND owner_id=?""",
                    [
                        (attachment_id, str(session_id), str(owner_id))
                        for attachment_id in durable_attachment_ids
                    ],
                )
        return {**group, "replayed": replayed}

    def audio_asset_bytes(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> tuple[dict[str, Any], bytes]:
        return self.audio_assets.read_bytes(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def claim_output_audio_asset(
        self,
        *,
        session_id: str,
        owner_id: str,
        request_id: str,
        asset_id: str,
    ) -> dict[str, Any]:
        return self.audio_assets.claim(
            asset_id,
            owner_id=owner_id,
            session_id=session_id,
            request_id=request_id,
        )

    def audio_asset_path(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> tuple[dict[str, Any], Path]:
        return self.audio_assets.authorized_path(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def acquire_audio_asset(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> dict[str, Any]:
        return self.audio_assets.acquire(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def release_audio_asset(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> dict[str, Any]:
        return self.audio_assets.release(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def archive_audio_asset(
        self, *, session_id: str, owner_id: str, asset_id: str
    ) -> dict[str, Any]:
        return self.audio_assets.set_indefinite(
            asset_id, owner_id=owner_id, session_id=session_id
        )

    def create_output_audio_asset(
        self,
        payload: bytes,
        *,
        session_id: str,
        owner_id: str,
        run_id: str,
        request_id: str,
        mime_type: str,
        audio_format: str,
        filename: str = "",
        duration_ms: int | None = None,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        retention_indefinite: bool = False,
        provider: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        self.get_run(run_id, owner_id=owner_id)
        asset = self.audio_assets.create(
            payload,
            owner_id=owner_id,
            session_id=session_id,
            direction="output",
            mime_type=mime_type,
            audio_format=audio_format,
            filename=filename,
            duration_ms=duration_ms,
            retention_seconds=retention_seconds,
            retention_indefinite=retention_indefinite,
            correlation={
                "run_id": str(run_id),
                "request_id": str(request_id),
                "provider": str(provider),
                "model": str(model),
            },
        )
        return {
            "type": "audio",
            "asset_id": asset["asset_id"],
            "mime_type": asset["mime_type"],
            "format": asset["format"],
            "duration_ms": asset.get("duration_ms"),
            "size_bytes": asset["size_bytes"],
            "sha256": asset["sha256"],
            "retention_expires_at": asset.get("retention_expires_at"),
            "retention_indefinite": asset["retention_indefinite"],
        }

    def cleanup_attachments(
        self, *, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """Expire only unbound, finite non-audio bytes outside active delivery."""

        observed_now = now or datetime.now(timezone.utc)
        if observed_now.tzinfo is None:
            observed_now = observed_now.replace(tzinfo=timezone.utc)
        expired: list[dict[str, Any]] = []
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM session_attachments
                WHERE state IN ('staged','committed')
                  AND retention_indefinite=0
                  AND retention_seconds IS NOT NULL
                  AND media_type NOT LIKE 'audio/%'
                ORDER BY created_at, attachment_id
                """
            ).fetchall()
            for row in rows:
                try:
                    created_at = datetime.fromisoformat(
                        str(row["created_at"]).replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                deadline = created_at + timedelta(
                    seconds=max(1, int(row["retention_seconds"]))
                )
                if observed_now < deadline:
                    continue
                attachment_id = str(row["attachment_id"])
                reference_pattern = (
                    '%"attachment_id":"'
                    + attachment_id
                    + '"%'
                )
                message_bound = connection.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE session_id=? AND content_json LIKE ? LIMIT 1
                    """,
                    (str(row["session_id"]), reference_pattern),
                ).fetchone()
                output_bound = connection.execute(
                    """
                    SELECT 1 FROM run_output_attachments
                    WHERE attachment_id=? LIMIT 1
                    """,
                    (attachment_id,),
                ).fetchone()
                if message_bound is not None or output_bound is not None:
                    connection.execute(
                        """
                        UPDATE session_attachments
                        SET retention_seconds=NULL, retention_indefinite=1
                        WHERE attachment_id=?
                        """,
                        (attachment_id,),
                    )
                    continue
                active_delivery = connection.execute(
                    """
                    SELECT 1
                    FROM connector_delivery_tasks AS t
                    JOIN run_events AS e ON e.event_id=t.event_id
                    WHERE t.session_id=?
                      AND t.state IN ('pending','retry','claimed','unknown')
                      AND e.detail_json LIKE ?
                    LIMIT 1
                    """,
                    (str(row["session_id"]), reference_pattern),
                ).fetchone()
                if active_delivery is not None:
                    continue
                if str(row["asset_id"] or ""):
                    try:
                        self._attachment_file_path(
                            attachment_id, str(row["filename"])
                        ).unlink(missing_ok=True)
                    except OSError:
                        continue
                expired_at = observed_now.astimezone(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                )
                connection.execute(
                    """
                    UPDATE session_attachments SET state='expired'
                    WHERE attachment_id=? AND state IN ('staged','committed')
                    """,
                    (attachment_id,),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=None,
                    kind="attachment.expired",
                    status="expired",
                    phase="retention",
                    summary="Unbound attachment bytes expired",
                    detail={"attachment_id": attachment_id},
                )
                expired.append(
                    {
                        "attachment_id": attachment_id,
                        "session_id": str(row["session_id"]),
                        "state": "expired",
                        "expired_at": expired_at,
                    }
                )
        return expired

    def cleanup_audio_assets(self) -> list[dict[str, Any]]:
        expired = self.audio_assets.cleanup()
        if not expired:
            return []
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for asset in expired:
                asset_id = str(asset["asset_id"])
                connection.execute(
                    """UPDATE session_attachments SET state='expired'
                       WHERE asset_id=? AND state='committed'""",
                    (asset_id,),
                )
                session_id = str(asset.get("session_id") or "")
                if not session_id:
                    continue
                session_exists = connection.execute(
                    "SELECT 1 FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if session_exists is None:
                    continue
                run_id = str(
                    dict(asset.get("correlation") or {}).get("run_id") or ""
                ) or None
                if run_id is not None:
                    run_exists = connection.execute(
                        "SELECT 1 FROM runs WHERE run_id=? AND session_id=?",
                        (run_id, session_id),
                    ).fetchone()
                    if run_exists is None:
                        run_id = None
                self._append_event(
                    connection,
                    session_id=session_id,
                    run_id=run_id,
                    kind="audio.asset.expired",
                    status="expired",
                    phase="retention",
                    summary="Retained audio bytes expired",
                    detail={"asset_id": asset_id},
                    outbox=True,
                )
        return expired

    def record_voice_transcript(
        self,
        *,
        request_id: str,
        attachment_id: str,
        text: str,
        provenance: str,
        safe_voice_state: str,
    ) -> dict[str, Any]:
        clean = str(text or "").strip()
        state = str(safe_voice_state or "released").strip().casefold()
        if state not in {
            "released",
            "ready",
            "pending_confirmation",
            "discarded",
            "unavailable",
        }:
            raise ValueError("invalid Safe Voice transcript state")
        if not clean and state != "unavailable":
            raise ValueError("voice transcript cannot be empty")
        now = _utc_now()
        transcript_id = _new_id("transcript")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM runs WHERE request_id=?", (str(request_id),)
            ).fetchone()
            if run is None:
                raise SessionNotFound(str(request_id))
            attachment = connection.execute(
                """SELECT * FROM session_attachments
                   WHERE attachment_id=? AND session_id=?""",
                (str(attachment_id), str(run["session_id"])),
            ).fetchone()
            if attachment is None:
                raise SessionConflict("transcript attachment is not part of the Session")
            existing = connection.execute(
                """SELECT * FROM voice_transcripts
                   WHERE run_id=? AND attachment_id=?
                   ORDER BY created_at ASC LIMIT 1""",
                (str(run["run_id"]), str(attachment_id)),
            ).fetchone()
            if existing is not None:
                stored_state = str(existing["safe_voice_state"])
                state_is_compatible = stored_state == state or (
                    state == "pending_confirmation"
                    and stored_state in {"released", "discarded"}
                )
                if (
                    str(existing["text"]) != clean
                    or str(existing["provenance"])
                    != str(provenance or "local_stt")
                    or not state_is_compatible
                ):
                    raise SessionConflict(
                        "voice transcript replay conflicts with the stored record"
                    )
                return dict(existing)
            connection.execute(
                """INSERT INTO voice_transcripts(
                       transcript_id,session_id,run_id,message_id,attachment_id,
                       text,provenance,safe_voice_state,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    transcript_id,
                    str(run["session_id"]),
                    str(run["run_id"]),
                    str(run["user_message_id"]),
                    str(attachment_id),
                    clean,
                    str(provenance or "local_stt"),
                    state,
                    now,
                ),
            )
            if state == "released":
                self._append_voice_transcript_projection(
                    connection,
                    message_id=str(run["user_message_id"]),
                    transcript=clean,
                )
            transcript_event_kind = (
                "voice.input.transcript_unavailable"
                if state == "unavailable"
                else "voice.input.transcript_ready"
            )
            self._append_event(
                connection,
                session_id=str(run["session_id"]),
                run_id=str(run["run_id"]),
                kind=transcript_event_kind,
                status=state,
                phase="transcription",
                summary=(
                    "Local input transcript unavailable"
                    if state == "unavailable"
                    else "Local input transcript available"
                ),
                detail={
                    "transcript_id": transcript_id,
                    "attachment_id": str(attachment_id),
                    "text": clean,
                    "provenance": str(provenance or "local_stt"),
                    "safe_voice_state": state,
                },
                outbox=True,
            )
            if state == "unavailable":
                self._append_event(
                    connection,
                    session_id=str(run["session_id"]),
                    run_id=str(run["run_id"]),
                    kind="voice.warning",
                    status="degraded",
                    phase="transcription",
                    summary=(
                        "Local voice transcription is unavailable; the native "
                        "audio response will continue."
                    ),
                    detail={
                        "transcript_id": transcript_id,
                        "attachment_id": str(attachment_id),
                        "warning_code": "local_stt_unavailable",
                    },
                    outbox=True,
                )
            if state == "pending_confirmation":
                self._append_event(
                    connection,
                    session_id=str(run["session_id"]),
                    run_id=str(run["run_id"]),
                    kind="voice.input.transcript_pending_confirmation",
                    status=state,
                    phase="safe_voice",
                    summary="Safe Voice confirmation required",
                    detail={
                        "transcript_id": transcript_id,
                        "attachment_id": str(attachment_id),
                        "text": clean,
                        "provenance": str(provenance or "local_stt"),
                        "safe_voice_state": state,
                    },
                    outbox=True,
                )
            row = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (transcript_id,),
            ).fetchone()
        return dict(row)

    def require_voice_transcript_confirmation(
        self, *, request_id: str
    ) -> dict[str, Any]:
        """Move deferred native STT to Safe Voice only when a stage needs it."""

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT vt.* FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE r.request_id=?
                   ORDER BY vt.created_at ASC, vt.transcript_id ASC""",
                (str(request_id),),
            ).fetchall()
            if not rows:
                raise SessionNotFound("voice transcript not found")
            eligible = [
                row for row in rows if str(row["safe_voice_state"]) == "ready"
            ]
            if not eligible:
                pending = [
                    row
                    for row in rows
                    if str(row["safe_voice_state"]) == "pending_confirmation"
                ]
                if pending:
                    return dict(pending[-1])
                raise SessionConflict(
                    "voice transcript is not awaiting a transcript consumer"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state='pending_confirmation' "
                    "WHERE transcript_id=?",
                    (str(row["transcript_id"]),),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind="voice.input.transcript_pending_confirmation",
                    status="pending_confirmation",
                    phase="safe_voice",
                    summary="Safe Voice confirmation required",
                    detail={
                        "transcript_id": str(row["transcript_id"]),
                        "attachment_id": str(row["attachment_id"]),
                        "text": str(row["text"]),
                        "provenance": str(row["provenance"]),
                        "safe_voice_state": "pending_confirmation",
                    },
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (str(eligible[-1]["transcript_id"]),),
            ).fetchone()
        return dict(updated)

    def release_ready_voice_transcript(
        self,
        *,
        request_id: str,
        reason: str = "native_audio_direct_completed",
    ) -> dict[str, Any]:
        """Release STT after native chat completes without another consumer."""

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT vt.*, r.user_message_id FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE r.request_id=?
                   ORDER BY vt.created_at ASC, vt.transcript_id ASC""",
                (str(request_id),),
            ).fetchall()
            if not rows:
                raise SessionNotFound("voice transcript not found")
            eligible = [
                row for row in rows if str(row["safe_voice_state"]) == "ready"
            ]
            if not eligible:
                released = [
                    row
                    for row in rows
                    if str(row["safe_voice_state"]) == "released"
                ]
                if released:
                    return dict(released[-1])
                raise SessionConflict(
                    "voice transcript cannot be auto-released after Safe Voice started"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state='released' "
                    "WHERE transcript_id=?",
                    (str(row["transcript_id"]),),
                )
                self._append_voice_transcript_projection(
                    connection,
                    message_id=str(row["user_message_id"]),
                    transcript=str(row["text"]),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind="voice.input.transcript_released",
                    status="released",
                    phase="transcription",
                    summary="Local input transcript released after native audio chat",
                    detail={
                        "transcript_id": str(row["transcript_id"]),
                        "attachment_id": str(row["attachment_id"]),
                        "release_reason": str(reason),
                    },
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (str(eligible[-1]["transcript_id"]),),
            ).fetchone()
        return dict(updated)

    def reconcile_completed_native_audio_transcript(
        self, *, request_id: str
    ) -> dict[str, Any]:
        """Repair the pre-gate beta state for a completed native audio reply.

        Early native-audio builds opened Safe Voice as soon as STT completed,
        even when a no-tool Direct response had already answered from original
        audio.  This narrowly releases only that impossible-to-resume state: a
        completed Run with a durable assistant audio part.
        """

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT vt.*, r.user_message_id, r.state AS run_state
                   FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE r.request_id=?
                   ORDER BY vt.created_at ASC, vt.transcript_id ASC""",
                (str(request_id),),
            ).fetchall()
            if not rows:
                raise SessionNotFound("voice transcript not found")
            if any(str(row["run_state"]) != "completed" for row in rows):
                raise SessionConflict(
                    "only a completed native audio Run can be reconciled"
                )
            assistant_rows = connection.execute(
                """SELECT content_json FROM messages
                   WHERE run_id=? AND role='assistant'""",
                (str(rows[0]["run_id"]),),
            ).fetchall()
            has_native_audio = False
            for assistant in assistant_rows:
                try:
                    content = json.loads(str(assistant["content_json"] or "[]"))
                except (TypeError, ValueError):
                    content = []
                if isinstance(content, list) and any(
                    isinstance(part, Mapping)
                    and str(part.get("type") or "") == "audio"
                    and bool(str(part.get("asset_id") or "").strip())
                    for part in content
                ):
                    has_native_audio = True
                    break
            if not has_native_audio:
                raise SessionConflict(
                    "completed Run has no durable native audio response"
                )
            eligible = [
                row
                for row in rows
                if str(row["safe_voice_state"]) == "pending_confirmation"
            ]
            if not eligible:
                released = [
                    row
                    for row in rows
                    if str(row["safe_voice_state"]) == "released"
                ]
                if released:
                    return dict(released[-1])
                raise SessionConflict(
                    "completed native audio transcript is not reconcilable"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state='released' "
                    "WHERE transcript_id=?",
                    (str(row["transcript_id"]),),
                )
                self._append_voice_transcript_projection(
                    connection,
                    message_id=str(row["user_message_id"]),
                    transcript=str(row["text"]),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind="voice.input.transcript_released",
                    status="released",
                    phase="migration",
                    summary=(
                        "Deferred input transcript released after native audio "
                        "Direct migration"
                    ),
                    detail={
                        "transcript_id": str(row["transcript_id"]),
                        "attachment_id": str(row["attachment_id"]),
                        "release_reason": "pre_gate_native_direct_reconciliation",
                    },
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (str(eligible[-1]["transcript_id"]),),
            ).fetchone()
        return dict(updated)

    @staticmethod
    def _append_voice_transcript_projection(
        connection: sqlite3.Connection,
        *,
        message_id: str,
        transcript: str,
    ) -> None:
        """Add accepted speech to the user text projection exactly once."""

        clean = str(transcript or "").strip()
        if not clean:
            return
        row = connection.execute(
            "SELECT text FROM messages WHERE message_id=?", (str(message_id),)
        ).fetchone()
        if row is None:
            raise SessionNotFound("voice transcript Message not found")
        current = str(row["text"] or "").strip()
        segments = [segment.strip() for segment in current.split("\n\n")]
        if clean in segments:
            return
        projected = f"{current}\n\n{clean}" if current else clean
        connection.execute(
            "UPDATE messages SET text=? WHERE message_id=?",
            (projected, str(message_id)),
        )

    def append_native_audio_runtime_event(
        self,
        *,
        request_id: str,
        source_event_id: str,
        event_kind: str,
        summary: str,
        phase: str,
        content: Iterable[Mapping[str, Any]] = (),
        resolution: str = "",
        target_event_id: str = "",
    ) -> dict[str, Any] | None:
        stable_source_id = str(source_event_id or "").strip()
        if not stable_source_id:
            return None
        parts = [dict(part) for part in content if isinstance(part, Mapping)]
        if contains_persistent_inline_media(parts):
            raise SessionConflict("runtime audio Event cannot contain inline bytes")
        has_audio = any(
            part.get("type") == "audio" and str(part.get("asset_id") or "")
            for part in parts
        )
        summary_text = str(summary or "").strip()
        if has_audio and summary_text and not any(
            str(part.get("type") or "").casefold() == "text"
            and str(part.get("text") or "").strip()
            for part in parts
        ):
            # Provider-native audio commentary often carries its companion
            # transcript in the typed Event summary.  Promote that semantic
            # text into the canonical Message so every Connector sees the same
            # audio+text result; audio-only policy remains a renderer choice.
            parts.insert(0, {"type": "text", "text": summary_text})
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT e.* FROM runtime_event_correlations AS c
                   JOIN run_events AS e ON e.event_id=c.event_id
                   WHERE c.source_event_id=?""",
                (stable_source_id,),
            ).fetchone()
            if existing is not None:
                result = dict(existing)
                result["detail"] = _json_object(result.pop("detail_json"))
                return result
            run = connection.execute(
                """SELECT r.*, s.owner_id FROM runs AS r
                   JOIN sessions AS s ON s.session_id=r.session_id
                   WHERE r.request_id=?""",
                (str(request_id),),
            ).fetchone()
            if run is None:
                return None
            canonical_kind = ""
            detail: dict[str, Any] = {}
            if has_audio:
                canonical_kind = "assistant.output.available"
                for part in parts:
                    if part.get("type") != "audio":
                        continue
                    self.audio_assets.claim(
                        str(part["asset_id"]),
                        owner_id=str(run["owner_id"]),
                        session_id=str(run["session_id"]),
                        request_id=str(request_id),
                    )
                content_json = _json(parts)
                content_hash = hashlib.sha256(
                    content_json.encode("utf-8")
                ).hexdigest()
                existing_message = connection.execute(
                    """SELECT message_id FROM messages
                       WHERE run_id=? AND role='assistant' AND content_hash=?
                       ORDER BY ordinal ASC LIMIT 1""",
                    (str(run["run_id"]), content_hash),
                ).fetchone()
                if existing_message is None:
                    message_id = _new_id("msg")
                    text_projection = "\n".join(
                        str(part.get("text") or "").strip()
                        for part in parts
                        if part.get("type") == "text"
                        and str(part.get("text") or "").strip()
                    ).strip()
                    connection.execute(
                        """
                        INSERT INTO messages(
                            message_id, session_id, run_id, ordinal,
                            context_generation, role, author_id, source,
                            content_json, text, content_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'assistant', ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_id,
                            str(run["session_id"]),
                            str(run["run_id"]),
                            self._next_ordinal(connection, str(run["session_id"])),
                            int(run["context_generation"]),
                            str(run["agent_id"]),
                            str(run["agent_id"]),
                            content_json,
                            text_projection,
                            content_hash,
                            now,
                        ),
                    )
                else:
                    message_id = str(existing_message["message_id"])
                self._queue_foreground_message(
                    connection, session_id=str(run["session_id"]), message_id=message_id
                )
                detail = {
                    "message_id": message_id,
                    "request_id": str(request_id),
                    "phase": str(phase or "immediate"),
                    "disposition": "unresolved",
                    "content": parts,
                }
            elif str(event_kind) == "voice_fallback_started":
                canonical_kind = "voice.fallback.started"
                detail = {"request_id": str(request_id), "phase": str(phase)}
            elif str(event_kind) == "voice_warning":
                canonical_kind = "voice.warning"
                detail = {"request_id": str(request_id), "warning": str(summary)}
            elif str(event_kind) == "initial_resolution" and target_event_id:
                target = connection.execute(
                    """SELECT kind FROM runtime_event_correlations
                       WHERE source_event_id=?""",
                    (str(target_event_id),),
                ).fetchone()
                if target is None or str(target["kind"]) != "assistant.output.available":
                    return None
                canonical_kind = "assistant.output.resolved"
                detail = {
                    "request_id": str(request_id),
                    "target_event_id": str(target_event_id),
                    "resolution": str(resolution or "acknowledgement"),
                }
            else:
                return None
            event = self._append_event(
                connection,
                session_id=str(run["session_id"]),
                run_id=str(run["run_id"]),
                kind=canonical_kind,
                status=(
                    str(resolution or "resolved")
                    if canonical_kind == "assistant.output.resolved"
                    else "available"
                ),
                phase=str(phase or "immediate"),
                summary=str(summary or canonical_kind),
                detail=detail,
                outbox=True,
            )
            connection.execute(
                """INSERT INTO runtime_event_correlations(
                       source_event_id,session_id,run_id,event_id,kind,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    stable_source_id,
                    str(run["session_id"]),
                    str(run["run_id"]),
                    event["event_id"],
                    canonical_kind,
                    now,
                ),
            )
        return event

    def runtime_event_delivery_target(
        self,
        *,
        source_event_id: str,
        request_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        """Resolve a persisted provider Event to its FC delivery identity."""

        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT c.event_id, c.session_id, c.run_id, c.kind
                FROM runtime_event_correlations AS c
                JOIN runs AS r ON r.run_id=c.run_id
                JOIN sessions AS s ON s.session_id=c.session_id
                WHERE c.source_event_id=? AND r.request_id=?
                  AND s.instance_id=? AND s.owner_id=?
                """,
                (
                    str(source_event_id),
                    str(request_id),
                    self.instance_id,
                    str(owner_id),
                ),
            ).fetchone()
        return dict(row) if row is not None else None

    def decide_voice_transcript(
        self, *, request_id: str, confirmed: bool
    ) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT vt.*, r.user_message_id FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE r.request_id=?
                   ORDER BY vt.created_at ASC, vt.transcript_id ASC""",
                (str(request_id),),
            ).fetchall()
            if not rows:
                raise SessionNotFound("voice transcript not found")
            state = "released" if confirmed else "discarded"
            eligible = [
                row
                for row in rows
                if str(row["safe_voice_state"]) == "pending_confirmation"
            ]
            if not eligible:
                matching = [
                    row for row in rows if str(row["safe_voice_state"]) == state
                ]
                if matching:
                    return dict(matching[-1])
                raise SessionConflict(
                    "voice transcript is not waiting for Safe Voice confirmation"
                )
            for row in eligible:
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state=? WHERE transcript_id=?",
                    (state, str(row["transcript_id"])),
                )
                if confirmed:
                    self._append_voice_transcript_projection(
                        connection,
                        message_id=str(row["user_message_id"]),
                        transcript=str(row["text"]),
                    )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind=(
                        "voice.input.transcript_confirmed"
                        if confirmed
                        else "voice.input.transcript_discarded"
                    ),
                    status=state,
                    phase="safe_voice",
                    summary=(
                        "Safe Voice transcript confirmed"
                        if confirmed
                        else "Safe Voice transcript discarded"
                    ),
                    detail={"transcript_id": str(row["transcript_id"])},
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM voice_transcripts WHERE transcript_id=?",
                (str(eligible[-1]["transcript_id"]),),
            ).fetchone()
        return dict(updated)

    def decide_voice_transcript_by_id(
        self,
        *,
        session_id: str,
        owner_id: str,
        transcript_id: str,
        confirmed: bool,
        expected_context_generation: int | None = None,
    ) -> dict[str, Any]:
        """Apply an authenticated generic-client Safe Voice decision."""

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT vt.*, r.user_message_id, r.request_id,
                          r.context_generation AS run_context_generation,
                          s.context_generation AS current_context_generation
                   FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   JOIN sessions AS s ON s.session_id=vt.session_id
                   WHERE vt.transcript_id=? AND vt.session_id=? AND s.owner_id=?""",
                (str(transcript_id), str(session_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound("voice transcript not found")
            if expected_context_generation is not None and (
                int(row["run_context_generation"])
                != int(expected_context_generation)
                or int(row["current_context_generation"])
                != int(expected_context_generation)
            ):
                raise SessionConflict("voice transcript belongs to a stale Session context")
            current = str(row["safe_voice_state"])
            desired = "released" if confirmed else "discarded"
            if current not in {"pending_confirmation", desired}:
                raise SessionConflict(
                    f"voice transcript cannot change from {current} to {desired}"
                )
            if current == "pending_confirmation":
                connection.execute(
                    "UPDATE voice_transcripts SET safe_voice_state=? "
                    "WHERE transcript_id=?",
                    (desired, str(transcript_id)),
                )
                if confirmed:
                    self._append_voice_transcript_projection(
                        connection,
                        message_id=str(row["user_message_id"]),
                        transcript=str(row["text"]),
                    )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind=(
                        "voice.input.transcript_confirmed"
                        if confirmed
                        else "voice.input.transcript_discarded"
                    ),
                    status=desired,
                    phase="safe_voice",
                    summary=(
                        "Safe Voice transcript confirmed"
                        if confirmed
                        else "Safe Voice transcript discarded"
                    ),
                    detail={"transcript_id": str(transcript_id)},
                    outbox=True,
                )
            updated = connection.execute(
                """SELECT vt.*, r.request_id FROM voice_transcripts AS vt
                   JOIN runs AS r ON r.run_id=vt.run_id
                   WHERE vt.transcript_id=?""",
                (str(transcript_id),),
            ).fetchone()
        return dict(updated)

    def create_approval(
        self,
        *,
        run_id: str,
        owner_id: str,
        fencing_token: int,
        scope: Mapping[str, Any],
    ) -> dict[str, Any]:
        run = self.get_run(run_id, owner_id=owner_id)
        if int(run["fencing_token"]) != int(fencing_token) or run["state"] != "running":
            raise StaleFencingToken("approval origin is no longer authoritative")
        approval_id, now = _new_id("approval"), _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO run_approvals(approval_id,session_id,run_id,owner_id,attempt,
                   fencing_token,scope_json,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    run["session_id"],
                    str(run_id),
                    str(owner_id),
                    int(run["attempt"]),
                    int(fencing_token),
                    _json(dict(scope)),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM run_approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        result = dict(row)
        result["scope"] = _json_object(result.pop("scope_json"))
        return result

    def decide_approval(
        self, *, approval_id: str, owner_id: str, decision: str
    ) -> dict[str, Any]:
        resolved = str(decision).lower()
        if resolved not in {"approved", "denied"}:
            raise ValueError("decision must be approved or denied")
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM run_approvals WHERE approval_id=? AND owner_id=?",
                (str(approval_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound("approval not found")
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (row["run_id"],)
            ).fetchone()
            if str(row["state"]) == "pending" and (
                run is None
                or run["state"] != "running"
                or int(run["fencing_token"]) != int(row["fencing_token"])
            ):
                raise StaleFencingToken("approval expired with its originating attempt")
            if str(row["state"]) == "pending":
                connection.execute(
                    "UPDATE run_approvals SET state='decided',decision=?,decided_at=? WHERE approval_id=?",
                    (resolved, now, str(approval_id)),
                )
                self._append_event(
                    connection,
                    session_id=str(row["session_id"]),
                    run_id=str(row["run_id"]),
                    kind="approval.decided",
                    status=resolved,
                    phase="control",
                    summary=f"Approval {resolved}",
                    detail={"approval_id": str(approval_id), "decision": resolved},
                    outbox=True,
                )
            updated = connection.execute(
                "SELECT * FROM run_approvals WHERE approval_id=?", (str(approval_id),)
            ).fetchone()
        result = dict(updated)
        result["scope"] = _json_object(result.pop("scope_json"))
        return result

    def reconcile_incomplete_runs(
        self,
        *,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Terminalize Runs whose in-memory executor was lost on restart.

        The current runtime has no general durable queue or safe execution-stack replay.
        Leaving either an accepted ``queued`` Run or a claimed ``running`` Run
        non-terminal would make clients wait forever.  Reconciliation therefore
        fences every pre-existing non-terminal Run as ``interrupted``, preserves
        its user Message and evidence, and appends one durable terminal Event.
        A later user continuation is a new child Run with a new idempotency key.

        The narrow hchat-exchange ingress is the exception for queued Runs: its
        PAO-owned inbox retains the accepted input and can restore the queue
        item with the same idempotency key.  A running Exchange Run is still
        fenced as interrupted because unknown execution state is never replayed.

        Process startup reconciles the whole instance.  Per-Agent Function
        Worker startup and recovery pass ``agent_id`` so one executor cannot
        interrupt Runs that still belong to another live Agent.
        """

        now = _utc_now()
        target_agent_id = str(agent_id or "").strip().lower() or None
        reconciled_ids: list[str] = []
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            agent_clause = " AND r.agent_id = ?" if target_agent_id else ""
            params: tuple[str, ...] = (
                (self.instance_id, "hchat-exchange", target_agent_id)
                if target_agent_id
                else (self.instance_id, "hchat-exchange")
            )
            rows = connection.execute(
                f"""
                SELECT r.* FROM runs AS r
                JOIN sessions AS s ON s.session_id = r.session_id
                WHERE s.instance_id = ? AND r.state IN ('queued', 'running')
                  AND NOT (r.source = ? AND r.state = 'queued')
                {agent_clause}
                ORDER BY r.created_at, r.run_id
                """,
                params,
            ).fetchall()
            for run in rows:
                prior_state = str(run["state"])
                run_id = str(run["run_id"])
                session_id = str(run["session_id"])
                if target_agent_id:
                    reason = (
                        f"Agent '{target_agent_id}' restarted before the accepted Run began"
                        if prior_state == "queued"
                        else f"Agent '{target_agent_id}' restarted while the Run was executing"
                    )
                else:
                    reason = (
                        "HASHI restarted before the accepted Run began"
                        if prior_state == "queued"
                        else "HASHI restarted while the Run was executing"
                    )
                updated = connection.execute(
                    """
                    UPDATE runs
                    SET state = 'interrupted', fencing_token = fencing_token + 1,
                        worker_id = NULL, error_code = 'runtime_restart_interrupted',
                        error_text = ?, completed_at = ?, updated_at = ?
                    WHERE run_id = ? AND state = ?
                    """,
                    (reason, now, now, run_id, prior_state),
                )
                if updated.rowcount != 1:
                    continue
                connection.execute(
                    """
                    UPDATE run_attempts
                    SET state = 'interrupted', finished_at = ?
                    WHERE run_id = ? AND state = 'running'
                    """,
                    (now, run_id),
                )
                self._release_run_audio_leases(
                    connection, run_id=run_id, released_at=now
                )
                event = self._append_event(
                    connection,
                    session_id=session_id,
                    run_id=run_id,
                    kind="run.interrupted",
                    status="interrupted",
                    phase="recovery",
                    summary=reason,
                    detail={
                        "agent_id": str(run["agent_id"]),
                        "error_code": "runtime_restart_interrupted",
                        "prior_state": prior_state,
                        "recovery_scope": "agent" if target_agent_id else "instance",
                    },
                    outbox=True,
                )
                projection = {
                    "run_id": run_id,
                    "session_id": session_id,
                    "state": "interrupted",
                    "user_message_id": str(run["user_message_id"]),
                    "final_message_id": None,
                    "error": reason,
                    "error_code": "runtime_restart_interrupted",
                    "prior_state": prior_state,
                    "latest_event_sequence": event["sequence"],
                }
                connection.execute(
                    """
                    INSERT INTO run_projection_records(
                        run_id, session_id, projection_json, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        projection_json = excluded.projection_json,
                        updated_at = excluded.updated_at
                    """,
                    (run_id, session_id, _json(projection), now),
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET updated_at = ?, revision = revision + 1
                    WHERE session_id = ?
                    """,
                    (now, session_id),
                )
                reconciled_ids.append(run_id)

            if not reconciled_ids:
                return []
            placeholders = ",".join("?" for _ in reconciled_ids)
            result = connection.execute(
                f"SELECT * FROM runs WHERE run_id IN ({placeholders}) "
                "ORDER BY created_at, run_id",
                reconciled_ids,
            ).fetchall()
        return [self._run_dict(row) for row in result]

    def get_run(self, run_id: str, *, owner_id: str | None = None) -> dict[str, Any]:
        clauses = ["r.run_id = ?", "s.instance_id = ?"]
        params: list[Any] = [str(run_id), self.instance_id]
        if owner_id is not None:
            clauses.append("s.owner_id = ?")
            params.append(str(owner_id))
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT r.* FROM runs AS r JOIN sessions AS s ON s.session_id = r.session_id
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchone()
        if row is None:
            raise SessionNotFound(str(run_id))
        return self._run_dict(row)

    def recent_session_runs(self, session_id: str, *, owner_id: str, limit: int = 64, context_generation: int | None = None) -> list[dict[str, Any]]:
        """Bounded read-only request discovery for the caller's current Session."""
        session = self.get_session(session_id, owner_id=owner_id)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT request_id, run_id, session_id, agent_id, context_generation,
                          state, created_at, completed_at, final_message_id
                   FROM runs WHERE session_id = ? AND context_generation = ?
                   ORDER BY (state IN ('queued', 'running', 'awaiting_approval')) DESC,
                            created_at DESC, run_id DESC LIMIT ?""",
                (session_id, context_generation if context_generation is not None else session["context_generation"], max(1, min(int(limit), 64))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_run_by_request(
        self,
        request_id: str,
        *,
        owner_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["r.request_id = ?", "s.instance_id = ?"]
        params: list[Any] = [str(request_id), self.instance_id]
        if owner_id is not None:
            clauses.append("s.owner_id = ?")
            params.append(str(owner_id))
        if agent_id is not None:
            clauses.append("r.agent_id = ?")
            params.append(str(agent_id).lower())
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT r.* FROM runs AS r
                JOIN sessions AS s ON s.session_id = r.session_id
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchone()
        if row is None:
            raise SessionNotFound(str(request_id))
        return self._run_dict(row)

    def request_failure_detail(
        self,
        request_id: str,
        *,
        owner_id: str,
        agent_id: str,
    ) -> dict[str, Any] | None:
        """Return the owner-scoped, durable user-safe terminal failure fields."""
        run = self.get_run_by_request(request_id, owner_id=owner_id, agent_id=agent_id)
        if run.get("state") != "failed":
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT detail_json FROM run_events
                   WHERE run_id = ? AND kind = 'run.failed'
                   ORDER BY sequence DESC LIMIT 1""",
                (run["run_id"],),
            ).fetchone()
        detail = _json_object(row["detail_json"]) if row is not None else {}
        context = detail.get("error_context")
        return {
            "request_id": run["request_id"],
            "error": str(run.get("error_text") or detail.get("error") or "Run failed"),
            **(dict(context) if isinstance(context, Mapping) else {}),
        }

    def update_session(
        self,
        session_id: str,
        *,
        owner_id: str,
        expected_revision: int,
        title: str | None = None,
    ) -> dict[str, Any]:
        clean_title = str(title or "").strip()
        if title is not None and not clean_title:
            raise ValueError("title cannot be empty")
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND owner_id = ?
                """,
                (str(session_id), self.instance_id, str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound(str(session_id))
            if int(row["revision"]) != int(expected_revision):
                raise SessionConflict("Session revision does not match")
            if title is not None:
                connection.execute(
                    """
                    UPDATE sessions SET title = ?, title_source = 'user',
                        revision = revision + 1, updated_at = ? WHERE session_id = ?
                    """,
                    (clean_title, now, str(session_id)),
                )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            return self._session_dict(updated)

    def messages(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        after_ordinal: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        self.get_session(session_id, owner_id=owner_id)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages WHERE session_id = ? AND ordinal > ?
                ORDER BY ordinal ASC LIMIT ?
                """,
                (
                    str(session_id),
                    max(0, int(after_ordinal)),
                    max(1, min(int(limit), 1000)),
                ),
            ).fetchall()
        return [self._message_dict(row) for row in rows]

    def get_message(
        self,
        message_id: str,
        *,
        session_id: str,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        """Return one owner-scoped canonical Message by its durable identity."""

        self.get_session(str(session_id), owner_id=owner_id)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM messages
                WHERE message_id=? AND session_id=?
                """,
                (str(message_id), str(session_id)),
            ).fetchone()
        if row is None:
            raise SessionNotFound(str(message_id))
        return self._message_dict(row)

    def recent_messages(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        context_generation: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return the newest canonical visible messages in display order."""

        session = self.get_session(session_id, owner_id=owner_id)
        generation = int(
            context_generation
            if context_generation is not None
            else session["context_generation"]
        )
        bounded = max(1, min(int(limit), 1001))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE session_id=? AND context_generation=?
                  AND visibility='visible' AND history_eligible=1
                ORDER BY ordinal DESC LIMIT ?
                """,
                (str(session_id), generation, bounded),
            ).fetchall()
        return [self._message_dict(row) for row in reversed(rows)]

    def live_transcript_segments(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        context_generation: int | None = None,
        call_id: str | None = None,
        call_epoch: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return a derived, role-preserving view of durable Live fragments."""

        session = self.get_session(session_id, owner_id=owner_id)
        generation = int(
            context_generation
            if context_generation is not None
            else session["context_generation"]
        )
        clauses = [
            "f.session_id = ?",
            "f.owner_id = ?",
            "c.context_generation = ?",
        ]
        params: list[Any] = [
            str(session_id),
            str(session["owner_id"]),
            generation,
        ]
        if call_id is not None:
            clauses.append("f.call_id = ?")
            params.append(str(call_id))
        if call_epoch is not None:
            clauses.append("f.call_epoch = ?")
            params.append(int(call_epoch))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT f.call_id, f.call_epoch, f.provider_event_id,
                       f.speaker, f.start_ms, f.end_ms, f.sequence,
                       f.created_at, e.detail_json, c.started_at
                FROM live_fragments AS f
                JOIN live_calls AS c ON c.call_id = f.call_id
                JOIN run_events AS e ON e.event_id = f.event_id
                WHERE {" AND ".join(clauses)}
                ORDER BY c.started_at, f.call_epoch, f.start_ms, f.end_ms,
                         f.sequence, f.provider_event_id
                """,
                params,
            ).fetchall()

        segments: list[dict[str, Any]] = []
        latest_by_speaker: dict[tuple[str, int, str], dict[str, Any]] = {}
        for row in rows:
            try:
                detail = json.loads(row["detail_json"] or "{}")
            except (TypeError, ValueError):
                continue
            speaker = str(row["speaker"] or detail.get("speaker") or "")
            text = detail.get("text")
            if speaker not in {"user", "assistant"} or not isinstance(text, str):
                continue
            start_ms, end_ms = int(row["start_ms"]), int(row["end_ms"])
            call_key = (str(row["call_id"]), int(row["call_epoch"]))
            previous = latest_by_speaker.get((*call_key, speaker))
            other_speaker = "assistant" if speaker == "user" else "user"
            other = latest_by_speaker.get((*call_key, other_speaker))
            complete_reply_between = (
                other is not None
                and previous is not None
                and int(other["end_ms"]) > int(previous["end_ms"])
                and int(other["end_ms"]) < start_ms
                and (
                    int(other["end_ms"]) - int(other["start_ms"]) >= 800
                    or str(other["text"]).rstrip().endswith((".", "!", "?", "。", "！", "？"))
                )
            )
            if (
                previous is not None
                and start_ms - int(previous["end_ms"]) <= 1200
                and not complete_reply_between
            ):
                previous["text"] += text
                previous["end_ms"] = max(int(previous["end_ms"]), end_ms)
                previous["provider_event_ids"].append(str(row["provider_event_id"]))
                previous["last_sequence"] = max(
                    int(previous["last_sequence"]), int(row["sequence"])
                )
                continue
            segment = {
                    "call_key": call_key,
                    "call_id": call_key[0],
                    "call_epoch": call_key[1],
                    "role": speaker,
                    "text": text,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "sequence": int(row["sequence"]),
                    "last_sequence": int(row["sequence"]),
                    "created_at": str(row["created_at"] or row["started_at"] or ""),
                    "provider_event_ids": [str(row["provider_event_id"])],
                }
            segments.append(segment)
            latest_by_speaker[(*call_key, speaker)] = segment
        return segments

    def _live_history_messages(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        context_generation: int | None = None,
    ) -> list[dict[str, Any]]:
        segments = self.live_transcript_segments(
            session_id,
            owner_id=owner_id,
            context_generation=context_generation,
        )
        result: list[dict[str, Any]] = []
        current_call: tuple[str, int] | None = None
        current_unit = ""
        current_roles: set[str] = set()
        unit_number = 0
        for segment in segments:
            call_key = segment["call_key"]
            if call_key != current_call:
                current_call = call_key
                current_unit = ""
                current_roles = set()
                unit_number = 0
            role = str(segment["role"])
            may_complete_user = role == "assistant" and current_roles == {"user"}
            if not current_unit or not may_complete_user:
                unit_number += 1
                current_unit = (
                    f"live:{segment['call_id']}:{segment['call_epoch']}:{unit_number}"
                )
                current_roles = set()
            current_roles.add(role)
            identity_material = "\n".join(segment["provider_event_ids"])
            history_id = "live_" + hashlib.sha256(
                identity_material.encode("utf-8")
            ).hexdigest()[:32]
            result.append(
                {
                    "message_id": history_id,
                    "history_id": history_id,
                    "history_unit_id": current_unit,
                    "session_id": str(session_id),
                    "role": role,
                    "text": segment["text"],
                    "source": "live-phone",
                    "created_at": segment["created_at"],
                    "sequence": int(segment["sequence"]),
                    "call_id": segment["call_id"],
                    "call_epoch": int(segment["call_epoch"]),
                    "start_ms": int(segment["start_ms"]),
                    "end_ms": int(segment["end_ms"]),
                    "transcript_provenance": "gpt_live_transcript",
                }
            )
        return result

    def recent_history_messages(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        context_generation: int | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        """Return completed chat plus Live speech as one canonical timeline."""

        session = self.get_session(session_id, owner_id=owner_id)
        generation = int(
            context_generation
            if context_generation is not None
            else session["context_generation"]
        )
        bounded = max(1, min(int(limit), 1000))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT m.* FROM messages AS m
                JOIN runs AS r ON r.run_id = m.run_id
                WHERE m.session_id=? AND m.context_generation=?
                  AND m.visibility='visible' AND m.history_eligible=1
                  AND r.state='completed'
                  AND (m.message_id=r.user_message_id OR m.message_id=r.final_message_id)
                ORDER BY m.created_at DESC, m.ordinal DESC
                LIMIT ?
                """,
                (str(session_id), generation, bounded * 2),
            ).fetchall()
        normal: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = self._message_dict(row)
            item["history_id"] = str(item["message_id"])
            item["history_unit_id"] = f"run:{item['run_id']}"
            item["sequence"] = int(item["ordinal"])
            normal.append(item)
        combined = normal + self._live_history_messages(
            session_id,
            owner_id=str(session["owner_id"]),
            context_generation=generation,
        )

        def order_key(item: Mapping[str, Any]) -> tuple[str, int, str]:
            return (
                str(item.get("created_at") or ""),
                int(item.get("sequence") or 0),
                str(item.get("history_id") or item.get("message_id") or ""),
            )

        combined.sort(key=order_key)
        units: dict[str, list[dict[str, Any]]] = {}
        for item in combined:
            units.setdefault(str(item["history_unit_id"]), []).append(item)
        selected: set[str] = set()
        selected_count = 0
        ordered_units = sorted(
            units.items(),
            key=lambda pair: max(order_key(item) for item in pair[1]),
        )
        for unit_id, items in reversed(ordered_units):
            if selected_count + len(items) > bounded:
                break
            selected.add(unit_id)
            selected_count += len(items)
        return [item for item in combined if item["history_unit_id"] in selected]

    def recent_agent_activity_results(
        self,
        *,
        owner_id: str,
        agent_id: str,
        limit: int = 8,
        since_hours: int = 24,
    ) -> list[dict[str, Any]]:
        """Return bounded completed Agent-activity results for foreground context."""

        bounded_limit = max(1, min(int(limit), 32))
        bounded_hours = max(1, min(int(since_hours), 168))
        since = (
            datetime.now(timezone.utc) - timedelta(hours=bounded_hours)
        ).isoformat().replace("+00:00", "Z")
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT m.message_id, m.text, m.source, m.created_at,
                          r.run_id, s.session_id
                   FROM sessions AS s
                   JOIN runs AS r ON r.session_id = s.session_id
                   JOIN messages AS m ON m.message_id = r.final_message_id
                   WHERE s.instance_id = ? AND s.owner_id = ? AND s.agent_id = ?
                     AND s.session_kind = 'agent_activity' AND s.status != 'deleted'
                     AND r.state = 'completed'
                     AND m.role = 'assistant' AND m.visibility = 'visible'
                     AND m.history_eligible = 1 AND m.created_at >= ?
                     AND TRIM(m.text) != ''
                   ORDER BY m.created_at DESC, m.ordinal DESC
                   LIMIT ?""",
                (
                    self.instance_id,
                    str(owner_id),
                    str(agent_id).lower(),
                    since,
                    bounded_limit,
                ),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def recent_phone_result_references(
        self,
        *,
        owner_id: str,
        agent_id: str,
        session_id: str,
        limit: int = 24,
        since_hours: int = 24,
    ) -> list[dict[str, Any]]:
        """PAO-owned completed answers available to the same Agent's phone.

        Include the selected Conversation and the Agent's scheduled activity.
        A provider receives an addressable projection, never a second result store.
        """

        bounded_limit = max(1, min(int(limit), 32))
        bounded_hours = max(1, min(int(since_hours), 168))
        since = (
            datetime.now(timezone.utc) - timedelta(hours=bounded_hours)
        ).isoformat().replace("+00:00", "Z")
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """SELECT m.message_id, m.text, m.source, m.created_at,
                          u.text AS request_text,
                          r.run_id, s.session_id, s.session_kind, s.agent_id
                   FROM sessions AS s
                   JOIN runs AS r ON r.session_id = s.session_id
                   JOIN messages AS u ON u.message_id = r.user_message_id
                   JOIN messages AS m ON m.message_id = r.final_message_id
                   WHERE s.instance_id = ? AND s.owner_id = ? AND s.agent_id = ?
                     AND s.status != 'deleted'
                     AND (s.session_kind = 'agent_activity' OR s.session_id = ?)
                     AND r.state = 'completed'
                     AND m.role = 'assistant' AND m.visibility = 'visible'
                     AND m.history_eligible = 1 AND m.created_at >= ?
                     AND TRIM(m.text) != ''
                   ORDER BY m.created_at DESC, m.ordinal DESC
                   LIMIT ?""",
                (self.instance_id, str(owner_id), str(agent_id).lower(),
                 str(session_id), since, bounded_limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_phone_result_text(
        self,
        *,
        owner_id: str,
        agent_id: str,
        current_session_id: str,
        message_id: str,
    ) -> str | None:
        """Read one complete final answer under the call's owner/Agent scope."""

        with self._lock, self._connection() as connection:
            row = connection.execute(
                """SELECT m.text FROM messages AS m
                   JOIN runs AS r ON r.final_message_id = m.message_id
                   JOIN sessions AS s ON s.session_id = r.session_id
                   WHERE m.message_id = ? AND s.instance_id = ?
                     AND s.owner_id = ? AND s.agent_id = ?
                     AND s.status != 'deleted'
                     AND (s.session_id = ? OR s.session_kind = 'agent_activity')
                     AND r.state = 'completed' AND m.role = 'assistant'
                     AND m.visibility = 'visible' AND m.history_eligible = 1
                   LIMIT 1""",
                (str(message_id), self.instance_id, str(owner_id),
                 str(agent_id).lower(), str(current_session_id)),
            ).fetchone()
        return str(row["text"]) if row is not None else None

    def recent_visible_messages(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        context_generation: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return visible UI history, including presentation-only messages."""

        session = self.get_session(session_id, owner_id=owner_id)
        generation = int(
            context_generation
            if context_generation is not None
            else session["context_generation"]
        )
        bounded = max(1, min(int(limit), 1001))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT m.*, r.request_id,
                       r.state AS run_state,
                       r.final_message_id AS run_final_message_id
                FROM messages AS m
                LEFT JOIN runs AS r ON r.run_id = m.run_id
                WHERE m.session_id=? AND m.context_generation=?
                  AND m.visibility='visible'
                ORDER BY m.ordinal DESC LIMIT ?
                """,
                (str(session_id), generation, bounded),
            ).fetchall()
        return [self._message_dict(row) for row in reversed(rows)]

    @staticmethod
    def _agent_history_boundary(row: Mapping[str, Any]) -> dict[str, Any]:
        """Return the stable, internal ordering key for one visible message."""

        try:
            ordinal = int(row["ordinal"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("agent history boundary ordinal is invalid") from exc
        boundary = {
            "created_at": str(row.get("created_at") or "").strip(),
            "session_id": str(row.get("session_id") or "").strip(),
            "ordinal": ordinal,
            "message_id": str(row.get("message_id") or "").strip(),
        }
        if (
            not boundary["created_at"]
            or not boundary["session_id"]
            or boundary["ordinal"] < 1
            or not boundary["message_id"]
        ):
            raise ValueError("agent history boundary is invalid")
        return boundary

    def agent_history_anchor(
        self,
        *,
        owner_id: str,
        agent_id: str,
        session_id: str,
        ordinal: int,
    ) -> dict[str, Any]:
        """Resolve one canonical Session message into a global history boundary."""

        owner = str(owner_id).strip()
        agent = str(agent_id).strip().lower()
        target_session = str(session_id).strip()
        try:
            target_ordinal = int(ordinal)
        except (TypeError, ValueError) as exc:
            raise ValueError("agent history anchor ordinal is invalid") from exc
        if not owner or not agent or not target_session or target_ordinal < 1:
            raise ValueError("agent history anchor is invalid")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT m.created_at, m.session_id, m.ordinal, m.message_id
                FROM messages AS m
                JOIN sessions AS s ON s.session_id = m.session_id
                WHERE s.instance_id = ? AND s.owner_id = ? AND s.agent_id = ?
                  AND s.session_kind = 'conversation'
                  AND s.status != 'deleted' AND m.session_id = ? AND m.ordinal = ?
                  AND m.visibility = 'visible'
                """,
                (
                    self.instance_id,
                    owner,
                    agent,
                    target_session,
                    target_ordinal,
                ),
            ).fetchone()
        if row is None:
            raise SessionNotFound("agent history anchor was not found")
        return self._agent_history_boundary(dict(row))

    def agent_history_page(
        self,
        *,
        owner_id: str,
        agent_id: str,
        before: Mapping[str, Any] | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read one chronological, cross-Session page of visible Agent history.

        This is a presentation read only.  It never changes the primary Session
        binding and must not be used as a model-context source.
        """

        owner = str(owner_id).strip()
        agent = str(agent_id).strip().lower()
        if not owner or not agent:
            raise ValueError("owner_id and agent_id are required")
        bounded = max(1, min(int(limit), 200))
        boundary = self._agent_history_boundary(before) if before is not None else None
        clauses = [
            "s.instance_id = ?",
            "s.owner_id = ?",
            "s.agent_id = ?",
            "s.session_kind = 'conversation'",
            "s.status != 'deleted'",
            "m.visibility = 'visible'",
        ]
        params: list[Any] = [self.instance_id, owner, agent]
        if boundary is not None:
            clauses.append(
                """
                (m.created_at < ? OR (
                    m.created_at = ? AND (
                        m.session_id < ? OR (
                            m.session_id = ? AND (
                                m.ordinal < ? OR (
                                    m.ordinal = ? AND m.message_id < ?
                                )
                            )
                        )
                    )
                ))
                """
            )
            params.extend(
                [
                    boundary["created_at"],
                    boundary["created_at"],
                    boundary["session_id"],
                    boundary["session_id"],
                    boundary["ordinal"],
                    boundary["ordinal"],
                    boundary["message_id"],
                ]
            )
        params.append(bounded + 1)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT m.*, r.request_id,
                       r.state AS run_state,
                       r.final_message_id AS run_final_message_id
                FROM messages AS m
                JOIN sessions AS s ON s.session_id = m.session_id
                LEFT JOIN runs AS r ON r.run_id = m.run_id
                WHERE {' AND '.join(clauses)}
                ORDER BY m.created_at DESC, m.session_id DESC,
                         m.ordinal DESC, m.message_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        has_more = len(rows) > bounded
        page_rows = rows[:bounded]
        next_boundary = (
            self._agent_history_boundary(dict(page_rows[-1]))
            if has_more and page_rows
            else None
        )
        return {
            "messages": [self._message_dict(row) for row in reversed(page_rows)],
            "history_complete": not has_more,
            "next_boundary": next_boundary,
        }

    def visible_message_attachment(
        self,
        session_id: str,
        *,
        owner_id: str,
        message_id: str,
        attachment_id: str,
        context_generation: int | None = None,
    ) -> dict[str, Any]:
        """Return one canonical attachment after Session ownership checks.

        This lookup deliberately returns the stored canonical part only to an
        in-process caller.  Connector projections continue to omit local paths
        and digests.
        """

        session = self.get_session(session_id, owner_id=owner_id)
        generation = int(
            context_generation
            if context_generation is not None
            else session["context_generation"]
        )
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT content_json FROM messages
                WHERE session_id=? AND message_id=? AND context_generation=?
                  AND visibility='visible'
                """,
                (str(session_id), str(message_id), generation),
            ).fetchone()
        if row is None:
            raise SessionNotFound("visible message attachment not found")
        try:
            content = json.loads(str(row["content_json"] or "[]"))
        except (TypeError, ValueError):
            content = []
        for item_index, part in enumerate(
            content if isinstance(content, list) else (), start=1
        ):
            part_type = (
                str(part.get("type") or "").casefold()
                if isinstance(part, Mapping)
                else ""
            )
            part_attachment_id = (
                str(
                    part.get("attachment_id")
                    or (part.get("asset_id") if part_type == "audio" else "")
                    or ""
                )
                if isinstance(part, Mapping)
                else ""
            )
            if (
                isinstance(part, Mapping)
                and part_type in {"attachment", "media", "audio"}
                and part_attachment_id == str(attachment_id)
            ):
                if part_type == "audio":
                    metadata, local_path = self.audio_asset_path(
                        session_id=str(session_id),
                        owner_id=str(owner_id),
                        asset_id=part_attachment_id,
                    )
                    return {
                        **dict(part),
                        "attachment_id": part_attachment_id,
                        "local_ref": str(local_path),
                        "filename": str(
                            part.get("filename")
                            or metadata.get("filename")
                            or local_path.name
                        ),
                        "mime_type": str(
                            part.get("mime_type")
                            or metadata.get("mime_type")
                            or "audio/ogg"
                        ),
                        "size_bytes": int(metadata.get("size_bytes") or 0),
                        "sha256": str(metadata.get("sha256") or ""),
                    }
                if part_type == "media":
                    return dict(part)
                return self.attachment_canonical_part(
                    session_id=str(session_id),
                    owner_id=str(owner_id),
                    attachment_id=str(attachment_id),
                    item_index=int(part.get("item_index") or item_index),
                    semantic_role=str(part.get("semantic_role") or "") or None,
                    caption=str(part.get("caption") or ""),
                    detail=str(part.get("detail") or ""),
                )
        raise SessionNotFound("visible message attachment not found")

    def visible_agent_message_attachment(
        self,
        *,
        owner_id: str,
        agent_id: str,
        message_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:
        """Resolve a visible attachment across an Agent's retained Sessions.

        Transcript attachment URLs intentionally identify a message, not the
        currently selected Session.  A primary-Session switch therefore must
        not make a retained historical attachment inaccessible.  The lookup
        remains owner- and Agent-scoped and excludes tombstoned Sessions.
        """

        owner = str(owner_id).strip()
        agent = str(agent_id).strip().lower()
        target_message = str(message_id).strip()
        if not owner or not agent or not target_message:
            raise ValueError("agent message attachment lookup is invalid")
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT m.session_id, m.context_generation
                FROM messages AS m
                JOIN sessions AS s ON s.session_id = m.session_id
                WHERE s.instance_id = ? AND s.owner_id = ? AND s.agent_id = ?
                  AND s.session_kind = 'conversation'
                  AND s.status != 'deleted' AND m.message_id = ?
                  AND m.visibility = 'visible'
                """,
                (self.instance_id, owner, agent, target_message),
            ).fetchone()
        if row is None:
            raise SessionNotFound("visible message attachment not found")
        return self.visible_message_attachment(
            str(row["session_id"]),
            owner_id=owner,
            message_id=target_message,
            attachment_id=str(attachment_id),
            context_generation=int(row["context_generation"]),
        )

    def visible_messages_after(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        context_generation: int | None = None,
        after_ordinal: int = 0,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return the next visible UI messages after one canonical cursor."""

        session = self.get_session(session_id, owner_id=owner_id)
        generation = int(
            context_generation
            if context_generation is not None
            else session["context_generation"]
        )
        bounded = max(1, min(int(limit), 1001))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT m.*, r.request_id,
                       r.state AS run_state,
                       r.final_message_id AS run_final_message_id
                FROM messages AS m
                LEFT JOIN runs AS r ON r.run_id = m.run_id
                WHERE m.session_id=? AND m.context_generation=?
                  AND m.visibility='visible' AND m.ordinal>?
                ORDER BY m.ordinal ASC LIMIT ?
                """,
                (str(session_id), generation, max(0, int(after_ordinal)), bounded),
            ).fetchall()
        return [self._message_dict(row) for row in rows]

    def conversation_owner_ids(self, *, agent_id: str) -> list[str]:
        """Return authoritative Session owners observed for one local Agent."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT owner_id FROM sessions
                WHERE instance_id = ? AND agent_id = ? AND status != 'deleted'
                ORDER BY owner_id
                """,
                (self.instance_id, str(agent_id).strip().lower()),
            ).fetchall()
        return [str(row["owner_id"]) for row in rows]

    def export_conversation_continuity(
        self,
        *,
        owner_id: str,
        agent_id: str,
        source_instance: str,
        transfer_id: str,
        history_mode: str = "move",
    ) -> dict[str, Any]:
        """Export owner-checked visible history without carrying execution state."""
        owner = str(owner_id or "").strip()
        agent = str(agent_id or "").strip().lower()
        source = str(source_instance or "").strip().upper()
        transfer = str(transfer_id or "").strip()
        mode = str(history_mode or "").strip().lower()
        if not owner or not agent or not transfer:
            raise ValueError("owner_id, agent_id, and transfer_id are required")
        if source != self.instance_id:
            raise SessionConflict("conversation source instance does not match SessionStore")
        if mode not in CONVERSATION_HISTORY_MODES:
            raise ValueError("unsupported conversation history mode")

        exported_sessions: list[dict[str, Any]] = []
        eligible_count = 0
        excluded_count = 0
        seen_origins: set[str] = set()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN")
            sessions = connection.execute(
                """
                SELECT * FROM sessions
                WHERE instance_id = ? AND owner_id = ? AND agent_id = ?
                  AND status != 'deleted'
                ORDER BY is_default DESC, created_at, session_id
                """,
                (self.instance_id, owner, agent),
            ).fetchall()
            for session in sessions:
                session_id = str(session["session_id"])
                bindings = connection.execute(
                    """
                    SELECT surface, channel_key FROM channel_bindings
                    WHERE instance_id = ? AND owner_id = ? AND agent_id = ?
                      AND session_id = ?
                    ORDER BY surface, channel_key
                    """,
                    (self.instance_id, owner, agent, session_id),
                ).fetchall()
                rows = connection.execute(
                    """
                    SELECT m.*, r.state AS run_state
                    FROM messages AS m
                    LEFT JOIN runs AS r ON r.run_id = m.run_id
                    WHERE m.session_id = ?
                      AND m.visibility = 'visible'
                      AND m.history_eligible = 1
                      AND m.role IN ('user', 'assistant')
                      AND (m.run_id IS NULL OR r.state IN ('completed', 'failed',
                           'stopped', 'superseded', 'interrupted'))
                    ORDER BY m.ordinal
                    """,
                    (session_id,),
                ).fetchall()
                messages: list[dict[str, Any]] = []
                all_message_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM messages WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()[0]
                )
                for row in rows:
                    try:
                        content = json.loads(str(row["content_json"] or "[]"))
                    except (TypeError, ValueError):
                        excluded_count += 1
                        continue
                    if (
                        not isinstance(content, list)
                        or not all(
                            isinstance(part, Mapping)
                            and str(part.get("type") or "").strip().casefold() == "text"
                            and isinstance(part.get("text"), str)
                            for part in content
                        )
                        or contains_persistent_inline_media(content)
                    ):
                        excluded_count += 1
                        continue
                    existing_context = _json_object(row["message_context_json"])
                    prior = existing_context.get("conversation_continuity")
                    if not isinstance(prior, Mapping):
                        prior = {}
                    origin_instance = str(prior.get("source_instance") or source).upper()
                    origin_session_id = str(prior.get("source_session_id") or session_id)
                    origin_message_id = str(prior.get("source_message_id") or row["message_id"])
                    origin_ordinal = int(prior.get("source_ordinal") or row["ordinal"])
                    origin_created_at = str(prior.get("source_created_at") or row["created_at"])
                    origin_ref = str(prior.get("origin_ref") or "") or _continuity_origin_ref(
                        source_instance=origin_instance,
                        source_session_id=origin_session_id,
                        source_message_id=origin_message_id,
                        source_ordinal=origin_ordinal,
                        content_hash=str(row["content_hash"]),
                    )
                    if origin_ref in seen_origins:
                        raise SessionConflict("duplicate conversation origin in source history")
                    seen_origins.add(origin_ref)
                    messages.append(
                        {
                            "origin_ref": origin_ref,
                            "source_instance": origin_instance,
                            "source_session_id": origin_session_id,
                            "source_message_id": origin_message_id,
                            "source_ordinal": origin_ordinal,
                            "source_created_at": origin_created_at,
                            "role": str(row["role"]),
                            "source": str(row["source"]),
                            "content": [dict(part) for part in content],
                            "text": str(row["text"]),
                            **(
                                {"display_text": str(row["display_text"])}
                                if row["display_text"] is not None
                                else {}
                            ),
                            "content_hash": str(row["content_hash"]),
                        }
                    )
                excluded_count += max(0, all_message_count - len(rows))
                eligible_count += len(messages)
                exported_sessions.append(
                    {
                        "source_session_id": session_id,
                        "title": str(session["title"]),
                        "title_source": str(session["title_source"]),
                        "status": str(session["status"]),
                        "is_default": bool(session["is_default"]),
                        "created_at": str(session["created_at"]),
                        "bindings": [
                            {
                                "surface": str(binding["surface"]),
                                "channel_key": str(binding["channel_key"]),
                            }
                            for binding in bindings
                        ],
                        "messages": messages,
                    }
                )
        capsule: dict[str, Any] = {
            "type": CONVERSATION_CONTINUITY_TYPE,
            "schema_version": CONVERSATION_CONTINUITY_VERSION,
            "transfer_id": transfer,
            "source_instance": source,
            "owner_id": owner,
            "agent_id": agent,
            "history_mode": mode,
            "sessions": exported_sessions,
            "summary": {
                "session_count": len(exported_sessions),
                "eligible_message_count": eligible_count,
                "excluded_message_count": excluded_count,
                "attachments_included": 0,
            },
        }
        capsule["capsule_digest"] = conversation_continuity_digest(capsule)
        return capsule

    def import_conversation_continuity(
        self,
        capsule: Mapping[str, Any],
        *,
        owner_id: str,
        agent_id: str,
        transfer_id: str,
        history_mode: str,
    ) -> dict[str, Any]:
        """Atomically prepend validated history and retain stable import provenance."""
        owner = str(owner_id or "").strip()
        agent = str(agent_id or "").strip().lower()
        transfer = str(transfer_id or "").strip()
        mode = str(history_mode or "").strip().lower()
        if mode not in CONVERSATION_HISTORY_MODES:
            raise ValueError("unsupported conversation history mode")
        validated = validate_conversation_continuity_capsule(
            capsule,
            owner_id=owner,
            transfer_id=transfer,
            history_mode=mode,
        )
        payload = validated["payload"]
        capsule_digest = str(validated["capsule_digest"])
        normalized_sessions = list(validated["sessions"])
        message_count = int(validated["message_count"])

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_batch = connection.execute(
                "SELECT * FROM conversation_continuity_batches WHERE transfer_id = ?",
                (transfer,),
            ).fetchone()
            if existing_batch is not None:
                if (
                    str(existing_batch["capsule_digest"]) != capsule_digest
                    or str(existing_batch["owner_id"]) != owner
                    or str(existing_batch["agent_id"]) != agent
                    or str(existing_batch["history_mode"]) != mode
                ):
                    raise SessionConflict("conversation continuity transfer ID is already in use")
                details = _json_object(existing_batch["details_json"])
                return {
                    **details,
                    "imported_messages": 0,
                    "replayed": True,
                }

            mapping: dict[str, str] = {}
            created_session_ids: list[str] = []
            state_before: dict[str, dict[str, Any]] = {}
            binding_state_before: dict[str, str | None] = {}
            binding_state_after: dict[str, str] = {}
            origin_refs = [
                str(message["origin_ref"])
                for source_session in normalized_sessions
                for message in source_session["messages"]
            ]
            existing_import_targets: dict[str, str] = {}
            # Keep this below SQLite's common bind-variable limit while avoiding
            # one lookup per message for large continuity capsules.
            for offset in range(0, len(origin_refs), 400):
                chunk = origin_refs[offset : offset + 400]
                placeholders = ",".join("?" for _item in chunk)
                rows = connection.execute(
                    f"""
                    SELECT origin_ref, target_session_id
                    FROM conversation_continuity_imports
                    WHERE owner_id=? AND agent_id=?
                      AND origin_ref IN ({placeholders})
                    """,
                    (owner, agent, *chunk),
                ).fetchall()
                existing_import_targets.update(
                    {
                        str(row["origin_ref"]): str(row["target_session_id"])
                        for row in rows
                    }
                )

            def current_default() -> sqlite3.Row | None:
                return connection.execute(
                    """
                    SELECT * FROM sessions WHERE instance_id=? AND owner_id=?
                      AND agent_id=? AND is_default=1
                    """,
                    (self.instance_id, owner, agent),
                ).fetchone()

            def create_target_session(source_session: Mapping[str, Any]) -> str:
                session_id = _new_id("ses")
                now = _utc_now()
                read_only = mode == "inherit_read_only"
                wants_default = bool(source_session.get("is_default")) and not read_only
                is_default = bool(wants_default and current_default() is None)
                status = "archived" if read_only else (
                    "archived" if source_session.get("status") == "archived" else "active"
                )
                title = str(source_session.get("title") or "Imported history")
                if read_only:
                    title = f"{title} (inherited history)"
                connection.execute(
                    """
                    INSERT INTO sessions(
                        session_id, instance_id, owner_id, agent_id, title,
                        title_source, status, is_default, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        self.instance_id,
                        owner,
                        agent,
                        title[:500],
                        str(source_session.get("title_source") or "system"),
                        status,
                        int(is_default),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO session_participants(session_id, agent_id, created_at) VALUES (?, ?, ?)",
                    (session_id, agent, now),
                )
                connection.execute(
                    """
                    INSERT INTO session_context_generations(session_id, generation, reason, created_at)
                    VALUES (?, 1, 'conversation_continuity_import', ?)
                    """,
                    (session_id, now),
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    run_id=None,
                    kind="session.created",
                    status=status,
                    summary="Session created for imported conversation history",
                    detail={"history_mode": mode, "transfer_id": transfer},
                )
                created_session_ids.append(session_id)
                return session_id

            for source_session in normalized_sessions:
                target_session_id = ""
                claimed_target_ids = set(mapping.values())
                if mode != "inherit_read_only":
                    for binding in source_session["bindings"]:
                        bound = connection.execute(
                            """
                            SELECT session_id FROM channel_bindings
                            WHERE instance_id=? AND owner_id=? AND agent_id=?
                              AND surface=? AND channel_key=?
                            """,
                            (
                                self.instance_id,
                                owner,
                                agent,
                                binding["surface"],
                                binding["channel_key"],
                            ),
                        ).fetchone()
                        candidate_session_id = (
                            str(bound["session_id"])
                            if bound is not None
                            else ""
                        )
                        if (
                            candidate_session_id
                            and candidate_session_id not in claimed_target_ids
                        ):
                            target_session_id = candidate_session_id
                            break
                    if not target_session_id and source_session["is_default"]:
                        default = current_default()
                        candidate_session_id = (
                            str(default["session_id"])
                            if default is not None
                            else ""
                        )
                        if candidate_session_id not in claimed_target_ids:
                            target_session_id = candidate_session_id
                if not target_session_id and source_session["messages"]:
                    prior_targets = {
                        existing_import_targets.get(str(message["origin_ref"]))
                        for message in source_session["messages"]
                    }
                    prior_targets.discard(None)
                    if (
                        len(prior_targets) == 1
                        and all(
                            str(message["origin_ref"]) in existing_import_targets
                            for message in source_session["messages"]
                        )
                    ):
                        # A differently identified retry must not manufacture an
                        # empty read-only archive after stable origins were
                        # already satisfied for this owner and target Agent.
                        target_session_id = str(next(iter(prior_targets)))
                if not target_session_id:
                    target_session_id = create_target_session(source_session)
                mapping[source_session["source_session_id"]] = target_session_id

                target_row = connection.execute(
                    "SELECT * FROM sessions WHERE session_id=? AND instance_id=? AND owner_id=? AND agent_id=?",
                    (target_session_id, self.instance_id, owner, agent),
                ).fetchone()
                if target_row is None:
                    raise SessionConflict("conversation continuity target Session is not owned by the target")
                state_before.setdefault(
                    target_session_id,
                    {
                        "revision": int(target_row["revision"]),
                        "history_generation": int(target_row["history_generation"]),
                        "next_message_ordinal": int(target_row["next_message_ordinal"]),
                        "updated_at": str(target_row["updated_at"]),
                    },
                )
                if mode != "inherit_read_only" and str(target_row["status"]) == "active":
                    for binding in source_session["bindings"]:
                        binding_key = f"{binding['surface']}\u0000{binding['channel_key']}"
                        if binding_key not in binding_state_before:
                            previous = connection.execute(
                                """
                                SELECT session_id FROM channel_bindings
                                WHERE instance_id=? AND owner_id=? AND agent_id=?
                                  AND surface=? AND channel_key=?
                                """,
                                (
                                    self.instance_id,
                                    owner,
                                    agent,
                                    binding["surface"],
                                    binding["channel_key"],
                                ),
                            ).fetchone()
                            binding_state_before[binding_key] = (
                                str(previous["session_id"])
                                if previous is not None
                                else None
                            )
                        connection.execute(
                            """
                            INSERT INTO channel_bindings(
                                instance_id, owner_id, agent_id, surface,
                                channel_key, session_id, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(instance_id, owner_id, agent_id, surface, channel_key)
                            DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at
                            """,
                            (
                                self.instance_id,
                                owner,
                                agent,
                                binding["surface"],
                                binding["channel_key"],
                                target_session_id,
                                _utc_now(),
                            ),
                        )
                        binding_state_after[binding_key] = target_session_id

            by_target: dict[str, list[dict[str, Any]]] = {}
            for source_session in normalized_sessions:
                target_session_id = mapping[source_session["source_session_id"]]
                by_target.setdefault(target_session_id, []).extend(source_session["messages"])

            imported_count = 0
            inserted_by_session: dict[str, int] = {}
            imported_at = _utc_now()
            for target_session_id, candidates in by_target.items():
                new_messages = [
                    item
                    for item in candidates
                    if str(item["origin_ref"]) not in existing_import_targets
                ]
                new_messages.sort(
                    key=lambda item: (
                        str(item.get("source_created_at") or ""),
                        str(item.get("source_session_id") or ""),
                        int(item.get("source_ordinal") or 0),
                        str(item.get("origin_ref") or ""),
                    )
                )
                count = len(new_messages)
                inserted_by_session[target_session_id] = count
                if not count:
                    continue
                bounds = connection.execute(
                    "SELECT COALESCE(MAX(ordinal), 0) AS maximum FROM messages WHERE session_id=?",
                    (target_session_id,),
                ).fetchone()
                maximum = int(bounds["maximum"])
                shift = maximum + count + 1000
                connection.execute(
                    "UPDATE messages SET ordinal=ordinal+? WHERE session_id=?",
                    (shift, target_session_id),
                )
                connection.execute(
                    "UPDATE messages SET ordinal=ordinal-?+? WHERE session_id=?",
                    (shift, count, target_session_id),
                )
                target_session = connection.execute(
                    "SELECT context_generation FROM sessions WHERE session_id=?",
                    (target_session_id,),
                ).fetchone()
                generation = int(target_session["context_generation"])
                for ordinal, item in enumerate(new_messages, 1):
                    message_id = _new_id("msg")
                    provenance = {
                        "schema_version": CONVERSATION_CONTINUITY_VERSION,
                        "source_instance": item["source_instance"],
                        "source_session_id": str(item["source_session_id"]),
                        "source_message_id": str(item["source_message_id"]),
                        "source_ordinal": int(item["source_ordinal"]),
                        "source_created_at": str(item["source_created_at"]),
                        "origin_ref": str(item["origin_ref"]),
                        "transfer_id": transfer,
                        "history_mode": mode,
                    }
                    author_id = owner if item["role"] == "user" else agent
                    connection.execute(
                        """
                        INSERT INTO messages(
                            message_id, session_id, run_id, ordinal,
                            context_generation, role, author_id, source,
                            message_context_json, content_json, text, display_text,
                            visibility,
                            history_eligible, content_hash, created_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'visible', 1, ?, ?)
                        """,
                        (
                            message_id,
                            target_session_id,
                            ordinal,
                            generation,
                            item["role"],
                            author_id,
                            f"continuity:{str(item.get('source') or 'unknown')[:120]}",
                            _json({"conversation_continuity": provenance}),
                            item["content_json"],
                            str(item.get("text") or ""),
                            item.get("display_text"),
                            item["content_hash"],
                            str(item.get("source_created_at") or imported_at),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO conversation_continuity_imports(
                            owner_id, agent_id, origin_ref, transfer_id,
                            target_session_id, target_message_id,
                            capsule_digest, imported_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            owner,
                            agent,
                            item["origin_ref"],
                            transfer,
                            target_session_id,
                            message_id,
                            capsule_digest,
                            imported_at,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE sessions
                    SET next_message_ordinal=next_message_ordinal+?,
                        revision=revision+1,
                        history_generation=history_generation+1,
                        updated_at=?
                    WHERE session_id=?
                    """,
                    (count, imported_at, target_session_id),
                )
                self._append_event(
                    connection,
                    session_id=target_session_id,
                    run_id=None,
                    kind="session.history_imported",
                    status="completed",
                    phase="migration",
                    summary="Conversation history imported",
                    detail={
                        "transfer_id": transfer,
                        "history_mode": mode,
                        "message_count": count,
                        "source_instance": str(payload.get("source_instance") or ""),
                    },
                )
                imported_count += count

            details = {
                "transfer_id": transfer,
                "history_mode": mode,
                "target_session_ids": sorted(set(mapping.values())),
                "created_session_ids": created_session_ids,
                "source_session_map": mapping,
                "session_state_before": state_before,
                "binding_state_before": binding_state_before,
                "binding_state_after": binding_state_after,
                "inserted_by_session": inserted_by_session,
                "imported_messages": imported_count,
                "satisfied_messages": message_count,
                "deduplicated_messages": message_count - imported_count,
                "replayed": False,
            }
            connection.execute(
                """
                INSERT INTO conversation_continuity_batches(
                    transfer_id, capsule_digest, owner_id, agent_id,
                    history_mode, details_json, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transfer,
                    capsule_digest,
                    owner,
                    agent,
                    mode,
                    _json(details),
                    imported_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO conversation_continuity_origin_claims(
                    transfer_id, owner_id, agent_id, origin_ref, claimed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (transfer, owner, agent, origin_ref, imported_at)
                    for origin_ref in origin_refs
                ],
            )
            return details

    def rollback_conversation_continuity(self, transfer_id: str) -> dict[str, Any]:
        """Remove only one transfer's imported messages, preserving later target work."""
        transfer = str(transfer_id or "").strip()
        if not transfer:
            raise ValueError("transfer_id is required")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                "SELECT * FROM conversation_continuity_batches WHERE transfer_id=?",
                (transfer,),
            ).fetchone()
            if batch is None:
                return {"transfer_id": transfer, "removed_messages": 0, "replayed": True}
            details = _json_object(batch["details_json"])
            created = set(details.get("created_session_ids") or [])
            connection.execute(
                "DELETE FROM conversation_continuity_origin_claims WHERE transfer_id=?",
                (transfer,),
            )
            imports = connection.execute(
                """
                SELECT * FROM conversation_continuity_imports
                WHERE transfer_id=? ORDER BY target_session_id, target_message_id
                """,
                (transfer,),
            ).fetchall()
            by_session: dict[str, list[str]] = {}
            retained_shared_messages = 0
            replacement_sessions: dict[str, set[str]] = {}
            for item in imports:
                replacement = connection.execute(
                    """
                    SELECT claim.transfer_id, batch.capsule_digest
                    FROM conversation_continuity_origin_claims AS claim
                    JOIN conversation_continuity_batches AS batch
                      ON batch.transfer_id = claim.transfer_id
                    WHERE claim.owner_id=? AND claim.agent_id=?
                      AND claim.origin_ref=?
                    ORDER BY batch.imported_at, claim.transfer_id
                    LIMIT 1
                    """,
                    (
                        str(item["owner_id"]),
                        str(item["agent_id"]),
                        str(item["origin_ref"]),
                    ),
                ).fetchone()
                if replacement is not None:
                    replacement_transfer = str(replacement["transfer_id"])
                    connection.execute(
                        """
                        UPDATE conversation_continuity_imports
                        SET transfer_id=?, capsule_digest=?
                        WHERE owner_id=? AND agent_id=? AND origin_ref=?
                        """,
                        (
                            replacement_transfer,
                            str(replacement["capsule_digest"]),
                            str(item["owner_id"]),
                            str(item["agent_id"]),
                            str(item["origin_ref"]),
                        ),
                    )
                    replacement_sessions.setdefault(
                        replacement_transfer,
                        set(),
                    ).add(str(item["target_session_id"]))
                    retained_shared_messages += 1
                    continue
                connection.execute(
                    """
                    DELETE FROM conversation_continuity_imports
                    WHERE owner_id=? AND agent_id=? AND origin_ref=?
                    """,
                    (
                        str(item["owner_id"]),
                        str(item["agent_id"]),
                        str(item["origin_ref"]),
                    ),
                )
                by_session.setdefault(str(item["target_session_id"]), []).append(
                    str(item["target_message_id"])
                )

            # If another transfer deduplicated against a Session created by the
            # batch being rolled back, hand that cleanup responsibility (and
            # the original binding predecessor) to every remaining claimant.
            for replacement_transfer, session_ids in replacement_sessions.items():
                replacement_batch = connection.execute(
                    """
                    SELECT details_json FROM conversation_continuity_batches
                    WHERE transfer_id=?
                    """,
                    (replacement_transfer,),
                ).fetchone()
                if replacement_batch is None:
                    raise SessionConflict(
                        "conversation continuity replacement journal is missing"
                    )
                replacement_details = _json_object(
                    replacement_batch["details_json"]
                )
                inherited_created = set(
                    replacement_details.get("created_session_ids") or []
                )
                inherited_created.update(created.intersection(session_ids))
                replacement_details["created_session_ids"] = sorted(
                    inherited_created
                )
                current_before = dict(details.get("binding_state_before") or {})
                current_after = dict(details.get("binding_state_after") or {})
                replacement_before = dict(
                    replacement_details.get("binding_state_before") or {}
                )
                replacement_after = dict(
                    replacement_details.get("binding_state_after") or {}
                )
                for key, target_session_id in current_after.items():
                    if (
                        str(target_session_id) in session_ids
                        and replacement_after.get(key) == target_session_id
                        and replacement_before.get(key) == target_session_id
                    ):
                        replacement_before[key] = current_before.get(key)
                replacement_details["binding_state_before"] = replacement_before
                connection.execute(
                    """
                    UPDATE conversation_continuity_batches SET details_json=?
                    WHERE transfer_id=?
                    """,
                    (_json(replacement_details), replacement_transfer),
                )
            for message_ids in by_session.values():
                connection.executemany(
                    "DELETE FROM messages WHERE message_id=?",
                    [(message_id,) for message_id in message_ids],
                )

            empty_created_sessions: list[str] = []
            for session_id in set(details.get("target_session_ids") or []) | set(by_session):
                session = connection.execute(
                    "SELECT * FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    continue
                remaining = connection.execute(
                    "SELECT message_id FROM messages WHERE session_id=? ORDER BY ordinal",
                    (session_id,),
                ).fetchall()
                if session_id in created and not remaining:
                    empty_created_sessions.append(session_id)
                    continue
                high = len(remaining) + 1_000_000
                for index, row in enumerate(remaining, 1):
                    connection.execute(
                        "UPDATE messages SET ordinal=? WHERE message_id=?",
                        (high + index, str(row["message_id"])),
                    )
                for index, row in enumerate(remaining, 1):
                    connection.execute(
                        "UPDATE messages SET ordinal=? WHERE message_id=?",
                        (index, str(row["message_id"])),
                    )
                connection.execute(
                    """
                    UPDATE sessions SET next_message_ordinal=?, revision=revision+1,
                        history_generation=history_generation+1, updated_at=?
                    WHERE session_id=?
                    """,
                    (len(remaining) + 1, _utc_now(), session_id),
                )
            binding_state_after = dict(details.get("binding_state_after") or {})
            for key, previous_session_id in dict(
                details.get("binding_state_before") or {}
            ).items():
                surface, separator, channel_key = str(key).partition("\u0000")
                if not separator:
                    raise SessionConflict(
                        "conversation continuity rollback binding journal is invalid"
                    )
                expected_session_id = str(binding_state_after.get(key) or "")
                if expected_session_id:
                    current_binding = connection.execute(
                        """
                        SELECT session_id FROM channel_bindings
                        WHERE instance_id=? AND owner_id=? AND agent_id=?
                          AND surface=? AND channel_key=?
                        """,
                        (
                            self.instance_id,
                            str(batch["owner_id"]),
                            str(batch["agent_id"]),
                            surface,
                            channel_key,
                        ),
                    ).fetchone()
                    current_session_id = (
                        str(current_binding["session_id"])
                        if current_binding is not None
                        else ""
                    )
                    if current_session_id != expected_session_id:
                        # A user or later operation has rebound this channel.
                        # The rollback owns only the value it published.
                        continue
                    remaining_claim = connection.execute(
                        """
                        SELECT 1
                        FROM conversation_continuity_origin_claims AS claim
                        JOIN conversation_continuity_imports AS imported
                          ON imported.owner_id = claim.owner_id
                         AND imported.agent_id = claim.agent_id
                         AND imported.origin_ref = claim.origin_ref
                        WHERE claim.owner_id=? AND claim.agent_id=?
                          AND imported.target_session_id=?
                        LIMIT 1
                        """,
                        (
                            str(batch["owner_id"]),
                            str(batch["agent_id"]),
                            expected_session_id,
                        ),
                    ).fetchone()
                    if remaining_claim is not None:
                        continue
                if previous_session_id:
                    connection.execute(
                        """
                        INSERT INTO channel_bindings(
                            instance_id, owner_id, agent_id, surface,
                            channel_key, session_id, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(instance_id, owner_id, agent_id, surface, channel_key)
                        DO UPDATE SET session_id=excluded.session_id,
                                      updated_at=excluded.updated_at
                        """,
                        (
                            self.instance_id,
                            str(batch["owner_id"]),
                            str(batch["agent_id"]),
                            surface,
                            channel_key,
                            str(previous_session_id),
                            _utc_now(),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM channel_bindings
                        WHERE instance_id=? AND owner_id=? AND agent_id=?
                          AND surface=? AND channel_key=?
                        """,
                        (
                            self.instance_id,
                            str(batch["owner_id"]),
                            str(batch["agent_id"]),
                            surface,
                            channel_key,
                        ),
                    )
            for session_id in empty_created_sessions:
                for table in (
                    "delivery_outbox",
                    "connector_delivery_receipts",
                    "frontend_message_events",
                    "event_consumers",
                    "run_events",
                    "session_workzones",
                    "session_participants",
                    "session_context_generations",
                    "channel_bindings",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE session_id=?",
                        (session_id,),
                    )
                connection.execute(
                    "DELETE FROM sessions WHERE session_id=?",
                    (session_id,),
                )
            connection.execute(
                "DELETE FROM conversation_continuity_batches WHERE transfer_id=?",
                (transfer,),
            )
            return {
                "transfer_id": transfer,
                "removed_messages": sum(len(items) for items in by_session.values()),
                "retained_shared_messages": retained_shared_messages,
                "replayed": False,
            }

    def conversation_continuity_import_status(
        self,
        transfer_id: str,
    ) -> dict[str, Any] | None:
        transfer = str(transfer_id or "").strip()
        if not transfer:
            raise ValueError("transfer_id is required")
        with self._lock, self._connection() as connection:
            batch = connection.execute(
                "SELECT * FROM conversation_continuity_batches WHERE transfer_id=?",
                (transfer,),
            ).fetchone()
            if batch is None:
                return None
            imported = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM conversation_continuity_imports
                    WHERE transfer_id=?
                    """,
                    (transfer,),
                ).fetchone()[0]
            )
        return {
            **_json_object(batch["details_json"]),
            "transfer_id": transfer,
            "capsule_digest": str(batch["capsule_digest"]),
            "owner_id": str(batch["owner_id"]),
            "agent_id": str(batch["agent_id"]),
            "history_mode": str(batch["history_mode"]),
            "imported_messages": imported,
            "imported_at": str(batch["imported_at"]),
        }

    def archive_agent_conversation_sessions(
        self,
        *,
        owner_id: str,
        agent_id: str,
        transfer_id: str,
    ) -> dict[str, Any]:
        """Retire one moved source's bindings without deleting recoverable history."""

        owner = str(owner_id or "").strip()
        agent = str(agent_id or "").strip().lower()
        transfer = str(transfer_id or "").strip()
        if not all((owner, agent, transfer)):
            raise ValueError("owner_id, agent_id, and transfer_id are required")
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM conversation_continuity_retirements
                WHERE transfer_id=?
                """,
                (transfer,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["owner_id"]) != owner
                    or str(existing["agent_id"]) != agent
                ):
                    raise SessionConflict(
                        "conversation retirement transfer ID is already in use"
                    )
                return {
                    "transfer_id": transfer,
                    "session_ids": json.loads(existing["session_ids_json"]),
                    "replayed": True,
                }
            sessions = connection.execute(
                """
                SELECT session_id FROM sessions
                WHERE instance_id=? AND owner_id=? AND agent_id=?
                  AND status != 'deleted'
                ORDER BY session_id
                """,
                (self.instance_id, owner, agent),
            ).fetchall()
            session_ids = [str(row["session_id"]) for row in sessions]
            retired_at = _utc_now()
            for session_id in session_ids:
                connection.execute(
                    """
                    UPDATE sessions SET status='archived', is_default=0,
                        revision=revision+1,
                        history_generation=history_generation+1,
                        updated_at=? WHERE session_id=?
                    """,
                    (retired_at, session_id),
                )
                connection.execute(
                    "DELETE FROM channel_bindings WHERE session_id=?",
                    (session_id,),
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    run_id=None,
                    kind="session.history_moved_out",
                    status="archived",
                    phase="migration",
                    summary="Conversation history retired after verified Agent move",
                    detail={"transfer_id": transfer},
                )
            connection.execute(
                """
                INSERT INTO conversation_continuity_retirements(
                    transfer_id, owner_id, agent_id, session_ids_json, retired_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (transfer, owner, agent, _json(session_ids), retired_at),
            )
            return {
                "transfer_id": transfer,
                "session_ids": session_ids,
                "replayed": False,
            }

    def archive_agent_sessions(self, agent_id: str) -> list[str]:
        """Archive all active sessions for an agent upon deletion."""
        agent = str(agent_id or "").strip().lower()
        if not agent:
            return []
        retired_at = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            sessions = connection.execute(
                """
                SELECT session_id FROM sessions
                WHERE instance_id=? AND agent_id=?
                  AND status != 'deleted' AND status != 'archived'
                ORDER BY session_id
                """,
                (self.instance_id, agent),
            ).fetchall()
            session_ids = [str(row["session_id"]) for row in sessions]
            for session_id in session_ids:
                connection.execute(
                    """
                    UPDATE sessions SET status='archived', is_default=0,
                        revision=revision+1,
                        history_generation=history_generation+1,
                        updated_at=? WHERE session_id=?
                    """,
                    (retired_at, session_id),
                )
                connection.execute(
                    "DELETE FROM channel_bindings WHERE session_id=?",
                    (session_id,),
                )
                self._append_event(
                    connection,
                    session_id=session_id,
                    run_id=None,
                    kind="session.archived",
                    status="archived",
                    phase="deletion",
                    summary="Conversation history archived after Agent deletion",
                    detail={"agent_id": agent},
                )
            return session_ids

    def recent_exchanges(
        self,
        session_id: str,
        *,
        context_generation: int | None = None,
        max_user_ordinal: int | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        session = self.get_session(session_id)
        generation = int(context_generation or session["context_generation"])
        high_water = (
            None if max_user_ordinal is None else max(0, int(max_user_ordinal))
        )
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.run_id, u.ordinal AS sequence, u.message_id AS user_message_id,
                       a.message_id AS assistant_message_id,
                       u.created_at AS user_ts, a.created_at AS assistant_ts,
                       u.source AS user_source, a.source AS assistant_source,
                       u.text AS user_text, a.text AS assistant_text,
                       (SELECT GROUP_CONCAT(DISTINCT vt.provenance)
                          FROM voice_transcripts AS vt
                         WHERE vt.message_id=u.message_id
                           AND vt.safe_voice_state='released')
                           AS user_transcript_provenance,
                       CASE WHEN a.content_json LIKE '%provider_audio_transcript%'
                            THEN 'provider_audio_transcript' ELSE '' END
                           AS assistant_transcript_provenance
                FROM runs AS r
                JOIN messages AS u ON u.message_id = r.user_message_id
                JOIN messages AS a ON a.message_id = r.final_message_id
                WHERE r.session_id = ? AND r.context_generation = ?
                  AND r.state = 'completed'
                  AND u.visibility = 'visible' AND a.visibility = 'visible'
                  AND u.history_eligible = 1 AND a.history_eligible = 1
                  AND (? IS NULL OR u.ordinal <= ?)
                ORDER BY u.ordinal DESC LIMIT ?
                """,
                (
                    str(session_id),
                    generation,
                    high_water,
                    high_water,
                    max(1, min(int(limit), 100)),
                ),
            ).fetchall()
        exchanges = [dict(row) for row in reversed(rows)]
        for exchange in exchanges:
            exchange["exchange_id"] = str(exchange.get("run_id") or "")

        live_units: dict[str, list[dict[str, Any]]] = {}
        if high_water is None:
            for item in self._live_history_messages(
                session_id,
                owner_id=str(session["owner_id"]),
                context_generation=generation,
            ):
                live_units.setdefault(str(item["history_unit_id"]), []).append(item)
        for unit_id, items in live_units.items():
            user = next((item for item in items if item["role"] == "user"), None)
            assistant = next(
                (item for item in items if item["role"] == "assistant"),
                None,
            )
            if user is None and assistant is None:
                continue
            sequences = [int(item.get("sequence") or 0) for item in items]
            exchanges.append(
                {
                    "run_id": None,
                    "exchange_id": unit_id,
                    "sequence": min(sequences) if sequences else 0,
                    "user_message_id": user.get("message_id") if user else None,
                    "assistant_message_id": (
                        assistant.get("message_id") if assistant else None
                    ),
                    "user_ts": user.get("created_at") if user else None,
                    "assistant_ts": (
                        assistant.get("created_at") if assistant else None
                    ),
                    "user_source": user.get("source") if user else None,
                    "assistant_source": (
                        assistant.get("source") if assistant else None
                    ),
                    "user_text": user.get("text", "") if user else "",
                    "assistant_text": (
                        assistant.get("text", "") if assistant else ""
                    ),
                    "user_transcript_provenance": (
                        "gpt_live_transcript" if user else ""
                    ),
                    "assistant_transcript_provenance": (
                        "gpt_live_transcript" if assistant else ""
                    ),
                }
            )

        def exchange_time(item: Mapping[str, Any]) -> str:
            return str(item.get("user_ts") or item.get("assistant_ts") or "")

        exchanges.sort(
            key=lambda item: (
                exchange_time(item),
                int(item.get("sequence") or 0),
                str(item.get("exchange_id") or item.get("run_id") or ""),
            )
        )
        return exchanges[-max(1, min(int(limit), 100)) :]

    def recent_agent_exchanges(
        self,
        *,
        owner_id: str,
        agent_id: str,
        limit: int = 10,
        excluded_sources: Iterable[str] = (),
    ) -> list[dict[str, Any]]:
        """Return recent completed Bridge exchanges across Session boundaries.

        Session-local history remains the authority for ordinary turn context.
        Explicit continuity operations such as ``/handoff`` instead need the
        owner's recent Agent timeline regardless of which Session is currently
        selected. Deleted Sessions are intentionally excluded; archived
        Sessions remain eligible because archival retains their history.
        """

        normalized_sources = sorted(
            {
                str(source).strip().lower()
                for source in excluded_sources
                if str(source).strip()
            }
        )
        source_clause = ""
        source_params: list[Any] = []
        if normalized_sources:
            placeholders = ",".join("?" for _ in normalized_sources)
            source_clause = f" AND LOWER(u.source) NOT IN ({placeholders})"
            source_params.extend(normalized_sources)

        params: list[Any] = [
            self.instance_id,
            str(owner_id),
            str(agent_id).strip().lower(),
            *source_params,
            max(1, min(int(limit), 100)),
        ]
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.run_id, r.session_id, r.context_generation,
                       u.ordinal AS session_sequence,
                       u.message_id AS user_message_id,
                       a.message_id AS assistant_message_id,
                       u.created_at AS user_ts, a.created_at AS assistant_ts,
                       u.source AS user_source, a.source AS assistant_source,
                       u.text AS user_text, a.text AS assistant_text,
                       (SELECT GROUP_CONCAT(DISTINCT vt.provenance)
                          FROM voice_transcripts AS vt
                         WHERE vt.message_id=u.message_id
                           AND vt.safe_voice_state='released')
                           AS user_transcript_provenance,
                       CASE WHEN a.content_json LIKE '%provider_audio_transcript%'
                            THEN 'provider_audio_transcript' ELSE '' END
                           AS assistant_transcript_provenance
                FROM runs AS r
                JOIN sessions AS s ON s.session_id = r.session_id
                JOIN messages AS u ON u.message_id = r.user_message_id
                JOIN messages AS a ON a.message_id = r.final_message_id
                WHERE s.instance_id = ? AND s.owner_id = ? AND s.agent_id = ?
                  AND s.session_kind = 'conversation'
                  AND s.status != 'deleted'
                  AND r.state = 'completed'
                  AND u.visibility = 'visible' AND a.visibility = 'visible'
                  AND u.history_eligible = 1 AND a.history_eligible = 1
                  {source_clause}
                ORDER BY u.created_at DESC, a.created_at DESC, r.run_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def last_user_message_at(
        self,
        *,
        agent_id: str,
        owner_id: str | None = None,
    ) -> str | None:
        clauses = [
            "s.instance_id = ?",
            "s.agent_id = ?",
            "s.session_kind = 'conversation'",
            "m.role = 'user'",
        ]
        params: list[Any] = [self.instance_id, str(agent_id)]
        if owner_id is not None:
            clauses.append("s.owner_id = ?")
            params.append(str(owner_id))
        with self._lock, self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT MAX(m.created_at) AS created_at
                FROM messages AS m
                JOIN sessions AS s ON s.session_id = m.session_id
                WHERE {" AND ".join(clauses)}
                """,
                params,
            ).fetchone()
        value = row["created_at"] if row is not None else None
        return str(value) if value else None

    def start_fresh_generation(
        self, session_id: str, *, reason: str = "fresh"
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status = 'active'",
                (str(session_id),),
            ).fetchone()
            if row is None:
                raise SessionNotFound(str(session_id))
            active_run = connection.execute(
                """
                SELECT run_id FROM runs
                WHERE session_id = ? AND state NOT IN (
                    'completed', 'failed', 'stopped', 'superseded', 'interrupted'
                ) LIMIT 1
                """,
                (str(session_id),),
            ).fetchone()
            if active_run is not None:
                raise SessionConflict(
                    "fresh context is blocked while the Session has an active Run"
                )
            generation = int(row["context_generation"]) + 1
            connection.execute(
                """
                UPDATE sessions SET context_generation = ?, revision = revision + 1,
                    updated_at = ? WHERE session_id = ?
                """,
                (generation, now, str(session_id)),
            )
            connection.execute(
                """
                INSERT INTO session_context_generations(session_id, generation, reason, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(session_id), generation, str(reason), now),
            )
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind="session.context_generation_started",
                status="active",
                phase="control",
                summary="Fresh context generation started",
                detail={"generation": generation, "reason": str(reason)},
                outbox=True,
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            return self._session_dict(updated)

    @staticmethod
    def _workzone_slot_sort_key(slot_id: str) -> tuple[int, int]:
        if str(slot_id) == "main":
            return (0, 0)
        try:
            return (1, int(slot_id))
        except (TypeError, ValueError):
            return (2, 0)

    def _workzone_set_from_connection(
        self, connection: sqlite3.Connection, session_id: str
    ) -> dict[str, Any]:
        session = connection.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND instance_id = ?",
            (str(session_id), self.instance_id),
        ).fetchone()
        if session is None:
            raise SessionNotFound(str(session_id))
        rows = connection.execute(
            """
            SELECT slot_id, path, enabled, label, created_at, updated_at
            FROM session_workzones WHERE session_id = ?
            """,
            (str(session_id),),
        ).fetchall()
        slots = []
        for row in sorted(
            rows,
            key=lambda item: self._workzone_slot_sort_key(str(item["slot_id"])),
        ):
            item = dict(row)
            item["enabled"] = bool(item.get("enabled"))
            slots.append(item)
        return {
            "session_id": str(session_id),
            "revision": int(session["workzone_revision"] or 0),
            "slots": slots,
        }

    def get_workzone_set(self, session_id: str) -> dict[str, Any]:
        """Return the Session-scoped, revisioned Workzone slot collection."""

        with self._lock, self._connection() as connection:
            return self._workzone_set_from_connection(connection, str(session_id))

    @staticmethod
    def _agent_workzone_identity(owner_id: str, agent_id: str) -> tuple[str, str]:
        owner = str(owner_id or "").strip()
        agent = str(agent_id or "").strip().lower()
        if not owner or not agent:
            raise ValueError("owner_id and agent_id are required")
        return owner, agent

    def _agent_workzone_set_from_connection(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        agent_id: str,
    ) -> dict[str, Any]:
        owner, agent = self._agent_workzone_identity(owner_id, agent_id)
        profile = connection.execute(
            """
            SELECT revision FROM agent_workzone_profiles
            WHERE instance_id = ? AND owner_id = ? AND agent_id = ?
            """,
            (self.instance_id, owner, agent),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT slot_id, path, enabled, label, created_at, updated_at
            FROM agent_workzones
            WHERE instance_id = ? AND owner_id = ? AND agent_id = ?
            """,
            (self.instance_id, owner, agent),
        ).fetchall()
        slots = []
        for row in sorted(
            rows,
            key=lambda item: self._workzone_slot_sort_key(str(item["slot_id"])),
        ):
            item = dict(row)
            item["enabled"] = bool(item.get("enabled"))
            slots.append(item)
        return {
            "owner_id": owner,
            "agent_id": agent,
            "revision": int(profile["revision"] if profile is not None else 0),
            "slots": slots,
        }

    def get_agent_workzone_set(
        self, *, owner_id: str, agent_id: str
    ) -> dict[str, Any]:
        """Return the sole Agent-scoped Workzone profile; Session rows are ignored."""

        with self._lock, self._connection() as connection:
            return self._agent_workzone_set_from_connection(
                connection, owner_id=owner_id, agent_id=agent_id
            )

    @staticmethod
    def _check_agent_workzone_revision(
        profile: sqlite3.Row | None, expected_revision: int | None
    ) -> int:
        actual = int(profile["revision"] if profile is not None else 0)
        if expected_revision is not None and actual != int(expected_revision):
            raise SessionConflict(
                f"Workzone menu is stale (expected revision {expected_revision}, current {actual})"
            )
        return actual

    def _append_agent_workzone_event(
        self,
        connection: sqlite3.Connection,
        *,
        owner_id: str,
        agent_id: str,
        revision: int,
        kind: str,
        source: str,
        detail: Mapping[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO agent_workzone_events(
                event_id, instance_id, owner_id, agent_id, revision,
                kind, source, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _new_id("wze"),
                self.instance_id,
                owner_id,
                agent_id,
                int(revision),
                str(kind),
                str(source),
                _json(detail),
                created_at,
            ),
        )

    def set_agent_workzone_slot(
        self,
        *,
        owner_id: str,
        agent_id: str,
        slot_id: str,
        path: str | None = None,
        enabled: bool | None = None,
        label: str | None = None,
        expected_revision: int | None = None,
        source: str = "telegram",
    ) -> dict[str, Any]:
        """Atomically create or update one Agent-scoped Workzone slot."""

        owner, agent = self._agent_workzone_identity(owner_id, agent_id)
        slot = self._require_workzone_slot(slot_id)
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            profile = connection.execute(
                """
                SELECT revision FROM agent_workzone_profiles
                WHERE instance_id = ? AND owner_id = ? AND agent_id = ?
                """,
                (self.instance_id, owner, agent),
            ).fetchone()
            actual_revision = self._check_agent_workzone_revision(
                profile, expected_revision
            )
            existing = connection.execute(
                """
                SELECT * FROM agent_workzones
                WHERE instance_id = ? AND owner_id = ? AND agent_id = ? AND slot_id = ?
                """,
                (self.instance_id, owner, agent, slot),
            ).fetchone()
            before = None
            if existing is not None:
                before = {
                    "path": str(existing["path"]),
                    "enabled": bool(existing["enabled"]),
                    "label": str(existing["label"] or ""),
                }
            resolved_path = str(path).strip() if path is not None else ""
            if not resolved_path and existing is not None:
                resolved_path = str(existing["path"])
            if not resolved_path:
                raise ValueError("path is required for an empty workzone slot")
            after = {
                "path": resolved_path,
                "enabled": (
                    bool(enabled)
                    if enabled is not None
                    else bool(existing["enabled"] if existing is not None else True)
                ),
                "label": (
                    str(label).strip()
                    if label is not None
                    else str(existing["label"] or "") if existing is not None else ""
                ),
            }
            if before == after:
                return self._agent_workzone_set_from_connection(
                    connection, owner_id=owner, agent_id=agent
                )
            if profile is None:
                connection.execute(
                    """
                    INSERT INTO agent_workzone_profiles(
                        instance_id, owner_id, agent_id, revision, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (self.instance_id, owner, agent, now, now),
                )
                revision = 1
            else:
                revision = actual_revision + 1
                connection.execute(
                    """
                    UPDATE agent_workzone_profiles SET revision = ?, updated_at = ?
                    WHERE instance_id = ? AND owner_id = ? AND agent_id = ?
                    """,
                    (revision, now, self.instance_id, owner, agent),
                )
            connection.execute(
                """
                INSERT INTO agent_workzones(
                    instance_id, owner_id, agent_id, slot_id, path,
                    enabled, label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id, owner_id, agent_id, slot_id) DO UPDATE SET
                    path = excluded.path,
                    enabled = excluded.enabled,
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (
                    self.instance_id,
                    owner,
                    agent,
                    slot,
                    after["path"],
                    int(after["enabled"]),
                    after["label"],
                    now,
                    now,
                ),
            )
            self._append_agent_workzone_event(
                connection,
                owner_id=owner,
                agent_id=agent,
                revision=revision,
                kind="agent.workzone_slot_changed",
                source=source,
                detail={"slot": slot, "before": before, "after": after},
                created_at=now,
            )
            return self._agent_workzone_set_from_connection(
                connection, owner_id=owner, agent_id=agent
            )

    def delete_agent_workzone_slot(
        self,
        *,
        owner_id: str,
        agent_id: str,
        slot_id: str,
        expected_revision: int | None = None,
        source: str = "telegram",
    ) -> dict[str, Any]:
        owner, agent = self._agent_workzone_identity(owner_id, agent_id)
        slot = self._require_workzone_slot(slot_id)
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            profile = connection.execute(
                """SELECT revision FROM agent_workzone_profiles
                   WHERE instance_id = ? AND owner_id = ? AND agent_id = ?""",
                (self.instance_id, owner, agent),
            ).fetchone()
            actual_revision = self._check_agent_workzone_revision(
                profile, expected_revision
            )
            existing = connection.execute(
                """SELECT * FROM agent_workzones
                   WHERE instance_id = ? AND owner_id = ? AND agent_id = ? AND slot_id = ?""",
                (self.instance_id, owner, agent, slot),
            ).fetchone()
            if existing is None:
                return self._agent_workzone_set_from_connection(
                    connection, owner_id=owner, agent_id=agent
                )
            revision = actual_revision + 1
            connection.execute(
                """DELETE FROM agent_workzones
                   WHERE instance_id = ? AND owner_id = ? AND agent_id = ? AND slot_id = ?""",
                (self.instance_id, owner, agent, slot),
            )
            connection.execute(
                """UPDATE agent_workzone_profiles SET revision = ?, updated_at = ?
                   WHERE instance_id = ? AND owner_id = ? AND agent_id = ?""",
                (revision, now, self.instance_id, owner, agent),
            )
            before = {
                "path": str(existing["path"]),
                "enabled": bool(existing["enabled"]),
                "label": str(existing["label"] or ""),
            }
            self._append_agent_workzone_event(
                connection,
                owner_id=owner,
                agent_id=agent,
                revision=revision,
                kind="agent.workzone_slot_deleted",
                source=source,
                detail={"slot": slot, "before": before},
                created_at=now,
            )
            return self._agent_workzone_set_from_connection(
                connection, owner_id=owner, agent_id=agent
            )

    def disable_all_agent_workzones(
        self,
        *,
        owner_id: str,
        agent_id: str,
        expected_revision: int | None = None,
        source: str = "telegram",
    ) -> dict[str, Any]:
        owner, agent = self._agent_workzone_identity(owner_id, agent_id)
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            profile = connection.execute(
                """SELECT revision FROM agent_workzone_profiles
                   WHERE instance_id = ? AND owner_id = ? AND agent_id = ?""",
                (self.instance_id, owner, agent),
            ).fetchone()
            actual_revision = self._check_agent_workzone_revision(
                profile, expected_revision
            )
            changed = connection.execute(
                """UPDATE agent_workzones SET enabled = 0, updated_at = ?
                   WHERE instance_id = ? AND owner_id = ? AND agent_id = ? AND enabled != 0""",
                (now, self.instance_id, owner, agent),
            )
            if changed.rowcount:
                revision = actual_revision + 1
                connection.execute(
                    """UPDATE agent_workzone_profiles SET revision = ?, updated_at = ?
                       WHERE instance_id = ? AND owner_id = ? AND agent_id = ?""",
                    (revision, now, self.instance_id, owner, agent),
                )
                self._append_agent_workzone_event(
                    connection,
                    owner_id=owner,
                    agent_id=agent,
                    revision=revision,
                    kind="agent.workzones_disabled",
                    source=source,
                    detail={"changed": int(changed.rowcount)},
                    created_at=now,
                )
            return self._agent_workzone_set_from_connection(
                connection, owner_id=owner, agent_id=agent
            )

    def record_agent_workzone_reload(
        self,
        *,
        owner_id: str,
        agent_id: str,
        slots: Iterable[str],
        source: str = "telegram",
    ) -> None:
        owner, agent = self._agent_workzone_identity(owner_id, agent_id)
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            profile = connection.execute(
                """SELECT revision FROM agent_workzone_profiles
                   WHERE instance_id = ? AND owner_id = ? AND agent_id = ?""",
                (self.instance_id, owner, agent),
            ).fetchone()
            revision = int(profile["revision"] if profile is not None else 0)
            self._append_agent_workzone_event(
                connection,
                owner_id=owner,
                agent_id=agent,
                revision=revision,
                kind="agent.workzones_reloaded",
                source=source,
                detail={
                    "slots": [self._require_workzone_slot(slot) for slot in slots]
                },
                created_at=now,
            )

    @staticmethod
    def _require_workzone_slot(slot_id: str) -> str:
        slot = str(slot_id or "").strip().lower()
        if slot in {"default", "0"}:
            slot = "main"
        if slot != "main" and slot not in {str(number) for number in range(1, 10)}:
            raise ValueError("workzone slot must be main or 1..9")
        return slot

    @staticmethod
    def _check_workzone_revision(
        session: sqlite3.Row, expected_revision: int | None
    ) -> None:
        if expected_revision is None:
            return
        actual = int(session["workzone_revision"] or 0)
        if actual != int(expected_revision):
            raise SessionConflict(
                f"Workzone menu is stale (expected revision {expected_revision}, current {actual})"
            )

    def set_workzone_slot(
        self,
        session_id: str,
        slot_id: str,
        *,
        path: str | None = None,
        enabled: bool | None = None,
        label: str | None = None,
        expected_revision: int | None = None,
        source: str = "telegram",
    ) -> dict[str, Any]:
        """Atomically create or update one Workzone slot."""

        slot = self._require_workzone_slot(slot_id)
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND status = 'active'
                """,
                (str(session_id), self.instance_id),
            ).fetchone()
            if session is None:
                raise SessionNotFound(str(session_id))
            self._check_workzone_revision(session, expected_revision)
            existing = connection.execute(
                "SELECT * FROM session_workzones WHERE session_id = ? AND slot_id = ?",
                (str(session_id), slot),
            ).fetchone()
            before = None
            if existing is not None:
                before = {
                    "path": str(existing["path"]),
                    "enabled": bool(existing["enabled"]),
                    "label": str(existing["label"] or ""),
                }
            resolved_path = str(path).strip() if path is not None else ""
            if not resolved_path and existing is not None:
                resolved_path = str(existing["path"])
            if not resolved_path:
                raise ValueError("path is required for an empty workzone slot")
            resolved_enabled = (
                bool(enabled)
                if enabled is not None
                else bool(existing["enabled"] if existing is not None else True)
            )
            resolved_label = (
                str(label).strip()
                if label is not None
                else str(existing["label"] or "") if existing is not None else ""
            )
            after = {
                "path": resolved_path,
                "enabled": resolved_enabled,
                "label": resolved_label,
            }
            if before == after:
                return self._workzone_set_from_connection(connection, str(session_id))
            connection.execute(
                """
                INSERT INTO session_workzones(
                    session_id, slot_id, path, enabled, label, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, slot_id) DO UPDATE SET
                    path = excluded.path,
                    enabled = excluded.enabled,
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (
                    str(session_id),
                    slot,
                    resolved_path,
                    int(resolved_enabled),
                    resolved_label,
                    now,
                    now,
                ),
            )
            legacy_main = resolved_path if slot == "main" and resolved_enabled else None
            if slot == "main":
                connection.execute(
                    """
                    UPDATE sessions
                    SET workzone = ?, workzone_revision = workzone_revision + 1,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (legacy_main, now, str(session_id)),
                )
            else:
                connection.execute(
                    """
                    UPDATE sessions
                    SET workzone_revision = workzone_revision + 1,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, str(session_id)),
                )
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind="session.workzone_slot_changed",
                status="active",
                phase="control",
                summary=f"Session Workzone slot {slot} changed",
                detail={
                    "slot": slot,
                    "source": str(source),
                    "before": before,
                    "after": after,
                },
            )
            return self._workzone_set_from_connection(connection, str(session_id))

    def delete_workzone_slot(
        self,
        session_id: str,
        slot_id: str,
        *,
        expected_revision: int | None = None,
        source: str = "telegram",
    ) -> dict[str, Any]:
        """Delete one slot configuration without touching its filesystem path."""

        slot = self._require_workzone_slot(slot_id)
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND status = 'active'
                """,
                (str(session_id), self.instance_id),
            ).fetchone()
            if session is None:
                raise SessionNotFound(str(session_id))
            self._check_workzone_revision(session, expected_revision)
            existing = connection.execute(
                "SELECT * FROM session_workzones WHERE session_id = ? AND slot_id = ?",
                (str(session_id), slot),
            ).fetchone()
            if existing is None:
                return self._workzone_set_from_connection(connection, str(session_id))
            before = {
                "path": str(existing["path"]),
                "enabled": bool(existing["enabled"]),
                "label": str(existing["label"] or ""),
            }
            connection.execute(
                "DELETE FROM session_workzones WHERE session_id = ? AND slot_id = ?",
                (str(session_id), slot),
            )
            if slot == "main":
                connection.execute(
                    """
                    UPDATE sessions
                    SET workzone = NULL, workzone_revision = workzone_revision + 1,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, str(session_id)),
                )
            else:
                connection.execute(
                    """
                    UPDATE sessions
                    SET workzone_revision = workzone_revision + 1,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, str(session_id)),
                )
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind="session.workzone_slot_deleted",
                status="active",
                phase="control",
                summary=f"Session Workzone slot {slot} deleted",
                detail={"slot": slot, "source": str(source), "before": before},
            )
            return self._workzone_set_from_connection(connection, str(session_id))

    def disable_all_workzones(
        self,
        session_id: str,
        *,
        expected_revision: int | None = None,
        source: str = "telegram",
    ) -> dict[str, Any]:
        """Disable every configured slot in one revisioned transaction."""

        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                """
                SELECT * FROM sessions
                WHERE session_id = ? AND instance_id = ? AND status = 'active'
                """,
                (str(session_id), self.instance_id),
            ).fetchone()
            if session is None:
                raise SessionNotFound(str(session_id))
            self._check_workzone_revision(session, expected_revision)
            changed = connection.execute(
                """
                UPDATE session_workzones SET enabled = 0, updated_at = ?
                WHERE session_id = ? AND enabled != 0
                """,
                (now, str(session_id)),
            )
            if changed.rowcount:
                connection.execute(
                    """
                    UPDATE sessions
                    SET workzone = NULL, workzone_revision = workzone_revision + 1,
                        revision = revision + 1, updated_at = ?
                    WHERE session_id = ?
                    """,
                    (now, str(session_id)),
                )
                self._append_event(
                    connection,
                    session_id=str(session_id),
                    run_id=None,
                    kind="session.workzones_disabled",
                    status="active",
                    phase="control",
                    summary="All Session Workzones disabled",
                    detail={"source": str(source), "changed": int(changed.rowcount)},
                )
            return self._workzone_set_from_connection(connection, str(session_id))

    def record_workzone_reload(
        self, session_id: str, *, slots: Iterable[str], source: str = "telegram"
    ) -> None:
        """Audit a revalidation/rebind that deliberately leaves state unchanged."""

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT status FROM sessions WHERE session_id = ? AND instance_id = ?",
                (str(session_id), self.instance_id),
            ).fetchone()
            if session is None or str(session["status"]) != "active":
                raise SessionNotFound(str(session_id))
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind="session.workzones_reloaded",
                status="active",
                phase="control",
                summary="Session Workzones reloaded",
                detail={
                    "slots": [self._require_workzone_slot(slot) for slot in slots],
                    "source": str(source),
                },
            )

    def set_workzone(self, session_id: str, workzone: str | None) -> dict[str, Any]:
        """Compatibility wrapper for the former scalar ``sessions.workzone`` API."""

        if workzone:
            self.set_workzone_slot(
                session_id,
                "main",
                path=str(workzone),
                enabled=True,
                source="legacy_set_workzone",
            )
            return self.get_session(session_id)
        state = self.get_workzone_set(session_id)
        if any(item["slot_id"] == "main" for item in state["slots"]):
            self.set_workzone_slot(
                session_id,
                "main",
                enabled=False,
                source="legacy_set_workzone",
            )
            return self.get_session(session_id)
        now = _utc_now()
        with self._lock, self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE sessions SET workzone = NULL, revision = revision + 1, updated_at = ?
                WHERE session_id = ? AND status = 'active'
                """,
                (now, str(session_id)),
            )
            if updated.rowcount != 1:
                raise SessionNotFound(str(session_id))
        return self.get_session(session_id)

    def archive_session(
        self, session_id: str, *, deleted: bool = False
    ) -> dict[str, Any]:
        now = _utc_now()
        target = "deleted" if deleted else "archived"
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            if row is None:
                raise SessionNotFound(str(session_id))
            if bool(row["is_default"]):
                raise SessionConflict(
                    "the permanent default Session cannot be archived"
                )
            connection.execute(
                """
                UPDATE sessions SET status = ?, deleted_at = ?, revision = revision + 1,
                    updated_at = ? WHERE session_id = ?
                """,
                (target, now if deleted else None, now, str(session_id)),
            )
            connection.execute(
                "DELETE FROM channel_bindings WHERE session_id = ?", (str(session_id),)
            )
            self._append_event(
                connection,
                session_id=str(session_id),
                run_id=None,
                kind=f"session.{target}",
                status=target,
                phase="control",
                summary=f"Session {target}",
                detail={"records_deleted": False},
                outbox=True,
            )
            updated = connection.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (str(session_id),)
            ).fetchone()
            return self._session_dict(updated)

    def backend_binding(
        self,
        *,
        agent_id: str,
        session_id: str,
        context_generation: int,
        backend_id: str,
    ) -> str | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT backend_thread_id FROM backend_bindings
                WHERE agent_id = ? AND session_id = ? AND context_generation = ?
                  AND backend_id = ?
                """,
                (
                    str(agent_id).lower(),
                    str(session_id),
                    int(context_generation),
                    str(backend_id),
                ),
            ).fetchone()
        return str(row["backend_thread_id"]) if row is not None else None

    def save_backend_binding(
        self,
        *,
        agent_id: str,
        session_id: str,
        context_generation: int,
        backend_id: str,
        backend_thread_id: str | None,
    ) -> None:
        with self._lock, self._connection() as connection:
            if not backend_thread_id:
                connection.execute(
                    """
                    DELETE FROM backend_bindings WHERE agent_id = ? AND session_id = ?
                      AND context_generation = ? AND backend_id = ?
                    """,
                    (
                        str(agent_id).lower(),
                        str(session_id),
                        int(context_generation),
                        str(backend_id),
                    ),
                )
                return
            connection.execute(
                """
                INSERT INTO backend_bindings(
                    agent_id, session_id, context_generation, backend_id,
                    backend_thread_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, session_id, context_generation, backend_id)
                DO UPDATE SET backend_thread_id = excluded.backend_thread_id,
                              updated_at = excluded.updated_at
                """,
                (
                    str(agent_id).lower(),
                    str(session_id),
                    int(context_generation),
                    str(backend_id),
                    str(backend_thread_id),
                    _utc_now(),
                ),
            )

    def events(
        self,
        session_id: str,
        *,
        owner_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 500,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.get_session(session_id, owner_id=owner_id)
        normalized_run_id = str(run_id or "").strip()
        with self._lock, self._connection() as connection:
            if normalized_run_id:
                rows = connection.execute(
                    """
                    SELECT * FROM run_events
                    WHERE session_id = ? AND run_id = ? AND sequence > ?
                    ORDER BY sequence ASC LIMIT ?
                    """,
                    (
                        str(session_id),
                        normalized_run_id,
                        max(0, int(after_sequence)),
                        max(1, min(int(limit), 2000)),
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM run_events WHERE session_id = ? AND sequence > ?
                    ORDER BY sequence ASC LIMIT ?
                    """,
                    (
                        str(session_id),
                        max(0, int(after_sequence)),
                        max(1, min(int(limit), 2000)),
                    ),
                ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = _json_object(item.pop("detail_json", "{}"))
            result.append(item)
        return result

    def frontend_event_reference(
        self,
        *,
        session_id: str,
        owner_id: str,
        event_id: str,
    ) -> dict[str, Any] | None:
        """Return the visible assistant message bound to one Session Event."""

        resolved_session_id = str(session_id or "").strip()
        resolved_owner_id = str(owner_id or "").strip()
        resolved_event_id = str(event_id or "").strip()
        self.get_session(resolved_session_id, owner_id=resolved_owner_id)
        if not resolved_event_id or len(resolved_event_id) > 512:
            return None
        with self._lock, self._connection() as connection:
            event = connection.execute(
                """
                SELECT e.detail_json,
                       fme.message_id AS presentation_message_id,
                       r.final_message_id
                FROM run_events AS e
                JOIN sessions AS s ON s.session_id=e.session_id
                LEFT JOIN frontend_message_events AS fme
                  ON fme.event_id=e.event_id AND fme.session_id=e.session_id
                LEFT JOIN runs AS r
                  ON r.run_id=e.run_id AND r.session_id=e.session_id
                WHERE e.event_id=? AND e.session_id=?
                  AND s.owner_id=? AND s.instance_id=?
                """,
                (
                    resolved_event_id,
                    resolved_session_id,
                    resolved_owner_id,
                    self.instance_id,
                ),
            ).fetchone()
            if event is None:
                return None
            detail = _json_object(event["detail_json"] or "{}")
            message_id = str(
                event["presentation_message_id"]
                or event["final_message_id"]
                or detail.get("message_id")
                or ""
            ).strip()
            if not message_id:
                return None
            message = connection.execute(
                """
                SELECT message_id, role, text, created_at,
                       context_generation, visibility
                FROM messages WHERE message_id=? AND session_id=?
                """,
                (message_id, resolved_session_id),
            ).fetchone()
        if (
            message is None
            or str(message["role"] or "").casefold() != "assistant"
            or str(message["visibility"] or "").casefold() != "visible"
        ):
            return None
        return {
            "event_id": resolved_event_id,
            "message_id": str(message["message_id"]),
            "role": str(message["role"]),
            "text": str(message["text"] or ""),
            "created_at": str(message["created_at"] or ""),
            "context_generation": int(message["context_generation"] or 1),
        }

    def record_frontend_delivery_receipt(
        self,
        *,
        session_id: str,
        owner_id: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist one endpoint's typed delivery outcome without leaking address data."""

        normalized = normalize_delivery_receipt(receipt)
        resolved_session_id = str(session_id)
        self.get_session(resolved_session_id, owner_id=owner_id)
        event_id = normalized["event_id"]
        endpoint_id = normalized["endpoint_id"]
        proof_json = _json(normalized["proof"]) if normalized["proof"] is not None else None
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                """
                SELECT e.event_id
                FROM run_events AS e
                JOIN sessions AS s ON s.session_id = e.session_id
                WHERE e.event_id = ? AND e.session_id = ?
                  AND s.owner_id = ? AND s.instance_id = ?
                """,
                (event_id, resolved_session_id, str(owner_id), self.instance_id),
            ).fetchone()
            if event is None:
                raise SessionNotFound("frontend event not found")
            existing = connection.execute(
                """
                SELECT * FROM connector_delivery_receipts
                WHERE event_id = ? AND endpoint_id = ?
                """,
                (event_id, endpoint_id),
            ).fetchone()
            if existing is not None:
                previous_status = str(existing["status"])
                previous_proof = existing["proof_json"]
                if previous_status == "delivered":
                    if normalized["status"] == "delivered" and previous_proof != proof_json:
                        raise SessionConflict(
                            "delivered receipt proof conflicts with the committed receipt"
                        )
                    result = dict(existing)
                    result["proof"] = (
                        _json_object(result.pop("proof_json"))
                        if result.get("proof_json")
                        else None
                    )
                    result["duplicate"] = normalized["status"] == "delivered"
                    result["ignored_regression"] = normalized["status"] != "delivered"
                    return result
                if (
                    previous_status == normalized["status"]
                    and previous_proof == proof_json
                ):
                    result = dict(existing)
                    result["proof"] = (
                        _json_object(result.pop("proof_json"))
                        if result.get("proof_json")
                        else None
                    )
                    result["duplicate"] = True
                    result["ignored_regression"] = False
                    return result
                connection.execute(
                    """
                    UPDATE connector_delivery_receipts
                    SET status = ?, proof_json = ?, attempt_count = attempt_count + 1,
                        updated_at = ?
                    WHERE event_id = ? AND endpoint_id = ?
                    """,
                    (
                        normalized["status"],
                        proof_json,
                        now,
                        event_id,
                        endpoint_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO connector_delivery_receipts(
                        event_id, session_id, endpoint_id, status,
                        proof_json, attempt_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        event_id,
                        resolved_session_id,
                        endpoint_id,
                        normalized["status"],
                        proof_json,
                        now,
                    ),
                )
            result = {
                "event_id": event_id,
                "session_id": resolved_session_id,
                "endpoint_id": endpoint_id,
                "status": normalized["status"],
                "proof": normalized["proof"],
                "attempt_count": (
                    int(existing["attempt_count"]) + 1 if existing is not None else 1
                ),
                "updated_at": now,
                "duplicate": False,
                "ignored_regression": False,
            }
        return result

    @staticmethod
    def _refresh_delivery_outbox_aggregate(
        connection: sqlite3.Connection, *, event_id: str
    ) -> None:
        """Project endpoint tasks into the legacy event-level rollback view."""

        rows = connection.execute(
            "SELECT state FROM connector_delivery_tasks WHERE event_id=?",
            (str(event_id),),
        ).fetchall()
        if not rows:
            return
        states = {str(row["state"]) for row in rows}
        error_code = None
        completed_at = None
        if "unknown" in states:
            aggregate = "unknown"
            error_code = "endpoint_outcome_unknown"
            completed_at = _utc_now()
        elif states & {"pending", "retry", "claimed"}:
            aggregate = "delegated"
        elif states & {"failed", "expired"}:
            aggregate = "failed"
            error_code = "endpoint_delivery_failed"
            completed_at = _utc_now()
        else:
            aggregate = "completed"
            completed_at = _utc_now()
        connection.execute(
            """
            UPDATE delivery_outbox
            SET state=?, completed_at=?, last_error_code=?
            WHERE event_id=?
            """,
            (aggregate, completed_at, error_code, str(event_id)),
        )

    def claim_delivery_outbox(
        self,
        *,
        session_id: str,
        owner_id: str,
        worker_id: str,
        event_id: str | None = None,
        connector_id: str | None = None,
        endpoint_id: str | None = None,
        limit: int = 20,
        lease_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Claim durable event deliveries with a fenced, expiring lease.

        Expired claims become ``unknown`` instead of being replayed: the prior
        worker may have caused an external side effect before it disappeared.
        A connector may retry only after it has independently proved that the
        previous attempt was not delivered.
        """

        resolved_session_id = str(session_id).strip()
        resolved_owner_id = str(owner_id).strip()
        resolved_worker_id = str(worker_id).strip()
        if not resolved_worker_id or len(resolved_worker_id) > 128:
            raise ValueError("delivery worker id is invalid")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("delivery claim limit must be between 1 and 100")
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("delivery lease must be between 1 and 3600 seconds")
        self.get_session(resolved_session_id, owner_id=resolved_owner_id)
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_text = (now + timedelta(seconds=int(lease_seconds))).isoformat()
        claimed: list[dict[str, Any]] = []
        task_filters = []
        task_params: list[Any] = [
            resolved_session_id,
            resolved_owner_id,
            self.instance_id,
        ]
        if event_id is not None:
            task_filters.append("t.event_id=?")
            task_params.append(str(event_id))
        if connector_id is not None:
            task_filters.append("t.connector_id=?")
            task_params.append(str(connector_id).strip().casefold())
        if endpoint_id is not None:
            task_filters.append("t.endpoint_id=?")
            task_params.append(str(endpoint_id).strip())
        task_where = "".join(f" AND {item}" for item in task_filters)
        task_params.append(int(limit))
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired_tasks = connection.execute(
                """
                SELECT task_id, event_id FROM connector_delivery_tasks
                WHERE session_id=? AND state='claimed'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
                """,
                (resolved_session_id, now_text),
            ).fetchall()
            for expired in expired_tasks:
                connection.execute(
                    """
                    UPDATE connector_delivery_tasks
                    SET state='unknown', completed_at=?,
                        last_error_code='lease_expired_outcome_unknown',
                        last_claim_token=lease_token, last_claim_status='unknown',
                        lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL
                    WHERE task_id=? AND state='claimed'
                    """,
                    (now_text, str(expired["task_id"])),
                )
                self._refresh_delivery_outbox_aggregate(
                    connection, event_id=str(expired["event_id"])
                )
            rows = connection.execute(
                f"""
                SELECT t.*, e.sequence, e.kind, e.status, e.phase, e.summary,
                       e.detail_json, e.created_at AS event_created_at,
                       r.delivery_route_json
                FROM connector_delivery_tasks AS t
                JOIN run_events AS e ON e.event_id=t.event_id
                JOIN sessions AS s ON s.session_id=t.session_id
                LEFT JOIN runs AS r ON r.run_id=t.run_id
                WHERE t.session_id=? AND s.owner_id=? AND s.instance_id=?
                  AND t.state IN ('pending','retry')
                  {task_where}
                ORDER BY t.created_at ASC, t.task_id ASC
                LIMIT ?
                """,
                task_params,
            ).fetchall()
            for row in rows:
                lease_token = _new_id("lease")
                updated = connection.execute(
                    """
                    UPDATE connector_delivery_tasks
                    SET state='claimed', lease_owner=?, lease_token=?,
                        lease_expires_at=?, attempt_count=attempt_count+1
                    WHERE task_id=? AND state IN ('pending','retry')
                    """,
                    (
                        resolved_worker_id,
                        lease_token,
                        expires_text,
                        str(row["task_id"]),
                    ),
                )
                if updated.rowcount != 1:
                    continue
                try:
                    content_modes = json.loads(row["content_modes_json"] or "[]")
                except (TypeError, ValueError):
                    content_modes = []
                claimed.append(
                    {
                        "outbox_id": str(row["task_id"]),
                        "session_id": resolved_session_id,
                        "run_id": row["run_id"],
                        "event_id": str(row["event_id"]),
                        "connector_id": str(row["connector_id"]),
                        "endpoint_id": str(row["endpoint_id"]),
                        "role": str(row["role"]),
                        "content_modes": (
                            list(content_modes)
                            if isinstance(content_modes, list)
                            else []
                        ),
                        "retry_class": str(row["retry_class"]),
                        "sequence": int(row["sequence"]),
                        "kind": str(row["kind"]),
                        "status": row["status"],
                        "phase": row["phase"],
                        "summary": str(row["summary"] or ""),
                        "detail": _json_object(row["detail_json"]),
                        "delivery_route": _json_object(
                            row["delivery_route_json"] or "{}"
                        ),
                        "attempt_count": int(row["attempt_count"]) + 1,
                        "lease_owner": resolved_worker_id,
                        "lease_token": lease_token,
                        "lease_expires_at": expires_text,
                    }
                )
        if claimed:
            return claimed
        event_filter = " AND o.event_id=?" if event_id is not None else ""
        query_params: list[Any] = [
            resolved_session_id,
            resolved_owner_id,
            self.instance_id,
        ]
        if event_id is not None:
            query_params.append(str(event_id))
        query_params.append(int(limit))
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE delivery_outbox
                SET state='unknown', completed_at=?,
                    last_error_code='lease_expired_outcome_unknown',
                    last_claim_token=lease_token, last_claim_status='unknown',
                    lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL
                WHERE session_id=? AND state='claimed'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at<=?
                """,
                (now_text, resolved_session_id, now_text),
            )
            rows = connection.execute(
                f"""
                SELECT o.outbox_id, o.session_id, o.run_id, o.event_id,
                       o.attempt_count, o.created_at,
                       e.sequence, e.kind, e.status, e.phase, e.summary,
                       e.detail_json, e.created_at AS event_created_at,
                       r.delivery_route_json
                FROM delivery_outbox AS o
                JOIN run_events AS e ON e.event_id=o.event_id
                JOIN sessions AS s ON s.session_id=o.session_id
                LEFT JOIN runs AS r ON r.run_id=o.run_id
                WHERE o.session_id=? AND s.owner_id=? AND s.instance_id=?
                  AND o.state IN ('pending','retry')
                  {event_filter}
                ORDER BY o.created_at ASC, o.outbox_id ASC
                LIMIT ?
                """,
                query_params,
            ).fetchall()
            for row in rows:
                lease_token = _new_id("lease")
                updated = connection.execute(
                    """
                    UPDATE delivery_outbox
                    SET state='claimed', lease_owner=?, lease_token=?,
                        lease_expires_at=?, attempt_count=attempt_count+1
                    WHERE outbox_id=? AND state IN ('pending','retry')
                    """,
                    (
                        resolved_worker_id,
                        lease_token,
                        expires_text,
                        str(row["outbox_id"]),
                    ),
                )
                if updated.rowcount != 1:
                    continue
                detail = _json_object(row["detail_json"])
                route = _json_object(row["delivery_route_json"] or "{}")
                claimed.append(
                    {
                        "outbox_id": str(row["outbox_id"]),
                        "session_id": resolved_session_id,
                        "run_id": row["run_id"],
                        "event_id": str(row["event_id"]),
                        "sequence": int(row["sequence"]),
                        "kind": str(row["kind"]),
                        "status": row["status"],
                        "phase": row["phase"],
                        "summary": str(row["summary"] or ""),
                        "detail": detail,
                        "delivery_route": route,
                        "attempt_count": int(row["attempt_count"]) + 1,
                        "lease_owner": resolved_worker_id,
                        "lease_token": lease_token,
                        "lease_expires_at": expires_text,
                    }
                )
        return claimed

    def claim_run_delivery_outbox(
        self,
        *,
        request_id: str,
        owner_id: str,
        surface: str,
        channel_key: str,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        """Claim exactly one terminal Run event for its frozen legacy route.

        ``None`` means this is not a canonical Run and callers may use their
        existing compatibility sender. Once a Run has a frozen route, every
        other result is authoritative: a mismatched destination or a prior
        claim must not fall back to an untracked second send.
        """

        try:
            run = self.get_run_by_request(str(request_id), owner_id=str(owner_id))
        except SessionNotFound:
            return None
        route_raw = run.get("delivery_route")
        if not route_raw:
            return None
        from orchestrator.frontend_delivery import normalize_run_delivery_route

        try:
            route = normalize_run_delivery_route(route_raw)
        except ValueError as exc:
            raise SessionConflict("Run delivery route is invalid") from exc
        resolved_surface = str(surface or "").strip().casefold()
        resolved_channel = str(channel_key or "").strip()
        destinations = [route.get("primary"), *route.get("mirrors", [])]
        if not any(
            isinstance(item, Mapping)
            and item.get("surface") == resolved_surface
            and item.get("channel_key") == resolved_channel
            for item in destinations
        ):
            return {
                "managed": True,
                "state": "suppressed",
                "reason": "destination_not_in_frozen_route",
                "request_id": str(request_id),
                "session_id": str(run["session_id"]),
            }
        run_state = str(run.get("state") or "")
        if run_state not in TERMINAL_RUN_STATES:
            return {
                "managed": True,
                "state": "not_ready",
                "request_id": str(request_id),
                "session_id": str(run["session_id"]),
            }
        terminal_kind = "run.completed" if run_state == "completed" else f"run.{run_state}"
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT e.event_id, o.state AS outbox_state
                FROM run_events AS e
                JOIN delivery_outbox AS o ON o.event_id=e.event_id
                WHERE e.run_id=? AND e.kind=? AND e.session_id=?
                ORDER BY e.sequence DESC LIMIT 1
                """,
                (str(run["run_id"]), terminal_kind, str(run["session_id"])),
            ).fetchone()
        if row is None:
            return {
                "managed": True,
                "state": "missing_outbox",
                "request_id": str(request_id),
                "session_id": str(run["session_id"]),
            }
        event_id = str(row["event_id"])
        from orchestrator.frontend_connector_registry import (
            canonical_connector_id,
            endpoint_id_for,
        )
        from orchestrator.frontend_contracts import normalize_delivery_intent

        delivery_destinations = []
        selected_destination: dict[str, Any] | None = None
        for raw_destination in destinations:
            if not isinstance(raw_destination, Mapping):
                continue
            destination_surface = str(raw_destination.get("surface") or "")
            destination_channel = str(raw_destination.get("channel_key") or "")
            connector_id = canonical_connector_id(
                destination_surface,
                ingress_transport=destination_surface,
                surface=destination_surface,
            )
            delivery_destination = {
                "connector_id": connector_id,
                "endpoint_id": endpoint_id_for(
                    connector_id,
                    ingress_transport=destination_surface,
                    channel_key=destination_channel,
                ),
                "channel_key": destination_channel,
                "role": (
                    "primary"
                    if route.get("primary") == raw_destination
                    else "mirror"
                ),
                "content_modes": ["text"],
                "retry_class": "query_before_retry",
            }
            delivery_destinations.append(delivery_destination)
            if (
                destination_surface == resolved_surface
                and destination_channel == resolved_channel
            ):
                selected_destination = delivery_destination
        digest_material = "\n".join(
            [str(request_id), event_id]
            + sorted(item["endpoint_id"] for item in delivery_destinations)
        )
        delivery_intent = normalize_delivery_intent(
            {
                "type": "hashi.delivery-intent",
                "version": 2,
                "scope": "run",
                "event_id": event_id,
                "session_id": str(run["session_id"]),
                "idempotency_digest": "sha256:"
                + hashlib.sha256(digest_material.encode("utf-8")).hexdigest(),
                "destinations": delivery_destinations,
            }
        )
        claims = self.claim_delivery_outbox(
            session_id=str(run["session_id"]),
            owner_id=str(owner_id),
            worker_id=worker_id,
            event_id=event_id,
            connector_id=(
                str(selected_destination["connector_id"])
                if selected_destination is not None
                else None
            ),
            endpoint_id=(
                str(selected_destination["endpoint_id"])
                if selected_destination is not None
                else None
            ),
            limit=1,
            lease_seconds=lease_seconds,
        )
        if claims:
            return {
                "managed": True,
                "state": "claimed",
                "request_id": str(request_id),
                "session_id": str(run["session_id"]),
                "run_id": str(run["run_id"]),
                "delivery_intent": delivery_intent,
                "claim": claims[0],
            }
        with self._lock, self._connection() as connection:
            current = connection.execute(
                """
                SELECT state FROM connector_delivery_tasks
                WHERE event_id=? AND endpoint_id=?
                """,
                (
                    event_id,
                    str(selected_destination["endpoint_id"])
                    if selected_destination is not None
                    else "",
                ),
            ).fetchone()
            if current is None:
                current = connection.execute(
                    "SELECT state FROM delivery_outbox WHERE event_id=?",
                    (event_id,),
                ).fetchone()
        return {
            "managed": True,
            "state": str(current["state"]) if current is not None else "missing_outbox",
            "request_id": str(request_id),
            "session_id": str(run["session_id"]),
            "run_id": str(run["run_id"]),
            "event_id": event_id,
        }

    def complete_delivery_outbox(
        self,
        *,
        outbox_id: str,
        lease_token: str,
        status: str,
        error_code: str | None = None,
        retry_safe: bool = False,
    ) -> dict[str, Any]:
        """Complete a claimed dispatch, requiring proof of safety to retry."""

        resolved_status = str(status or "").strip().casefold()
        allowed_statuses = {
            "completed",
            "failed",
            "unknown",
            "suppressed",
            "expired",
            "retry",
        }
        if resolved_status not in allowed_statuses:
            raise ValueError("delivery outbox status is invalid")
        if resolved_status == "retry" and retry_safe is not True:
            raise ValueError("delivery retry requires proof that retry is safe")
        token = str(lease_token or "").strip()
        if not token:
            raise ValueError("delivery lease token is required")
        safe_error_code = str(error_code or "").strip()
        if len(safe_error_code) > 128 or any(
            not (char.isalnum() or char in "._-") for char in safe_error_code
        ):
            raise ValueError("delivery error code is invalid")
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM connector_delivery_tasks WHERE task_id=?",
                (str(outbox_id),),
            ).fetchone()
            if task is not None:
                if (
                    str(task["state"]) != "claimed"
                    or str(task["lease_token"] or "") != token
                ):
                    if (
                        str(task["last_claim_token"] or "") == token
                        and str(task["last_claim_status"] or "")
                        == resolved_status
                    ):
                        return {
                            "outbox_id": str(outbox_id),
                            "state": resolved_status,
                            "attempt_count": int(task["attempt_count"]),
                            "connector_id": str(task["connector_id"]),
                            "endpoint_id": str(task["endpoint_id"]),
                            "duplicate": True,
                        }
                    raise SessionConflict("delivery outbox lease is stale")
                terminal = resolved_status != "retry"
                connection.execute(
                    """
                    UPDATE connector_delivery_tasks
                    SET state=?, delivered_at=?, completed_at=?,
                        last_error_code=?, last_claim_token=lease_token,
                        last_claim_status=?, lease_owner=NULL,
                        lease_token=NULL, lease_expires_at=NULL
                    WHERE task_id=? AND state='claimed' AND lease_token=?
                    """,
                    (
                        resolved_status,
                        now if resolved_status == "completed" else None,
                        now if terminal else None,
                        safe_error_code or None,
                        resolved_status,
                        str(outbox_id),
                        token,
                    ),
                )
                self._refresh_delivery_outbox_aggregate(
                    connection, event_id=str(task["event_id"])
                )
                return {
                    "outbox_id": str(outbox_id),
                    "state": resolved_status,
                    "attempt_count": int(task["attempt_count"]),
                    "connector_id": str(task["connector_id"]),
                    "endpoint_id": str(task["endpoint_id"]),
                    "duplicate": False,
                }
            row = connection.execute(
                "SELECT * FROM delivery_outbox WHERE outbox_id=?",
                (str(outbox_id),),
            ).fetchone()
            if row is None:
                raise SessionNotFound(str(outbox_id))
            if (
                str(row["state"]) != "claimed"
                or str(row["lease_token"] or "") != token
            ):
                if (
                    str(row["last_claim_token"] or "") == token
                    and str(row["last_claim_status"] or "") == resolved_status
                ):
                    return {
                        "outbox_id": str(outbox_id),
                        "state": resolved_status,
                        "attempt_count": int(row["attempt_count"]),
                        "duplicate": True,
                    }
                raise SessionConflict("delivery outbox lease is stale")
            terminal = resolved_status != "retry"
            connection.execute(
                """
                UPDATE delivery_outbox
                SET state=?, completed_at=?, last_error_code=?,
                    last_claim_token=lease_token, last_claim_status=?,
                    lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL
                WHERE outbox_id=? AND state='claimed' AND lease_token=?
                """,
                (
                    resolved_status,
                    now if terminal else None,
                    safe_error_code or None,
                    resolved_status,
                    str(outbox_id),
                    token,
                ),
            )
            return {
                "outbox_id": str(outbox_id),
                "state": resolved_status,
                "attempt_count": int(row["attempt_count"]),
                "duplicate": False,
            }

    def reconcile_unknown_delivery(
        self,
        *,
        outbox_id: str,
        resolution: str,
        proof: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve an unknown endpoint only from connector query evidence."""

        resolved = str(resolution or "").strip().casefold()
        if resolved not in {"delivered", "not_delivered"}:
            raise ValueError("unknown delivery resolution is invalid")
        proof_type = str(proof.get("type") or "").strip()
        proof_value = str(proof.get("value") or "").strip()
        if (
            not proof_type
            or not proof_value
            or len(proof_type) > 64
            or len(proof_value) > 256
            or any(ord(char) < 33 or ord(char) > 126 for char in proof_type)
            or any(ord(char) < 33 or ord(char) > 126 for char in proof_value)
        ):
            raise ValueError("unknown delivery reconciliation proof is invalid")
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = connection.execute(
                "SELECT * FROM connector_delivery_tasks WHERE task_id=?",
                (str(outbox_id),),
            ).fetchone()
            if task is None:
                raise SessionNotFound(str(outbox_id))
            state = str(task["state"])
            target_state = "completed" if resolved == "delivered" else "retry"
            if state == target_state:
                return {
                    "outbox_id": str(outbox_id),
                    "state": state,
                    "resolution": resolved,
                    "duplicate": True,
                }
            if state != "unknown":
                raise SessionConflict("delivery task is not awaiting reconciliation")
            connection.execute(
                """
                UPDATE connector_delivery_tasks
                SET state=?, delivered_at=?, completed_at=?,
                    last_error_code=?, last_claim_status=?,
                    lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL
                WHERE task_id=? AND state='unknown'
                """,
                (
                    target_state,
                    now if resolved == "delivered" else None,
                    now if resolved == "delivered" else None,
                    (
                        "reconciled_not_delivered"
                        if resolved == "not_delivered"
                        else None
                    ),
                    f"reconciled_{resolved}",
                    str(outbox_id),
                ),
            )
            if resolved == "delivered":
                proof_json = _json({"type": proof_type, "value": proof_value})
                connection.execute(
                    """
                    INSERT INTO connector_delivery_receipts(
                        event_id, session_id, endpoint_id, status,
                        proof_json, attempt_count, updated_at
                    ) VALUES (?, ?, ?, 'delivered', ?, 1, ?)
                    ON CONFLICT(event_id, endpoint_id) DO UPDATE SET
                        status='delivered', proof_json=excluded.proof_json,
                        attempt_count=connector_delivery_receipts.attempt_count+1,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(task["event_id"]),
                        str(task["session_id"]),
                        str(task["endpoint_id"]),
                        proof_json,
                        now,
                    ),
                )
            self._refresh_delivery_outbox_aggregate(
                connection, event_id=str(task["event_id"])
            )
            return {
                "outbox_id": str(outbox_id),
                "state": target_state,
                "resolution": resolved,
                "duplicate": False,
            }

    def frontend_delivery_receipts(
        self,
        *,
        session_id: str,
        owner_id: str,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.get_session(str(session_id), owner_id=owner_id)
        query = """
            SELECT r.*
            FROM connector_delivery_receipts AS r
            JOIN run_events AS e ON e.event_id = r.event_id
            WHERE r.session_id = ?
        """
        parameters: list[Any] = [str(session_id)]
        if event_id is not None:
            query += " AND r.event_id = ?"
            parameters.append(str(event_id))
        query += " ORDER BY r.updated_at, r.endpoint_id"
        with self._lock, self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["proof"] = (
                _json_object(item.pop("proof_json"))
                if item.get("proof_json")
                else None
            )
            result.append(item)
        return result

    def record_frontend_transport_reference(
        self,
        *,
        session_id: str,
        owner_id: str,
        connector_id: str,
        endpoint_id: str,
        transport_message_id: str,
        event_id: str,
    ) -> dict[str, Any]:
        """Bind one delivered transport message to its canonical assistant Event."""

        values = {
            "connector_id": str(connector_id or "").strip().casefold(),
            "endpoint_id": str(endpoint_id or "").strip(),
            "transport_message_id": str(transport_message_id or "").strip(),
            "event_id": str(event_id or "").strip(),
        }
        if any(not value or len(value) > 512 for value in values.values()):
            raise ValueError("frontend transport reference identity is invalid")
        resolved_session_id = str(session_id or "").strip()
        resolved_owner_id = str(owner_id or "").strip()
        self.get_session(resolved_session_id, owner_id=resolved_owner_id)

        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT event_id FROM frontend_transport_references
                WHERE connector_id=? AND endpoint_id=? AND transport_message_id=?
                """,
                (
                    values["connector_id"],
                    values["endpoint_id"],
                    values["transport_message_id"],
                ),
            ).fetchone()
            if existing is not None:
                if str(existing["event_id"]) != values["event_id"]:
                    raise SessionConflict(
                        "transport message reference is already bound to another Event"
                    )
                return {
                    "event_id": values["event_id"],
                    "duplicate": True,
                }

            event = connection.execute(
                """
                SELECT e.event_id, e.detail_json,
                       fme.message_id AS presentation_message_id,
                       r.final_message_id
                FROM run_events AS e
                JOIN sessions AS s ON s.session_id=e.session_id
                LEFT JOIN frontend_message_events AS fme
                  ON fme.event_id=e.event_id AND fme.session_id=e.session_id
                LEFT JOIN runs AS r
                  ON r.run_id=e.run_id AND r.session_id=e.session_id
                WHERE e.event_id=? AND e.session_id=?
                  AND s.owner_id=? AND s.instance_id=?
                """,
                (
                    values["event_id"],
                    resolved_session_id,
                    resolved_owner_id,
                    self.instance_id,
                ),
            ).fetchone()
            if event is None:
                raise SessionNotFound("frontend Event not found")
            detail = _json_object(event["detail_json"] or "{}")
            message_id = str(
                event["presentation_message_id"]
                or event["final_message_id"]
                or detail.get("message_id")
                or ""
            ).strip()
            if not message_id:
                raise SessionConflict(
                    "frontend Event is not bound to a canonical message"
                )
            message = connection.execute(
                """
                SELECT role, visibility FROM messages
                WHERE message_id=? AND session_id=?
                """,
                (message_id, resolved_session_id),
            ).fetchone()
            if (
                message is None
                or str(message["role"] or "").casefold() != "assistant"
                or str(message["visibility"] or "").casefold() != "visible"
            ):
                raise SessionConflict(
                    "frontend Event is not a visible assistant message"
                )
            connection.execute(
                """
                INSERT INTO frontend_transport_references(
                    connector_id, endpoint_id, transport_message_id,
                    session_id, owner_id, event_id, message_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["connector_id"],
                    values["endpoint_id"],
                    values["transport_message_id"],
                    resolved_session_id,
                    resolved_owner_id,
                    values["event_id"],
                    message_id,
                    _utc_now(),
                ),
            )
        return {
            "event_id": values["event_id"],
            "message_id": message_id,
            "duplicate": False,
        }

    def resolve_frontend_transport_reference(
        self,
        *,
        session_id: str,
        owner_id: str,
        connector_id: str,
        endpoint_id: str,
        transport_message_id: str,
    ) -> dict[str, Any] | None:
        """Resolve a quoted external message only in its owner and endpoint scope."""

        resolved_session_id = str(session_id or "").strip()
        resolved_owner_id = str(owner_id or "").strip()
        self.get_session(resolved_session_id, owner_id=resolved_owner_id)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT ref.event_id, ref.message_id, message.role,
                       message.text, message.created_at, message.visibility
                FROM frontend_transport_references AS ref
                JOIN messages AS message
                  ON message.message_id=ref.message_id
                 AND message.session_id=ref.session_id
                WHERE ref.session_id=? AND ref.owner_id=?
                  AND ref.connector_id=? AND ref.endpoint_id=?
                  AND ref.transport_message_id=?
                """,
                (
                    resolved_session_id,
                    resolved_owner_id,
                    str(connector_id or "").strip().casefold(),
                    str(endpoint_id or "").strip(),
                    str(transport_message_id or "").strip(),
                ),
            ).fetchone()
        if (
            row is None
            or str(row["role"] or "").casefold() != "assistant"
            or str(row["visibility"] or "").casefold() != "visible"
        ):
            return None
        return {
            "event_id": str(row["event_id"]),
            "message_id": str(row["message_id"]),
            "role": str(row["role"]),
            "text": str(row["text"] or ""),
            "created_at": str(row["created_at"] or ""),
        }

    def create_event_consumer(
        self,
        *,
        session_id: str,
        owner_id: str,
        consumer_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_session(session_id, owner_id=owner_id)
        resolved_id = str(consumer_id or _new_id("consumer"))
        now = _utc_now()
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id = ?",
                (resolved_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["session_id"]) != str(session_id) or str(
                    existing["owner_id"]
                ) != str(owner_id):
                    raise SessionConflict("event consumer belongs to another Session")
                return dict(existing)
            connection.execute(
                """
                INSERT INTO event_consumers(
                    consumer_id, session_id, owner_id,
                    acknowledged_sequence, issued_through_sequence, updated_at
                ) VALUES (?, ?, ?, 0, 0, ?)
                """,
                (resolved_id, str(session_id), str(owner_id), now),
            )
            row = connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id = ?",
                (resolved_id,),
            ).fetchone()
        return dict(row)

    def poll_event_consumer(
        self,
        *,
        session_id: str,
        owner_id: str,
        consumer_id: str,
        limit: int = 500,
    ) -> dict[str, Any]:
        self.get_session(session_id, owner_id=owner_id)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            consumer = connection.execute(
                """
                SELECT * FROM event_consumers
                WHERE consumer_id = ? AND session_id = ? AND owner_id = ?
                """,
                (str(consumer_id), str(session_id), str(owner_id)),
            ).fetchone()
            if consumer is None:
                raise SessionNotFound("event consumer not found")
            acknowledged = int(consumer["acknowledged_sequence"])
            rows = connection.execute(
                """
                SELECT * FROM run_events
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC LIMIT ?
                """,
                (
                    str(session_id),
                    acknowledged,
                    max(1, min(int(limit), 2000)),
                ),
            ).fetchall()
            issued = max(
                int(consumer["issued_through_sequence"]),
                int(rows[-1]["sequence"]) if rows else acknowledged,
            )
            connection.execute(
                """
                UPDATE event_consumers
                SET issued_through_sequence = ?, updated_at = ?
                WHERE consumer_id = ?
                """,
                (issued, _utc_now(), str(consumer_id)),
            )
            bounds = connection.execute(
                """
                SELECT COALESCE(MIN(sequence), 0) AS earliest,
                       COALESCE(MAX(sequence), 0) AS latest
                FROM run_events WHERE session_id = ?
                """,
                (str(session_id),),
            ).fetchone()
        events = []
        for row in rows:
            item = dict(row)
            item["detail"] = _json_object(item.pop("detail_json", "{}"))
            events.append(item)
        return {
            "consumer_id": str(consumer_id),
            "acknowledged_sequence": acknowledged,
            "issued_through_sequence": issued,
            "earliest_available_sequence": int(bounds["earliest"]),
            "latest_sequence": int(bounds["latest"]),
            "events": events,
        }

    def acknowledge_event_consumer(
        self,
        *,
        session_id: str,
        owner_id: str,
        consumer_id: str,
        sequence: int,
    ) -> dict[str, Any]:
        requested = max(0, int(sequence))
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM event_consumers
                WHERE consumer_id = ? AND session_id = ? AND owner_id = ?
                """,
                (str(consumer_id), str(session_id), str(owner_id)),
            ).fetchone()
            if row is None:
                raise SessionNotFound("event consumer not found")
            acknowledged = int(row["acknowledged_sequence"])
            issued = int(row["issued_through_sequence"])
            if requested > issued:
                raise SessionConflict(
                    "ACK cannot advance beyond events issued to this consumer"
                )
            resolved = max(acknowledged, requested)
            connection.execute(
                """
                UPDATE event_consumers
                SET acknowledged_sequence = ?, updated_at = ?
                WHERE consumer_id = ?
                """,
                (resolved, _utc_now(), str(consumer_id)),
            )
            updated = connection.execute(
                "SELECT * FROM event_consumers WHERE consumer_id = ?",
                (str(consumer_id),),
            ).fetchone()
        return dict(updated)

    def snapshot(
        self, session_id: str, *, owner_id: str | None = None
    ) -> dict[str, Any]:
        session = self.get_session(session_id, owner_id=owner_id)
        with self._lock, self._connection() as connection:
            projections = connection.execute(
                """
                SELECT projection_json FROM run_projection_records
                WHERE session_id = ? ORDER BY updated_at, run_id
                """,
                (str(session_id),),
            ).fetchall()
            bounds = connection.execute(
                """
                SELECT COALESCE(MIN(sequence), 0) AS earliest,
                       COALESCE(MAX(sequence), 0) AS latest
                FROM run_events WHERE session_id = ?
                """,
                (str(session_id),),
            ).fetchone()
        return {
            "session": session,
            "workzones": self.get_workzone_set(session_id),
            "messages": self.messages(session_id, owner_id=owner_id, limit=1000),
            "runs": [_json_object(row["projection_json"]) for row in projections],
            "earliest_available_sequence": int(
                bounds["earliest"] if bounds is not None else 0
            ),
            "latest_sequence": int(bounds["latest"] if bounds is not None else 0),
            "snapshot_revision": int(session["revision"]),
        }

    def promotion_candidates(
        self,
        *,
        agent_id: str,
        session_ids: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = [
            "r.agent_id = ?",
            "r.state = 'completed'",
            "p.run_id IS NULL",
            "s.session_kind = 'conversation'",
        ]
        params: list[Any] = [str(agent_id).lower()]
        selected = [str(item) for item in (session_ids or ()) if str(item)]
        if selected:
            clauses.append(f"r.session_id IN ({','.join('?' for _ in selected)})")
            params.extend(selected)
        params.append(max(1, min(int(limit), 5000)))
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT r.run_id, r.session_id, r.user_message_id,
                       r.final_message_id AS assistant_message_id,
                       u.ordinal AS user_ordinal, a.ordinal AS assistant_ordinal,
                       u.created_at AS user_ts, a.created_at AS assistant_ts,
                       u.source, u.text AS user_text, a.text AS assistant_text
                FROM runs AS r
                JOIN sessions AS s ON s.session_id = r.session_id
                JOIN messages AS u ON u.message_id = r.user_message_id
                JOIN messages AS a ON a.message_id = r.final_message_id
                LEFT JOIN agent_memory_records AS p ON p.run_id = r.run_id
                WHERE {" AND ".join(clauses)}
                ORDER BY a.created_at, a.ordinal, r.run_id LIMIT ?
                """,
                params,
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["memory_origin_ref"] = (
                f"session:{item['session_id']}:run:{item['run_id']}"
            )
            result.append(item)
        return result

    def record_promoted(self, *, agent_id: str, candidate: Mapping[str, Any]) -> bool:
        now = _utc_now()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO agent_memory_records(
                    promotion_record_id, agent_id, session_id, run_id,
                    user_message_id, assistant_message_id, memory_origin_ref, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_id("pmr"),
                    str(agent_id).lower(),
                    str(candidate["session_id"]),
                    str(candidate["run_id"]),
                    str(candidate["user_message_id"]),
                    str(candidate["assistant_message_id"]),
                    str(candidate["memory_origin_ref"]),
                    now,
                ),
            )
            if inserted.rowcount != 1:
                return False
            ordinal = int(candidate.get("assistant_ordinal") or 0)
            connection.execute(
                """
                INSERT INTO memory_promotion_watermarks(
                    agent_id, session_id, promoted_through_ordinal, promoted_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id, session_id) DO UPDATE SET
                    promoted_through_ordinal = MAX(
                        promoted_through_ordinal, excluded.promoted_through_ordinal
                    ),
                    promoted_at = excluded.promoted_at
                """,
                (str(agent_id).lower(), str(candidate["session_id"]), ordinal, now),
            )
            return True

    def promotion_status(self, *, agent_id: str) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            schedule = connection.execute(
                "SELECT * FROM memory_promotion_schedules WHERE agent_id = ?",
                (str(agent_id).lower(),),
            ).fetchone()
            promoted = connection.execute(
                "SELECT COUNT(*) AS value FROM agent_memory_records WHERE agent_id = ?",
                (str(agent_id).lower(),),
            ).fetchone()
        return {
            "schedule": dict(schedule)
            if schedule is not None
            else {
                "agent_id": str(agent_id).lower(),
                "enabled": 1,
                "local_time": "00:00",
                "timezone": "local",
                "last_local_date": None,
            },
            "promoted_count": int(promoted["value"] if promoted is not None else 0),
            "pending_count": len(
                self.promotion_candidates(agent_id=agent_id, limit=5000)
            ),
        }

    def set_promotion_schedule(
        self,
        *,
        agent_id: str,
        enabled: bool | None = None,
        local_time: str | None = None,
        timezone_name: str | None = None,
    ) -> dict[str, Any]:
        current = self.promotion_status(agent_id=agent_id)["schedule"]
        resolved_enabled = (
            bool(current.get("enabled", 1)) if enabled is None else bool(enabled)
        )
        resolved_time = str(local_time or current.get("local_time") or "00:00")
        parts = resolved_time.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("promotion time must be HH:MM")
        hour, minute = (int(parts[0]), int(parts[1]))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("promotion time must be HH:MM")
        resolved_time = f"{hour:02d}:{minute:02d}"
        resolved_timezone = str(timezone_name or current.get("timezone") or "local")
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO memory_promotion_schedules(
                    agent_id, enabled, local_time, timezone, last_local_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    local_time = excluded.local_time,
                    timezone = excluded.timezone,
                    updated_at = excluded.updated_at
                """,
                (
                    str(agent_id).lower(),
                    int(resolved_enabled),
                    resolved_time,
                    resolved_timezone,
                    current.get("last_local_date"),
                    _utc_now(),
                ),
            )
        return self.promotion_status(agent_id=agent_id)["schedule"]

    def mark_promotion_schedule_ran(self, *, agent_id: str, local_date: str) -> None:
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                UPDATE memory_promotion_schedules
                SET last_local_date = ?, updated_at = ? WHERE agent_id = ?
                """,
                (str(local_date), _utc_now(), str(agent_id).lower()),
            )

    def session_workspace(self, session_id: str, context_generation: int) -> Path:
        safe_session = "".join(
            character
            for character in str(session_id)
            if character.isalnum() or character in {"_", "-"}
        )
        if safe_session != str(session_id) or not safe_session:
            raise ValueError("invalid session_id")
        generation = max(1, int(context_generation))
        path = self.workspaces_root / safe_session / f"generation_{generation}"
        path.mkdir(parents=True, exist_ok=True)
        return path


__all__ = [
    "TERMINAL_RUN_STATES",
    "AcceptedRun",
    "IdempotencyConflict",
    "SessionConflict",
    "SessionNotFound",
    "SessionStore",
    "SessionStoreError",
    "StaleFencingToken",
]
