"""Stable, platform-neutral ``agent-move-v1`` archive format.

The archive contains durable Agent identity, memory and workspace state.  It
never contains active runtime sessions or plaintext credentials.  Receivers
own the conversion into their local ``agents.json`` schema; senders therefore
do not need to know the target filesystem layout or operating system.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from orchestrator.agent_incarnation import (
    AGENT_LIFECYCLE_FIELD,
    valid_agent_lifecycle_id,
)
from orchestrator.pcm_transfer import is_portable_memory_path
from orchestrator.telegram_delivery_state import (
    export_owned_state,
    record_owner,
    telegram_bot_fingerprint,
)
from orchestrator.pcm import (
    PCM_FILENAME,
    PCMValidationError,
    canonical_agent_md,
    load_pcm_document,
    parse_pcm_text,
)
from orchestrator.process_execution import is_wsl
from orchestrator.session_store import (
    SessionConflict,
    SessionStore,
    validate_conversation_continuity_capsule,
)
from orchestrator.telegram_identity import resolve_owner_scope_id, resolve_telegram_owner_id

PERSONAL_CLONE_HISTORY_NOT_INCLUDED_WARNING = (
    "The selected owner has no conversation history; other local "
    "conversation history remains on the source and was not included"
)

PACKAGE_TYPE = "hashi-agent-move"
PACKAGE_SCHEMA_MIN_VERSION = 1
PACKAGE_SCHEMA_VERSION = 5
TRANSFER_MODES_CAPABILITY = "agent_transfer_modes_v1"
CONVERSATION_CONTINUITY_CAPABILITY = "agent_conversation_continuity_v1"
RETAIN_SOURCE_HISTORY_CAPABILITY = "agent_move_retain_source_history_v1"
WORKSPACE_LIMIT_BYTES = 1_000_000_000
TRANSFER_MODES = {"identity_memory", "workspace"}
AGENT_MOVE_CAPABILITY = "agent_move_receive_v1"
RETAINED_IDENTITY_CAPABILITY = "agent_move_retained_identity_v1"
AGENT_TRANSFER_LIFECYCLE_CAPABILITY = "agent_transfer_lifecycle_v1"
INACTIVE_MOVE_CAPABILITY = "agent_move_inactive_target_v1"
INACTIVE_CLONE_BACKUP_CAPABILITY = "agent_clone_inactive_backup_v1"
PACKAGE_EXTENSION = ".hashi-agent"
RETAINED_IDENTITY_ARCHIVE_PATH = "retained-identity/AGENT.md"
RETAINED_IDENTITY_METADATA_PATH = "metadata/retained-identity.json"
AGENT_CAPABILITY_ARCHIVE_PATH = "identity/capability.json"
CONVERSATION_CONTINUITY_ARCHIVE_PATH = "continuity/conversations.json"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_WORKSPACE_BYTES = MAX_UNPACKED_BYTES - (16 * 1024 * 1024)
_CONTROL_MEMBER_LIMITS = {
    "manifest.json": 1024 * 1024,
    "identity/agent.json": 4 * 1024 * 1024,
    "identity/agent.md": 16 * 1024 * 1024,
    AGENT_CAPABILITY_ARCHIVE_PATH: 4 * 1024 * 1024,
    CONVERSATION_CONTINUITY_ARCHIVE_PATH: 128 * 1024 * 1024,
    RETAINED_IDENTITY_ARCHIVE_PATH: 16 * 1024 * 1024,
    RETAINED_IDENTITY_METADATA_PATH: 1024 * 1024,
    "access/requirements.json": 4 * 1024 * 1024,
    "schedules/tasks.json": 32 * 1024 * 1024,
    "metadata/workspace.json": 64 * 1024 * 1024,
    "secrets/agent.enc": 16 * 1024 * 1024,
    "checksums.json": 64 * 1024 * 1024,
}

MAX_TRANSFER_PACKAGE_BYTES = WORKSPACE_LIMIT_BYTES + sum(_CONTROL_MEMBER_LIMITS.values()) + MAX_ARCHIVE_MEMBERS * 4096

AGENT_ID_MAX_LENGTH = 128
_AGENT_ID_RE = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9_.-]{{0,{AGENT_ID_MAX_LENGTH - 1}}}$")
_SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
_SKIP_DIR_NAMES = {
    ".aws",
    ".azure",
    ".claw",
    ".docker",
    ".gnupg",
    ".git",
    ".hg",
    ".kube",
    ".password-store",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
    ".tox",
    ".tmp",
    ".venv",
    "__pycache__",
    "backend_state",
    "node_modules",
    "state",
    "tmp",
    "undelivered",
}
_SKIP_FILE_NAMES = {
    ".env",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".runtime_session.json",
    ".memory_plus.lock",
    "auth.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "oauth.json",
    "secrets.json",
    "service-account.json",
    "token.json",
    "workzone.json",
}
_SKIP_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".sqlite-shm",
    ".sqlite-wal",
}
_SECRET_FILE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
# Packaging and freshness are deliberately separate policies.  The append-only
# command audit remains portable evidence in the archive, but /move's own
# prepare/confirm audit append must not invalidate the staged source snapshot.
_FRESHNESS_EXCLUDED_WORKSPACE_PATHS = frozenset(
    {"slash_command_audit.jsonl"}
)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_WINDOWS_INVALID_CHARS = set('<>:"\\|?*')


class AgentMoveError(ValueError):
    """Raised when a move package or migration operation is invalid."""


class CloneImportPreflightError(AgentMoveError):
    """A definitive, pre-publication rejection, not an uncertain upload ACK."""

    code = "CLONE_IMPORT_PREFLIGHT_FAILED"


class AccessScopePreviewChanged(AgentMoveError):
    """The target rejected commit before publication because its policy changed."""

    code = "ACCESS_SCOPE_PREVIEW_CHANGED"


class _WorkspaceChangedError(AgentMoveError):
    def __init__(self, changes: list[dict[str, Any]]) -> None:
        self.workspace_changes = {
            "changed_count": len(changes),
            "changes": changes[:20],
        }
        super().__init__(
            "workspace changed while packaging; prepare a fresh transfer"
        )


@dataclass(frozen=True)
class WorkspaceEntry:
    source: Path
    relative_path: str
    mode: int
    size: int
    sqlite_snapshot: bool = False
    materialized_symlink: bool = False


@dataclass(frozen=True)
class AgentMoveArchive:
    package_path: Path
    manifest: dict[str, Any]
    agent_config: dict[str, Any]
    access_requirements: dict[str, Any]
    schedules: dict[str, Any]
    workspace_metadata: dict[str, Any]
    retained_identity: dict[str, Any] | None
    agent_capability: dict[str, Any] | None
    conversation_continuity: dict[str, Any] | None
    checksums: dict[str, str]
    names: tuple[str, ...]
    unpacked_bytes: int
    workspace_bytes: int

    @property
    def package_id(self) -> str:
        return str(self.manifest["package_id"])

    @property
    def agent_id(self) -> str:
        return str(self.manifest["agent_id"])


def _positive_archive_member_limit(value: Any, *, label: str = "archive member") -> int:
    if isinstance(value, bool):
        raise AgentMoveError(f"{label} limit must be a positive integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            result = int(value)
        except ValueError as exc:
            raise AgentMoveError(f"{label} limit must be a positive integer") from exc
    else:
        raise AgentMoveError(f"{label} limit must be a positive integer")
    if result <= 0:
        raise AgentMoveError(f"{label} limit must be a positive integer")
    return result


def configured_archive_member_limit() -> int:
    configured = os.environ.get("HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS")
    return _positive_archive_member_limit(
        MAX_ARCHIVE_MEMBERS if configured is None else configured.strip()
    )


def configured_transfer_package_limit() -> int:
    configured = os.environ.get("HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES")
    return _positive_archive_member_limit(
        MAX_TRANSFER_PACKAGE_BYTES if configured is None else configured.strip(),
        label="package byte",
    )


def configured_unpacked_byte_limit() -> int:
    configured = os.environ.get("HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES")
    return _positive_archive_member_limit(
        MAX_UNPACKED_BYTES if configured is None else configured.strip(),
        label="expanded package byte",
    )


def receiver_unpacked_byte_limit(capabilities: Mapping[str, Any]) -> int:
    return _positive_archive_member_limit(
        capabilities.get("max_unpacked_bytes", MAX_UNPACKED_BYTES),
        label="expanded package byte",
    )


def configured_workspace_byte_limit() -> int:
    configured = os.environ.get("HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES")
    return _positive_archive_member_limit(
        WORKSPACE_LIMIT_BYTES if configured is None else configured.strip(),
        label="workspace byte",
    )


def receiver_workspace_byte_limit(capabilities: Mapping[str, Any]) -> int:
    return _positive_archive_member_limit(
        capabilities.get("max_workspace_bytes", WORKSPACE_LIMIT_BYTES),
        label="workspace byte",
    )


class _UnpackedBudgetWriter:
    def __init__(self, stream: Any, archive: Any):
        self.stream = stream
        self.archive = archive

    def write(self, data: bytes) -> int:
        total = self.archive.unpacked_written + len(data)
        if total > self.archive.unpacked_limit:
            raise AgentMoveError(
                f"expanded Agent move package would exceed {total} bytes; "
                f"receiver limit is {self.archive.unpacked_limit} bytes"
            )
        written = self.stream.write(data)
        self.archive.unpacked_written += written
        return written

    def __enter__(self):
        return self

    def __exit__(self, *args: Any):
        return self.stream.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class _BudgetedZipFile(zipfile.ZipFile):
    def __init__(self, *args: Any, unpacked_limit: int | None = None, **kwargs: Any):
        self.unpacked_limit = unpacked_limit
        self.unpacked_written = 0
        super().__init__(*args, **kwargs)

    def open(self, name: Any, mode: str = "r", pwd: bytes | None = None,
             *, force_zip64: bool = False):
        stream = super().open(name, mode, pwd, force_zip64=force_zip64)
        if mode == "w" and self.unpacked_limit is not None:
            return _UnpackedBudgetWriter(stream, self)
        return stream


class _BoundedArchiveWriter:
    def __init__(self, stream: Any, limit: int):
        self.stream = stream
        self.limit = limit

    def write(self, data: bytes) -> int:
        end = self.stream.tell() + len(data)
        if end > self.limit:
            raise AgentMoveError(
                f"Agent move package would exceed {end} bytes; receiver limit is {self.limit} bytes"
            )
        return self.stream.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


@contextmanager
def _write_archive(path: Path, max_package_bytes: int | None, *,
                   max_unpacked_bytes: int | None = None):
    unpacked_limit = (None if max_unpacked_bytes is None
                      else _positive_archive_member_limit(max_unpacked_bytes, label="expanded package byte"))
    with path.open("w+b") as stream:
        writer = (_BoundedArchiveWriter(stream, max_package_bytes)
                  if max_package_bytes is not None else stream)
        with _BudgetedZipFile(
            writer, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True,
            unpacked_limit=unpacked_limit,
        ) as archive:
            yield archive


def receiver_archive_member_limit(capabilities: Mapping[str, Any]) -> int:
    # Older receivers did not advertise this dimension and enforce the
    # historical protocol limit. Never infer a larger allowance for them.
    value = capabilities.get("max_archive_members", MAX_ARCHIVE_MEMBERS)
    return _positive_archive_member_limit(value)


def _source_inventory_entry_limit() -> int | None:
    configured = os.environ.get("HASHI_AGENT_MOVE_MAX_INVENTORY_ENTRIES")
    if configured is None:
        return None
    return _positive_archive_member_limit(configured.strip(), label="source workspace inventory entry")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_agent_id(agent_id: str) -> str:
    value = str(agent_id or "").strip()
    if not _AGENT_ID_RE.fullmatch(value):
        raise AgentMoveError(
            "agent_id must begin with an alphanumeric character and contain "
            "only letters, digits, dots, underscores, or hyphens "
            f"(1 to {AGENT_ID_MAX_LENGTH} characters)"
        )
    return value


def detect_environment_kind() -> str:
    if os.name == "nt" or platform.system().lower() == "windows":
        return "windows"
    if is_wsl():
        return "wsl"
    system = platform.system().lower()
    return system or "unknown"


def validate_agent_id_for_platform(agent_id: str, target_platform: str) -> str:
    """Validate one portable Agent ID against the receiver filesystem rules."""

    value = normalize_agent_id(agent_id)
    if str(target_platform or "").strip().lower() == "windows":
        _validate_windows_component(value, context="Agent ID")
    return value


def _read_canonical_pcm(workspace: Path, agent_id: str) -> str:
    """Read the exact workspace PCM through its authoritative validator."""

    pcm_path = canonical_agent_md(workspace)
    try:
        document = load_pcm_document(pcm_path, workspace_dir=workspace)
    except PCMValidationError as exc:
        if exc.code == "pcm_missing":
            raise AgentMoveError(
                f"canonical agent.md is missing for Agent '{agent_id}'"
            ) from exc
        raise AgentMoveError(f"canonical agent.md is invalid: {exc}") from exc

    try:
        pcm_bytes = pcm_path.read_bytes()
    except OSError as exc:
        raise AgentMoveError(f"canonical agent.md is invalid: {exc}") from exc
    if hashlib.sha256(pcm_bytes).hexdigest() != document.content_sha256:
        raise AgentMoveError("canonical agent.md changed while preparing the package")
    return pcm_bytes.decode("utf-8")


def _read_retained_identity(
    workspace: Path, agent_id: str
) -> tuple[bytes | None, dict[str, Any] | None]:
    """Read the sole tolerated non-canonical identity without following links."""

    try:
        aliases = sorted(
            entry
            for entry in workspace.iterdir()
            if entry.name.casefold() == PCM_FILENAME.casefold()
            and entry.name != PCM_FILENAME
        )
    except OSError as exc:
        raise AgentMoveError(
            f"canonical agent.md cannot be inspected for Agent '{agent_id}'"
        ) from exc
    invalid = [entry.name for entry in aliases if entry.name != "AGENT.md"]
    if invalid:
        raise AgentMoveError(
            f"workspace/{invalid[0]} conflicts with the reserved Agent identity; "
            "only exact root AGENT.md may be retained as a non-authoritative attachment"
        )
    if not aliases:
        return None, None

    path = aliases[0]
    try:
        before = path.lstat()
    except OSError as exc:
        raise AgentMoveError("retained root AGENT.md cannot be inspected") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise AgentMoveError(
            "retained root AGENT.md must be a regular file owned by the workspace"
        )
    if int(before.st_size) > _CONTROL_MEMBER_LIMITS[RETAINED_IDENTITY_ARCHIVE_PATH]:
        raise AgentMoveError("retained root AGENT.md exceeds its size limit")

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise AgentMoveError(
                    "retained root AGENT.md must be a regular file owned by the workspace"
                )
            content = handle.read()
        after = path.lstat()
    except AgentMoveError:
        raise
    except OSError as exc:
        raise AgentMoveError("retained root AGENT.md cannot be read safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)

    def identity(info: os.stat_result) -> tuple[int, int, int, int]:
        return (
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
        )

    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or identity(before) != identity(opened)
        or identity(opened) != identity(after)
        or len(content) != int(opened.st_size)
    ):
        raise AgentMoveError("retained root AGENT.md changed while preparing the package")
    metadata = {
        "authoritative": False,
        "original_path": "AGENT.md",
        "archive_path": RETAINED_IDENTITY_ARCHIVE_PATH,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
    }
    return content, metadata


def create_agent_move_package(
    hashi_root: Path | str,
    agent_id: str,
    output_path: Path | str,
    *,
    source_instance: str | None = None,
    include_workspace: bool = True,
    include_agent_secrets: bool = False,
    secret_passphrase: str | None = None,
    package_id: str | None = None,
    max_package_bytes: int | None = None,
    max_archive_members: int | None = None,
    max_unpacked_bytes: int | None = None,
    max_workspace_bytes: int | None = None,
    operation: str = "move",
    include_telegram_secret: bool = True,
    schema_version: int | None = None,
    transfer_mode: str | None = None,
    history_mode: str | None = None,
    conversation_owner_id: str | None = None,
    desired_target_active: bool | None = None,
    retain_telegram_backup: bool = False,
) -> AgentMoveArchive:
    """Create one checksummed Agent move archive.

    ``secret_passphrase`` is normally the paired HASHI Remote shared token.
    Secret values are never emitted unless encryption is available and a
    passphrase is supplied.
    """

    member_limit = (configured_archive_member_limit() if max_archive_members is None
                    else _positive_archive_member_limit(max_archive_members))
    package_limit = (None if max_package_bytes is None
                     else _positive_archive_member_limit(max_package_bytes, label="package byte"))
    unpacked_limit = (configured_unpacked_byte_limit() if max_unpacked_bytes is None
                      else _positive_archive_member_limit(max_unpacked_bytes, label="expanded package byte"))
    root = Path(hashi_root).expanduser().resolve()
    name = normalize_agent_id(agent_id)
    transfer_operation = str(operation or "").strip().lower()
    if transfer_operation not in {"move", "clone"}:
        raise AgentMoveError("Agent transfer operation must be 'move' or 'clone'")
    if desired_target_active is not None and type(desired_target_active) is not bool:
        raise AgentMoveError("destination activation must be a boolean")
    if type(retain_telegram_backup) is not bool:
        raise AgentMoveError("Telegram backup selection must be boolean")
    if retain_telegram_backup and (
        transfer_operation != "clone" or desired_target_active is not False
    ):
        raise AgentMoveError("Telegram backup requires an explicitly inactive clone")
    if retain_telegram_backup and (not include_agent_secrets or not secret_passphrase):
        raise AgentMoveError("Telegram backup requires encrypted Agent credentials")
    # Ordinary Clone retains the independent identity default. The explicitly
    # inactive backup mode carries a token only inside encrypted credentials.
    include_telegram_secret = bool(
        (transfer_operation == "move" and include_telegram_secret)
        or (transfer_operation == "clone" and retain_telegram_backup)
    )
    package_key = str(package_id or uuid4())
    _validate_package_id(package_key)
    output = Path(output_path).expanduser().resolve()

    raw_config = _find_agent_config(root, name)
    if raw_config.get("transfer_state") == "moved_out_pending_reboot":
        raise AgentMoveError(
            f"source Agent '{name}' has already moved to another instance"
        )
    workspace = _resolve_workspace(root, raw_config, name)
    if transfer_mode is not None and transfer_mode not in TRANSFER_MODES:
        raise AgentMoveError("unknown Agent transfer mode")
    inventory_limit = (_source_inventory_entry_limit()
                       if transfer_mode and transfer_operation == "move" else None)
    workspace_limit = (
        (configured_workspace_byte_limit() if max_workspace_bytes is None
         else _positive_archive_member_limit(max_workspace_bytes, label="workspace byte"))
        if transfer_mode else MAX_WORKSPACE_BYTES
    )
    # Move discloses all files that source cleanup would delete. Clone neither
    # copies nor deletes excluded software/cache trees; inventorying/counting
    # those trees would introduce unrelated size, file-count and access gates.
    inventory = _workspace_inventory(workspace, max_entries=inventory_limit) if transfer_mode and transfer_operation == "move" else []
    total_workspace_bytes = sum(item["size"] for item in inventory)
    retained_identity_bytes, retained_identity = (
        (None, None) if transfer_mode == "identity_memory"
        else _read_retained_identity(workspace, name)
    )
    pcm_text = _read_canonical_pcm(workspace, name)
    requested_schema = int(schema_version) if schema_version is not None else (5 if transfer_mode else 3)
    if transfer_mode and requested_schema < 4:
        raise AgentMoveError("explicit transfer modes require schema 4")
    if requested_schema not in range(PACKAGE_SCHEMA_MIN_VERSION, PACKAGE_SCHEMA_VERSION + 1):
        raise AgentMoveError(f"unsupported requested Agent package schema {requested_schema}")
    inactive_source = transfer_operation == "move" and raw_config.get("is_active", True) is False
    inactive_target = (
        (transfer_operation == "move" and (
            desired_target_active is False or (desired_target_active is None and inactive_source)
        ))
        or (transfer_operation == "clone" and desired_target_active is False)
    )
    if (inactive_source or inactive_target) and requested_schema < 3:
        raise AgentMoveError("inactive Agent move requires transfer lifecycle support")
    if retained_identity is not None and requested_schema == 1:
        raise AgentMoveError("schema 1 cannot preserve retained root AGENT.md")
    agent_capability = (
        _collect_agent_capability(root, name) if requested_schema >= 3 else None
    )
    package_schema = requested_schema
    normalized_history_mode: str | None = None
    conversation_continuity: dict[str, Any] | None = None
    history_selection_warnings: list[str] = []
    if package_schema >= 5:
        normalized_history_mode = str(
            history_mode
            if history_mode is not None
            else ("move" if transfer_operation == "move" else "none")
        ).strip().lower()
        allowed_history_modes = (
            {"move", "retain_source"}
            if transfer_operation == "move"
            else {"none", "inherit_read_only", "copy"}
        )
        if normalized_history_mode not in allowed_history_modes:
            raise AgentMoveError(
                f"history mode {normalized_history_mode!r} is invalid for {transfer_operation}"
            )
        if normalized_history_mode not in {"none", "retain_source"}:
            configured_source_id = str(_configured_instance_id(root) or "").upper()
            if not configured_source_id:
                raise AgentMoveError(
                    "source instance identity is not configured for conversation history"
                )
            source_id = str(source_instance or configured_source_id).upper()
            if source_id != configured_source_id:
                raise AgentMoveError(
                    "conversation history source instance does not match configuration"
                )
            conversation_continuity = _export_conversation_continuity_snapshot(
                root,
                source_instance=source_id,
                agent_id=name,
                transfer_id=package_key,
                history_mode=normalized_history_mode,
                explicit_owner_id=conversation_owner_id,
                selection_warnings=history_selection_warnings,
            )
    elif history_mode is not None or conversation_owner_id is not None:
        raise AgentMoveError("conversation history options require package schema 5")

    agent_config, config_warnings = _portable_agent_config(raw_config, name)
    schedules = _collect_schedules(root, name)
    secret_values, required_secret_keys, telegram_secret_key = _collect_agent_secrets(
        root, raw_config, name
    )
    delivery_state = None
    retained_pending_count = 0
    if transfer_operation == "move":
        lifecycle_id = raw_config.get(AGENT_LIFECYCLE_FIELD)
        bot_fingerprint = telegram_bot_fingerprint(
            secret_values.get(telegram_secret_key)
        )
        if valid_agent_lifecycle_id(lifecycle_id) and bot_fingerprint:
            delivery_state = export_owned_state(
                root,
                name,
                {
                    "instance_id": str(
                        source_instance or _configured_instance_id(root) or "HASHI"
                    ).upper(),
                    "agent_lifecycle_id": str(lifecycle_id).casefold(),
                    "telegram_bot_fingerprint": bot_fingerprint,
                },
            )
        pending_requests = {
            str(request_id)
            for chat in (delivery_state or {}).get("per_chat", {}).values()
            if isinstance(chat, dict)
            for request_id in chat.get("undelivered_request_ids", [])
            if request_id
        }
        if pending_requests and transfer_mode != "workspace":
            # The source cleanup retains the complete workspace and an
            # owner-bound recovery-state snapshot. Do not send pointers to
            # response files that this narrower package does not carry.
            retained_pending_count = len(pending_requests)
            delivery_state = None
    if not include_telegram_secret:
        secret_values.pop(telegram_secret_key, None)
        required_secret_keys = [
            key for key in required_secret_keys if key != telegram_secret_key
        ]
    if include_agent_secrets and secret_values and not secret_passphrase:
        raise AgentMoveError(
            "an encryption passphrase is required to include Agent credentials"
        )
    encrypted_secrets = (
        _encrypt_json(secret_values, str(secret_passphrase))
        if include_agent_secrets and secret_values
        else None
    )

    if transfer_mode == "identity_memory":
        retained_identity_bytes, retained_identity = None, None
    entries: list[WorkspaceEntry] = []
    exclusions: list[dict[str, str]] = []
    if include_workspace or transfer_mode:
        workspace_payload_limit = workspace_limit - (len(pcm_text.encode("utf-8")) if transfer_mode else 0)
        if workspace_payload_limit < 0:
            raise AgentMoveError(f"Agent workspace identity exceeds the {workspace_limit} byte limit")
        entries, exclusions = _scan_workspace(
            workspace,
            max_workspace_bytes=workspace_payload_limit,
            transfer_mode=transfer_mode,
            include_undelivered=transfer_operation == "move",
        )

    source_kind = detect_environment_kind()
    access_requirements = {
        "schema_version": 1,
        "access_scope": str(raw_config.get("access_scope") or "project"),
        "active_backend": str(
            raw_config.get("active_backend") or raw_config.get("engine") or ""
        ),
        "allowed_backends": list(raw_config.get("allowed_backends") or []),
        "agent_secret_keys": required_secret_keys,
        "agent_secrets_included": bool(encrypted_secrets),
        "target_rebind_required": [
            "instance_shared_provider_credentials",
            "oauth_sessions",
            "browser_bridge_pairing",
            "operating_system_permissions",
            "workzone_mounts",
        ],
    }
    if package_schema >= 3:
        access_requirements.update(
            {
                "telegram_secret_key": telegram_secret_key,
                "telegram_secret_included": bool(
                    encrypted_secrets and include_telegram_secret and telegram_secret_key in secret_values
                ),
                "operation": transfer_operation,
                "telegram_delivery_state": delivery_state,
            }
        )
        if retained_pending_count:
            access_requirements["source_retained_pending_count"] = retained_pending_count
    workspace_metadata = {
        "schema_version": 1,
        "policy": "durable-workspace-v1"
        if include_workspace
        else "identity-and-memory-only-v1",
        "files": [
            {
                "path": item.relative_path,
                "mode": item.mode,
                "source_size": item.size,
                "sqlite_snapshot": item.sqlite_snapshot,
                "materialized_symlink": item.materialized_symlink,
            }
            for item in entries
        ],
        "excluded": exclusions,
        "source_bytes": sum(item.size for item in entries),
    }
    if transfer_mode:
        selected = {item.relative_path for item in entries} | {"agent.md"}
        if retained_identity is not None:
            selected.add("AGENT.md")
        workspace_metadata.update({
            "transfer_mode": transfer_mode,
            "policy": "explicit-transfer-scope-v1",
            "inventory": inventory,
            "total_workspace_bytes": total_workspace_bytes,
            "discarded": [item for item in inventory if item["path"] not in selected],
        })
    manifest = {
        "package_type": PACKAGE_TYPE,
        "schema_version": package_schema,
        "package_id": package_key,
        "created_at": utc_now_iso(),
        "source_instance": str(
            source_instance or _configured_instance_id(root) or "HASHI"
        ).upper(),
        "source_environment": source_kind,
        "agent_id": name,
        "workspace_policy": workspace_metadata["policy"],
        "sections": {
            "identity": True,
            "retained_identity": retained_identity is not None,
            "workspace": bool(include_workspace),
            "memory": True,
            "schedules": any(
                schedules.get(section) for section in ("heartbeats", "crons", "nudges")
            ),
            "access_requirements": True,
            "encrypted_agent_secrets": bool(encrypted_secrets),
        },
        "required_receiver_capabilities": [
            AGENT_MOVE_CAPABILITY,
            *(
                [RETAINED_IDENTITY_CAPABILITY]
                if retained_identity is not None
                else []
            ),
        ],
        "compatibility_floor": f"agent-move-v{package_schema}",
        "warnings": [
            *history_selection_warnings,
            *config_warnings,
            *(
                [f"{retained_pending_count} undelivered Telegram response(s) and their "
                 "recovery state will remain on the source; the target will not "
                 "automatically deliver those responses"]
                if retained_pending_count else []
            ),
            *(
                [
                    "root AGENT.md will be retained as a non-authoritative attachment; "
                    "only agent.md remains the live PCM identity"
                ]
                if retained_identity is not None
                else []
            ),
        ],
    }
    if package_schema >= 3:
        manifest["operation"] = transfer_operation
        manifest["sections"]["agent_capability"] = agent_capability is not None
        manifest["required_receiver_capabilities"].insert(
            1, AGENT_TRANSFER_LIFECYCLE_CAPABILITY
        )
    if inactive_target:
        manifest["desired_target_active"] = False
        manifest["required_receiver_capabilities"].append(
            INACTIVE_CLONE_BACKUP_CAPABILITY if transfer_operation == "clone"
            else INACTIVE_MOVE_CAPABILITY
        )
    if retain_telegram_backup:
        manifest["retain_telegram_backup"] = True

    if transfer_mode:
        manifest["transfer_mode"] = transfer_mode
        manifest["required_receiver_capabilities"].append(TRANSFER_MODES_CAPABILITY)
    if package_schema >= 5:
        manifest["history_mode"] = normalized_history_mode
        manifest["sections"]["conversation_continuity"] = (
            conversation_continuity is not None
        )
        manifest["conversation_continuity_summary"] = (
            dict(conversation_continuity.get("summary") or {})
            if conversation_continuity is not None
            else {
                "session_count": 0,
                "eligible_message_count": 0,
                "excluded_message_count": 0,
                "attachments_included": 0,
            }
        )
        manifest["required_receiver_capabilities"].append(
            CONVERSATION_CONTINUITY_CAPABILITY
        )
        if normalized_history_mode == "retain_source":
            manifest["required_receiver_capabilities"].append(
                RETAIN_SOURCE_HISTORY_CAPABILITY
            )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{package_key}.tmp")
    temporary.unlink(missing_ok=True)
    checksums: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="hashi-agent-move-") as temp_name:
        temp_dir = Path(temp_name)
        prepared_entries = _prepare_workspace_entries(
            entries, temp_dir, freeze_files=transfer_operation == "clone",
            max_workspace_bytes=(workspace_limit - len(pcm_text.encode("utf-8"))
                                 if transfer_mode else workspace_limit),
        )
        sqlite_snapshot_paths = {
            item.relative_path for item in prepared_entries if item.sqlite_snapshot
        }
        # Only a successfully backed-up database owns disposable reader
        # bookkeeping. A work file merely named *.db does not.
        for item in workspace_metadata["files"]:
            item["sqlite_snapshot"] = item["path"] in sqlite_snapshot_paths
        prepared_bytes = sum(item.size for item in prepared_entries)
        if transfer_operation == "clone" and transfer_mode:
            inventory = [
                {"path": item.relative_path, "size": item.size, "mtime_ns": 0, "link": False}
                for item in prepared_entries
            ]
            inventory.append({"path": "agent.md", "size": len(pcm_text.encode("utf-8")), "mtime_ns": 0, "link": False})
            if retained_identity_bytes is not None:
                inventory.append({"path": "AGENT.md", "size": len(retained_identity_bytes), "mtime_ns": 0, "link": False})
            total_workspace_bytes = sum(item["size"] for item in inventory)
            workspace_metadata.update(
                inventory=sorted(inventory, key=lambda item: item["path"]),
                inventory_scope="portable_clone_payload", total_workspace_bytes=total_workspace_bytes,
                # Disclose exclusions without recursively stat'ing software or
                # files outside the selected scope. Unknown size is explicit.
                discarded=[{"path": item["path"], "size": 0, "size_known": False, "reason": item["reason"]} for item in exclusions],
            )
        if prepared_bytes + (len(pcm_text.encode("utf-8")) if transfer_mode else 0) > workspace_limit:
            raise AgentMoveError(
                f"consistent Agent workspace snapshots exceed the portable move limit of {workspace_limit} bytes"
            )
        try:
            with _write_archive(temporary, package_limit, max_unpacked_bytes=unpacked_limit) as archive:
                _write_json_member(archive, "manifest.json", manifest, checksums)
                _write_json_member(archive, "identity/agent.json", agent_config, checksums)
                _write_bytes(
                    archive, "identity/agent.md", pcm_text.encode("utf-8"), checksums
                )
                if agent_capability is not None:
                    _write_json_member(
                        archive,
                        AGENT_CAPABILITY_ARCHIVE_PATH,
                        agent_capability,
                        checksums,
                    )
                if conversation_continuity is not None:
                    _write_json_member(
                        archive,
                        CONVERSATION_CONTINUITY_ARCHIVE_PATH,
                        conversation_continuity,
                        checksums,
                    )
                if retained_identity_bytes is not None and retained_identity is not None:
                    _write_bytes(
                        archive,
                        RETAINED_IDENTITY_ARCHIVE_PATH,
                        retained_identity_bytes,
                        checksums,
                    )
                    _write_json_member(
                        archive,
                        RETAINED_IDENTITY_METADATA_PATH,
                        retained_identity,
                        checksums,
                    )
                _write_json_member(
                    archive,
                    "access/requirements.json",
                    access_requirements,
                    checksums,
                )
                _write_json_member(archive, "schedules/tasks.json", schedules, checksums)
                if encrypted_secrets is not None:
                    _write_bytes(
                        archive, "secrets/agent.enc", encrypted_secrets, checksums
                    )
                for item in prepared_entries:
                    _write_file(
                        archive,
                        f"workspace/{item.relative_path}",
                        item.source,
                        checksums,
                        mode=item.mode,
                    )
                if transfer_mode and transfer_operation == "move":
                    final_inventory = _workspace_inventory(workspace, max_entries=inventory_limit)
                    _assert_packaging_inventory_current(
                        inventory, final_inventory, sqlite_snapshot_paths,
                    )
                    # Retain the complete observed inventory for deletion
                    # disclosure, including reader-created sidecars.
                    workspace_metadata.update({
                        "inventory": final_inventory,
                        "total_workspace_bytes": sum(
                            item["size"] for item in final_inventory
                        ),
                        "discarded": [
                            item for item in final_inventory
                            if item["path"] not in selected
                        ],
                    })
                _write_json_member(
                    archive,
                    "metadata/workspace.json",
                    workspace_metadata,
                    checksums,
                )
                _write_json_member(
                    archive, "checksums.json",
                    {"schema_version": 1, "files": dict(sorted(checksums.items()))},
                    None,
                )
            if package_limit is not None and temporary.stat().st_size > package_limit:
                raise AgentMoveError(
                    f"Agent move package is {temporary.stat().st_size} bytes; "
                    f"receiver limit is {package_limit} bytes"
                )
            if os.name != "nt":
                temporary.chmod(0o600)
            # Validate the completed archive before replacing a caller's
            # existing output. This also catches files that grew after the
            # initial workspace scan.
            read_agent_move_package(temporary, verify=True, max_archive_members=member_limit,
                                    max_unpacked_bytes=unpacked_limit,
                                    max_workspace_bytes=workspace_limit)
            if transfer_mode and transfer_operation == "move":
                final_inventory = _workspace_inventory(workspace, max_entries=inventory_limit)
                _assert_packaging_inventory_current(
                    inventory, final_inventory, sqlite_snapshot_paths,
                )
            if conversation_continuity is not None and transfer_operation == "move":
                final_continuity = _export_conversation_continuity_snapshot(
                    root,
                    source_instance=str(conversation_continuity["source_instance"]),
                    agent_id=name,
                    transfer_id=package_key,
                    history_mode=str(conversation_continuity["history_mode"]),
                    explicit_owner_id=str(conversation_continuity["owner_id"]),
                )
                if _durable_conversation_continuity(final_continuity) != (
                    _durable_conversation_continuity(conversation_continuity)
                ):
                    raise AgentMoveError(
                        "conversation history changed while packaging; prepare a fresh transfer"
                    )
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    return read_agent_move_package(output, verify=True, max_archive_members=member_limit,
                                   max_unpacked_bytes=unpacked_limit,
                                   max_workspace_bytes=workspace_limit)


def read_agent_move_package(
    package_path: Path | str,
    *,
    verify: bool = True,
    target_platform: str | None = None,
    max_archive_members: int | None = None,
    max_unpacked_bytes: int | None = None,
    max_workspace_bytes: int | None = None,
) -> AgentMoveArchive:
    workspace_limit = (configured_workspace_byte_limit() if max_workspace_bytes is None
                       else _positive_archive_member_limit(max_workspace_bytes, label="workspace byte"))
    unpacked_limit = (configured_unpacked_byte_limit() if max_unpacked_bytes is None
                      else _positive_archive_member_limit(max_unpacked_bytes, label="expanded package byte"))
    member_limit = (configured_archive_member_limit() if max_archive_members is None
                    else _positive_archive_member_limit(max_archive_members))
    path = Path(package_path).expanduser().resolve()
    if not path.is_file():
        raise AgentMoveError(f"Agent move package not found: {path}")
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise AgentMoveError("invalid Agent move archive") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > member_limit:
            raise AgentMoveError(
                f"Agent move archive contains {len(infos)} members; limit is {member_limit}"
            )
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise AgentMoveError("Agent move archive contains duplicate member names")
        for name in names:
            _safe_member_name(name)
        _validate_control_member_sizes(infos)
        unpacked = sum(max(0, int(info.file_size)) for info in infos)
        if unpacked > unpacked_limit:
            raise AgentMoveError(
                f"expanded Agent move archive is {unpacked} bytes; receiver limit is {unpacked_limit} bytes"
            )

        manifest = _read_json(archive, "manifest.json")
        checksums_obj = _read_json(archive, "checksums.json")
        checksums = checksums_obj.get("files")
        if not isinstance(checksums, dict):
            raise AgentMoveError("checksums.json files must be an object")
        if verify:
            _verify_checksums(archive, checksums)
        _validate_manifest(manifest)
        agent_config = _read_json(archive, "identity/agent.json")
        access = _read_json(archive, "access/requirements.json")
        if int(manifest.get("schema_version") or 0) >= 3:
            operation = str(manifest.get("operation") or "").strip().lower()
            if operation not in {"move", "clone"}:
                raise AgentMoveError("schema 3 package operation is invalid")
            if str(access.get("operation") or "").strip().lower() != operation:
                raise AgentMoveError(
                    "schema 3 access requirements do not match the operation"
                )
            telegram_key = access.get("telegram_secret_key")
            if not isinstance(telegram_key, str) or not telegram_key.strip():
                raise AgentMoveError(
                    "schema 3 access requirements omit the Telegram credential key"
                )
            if not isinstance(access.get("telegram_secret_included"), bool):
                raise AgentMoveError(
                    "schema 3 access requirements omit Telegram credential policy"
                )
            if (operation == "clone" and access.get("telegram_secret_included")
                    and manifest.get("retain_telegram_backup") is not True):
                raise AgentMoveError("active clone package declares a Telegram credential")
            if access.get("telegram_secret_included") and "secrets/agent.enc" not in names:
                raise AgentMoveError("packaged Telegram credential is not encrypted")
            retained_count = access.get("source_retained_pending_count", 0)
            if (type(retained_count) is not int or retained_count < 0
                    or (retained_count and (
                        operation != "move"
                        or manifest.get("transfer_mode") == "workspace"
                        or access.get("telegram_delivery_state") is not None
                    ))):
                raise AgentMoveError("source-retained pending delivery declaration is invalid")
            delivery_state = access.get("telegram_delivery_state")
            if delivery_state is not None:
                if operation != "move" or not isinstance(delivery_state, dict):
                    raise AgentMoveError(
                        "only a move package may carry owned Telegram delivery state"
                    )
                if record_owner(delivery_state) is None:
                    raise AgentMoveError(
                        "packaged Telegram delivery state has no verifiable owner"
                    )
                if any(key in delivery_state for key in ("token", "telegram_token")):
                    raise AgentMoveError(
                        "packaged Telegram delivery state contains raw credential data"
                    )
        schedules = _read_json(archive, "schedules/tasks.json")
        workspace_metadata = _read_json(archive, "metadata/workspace.json")
        workspace_bytes = sum(
            info.file_size for info in infos
            if info.filename.startswith("workspace/") or info.filename == "identity/agent.md"
        )
        if int(manifest.get("schema_version") or 0) >= 4:
            mode = manifest.get("transfer_mode")
            if mode not in TRANSFER_MODES or workspace_metadata.get("transfer_mode") != mode:
                raise AgentMoveError("package transfer mode is invalid or inconsistent")
            if TRANSFER_MODES_CAPABILITY not in manifest.get("required_receiver_capabilities", []):
                raise AgentMoveError("package omits explicit transfer mode capability")
            if workspace_bytes > workspace_limit:
                raise AgentMoveError(
                    f"Agent workspace payload is {workspace_bytes} bytes; receiver limit is {workspace_limit} bytes"
                )
            if mode == "workspace":
                try:
                    inventory_bytes = int(workspace_metadata.get("total_workspace_bytes", -1))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise AgentMoveError("workspace inventory size is invalid") from exc
                if inventory_bytes < 0:
                    raise AgentMoveError("workspace inventory size is invalid")
            if mode == "identity_memory" and any(name.startswith("workspace/") and not is_portable_memory_path(name[len("workspace/"):]) for name in names):
                raise AgentMoveError("identity-memory package contains files outside PCM scope")

        conversation_continuity: dict[str, Any] | None = None
        continuity_present = CONVERSATION_CONTINUITY_ARCHIVE_PATH in names
        package_schema = int(manifest.get("schema_version") or 0)
        if package_schema >= 5:
            operation = str(manifest.get("operation") or "").strip().lower()
            history_mode = str(manifest.get("history_mode") or "").strip().lower()
            allowed_history_modes = (
                {"move", "retain_source"}
                if operation == "move"
                else {"none", "inherit_read_only", "copy"}
            )
            if history_mode not in allowed_history_modes:
                raise AgentMoveError("package conversation history mode is invalid")
            declared = (manifest.get("sections") or {}).get(
                "conversation_continuity"
            )
            if not isinstance(declared, bool) or declared != continuity_present:
                raise AgentMoveError(
                    "conversation continuity attachment does not match the manifest"
                )
            if (history_mode in {"none", "retain_source"}) != (not continuity_present):
                raise AgentMoveError(
                    "package conversation history mode and attachment are inconsistent"
                )
            if CONVERSATION_CONTINUITY_CAPABILITY not in manifest.get(
                "required_receiver_capabilities", []
            ):
                raise AgentMoveError(
                    "package omits conversation continuity receiver capability"
                )
            if (history_mode == "retain_source" and
                    RETAIN_SOURCE_HISTORY_CAPABILITY not in manifest.get(
                        "required_receiver_capabilities", []
                    )):
                raise AgentMoveError("package omits retain-source history receiver capability")
            if continuity_present:
                conversation_continuity = _read_json(
                    archive,
                    CONVERSATION_CONTINUITY_ARCHIVE_PATH,
                )
                try:
                    validated = validate_conversation_continuity_capsule(
                        conversation_continuity,
                        source_agent_id=str(manifest.get("agent_id") or ""),
                        transfer_id=str(manifest.get("package_id") or ""),
                        history_mode=history_mode,
                    )
                except SessionConflict as exc:
                    raise AgentMoveError(str(exc)) from exc
                summary = dict(conversation_continuity.get("summary") or {})
                if summary != manifest.get("conversation_continuity_summary"):
                    raise AgentMoveError(
                        "conversation continuity summary does not match the manifest"
                    )
                if str(validated["payload"].get("source_instance") or "").upper() != str(
                    manifest.get("source_instance") or ""
                ).upper():
                    raise AgentMoveError(
                        "conversation continuity source instance does not match the manifest"
                    )
            elif manifest.get("conversation_continuity_summary") != {
                "session_count": 0,
                "eligible_message_count": 0,
                "excluded_message_count": 0,
                "attachments_included": 0,
            }:
                raise AgentMoveError(
                    "empty conversation continuity summary is invalid"
                )
        elif continuity_present or (manifest.get("sections") or {}).get(
            "conversation_continuity"
        ) not in {None, False}:
            raise AgentMoveError(
                "legacy Agent move packages cannot contain conversation continuity"
            )

        pcm_text = _read_required(archive, "identity/agent.md").decode("utf-8")
        try:
            parse_pcm_text(pcm_text, path=Path("agent.md"))
        except ValueError as exc:
            raise AgentMoveError(f"packaged agent.md is invalid: {exc}") from exc
        target_kind = target_platform or detect_environment_kind()
        if str(target_kind).lower() == "windows" and not (
            manifest.get("operation") == "clone" and int(manifest["schema_version"]) >= 3
        ):
            _validate_windows_component(
                str(manifest.get("agent_id") or ""),
                context="Agent ID",
            )
        _validate_workspace_members(names, target_kind)
        _validate_workspace_metadata(workspace_metadata, names)
        retained_identity = _read_and_validate_retained_identity(
            archive,
            manifest,
            names,
        )
        agent_capability = _read_and_validate_agent_capability(
            archive,
            manifest,
            names,
        )

    return AgentMoveArchive(
        package_path=path,
        manifest=manifest,
        agent_config=agent_config,
        access_requirements=access,
        schedules=schedules,
        workspace_metadata=workspace_metadata,
        retained_identity=retained_identity,
        agent_capability=agent_capability,
        conversation_continuity=conversation_continuity,
        checksums={str(k): str(v) for k, v in checksums.items()},
        names=tuple(names),
        unpacked_bytes=unpacked,
        workspace_bytes=workspace_bytes,
    )


def decrypt_agent_secrets(
    package: AgentMoveArchive, passphrase: str | None
) -> dict[str, Any]:
    if "secrets/agent.enc" not in package.names:
        return {}
    if not passphrase:
        raise AgentMoveError(
            "the package contains encrypted Agent credentials but no key is available"
        )
    with zipfile.ZipFile(package.package_path, "r") as archive:
        payload = archive.read("secrets/agent.enc")
    return _decrypt_json(payload, passphrase)


def read_retained_identity_bytes(package: AgentMoveArchive) -> bytes | None:
    """Return the validated non-authoritative identity attachment, if present."""

    if package.retained_identity is None:
        return None
    with zipfile.ZipFile(package.package_path, "r") as archive:
        content = _read_required(archive, RETAINED_IDENTITY_ARCHIVE_PATH)
    if hashlib.sha256(content).hexdigest() != package.retained_identity.get("sha256"):
        raise AgentMoveError("retained identity attachment changed after validation")
    return content


def extract_agent_workspace(
    package: AgentMoveArchive,
    destination: Path | str,
    *,
    target_platform: str | None = None,
) -> list[Path]:
    """Extract identity and workspace files into an empty staging directory."""

    dest = Path(destination)
    if dest.exists() and any(dest.iterdir()):
        raise AgentMoveError("workspace staging directory is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    target_kind = target_platform or detect_environment_kind()
    _validate_workspace_members(package.names, target_kind)
    metadata = {
        str(item.get("path")): item
        for item in package.workspace_metadata.get("files", [])
        if isinstance(item, dict) and item.get("path")
    }
    written: list[Path] = []
    with zipfile.ZipFile(package.package_path, "r") as archive:
        pcm_target = dest / "agent.md"
        with (
            archive.open("identity/agent.md", "r") as source,
            pcm_target.open("wb") as target_handle,
        ):
            shutil.copyfileobj(source, target_handle, length=1024 * 1024)
        _apply_mode(pcm_target, 0o600, target_kind)
        written.append(pcm_target)
        for name in package.names:
            if not name.startswith("workspace/"):
                continue
            relative = name[len("workspace/") :]
            if not relative:
                continue
            target = dest.joinpath(*PurePosixPath(relative).parts)
            resolved = target.resolve()
            if not resolved.is_relative_to(dest.resolve()):
                raise AgentMoveError(f"workspace member escapes destination: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name, "r") as source, target.open("wb") as target_handle:
                shutil.copyfileobj(source, target_handle, length=1024 * 1024)
            mode = int((metadata.get(relative) or {}).get("mode") or 0o600)
            _apply_mode(target, mode, target_kind)
            written.append(target)
    return written


def package_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _durable_conversation_continuity(
    capsule: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Compare transferred history, not counts of messages excluded from it.

    Move progress/confirmation notices are presentation-only and increment the
    exclusion diagnostic. The capsule retains that diagnostic under its full
    integrity digest, but it cannot invalidate unchanged eligible history.
    """
    if capsule is None:
        return None
    stable = dict(capsule)
    stable.pop("transfer_id", None)
    stable.pop("capsule_digest", None)
    summary = dict(stable["summary"])
    summary.pop("excluded_message_count", None)
    stable["summary"] = summary
    return stable


def archive_snapshot_fingerprint(
    package: AgentMoveArchive,
    *,
    secret_passphrase: str | None = None,
) -> str:
    """Return a stable digest of the durable source state in an archive.

    Creation timestamps, randomized encryption, exclusion diagnostics, and the
    excluded-history count and append-only slash-command audit are intentionally
    omitted. The audit remains packaged, but the confirmation callback's own
    audit append and presentation notices cannot invalidate the staged source.
    File contents, portable
    modes, Agent configuration, schedules, access requirements, and Agent-owned
    credential values remain covered so a staged move cannot silently cut over
    from a stale snapshot.
    """

    stable_control = {
        "identity/agent.json",
        "identity/agent.md",
        AGENT_CAPABILITY_ARCHIVE_PATH,
        "access/requirements.json",
        "schedules/tasks.json",
        RETAINED_IDENTITY_ARCHIVE_PATH,
        RETAINED_IDENTITY_METADATA_PATH,
    }
    file_checksums = {
        name: digest
        for name, digest in package.checksums.items()
        if name in stable_control
        or (
            name.startswith("workspace/")
            and name[len("workspace/") :].casefold()
            not in _FRESHNESS_EXCLUDED_WORKSPACE_PATHS
        )
    }
    portable_files = []
    for item in package.workspace_metadata.get("files", []) or []:
        if not isinstance(item, Mapping):
            continue
        relative_path = str(item.get("path") or "")
        if relative_path.casefold() in _FRESHNESS_EXCLUDED_WORKSPACE_PATHS:
            continue
        portable_files.append(
            {
                "path": relative_path,
                "mode": int(item.get("mode") or 0),
                "sqlite_snapshot": bool(item.get("sqlite_snapshot")),
                "materialized_symlink": bool(item.get("materialized_symlink")),
            }
        )
    credentials = decrypt_agent_secrets(package, secret_passphrase)
    payload = {
        "schema_version": int(package.manifest.get("schema_version") or 1),
        "file_checksums": dict(sorted(file_checksums.items())),
        "portable_files": sorted(portable_files, key=lambda item: item["path"]),
        "agent_credentials": credentials,
        "transfer_mode": package.manifest.get("transfer_mode"),
        "history_mode": package.manifest.get("history_mode"),
        "desired_target_active": package.manifest.get("desired_target_active", True),
        "conversation_continuity": _durable_conversation_continuity(
            package.conversation_continuity
        ),
        "deletion_inventory": [
            item for item in _durable_workspace_inventory(
                package.workspace_metadata.get("inventory", []),
                {
                    str(item.get("path") or "")
                    for item in package.workspace_metadata.get("files", [])
                    if item.get("sqlite_snapshot")
                },
            )
            if str(item.get("path", "")).casefold()
            not in _FRESHNESS_EXCLUDED_WORKSPACE_PATHS
        ],
    }
    digest = hashlib.sha256()
    for chunk in _iter_json_bytes(payload):
        digest.update(chunk)
    return digest.hexdigest()


def _find_agent_config(root: Path, agent_id: str) -> dict[str, Any]:
    path = root / "agents.json"
    if not path.is_file():
        raise AgentMoveError("agents.json is missing from the source HASHI instance")
    data = _load_json_file(path)
    agents = data if isinstance(data, list) else data.get("agents", [])
    for row in agents:
        if isinstance(row, dict) and (
            row.get("name") == agent_id or row.get("id") == agent_id
        ):
            return dict(row)
    raise AgentMoveError(f"Agent '{agent_id}' was not found in agents.json")


def _resolve_workspace(root: Path, config: Mapping[str, Any], agent_id: str) -> Path:
    value = (
        config.get("workspace_dir")
        or config.get("workspace")
        or f"workspaces/{agent_id}"
    )
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_dir():
        raise AgentMoveError(f"workspace for Agent '{agent_id}' does not exist")
    return path


def _portable_agent_config(
    config: Mapping[str, Any], agent_id: str
) -> tuple[dict[str, Any], list[str]]:
    result = dict(config)
    warnings: list[str] = []
    result.pop("id", None)
    result.pop("system_md", None)
    if result.pop("telegram_token", None):
        warnings.append(
            "inline Telegram credential was removed from Agent configuration"
        )
    result["name"] = agent_id
    result["workspace_dir"] = f"workspaces/{agent_id}"
    result["is_active"] = False
    result["transfer_import_state"] = "staged_inactive"
    result.setdefault("telegram_token_key", agent_id)
    return result, warnings


def _configured_instance_id(root: Path) -> str | None:
    try:
        data = _load_json_file(root / "agents.json")
    except AgentMoveError:
        return None
    if not isinstance(data, dict):
        return None
    return str((data.get("global") or {}).get("instance_id") or "").strip() or None


def _configured_owner_identity(
    root: Path, *, instance_role: str = "source"
) -> tuple[str, bool]:
    data = _load_json_file(root / "agents.json")
    global_config = data.get("global") if isinstance(data, Mapping) else {}
    if not isinstance(global_config, Mapping):
        raise AgentMoveError(
            f"{instance_role} authorized conversation owner configuration is invalid"
        )
    try:
        from orchestrator.enterprise.profile import resolve_deployment_profile
        from orchestrator.personal_data_owner import read_personal_data_binding

        if resolve_deployment_profile(dict(global_config)).value == "personal":
            binding = read_personal_data_binding(root)
            if binding is not None:
                return binding
        # PAO ownership remains stable when a Telegram account is replaced.
        # Telegram is optional when an explicit PAO owner scope exists.
        if str(global_config.get("owner_scope_id") or "").strip():
            return resolve_owner_scope_id(global_config, None), True
        secrets_path = root / "secrets.json"
        secrets = _load_json_file(secrets_path) if secrets_path.is_file() else {}
        if not isinstance(secrets, Mapping):
            raise ValueError("owner compatibility configuration must be an object")
        try:
            telegram_owner = resolve_telegram_owner_id(global_config, secrets)
        except ValueError:
            if resolve_deployment_profile(dict(global_config)).value != "personal":
                raise
            # Do not make an unrelated malformed connector a personal transfer
            # gate or infer an unknown legacy owner's history.
            telegram_owner = None
        return resolve_owner_scope_id(global_config, telegram_owner), (
            telegram_owner is not None
        )
    except ValueError as exc:
        raise AgentMoveError(
            f"{instance_role} authorized conversation owner configuration is invalid"
        ) from exc


def _export_conversation_continuity_snapshot(
    root: Path,
    *,
    source_instance: str,
    agent_id: str,
    transfer_id: str,
    history_mode: str,
    explicit_owner_id: str | None,
    selection_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Export through a disposable SQLite snapshot without migrating live state."""

    source_path = root / "state" / "sessions.sqlite3"
    with tempfile.TemporaryDirectory(prefix="hashi-session-continuity-") as name:
        snapshot_path = Path(name) / "sessions.sqlite3"
        if source_path.is_file():
            try:
                source_uri = source_path.resolve().as_uri() + "?mode=ro"
                with (
                    closing(sqlite3.connect(source_uri, uri=True)) as source_db,
                    closing(sqlite3.connect(snapshot_path)) as snapshot_db,
                ):
                    source_db.backup(snapshot_db)
            except (OSError, sqlite3.DatabaseError) as exc:
                raise AgentMoveError(
                    "could not create a consistent SessionStore snapshot"
                ) from exc
        store = SessionStore(snapshot_path, instance_id=source_instance)
        owner, has_explicit_owner = _resolve_conversation_owner(
            root,
            explicit_owner_id=explicit_owner_id,
        )
        if not has_explicit_owner:
            owners_with_history = store.conversation_history_owner_ids(agent_id=agent_id)
            if owners_with_history and owner not in owners_with_history:
                if (history_mode in {"copy", "inherit_read_only", "move"}
                        and _personal_transfer_source(root)):
                    if selection_warnings is not None:
                        selection_warnings.append(PERSONAL_CLONE_HISTORY_NOT_INCLUDED_WARNING)
                else:
                    _assert_source_history_owner(owner, owners_with_history)
        return store.export_conversation_continuity(
            owner_id=owner,
            agent_id=agent_id,
            source_instance=source_instance,
            transfer_id=transfer_id,
            history_mode=history_mode,
        )


def export_conversation_continuity_snapshot(
    root: Path,
    *,
    source_instance: str,
    agent_id: str,
    transfer_id: str,
    history_mode: str,
    explicit_owner_id: str | None,
) -> dict[str, Any]:
    """Public PAO helper for an owner-checked, read-only SessionStore snapshot."""

    return _export_conversation_continuity_snapshot(
        root,
        source_instance=source_instance,
        agent_id=agent_id,
        transfer_id=transfer_id,
        history_mode=history_mode,
        explicit_owner_id=explicit_owner_id,
    )


def establish_personal_target_conversation_owner(root: Path) -> str | None:
    """Establish the target's existing local data key after transfer verification."""
    from orchestrator.enterprise.profile import resolve_deployment_profile
    from orchestrator.personal_data_owner import resolve_personal_data_owner

    data = _load_json_file(root / "agents.json")
    global_config = data.get("global") if isinstance(data, Mapping) else None
    if not isinstance(global_config, Mapping):
        raise AgentMoveError("target conversation owner configuration is invalid")
    if resolve_deployment_profile(dict(global_config)).value != "personal":
        return None
    secrets_path = root / "secrets.json"
    secrets = _load_json_file(secrets_path) if secrets_path.is_file() else {}
    if not isinstance(secrets, Mapping):
        raise AgentMoveError("target compatibility configuration is invalid")
    try:
        telegram_owner = resolve_telegram_owner_id(global_config, secrets)
    except ValueError:
        # A malformed connector identity cannot authorize or block local transfer.
        telegram_owner = None
    return resolve_personal_data_owner(root, global_config, telegram_owner, establish=True)


def configured_conversation_owner_id(
    root: Path, *, instance_role: str = "source", agent_id: str | None = None
) -> str:
    """Return the configured conversation owner without exposing secret values."""

    owner, has_explicit_owner = _configured_owner_identity(root, instance_role=instance_role)
    if agent_id and not has_explicit_owner:
        try:
            owners = SessionStore.existing_conversation_history_owner_ids(
                root / "state" / "sessions.sqlite3",
                instance_id=str(_configured_instance_id(root) or "HASHI").upper(),
                agent_id=agent_id,
            )
        except (OSError, sqlite3.DatabaseError) as exc:
            raise AgentMoveError("source conversation ownership could not be read") from exc
        _assert_source_history_owner(owner, owners)
    return owner


def _personal_transfer_source(root: Path) -> bool:
    from orchestrator.enterprise.profile import resolve_deployment_profile

    data = _load_json_file(root / "agents.json")
    global_config = data.get("global") if isinstance(data, Mapping) else None
    if not isinstance(global_config, Mapping):
        raise AgentMoveError("source deployment configuration is invalid")
    try:
        return resolve_deployment_profile(dict(global_config)).value == "personal"
    except ValueError as exc:
        raise AgentMoveError("source deployment profile is invalid") from exc


def authenticated_conversation_command_owner(
    root: Path, *, allow_expired_context: bool = False,
) -> str | None:
    """Use the established command principal to select, never grant, history."""
    from orchestrator.frontend_command_context import transfer_command_owner_for_instance
    from orchestrator.telegram_actor_scope import transfer_owner_for_instance

    try:
        owner = transfer_command_owner_for_instance(root, allow_expired=allow_expired_context)
        if owner:
            return owner
        return transfer_owner_for_instance(root, allow_expired=allow_expired_context)
    except ValueError as exc:
        raise AgentMoveError(str(exc)) from exc


def source_conversation_owner_id(
    root: Path, *, agent_id: str | None = None, operation: str | None = None,
) -> str:
    """Resolve source history for the active authenticated command."""

    owner = authenticated_conversation_command_owner(root)
    if owner:
        return owner
    # Personal clone and move retain source partitions; the selected owner's
    # empty partition is valid even if another owner has historical messages.
    if operation in {"clone", "move"} and _personal_transfer_source(root):
        return configured_conversation_owner_id(root)
    return configured_conversation_owner_id(root, agent_id=agent_id)


def _assert_source_history_owner(owner: str, owners_with_history: list[str]) -> None:
    if owners_with_history and owner not in owners_with_history:
        raise AgentMoveError(
            "source authorized conversation owner is not configured for existing history"
        )


def _resolve_conversation_owner(
    root: Path,
    *,
    explicit_owner_id: str | None,
) -> tuple[str, bool]:
    explicit = str(explicit_owner_id or "").strip()
    authenticated = authenticated_conversation_command_owner(root)
    if authenticated:
        if explicit and explicit != authenticated:
            raise AgentMoveError("SessionStore history owner does not match the authenticated command owner")
        return authenticated, True
    configured, has_explicit_owner = _configured_owner_identity(root)
    if explicit and explicit != configured:
        raise AgentMoveError(
            "SessionStore history owner does not match the configured authorized owner"
        )
    # SessionStore already filters export by exact owner + Agent + instance.
    # Other scopes (including empty transport-account Sessions) neither grant
    # access nor veto the configured owner's export. No history is also valid.
    return configured, has_explicit_owner


def _collect_agent_capability(
    root: Path, agent_id: str
) -> dict[str, Any] | None:
    """Return the Agent-owned HChat/tool permission declaration, if present."""

    path = root / "agent_capabilities.json"
    if not path.is_file():
        return None
    data = _load_json_file(path)
    entries = data.get("agents", []) if isinstance(data, dict) else []
    if isinstance(entries, dict):
        raw = entries.get(agent_id)
        if not isinstance(raw, dict):
            return None
        result = dict(raw)
        result.setdefault("name", agent_id)
        return result
    if not isinstance(entries, list):
        return None
    matches = [
        dict(item)
        for item in entries
        if isinstance(item, dict)
        and str(item.get("name") or item.get("id") or "") == agent_id
    ]
    if len(matches) > 1:
        raise AgentMoveError(
            f"Agent '{agent_id}' has duplicate capability declarations"
        )
    return matches[0] if matches else None


def _collect_schedules(root: Path, agent_id: str) -> dict[str, Any]:
    path = root / "tasks.json"
    sections = ("heartbeats", "crons", "nudges")
    if not path.is_file():
        return {"version": 1, **{section: [] for section in sections},
                "source_enabled": {section: [] for section in sections},
                "source_originals": {section: [] for section in sections}}
    data = _load_json_file(path)
    result: dict[str, Any] = {
        "version": data.get("version", 1) if isinstance(data, dict) else 1,
        "source_enabled": {},
        "source_originals": {},
    }
    for section in sections:
        rows = data.get(section, []) if isinstance(data, dict) else []
        selected = []
        source_enabled = []
        source_originals = []
        for row in rows:
            if not isinstance(row, dict) or row.get("agent") != agent_id:
                continue
            item = dict(row)
            source_originals.append(dict(row))
            # The package stays quarantined for older receivers, while the
            # authenticated snapshot separately preserves the source intent.
            source_enabled.append(bool(item.get("enabled", False)))
            item["enabled"] = False
            item["import_state"] = "disabled_review_draft"
            selected.append(item)
        result[section] = selected
        result["source_enabled"][section] = source_enabled
        result["source_originals"][section] = source_originals
    return result


def _collect_agent_secrets(
    root: Path,
    agent_config: Mapping[str, Any],
    agent_id: str,
) -> tuple[dict[str, Any], list[str], str]:
    values: dict[str, Any] = {}
    path = root / "secrets.json"
    data: dict[str, Any] = {}
    if path.is_file():
        loaded = _load_json_file(path)
        if isinstance(loaded, dict):
            data = loaded
    token_key = (
        str(agent_config.get("telegram_token_key") or agent_id).strip() or agent_id
    )
    for key, value in data.items():
        if (
            key == token_key
            or key == agent_id
            or key.startswith((f"{agent_id}_", f"{agent_id}."))
        ):
            values[str(key)] = value
    inline = agent_config.get("telegram_token")
    if inline and token_key not in values:
        values[token_key] = inline
    required = sorted(values)
    return values, required, token_key


def _workspace_inventory(workspace: Path, *, max_entries: int | None = None) -> list[dict[str, Any]]:
    entries = []
    def fail(error):
        raise AgentMoveError(f"workspace inventory cannot be inspected: {error}") from error
    for current, dirs, files in os.walk(workspace, followlinks=False, onerror=fail):
        parent = Path(current)
        for name in list(dirs):
            path = parent / name
            if path.is_symlink() or _is_windows_junction(path):
                dirs.remove(name)
                files.append(name)
        for name in sorted(files):
            path = parent / name
            info = path.lstat()
            link = path.is_symlink() or _is_windows_junction(path)
            entries.append({"path": path.relative_to(workspace).as_posix(),
                            "size": 0 if link else info.st_size,
                            "mtime_ns": info.st_mtime_ns, "link": link})
            if max_entries is not None and len(entries) > max_entries:
                raise AgentMoveError(
                    f"source workspace inventory exceeds its {max_entries} entry limit"
                )
    return sorted(entries, key=lambda item: item["path"])


def _durable_workspace_inventory(
    inventory: Iterable[dict[str, Any]], sqlite_snapshot_paths: set[str],
) -> list[dict[str, Any]]:
    """Derive freshness without discarding the complete limit/deletion view.

    SQLite read-only backups can create, resize or remove a WAL-index (-shm)
    and an empty WAL. Neither contains durable edits. Never ignore a nonempty
    WAL, a link, or a similarly named file without a backed-up database.
    """
    durable = []
    for item in inventory:
        path = str(item.get("path") or "")
        reader_file = (
            not item.get("link")
            and path[:-4] in sqlite_snapshot_paths
            and (
                path.endswith("-shm")
                or (path.endswith("-wal") and item.get("size") == 0)
            )
        )
        if not reader_file:
            durable.append(item)
    return durable


def _assert_packaging_inventory_current(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    sqlite_snapshot_paths: set[str],
) -> None:
    old = {
        item["path"]: item
        for item in _durable_workspace_inventory(before, sqlite_snapshot_paths)
    }
    new = {
        item["path"]: item
        for item in _durable_workspace_inventory(after, sqlite_snapshot_paths)
    }
    changes = [
        {"path": path, "before": old.get(path), "after": new.get(path)}
        for path in sorted(old.keys() | new.keys())
        if old.get(path) != new.get(path)
    ]
    if changes:
        raise _WorkspaceChangedError(changes)


def _workspace_directory_exclusion(
    directory: Path, *, relative_path: str, at_workspace_root: bool,
    transfer_mode: str | None, include_undelivered: bool,
) -> str:
    """Apply the portable-workspace directory policy to a real directory."""
    name = directory.name.casefold()
    if name == PCM_FILENAME.casefold() and (not transfer_mode or at_workspace_root):
        raise AgentMoveError(
            f"workspace/{relative_path} conflicts with the reserved Agent identity; "
            "only exact root AGENT.md may be retained as a non-authoritative attachment"
        )
    if name in _SKIP_DIR_NAMES and not (
        include_undelivered and transfer_mode == "workspace"
        and at_workspace_root and name == "undelivered"
    ) and (
        not transfer_mode or at_workspace_root
        or name not in {"state", "tmp", "undelivered", "backend_state"}
    ):
        return "ephemeral_or_environment_directory"
    if directory.is_symlink() or _is_windows_junction(directory):
        return "directory_symlink_not_portable"
    if not transfer_mode and (directory / ".git").exists():
        return "nested_project_not_agent_state"
    return ""


def _workspace_file_exclusion(path: Path) -> str:
    """Keep the same policy for the selected name and any resolved link target."""
    name = path.name.casefold()
    environment_secret = name.startswith(".env.") and not name.endswith(
        (".example", ".sample", ".template")
    )
    credential = (
        name in {
            ".env", ".git-credentials", ".netrc", ".npmrc", ".pypirc",
            "auth.json", "credentials.json", "id_ed25519", "id_rsa",
            "oauth.json", "secrets.json", "service-account.json", "token.json",
        }
        or environment_secret
        or path.suffix.lower() in _SECRET_FILE_SUFFIXES
    )
    if name in _SKIP_FILE_NAMES or credential or any(name.endswith(x) for x in _SKIP_SUFFIXES):
        return "credential_material_requires_encrypted_rebind" if credential else "ephemeral_runtime_file"
    return ""


def _resolved_workspace_target_exclusion(
    target: Path, workspace: Path, *, transfer_mode: str | None,
    include_undelivered: bool,
) -> str:
    """A link cannot give excluded content a different, apparently safe name."""
    relative = target.relative_to(workspace)
    parent = workspace
    for part in relative.parts[:-1]:
        child = parent / part
        reason = _workspace_directory_exclusion(
            child, relative_path=child.relative_to(workspace).as_posix(),
            at_workspace_root=parent == workspace, transfer_mode=transfer_mode,
            include_undelivered=include_undelivered,
        )
        if reason:
            return reason
        parent = child
    if target.name.casefold() == PCM_FILENAME.casefold() and (
        not transfer_mode or parent == workspace
    ):
        return "agent_identity_is_not_workspace_content"
    return _workspace_file_exclusion(target)


def _scan_workspace(
    workspace: Path,
    *,
    max_workspace_bytes: int,
    transfer_mode: str | None = None,
    include_undelivered: bool = False,
) -> tuple[list[WorkspaceEntry], list[dict[str, str]]]:
    entries: list[WorkspaceEntry] = []
    excluded: list[dict[str, str]] = []
    total_bytes = 0
    workspace_resolved = workspace.resolve()
    for current, dir_names, file_names in os.walk(workspace, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for directory in sorted(dir_names):
            child = current_path / directory
            relative = child.relative_to(workspace).as_posix()
            reason = _workspace_directory_exclusion(
                child, relative_path=relative, at_workspace_root=current_path == workspace,
                transfer_mode=transfer_mode, include_undelivered=include_undelivered,
            )
            if reason:
                excluded.append({"path": relative, "reason": reason})
            else:
                kept_dirs.append(directory)
        dir_names[:] = kept_dirs

        for filename in sorted(file_names):
            source = current_path / filename
            relative = source.relative_to(workspace).as_posix()
            if relative == "agent.md":
                continue
            normalized_filename = filename.casefold()
            if normalized_filename == PCM_FILENAME.casefold() and (not transfer_mode or current_path == workspace):
                if relative == "AGENT.md":
                    continue
                raise AgentMoveError(
                    f"workspace/{relative} conflicts with the reserved Agent identity; "
                    "only exact root AGENT.md may be retained as a non-authoritative attachment"
                )
            reason = _workspace_file_exclusion(source)
            if reason:
                excluded.append({"path": relative, "reason": reason})
                continue
            if transfer_mode == "identity_memory" and not is_portable_memory_path(relative):
                excluded.append({"path": relative, "reason": "not_in_pcm_export_scope"})
                continue
            materialized = False
            actual = source
            if source.is_symlink():
                try:
                    actual = source.resolve(strict=True)
                except OSError:
                    excluded.append({"path": relative, "reason": "broken_symlink"})
                    continue
                if (
                    not actual.is_relative_to(workspace_resolved)
                    or not actual.is_file()
                ):
                    excluded.append({"path": relative, "reason": "external_symlink"})
                    continue
                reason = _resolved_workspace_target_exclusion(
                    actual, workspace_resolved, transfer_mode=transfer_mode,
                    include_undelivered=include_undelivered,
                )
                if reason:
                    excluded.append({"path": relative, "reason": reason})
                    continue
                materialized = True
            try:
                file_stat = actual.stat()
            except OSError:
                excluded.append({"path": relative, "reason": "unreadable"})
                continue
            if not stat.S_ISREG(file_stat.st_mode):
                excluded.append(
                    {"path": relative, "reason": "unsupported_file_type"}
                )
                continue
            projected_bytes = total_bytes + int(file_stat.st_size)
            if projected_bytes > max_workspace_bytes:
                raise AgentMoveError(
                    "durable Agent workspace exceeds the portable move limit "
                    f"of {max_workspace_bytes} bytes at {relative!r}; "
                    "move project/artifact data separately or remove it from the Agent workspace"
                )
            entries.append(
                WorkspaceEntry(
                    source=actual,
                    relative_path=relative,
                    mode=int(file_stat.st_mode & 0o777),
                    size=int(file_stat.st_size),
                    sqlite_snapshot=actual.suffix.lower() in _SQLITE_SUFFIXES,
                    materialized_symlink=materialized,
                )
            )
            total_bytes = projected_bytes
    return entries, excluded


def _is_windows_junction(path: Path) -> bool:
    """Treat Windows junctions as external directory links, when supported."""

    checker = getattr(path, "is_junction", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except OSError:
        return True


def _prepare_workspace_entries(
    entries: Iterable[WorkspaceEntry], temp_dir: Path, *, freeze_files: bool = False,
    max_workspace_bytes: int = MAX_WORKSPACE_BYTES,
) -> list[WorkspaceEntry]:
    prepared: list[WorkspaceEntry] = []
    prepared_total = 0
    for index, item in enumerate(entries):
        if not item.sqlite_snapshot:
            frozen = (_freeze_workspace_file(item, temp_dir, index,
                                             max_file_bytes=max_workspace_bytes - prepared_total)
                      if freeze_files else item)
            prepared_total += frozen.size
            if prepared_total > max_workspace_bytes:
                raise AgentMoveError(f"workspace snapshots exceed the {max_workspace_bytes} byte limit")
            prepared.append(frozen)
            continue
        snapshot = temp_dir / f"sqlite-{index}.db"
        try:
            source_uri = item.source.resolve().as_uri() + "?mode=ro"
            with (
                closing(sqlite3.connect(source_uri, uri=True)) as source_db,
                closing(sqlite3.connect(snapshot)) as target_db,
            ):
                source_db.backup(target_db)
            prepared.append(
                WorkspaceEntry(
                    source=snapshot,
                    relative_path=item.relative_path,
                    mode=item.mode,
                    size=snapshot.stat().st_size,
                    sqlite_snapshot=True,
                    materialized_symlink=item.materialized_symlink,
                )
            )
        except (OSError, sqlite3.DatabaseError) as exc:
            try:
                with item.source.open("rb") as reader:
                    is_sqlite = reader.read(16) == b"SQLite format 3\x00"
            except OSError:
                is_sqlite = True
            if is_sqlite:
                raise AgentMoveError(
                    f"could not create a consistent SQLite snapshot for {item.relative_path!r}"
                ) from exc
            fallback = WorkspaceEntry(
                    source=item.source,
                    relative_path=item.relative_path,
                    mode=item.mode,
                    size=item.size,
                    sqlite_snapshot=False,
                    materialized_symlink=item.materialized_symlink,
                )
            prepared.append(_freeze_workspace_file(
                fallback, temp_dir, index, max_file_bytes=max_workspace_bytes - prepared_total
            ) if freeze_files else fallback)
        prepared_total += prepared[-1].size
        if prepared_total > max_workspace_bytes:
            raise AgentMoveError(f"workspace snapshots exceed the {max_workspace_bytes} byte limit")
    return prepared


def _freeze_workspace_file(item: WorkspaceEntry, temp_dir: Path, index: int, *,
                           max_file_bytes: int) -> WorkspaceEntry:
    """Capture one stable file; later source work cannot veto this clone.

    SQLite uses the backup API above. Ordinary files are checked across the
    copy, not against a second *live workspace* after the whole archive builds.
    """
    snapshot = temp_dir / f"file-{index}"

    def identity(info: os.stat_result) -> tuple[int, ...]:
        # On Windows, stat() and fstat() may disagree about whether ctime
        # denotes creation or metadata-change time. Compare ctime only within
        # the same API, not across these two representations.
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    for _attempt in range(3):
        before = item.source.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise AgentMoveError(f"clone snapshot source is no longer a regular file: {item.relative_path!r}")
        with item.source.open("rb") as reader, snapshot.open("wb") as writer:
            opened = os.fstat(reader.fileno())
            if identity(before) != identity(opened):
                continue
            copied = 0
            while chunk := reader.read(1024 * 1024):
                copied += len(chunk)
                if copied > max_file_bytes:
                    raise AgentMoveError(
                        f"clone snapshot file exceeds its remaining workspace limit "
                        f"of {max_file_bytes} bytes: {item.relative_path!r}"
                    )
                writer.write(chunk)
            after = os.fstat(reader.fileno())
        end = item.source.lstat()
        if (
            identity(opened) == identity(after) == identity(end)
            and opened.st_ctime_ns == after.st_ctime_ns
            and before.st_ctime_ns == end.st_ctime_ns
        ):
            return replace(item, source=snapshot, size=copied)
    raise AgentMoveError(f"could not capture a stable clone snapshot of {item.relative_path!r} after 3 attempts")


def _write_bytes(
    archive: zipfile.ZipFile,
    name: str,
    content: bytes,
    checksums: dict[str, str],
) -> None:
    _safe_member_name(name)
    archive.writestr(name, content)
    checksums[name] = hashlib.sha256(content).hexdigest()


def _write_json_member(
    archive: zipfile.ZipFile, name: str, value: Mapping[str, Any],
    checksums: dict[str, str] | None,
) -> None:
    _safe_member_name(name)
    digest = hashlib.sha256()
    with archive.open(name, "w", force_zip64=True) as writer:
        for chunk in _iter_json_bytes(value):
            writer.write(chunk)
            digest.update(chunk)
    if checksums is not None:
        checksums[name] = digest.hexdigest()


def _write_file(
    archive: zipfile.ZipFile,
    name: str,
    source: Path,
    checksums: dict[str, str],
    *,
    mode: int,
) -> None:
    _safe_member_name(name)
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (int(mode) & 0xFFFF) << 16
    digest = hashlib.sha256()
    with (
        source.open("rb") as reader,
        archive.open(info, "w", force_zip64=True) as writer,
    ):
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
            digest.update(chunk)
    checksums[name] = digest.hexdigest()


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("package_type") != PACKAGE_TYPE:
        raise AgentMoveError("package_type is not a HASHI Agent move archive")
    schema = manifest.get("schema_version")
    if (
        not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema not in range(
            PACKAGE_SCHEMA_MIN_VERSION,
            PACKAGE_SCHEMA_VERSION + 1,
        )
    ):
        raise AgentMoveError(
            f"unsupported Agent move schema {schema!r}; this receiver accepts "
            f"schemas {PACKAGE_SCHEMA_MIN_VERSION} through {PACKAGE_SCHEMA_VERSION}"
        )
    normalize_agent_id(str(manifest.get("agent_id") or ""))
    _validate_package_id(str(manifest.get("package_id") or ""))
    if not str(manifest.get("source_instance") or "").strip():
        raise AgentMoveError("manifest source_instance is required")
    required = manifest.get("required_receiver_capabilities")
    if not isinstance(required, list) or AGENT_MOVE_CAPABILITY not in required:
        raise AgentMoveError("manifest does not require the agent-move-v1 receiver")
    if "desired_target_active" in manifest:
        operation = manifest.get("operation")
        capability = (
            INACTIVE_CLONE_BACKUP_CAPABILITY if operation == "clone" else INACTIVE_MOVE_CAPABILITY
        )
        if (manifest.get("desired_target_active") is not False or schema < 3
                or operation not in {"move", "clone"} or capability not in required):
            raise AgentMoveError("inactive target policy is invalid or unsupported")
    if "retain_telegram_backup" in manifest:
        if (manifest.get("retain_telegram_backup") is not True
                or manifest.get("operation") != "clone"
                or manifest.get("desired_target_active") is not False
                or INACTIVE_CLONE_BACKUP_CAPABILITY not in required):
            raise AgentMoveError("Telegram backup requires an explicitly inactive clone")
    sections = manifest.get("sections")
    if not isinstance(sections, Mapping):
        raise AgentMoveError("manifest sections must be an object")
    if schema == 2:
        if RETAINED_IDENTITY_CAPABILITY not in required:
            raise AgentMoveError(
                "schema 2 manifest does not require retained AGENT.md support"
            )
        if sections.get("retained_identity") is not True:
            raise AgentMoveError(
                "schema 2 manifest must declare the retained identity attachment"
            )
        if sections.get("agent_capability") not in {None, False}:
            raise AgentMoveError(
                "schema 2 manifest cannot declare an Agent capability attachment"
            )
    elif schema == 1:
        if sections.get("retained_identity") not in {None, False}:
            raise AgentMoveError(
                "schema 1 manifest cannot declare a retained identity attachment"
            )
        if sections.get("agent_capability") not in {None, False}:
            raise AgentMoveError(
                "schema 1 manifest cannot declare an Agent capability attachment"
            )
    elif schema >= 3:
        if str(manifest.get("operation") or "").strip().lower() not in {
            "move",
            "clone",
        }:
            raise AgentMoveError("schema 3 manifest operation is invalid")
        if AGENT_TRANSFER_LIFECYCLE_CAPABILITY not in required:
            raise AgentMoveError(
                "schema 3 manifest does not require transfer lifecycle support"
            )
        retained = sections.get("retained_identity") is True
        if retained and RETAINED_IDENTITY_CAPABILITY not in required:
            raise AgentMoveError(
                "schema 3 retained identity does not require receiver support"
            )
        if not retained and RETAINED_IDENTITY_CAPABILITY in required:
            raise AgentMoveError(
                "schema 3 declares retained identity support without an attachment"
            )
        if not isinstance(sections.get("agent_capability"), bool):
            raise AgentMoveError(
                "schema 3 must declare whether Agent capabilities are attached"
            )
        if schema >= 5:
            if not isinstance(sections.get("conversation_continuity"), bool):
                raise AgentMoveError(
                    "schema 5 must declare whether conversation continuity is attached"
                )
            if CONVERSATION_CONTINUITY_CAPABILITY not in required:
                raise AgentMoveError(
                    "schema 5 manifest does not require conversation continuity support"
                )


def _read_and_validate_agent_capability(
    archive: zipfile.ZipFile,
    manifest: Mapping[str, Any],
    names: Iterable[str],
) -> dict[str, Any] | None:
    present = AGENT_CAPABILITY_ARCHIVE_PATH in set(names)
    declared = bool((manifest.get("sections") or {}).get("agent_capability"))
    schema = int(manifest.get("schema_version") or 0)
    if schema < 3:
        if present:
            raise AgentMoveError(
                "legacy Agent move packages cannot contain capability declarations"
            )
        return None
    if present != declared:
        raise AgentMoveError(
            "Agent capability attachment does not match the manifest"
        )
    if not present:
        return None
    capability = _read_json(archive, AGENT_CAPABILITY_ARCHIVE_PATH)
    identity = str(capability.get("name") or capability.get("id") or "")
    if identity != str(manifest.get("agent_id") or ""):
        raise AgentMoveError(
            "Agent capability attachment does not match the packaged Agent"
        )
    return capability


def _read_and_validate_retained_identity(
    archive: zipfile.ZipFile,
    manifest: Mapping[str, Any],
    names: Iterable[str],
) -> dict[str, Any] | None:
    retained_members = [
        name
        for name in names
        if name.startswith("retained-identity/")
        or name == RETAINED_IDENTITY_METADATA_PATH
    ]
    schema = int(manifest.get("schema_version") or 0)
    if schema == 1:
        if retained_members:
            raise AgentMoveError(
                "schema 1 package cannot contain a retained identity attachment"
            )
        return None
    if schema >= 3 and not bool(
        (manifest.get("sections") or {}).get("retained_identity")
    ):
        if retained_members:
            raise AgentMoveError(
                "schema 3 package declares no retained identity attachment"
            )
        return None
    expected_members = {
        RETAINED_IDENTITY_ARCHIVE_PATH,
        RETAINED_IDENTITY_METADATA_PATH,
    }
    if set(retained_members) != expected_members:
        raise AgentMoveError(
            "schema 2 retained identity attachment is incomplete or contains unknown members"
        )
    metadata = _read_json(archive, RETAINED_IDENTITY_METADATA_PATH)
    content = _read_required(archive, RETAINED_IDENTITY_ARCHIVE_PATH)
    expected_keys = {
        "authoritative",
        "original_path",
        "archive_path",
        "sha256",
        "size",
    }
    if set(metadata) != expected_keys:
        raise AgentMoveError("retained identity metadata fields are invalid")
    digest = hashlib.sha256(content).hexdigest()
    if (
        metadata.get("authoritative") is not False
        or metadata.get("original_path") != "AGENT.md"
        or metadata.get("archive_path") != RETAINED_IDENTITY_ARCHIVE_PATH
        or metadata.get("sha256") != digest
        or not isinstance(metadata.get("size"), int)
        or metadata.get("size") != len(content)
    ):
        raise AgentMoveError("retained identity metadata does not match AGENT.md")
    return dict(metadata)


def _validate_package_id(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]{8,80}", str(value or "")):
        raise AgentMoveError("invalid Agent move package_id")


def _safe_member_name(name: str) -> str:
    raw = str(name or "")
    if "\\" in raw:
        raise AgentMoveError(f"archive member must use POSIX separators: {raw!r}")
    pure = PurePosixPath(raw)
    if (
        not raw
        or raw.startswith("/")
        or raw.endswith("/")
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise AgentMoveError(f"unsafe archive member: {raw!r}")
    return raw


def _validate_workspace_members(names: Iterable[str], target_platform: str) -> None:
    workspace_names = [
        name[len("workspace/") :] for name in names if name.startswith("workspace/")
    ]
    folded: dict[str, str] = {}
    windows = str(target_platform or "").lower() == "windows"
    for relative in workspace_names:
        _safe_member_name(relative)
        if PurePosixPath(relative).parts[0].casefold() == PCM_FILENAME.casefold():
            raise AgentMoveError(
                "workspace Agent identity aliases are reserved for control members"
            )
        if not windows:
            continue
        key = "/".join(
            unicodedata.normalize("NFC", component).casefold()
            for component in PurePosixPath(relative).parts
        )
        previous = folded.get(key)
        if previous is not None and previous != relative:
            raise AgentMoveError(
                f"Windows case-insensitive path collision: {previous!r} and {relative!r}"
            )
        folded[key] = relative
        for component in PurePosixPath(relative).parts:
            _validate_windows_component(component, context=relative)


def _validate_control_member_sizes(infos: Iterable[zipfile.ZipInfo]) -> None:
    by_name = {info.filename: info for info in infos}
    for name, limit in _CONTROL_MEMBER_LIMITS.items():
        info = by_name.get(name)
        if info is not None and int(info.file_size) > limit:
            raise AgentMoveError(
                f"Agent move control member exceeds its size limit: {name}"
            )


def _validate_workspace_metadata(
    workspace_metadata: Mapping[str, Any], names: Iterable[str]
) -> None:
    raw_files = workspace_metadata.get("files")
    if not isinstance(raw_files, list):
        raise AgentMoveError("workspace metadata files must be a list")
    metadata_paths: list[str] = []
    for item in raw_files:
        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
            raise AgentMoveError("workspace metadata file entries must contain a path")
        path = _safe_member_name(str(item["path"]))
        if PurePosixPath(path).parts[0].casefold() == PCM_FILENAME.casefold():
            raise AgentMoveError(
                "workspace Agent identity aliases are reserved for control members"
            )
        metadata_paths.append(path)
    if len(metadata_paths) != len(set(metadata_paths)):
        raise AgentMoveError("workspace metadata contains duplicate file paths")
    archive_paths = [
        name[len("workspace/") :]
        for name in names
        if name.startswith("workspace/")
    ]
    if set(metadata_paths) != set(archive_paths):
        raise AgentMoveError(
            "workspace metadata does not match the archived workspace files"
        )


def _validate_windows_component(component: str, *, context: str) -> None:
    if component.endswith((" ", ".")):
        raise AgentMoveError(f"Windows-incompatible trailing character in {context!r}")
    if any(char in _WINDOWS_INVALID_CHARS or ord(char) < 32 for char in component):
        raise AgentMoveError(f"Windows-incompatible filename in {context!r}")
    if len(component.encode("utf-16-le")) // 2 > 255:
        raise AgentMoveError(f"Windows filename component is too long in {context!r}")
    stem = component.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise AgentMoveError(f"Windows reserved filename in {context!r}")


def _verify_checksums(archive: zipfile.ZipFile, expected: Mapping[str, Any]) -> None:
    actual: dict[str, str] = {}
    for name in archive.namelist():
        if name == "checksums.json":
            continue
        digest = hashlib.sha256()
        with archive.open(name, "r") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual[name] = digest.hexdigest()
    normalized = {str(key): str(value) for key, value in expected.items()}
    if actual != normalized:
        missing = sorted(set(normalized) - set(actual))
        extra = sorted(set(actual) - set(normalized))
        changed = sorted(
            key
            for key in set(actual) & set(normalized)
            if actual[key] != normalized[key]
        )
        raise AgentMoveError(
            f"Agent move checksum mismatch missing={missing} extra={extra} changed={changed}"
        )


def _read_required(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except KeyError as exc:
        raise AgentMoveError(f"required archive member is missing: {name}") from exc


def _read_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    raw = _read_required(archive, name)
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise AgentMoveError(f"invalid JSON archive member: {name}") from exc
    if not isinstance(value, dict):
        raise AgentMoveError(f"JSON archive member must be an object: {name}")
    return value


def _load_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise AgentMoveError(f"invalid JSON file: {path.name}") from exc


def _iter_json_bytes(value: Mapping[str, Any]) -> Iterable[bytes]:
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2, sort_keys=True)
    for fragment in encoder.iterencode(value):
        for offset in range(0, len(fragment), 16 * 1024):
            yield fragment[offset:offset + 16 * 1024].encode("utf-8")
    yield b"\n"


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _derive_fernet_key(passphrase: str, salt: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as exc:
        raise AgentMoveError(
            "cryptography is required for encrypted Agent credentials"
        ) from exc
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _encrypt_json(value: Mapping[str, Any], passphrase: str) -> bytes:
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    key = _derive_fernet_key(passphrase, salt)
    return salt + Fernet(key).encrypt(_json_bytes(value))


def _decrypt_json(payload: bytes, passphrase: str) -> dict[str, Any]:
    from cryptography.fernet import Fernet, InvalidToken

    if len(payload) < 17:
        raise AgentMoveError("encrypted Agent credential payload is truncated")
    salt, token = payload[:16], payload[16:]
    try:
        raw = Fernet(_derive_fernet_key(passphrase, salt)).decrypt(token)
        value = json.loads(raw.decode("utf-8"))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentMoveError("Agent credential decryption failed") from exc
    if not isinstance(value, dict):
        raise AgentMoveError("decrypted Agent credentials must be an object")
    return value


def _apply_mode(path: Path, mode: int, target_platform: str) -> None:
    if str(target_platform or "").lower() == "windows":
        return
    try:
        path.chmod(int(mode) & 0o777)
    except OSError:
        pass
# HASHI-Fingerprint: hfp_50ee4c20cff0470abcff26b8567c4f6b
