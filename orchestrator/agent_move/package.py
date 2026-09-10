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
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from orchestrator.pcm_transfer import is_portable_memory_path
from orchestrator.pcm import (
    PCM_FILENAME,
    PCMValidationError,
    canonical_agent_md,
    load_pcm_document,
    parse_pcm_text,
)
from orchestrator.process_execution import is_wsl

PACKAGE_TYPE = "hashi-agent-move"
PACKAGE_SCHEMA_MIN_VERSION = 1
PACKAGE_SCHEMA_VERSION = 4
TRANSFER_MODES_CAPABILITY = "agent_transfer_modes_v1"
WORKSPACE_LIMIT_BYTES = 1_000_000_000
TRANSFER_MODES = {"identity_memory", "workspace"}
AGENT_MOVE_CAPABILITY = "agent_move_receive_v1"
RETAINED_IDENTITY_CAPABILITY = "agent_move_retained_identity_v1"
AGENT_TRANSFER_LIFECYCLE_CAPABILITY = "agent_transfer_lifecycle_v1"
PACKAGE_EXTENSION = ".hashi-agent"
RETAINED_IDENTITY_ARCHIVE_PATH = "retained-identity/AGENT.md"
RETAINED_IDENTITY_METADATA_PATH = "metadata/retained-identity.json"
AGENT_CAPABILITY_ARCHIVE_PATH = "identity/capability.json"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_WORKSPACE_BYTES = MAX_UNPACKED_BYTES - (16 * 1024 * 1024)
_CONTROL_MEMBER_LIMITS = {
    "manifest.json": 1024 * 1024,
    "identity/agent.json": 4 * 1024 * 1024,
    "identity/agent.md": 16 * 1024 * 1024,
    AGENT_CAPABILITY_ARCHIVE_PATH: 4 * 1024 * 1024,
    RETAINED_IDENTITY_ARCHIVE_PATH: 16 * 1024 * 1024,
    RETAINED_IDENTITY_METADATA_PATH: 1024 * 1024,
    "access/requirements.json": 4 * 1024 * 1024,
    "schedules/tasks.json": 32 * 1024 * 1024,
    "metadata/workspace.json": 64 * 1024 * 1024,
    "secrets/agent.enc": 16 * 1024 * 1024,
    "checksums.json": 64 * 1024 * 1024,
}

MAX_TRANSFER_PACKAGE_BYTES = WORKSPACE_LIMIT_BYTES + sum(_CONTROL_MEMBER_LIMITS.values()) + MAX_ARCHIVE_MEMBERS * 4096

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
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
    checksums: dict[str, str]
    names: tuple[str, ...]

    @property
    def package_id(self) -> str:
        return str(self.manifest["package_id"])

    @property
    def agent_id(self) -> str:
        return str(self.manifest["agent_id"])


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_agent_id(agent_id: str) -> str:
    value = str(agent_id or "").strip()
    if not _AGENT_ID_RE.fullmatch(value):
        raise AgentMoveError(
            "agent_id must begin with an alphanumeric character and contain "
            "only letters, digits, dots, underscores, or hyphens"
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
    operation: str = "move",
    include_telegram_secret: bool = True,
    schema_version: int | None = None,
    transfer_mode: str | None = None,
) -> AgentMoveArchive:
    """Create one checksummed Agent move archive.

    ``secret_passphrase`` is normally the paired HASHI Remote shared token.
    Secret values are never emitted unless encryption is available and a
    passphrase is supplied.
    """

    root = Path(hashi_root).expanduser().resolve()
    name = normalize_agent_id(agent_id)
    transfer_operation = str(operation or "").strip().lower()
    if transfer_operation not in {"move", "clone"}:
        raise AgentMoveError("Agent transfer operation must be 'move' or 'clone'")
    package_key = str(package_id or uuid4())
    _validate_package_id(package_key)
    output = Path(output_path).expanduser().resolve()

    raw_config = _find_agent_config(root, name)
    if raw_config.get("is_active", True) is False:
        raise AgentMoveError(
            f"source Agent '{name}' is inactive; activate and verify the intended "
            "source copy before moving it"
        )
    if raw_config.get("transfer_state") == "moved_out_pending_reboot":
        raise AgentMoveError(
            f"source Agent '{name}' has already moved to another instance"
        )
    workspace = _resolve_workspace(root, raw_config, name)
    if transfer_mode is not None and transfer_mode not in TRANSFER_MODES:
        raise AgentMoveError("unknown Agent transfer mode")
    inventory = _workspace_inventory(workspace) if transfer_mode else []
    total_workspace_bytes = sum(item["size"] for item in inventory)
    if transfer_mode == "workspace" and total_workspace_bytes > WORKSPACE_LIMIT_BYTES:
        raise AgentMoveError(
            f"Workspace is {total_workspace_bytes:,} bytes; full move limit is 1 GB "
            "(1,000,000,000 bytes). Move large projects outside the Agent workspace or clean it first.")
    retained_identity_bytes, retained_identity = (
        (None, None) if transfer_mode == "identity_memory"
        else _read_retained_identity(workspace, name)
    )
    pcm_text = _read_canonical_pcm(workspace, name)
    requested_schema = int(schema_version) if schema_version is not None else (4 if transfer_mode else 3)
    if transfer_mode and requested_schema < 4:
        raise AgentMoveError("explicit transfer modes require schema 4")
    if requested_schema not in range(PACKAGE_SCHEMA_MIN_VERSION, PACKAGE_SCHEMA_VERSION + 1):
        raise AgentMoveError(f"unsupported requested Agent package schema {requested_schema}")
    if retained_identity is not None and requested_schema == 1:
        raise AgentMoveError("schema 1 cannot preserve retained root AGENT.md")
    agent_capability = (
        _collect_agent_capability(root, name) if requested_schema >= 3 else None
    )
    package_schema = requested_schema

    agent_config, config_warnings = _portable_agent_config(raw_config, name)
    schedules = _collect_schedules(root, name)
    secret_values, required_secret_keys, telegram_secret_key = _collect_agent_secrets(
        root, raw_config, name
    )
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
        entries, exclusions = _scan_workspace(
            workspace,
            max_workspace_bytes=WORKSPACE_LIMIT_BYTES if transfer_mode else MAX_WORKSPACE_BYTES,
            transfer_mode=transfer_mode,
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
                    include_telegram_secret and telegram_secret_key in secret_values
                ),
                "operation": transfer_operation,
            }
        )
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
            *config_warnings,
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

    if transfer_mode:
        manifest["transfer_mode"] = transfer_mode
        manifest["required_receiver_capabilities"].append(TRANSFER_MODES_CAPABILITY)

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{package_key}.tmp")
    temporary.unlink(missing_ok=True)
    checksums: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="hashi-agent-move-") as temp_name:
        temp_dir = Path(temp_name)
        prepared_entries = _prepare_workspace_entries(entries, temp_dir)
        prepared_bytes = sum(item.size for item in prepared_entries)
        snapshot_growth = prepared_bytes - sum(item.size for item in entries)
        if transfer_mode == "workspace" and total_workspace_bytes + snapshot_growth > WORKSPACE_LIMIT_BYTES:
            raise AgentMoveError("consistent workspace snapshots exceed the 1 GB full move limit")
        if prepared_bytes > (WORKSPACE_LIMIT_BYTES if transfer_mode else MAX_WORKSPACE_BYTES):
            raise AgentMoveError(
                "consistent Agent workspace snapshots exceed the portable move "
                f"limit of {MAX_WORKSPACE_BYTES} bytes"
            )
        try:
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                _write_bytes(archive, "manifest.json", _json_bytes(manifest), checksums)
                _write_bytes(
                    archive, "identity/agent.json", _json_bytes(agent_config), checksums
                )
                _write_bytes(
                    archive, "identity/agent.md", pcm_text.encode("utf-8"), checksums
                )
                if agent_capability is not None:
                    _write_bytes(
                        archive,
                        AGENT_CAPABILITY_ARCHIVE_PATH,
                        _json_bytes(agent_capability),
                        checksums,
                    )
                if retained_identity_bytes is not None and retained_identity is not None:
                    _write_bytes(
                        archive,
                        RETAINED_IDENTITY_ARCHIVE_PATH,
                        retained_identity_bytes,
                        checksums,
                    )
                    _write_bytes(
                        archive,
                        RETAINED_IDENTITY_METADATA_PATH,
                        _json_bytes(retained_identity),
                        checksums,
                    )
                _write_bytes(
                    archive,
                    "access/requirements.json",
                    _json_bytes(access_requirements),
                    checksums,
                )
                _write_bytes(
                    archive, "schedules/tasks.json", _json_bytes(schedules), checksums
                )
                _write_bytes(
                    archive,
                    "metadata/workspace.json",
                    _json_bytes(workspace_metadata),
                    checksums,
                )
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
                archive.writestr(
                    "checksums.json",
                    _json_bytes(
                        {"schema_version": 1, "files": dict(sorted(checksums.items()))}
                    ),
                )
            if max_package_bytes is not None and temporary.stat().st_size > int(
                max_package_bytes
            ):
                raise AgentMoveError(
                    f"Agent move package is {temporary.stat().st_size} bytes; "
                    f"receiver limit is {int(max_package_bytes)} bytes"
                )
            if os.name != "nt":
                temporary.chmod(0o600)
            # Validate the completed archive before replacing a caller's
            # existing output. This also catches files that grew after the
            # initial workspace scan.
            read_agent_move_package(temporary, verify=True)
            if transfer_mode:
                final_inventory = _workspace_inventory(workspace)
                if final_inventory != inventory:
                    raise AgentMoveError("workspace changed while packaging; prepare a fresh transfer")
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    return read_agent_move_package(output, verify=True)


def read_agent_move_package(
    package_path: Path | str,
    *,
    verify: bool = True,
    target_platform: str | None = None,
) -> AgentMoveArchive:
    path = Path(package_path).expanduser().resolve()
    if not path.is_file():
        raise AgentMoveError(f"Agent move package not found: {path}")
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise AgentMoveError("invalid Agent move archive") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise AgentMoveError("Agent move archive contains too many files")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise AgentMoveError("Agent move archive contains duplicate member names")
        for name in names:
            _safe_member_name(name)
        _validate_control_member_sizes(infos)
        unpacked = sum(max(0, int(info.file_size)) for info in infos)
        if unpacked > MAX_UNPACKED_BYTES:
            raise AgentMoveError("Agent move archive expands beyond the supported size")

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
            if operation == "clone" and access.get("telegram_secret_included"):
                raise AgentMoveError("clone package declares a Telegram credential")
        schedules = _read_json(archive, "schedules/tasks.json")
        workspace_metadata = _read_json(archive, "metadata/workspace.json")
        if int(manifest.get("schema_version") or 0) >= 4:
            mode = manifest.get("transfer_mode")
            if mode not in TRANSFER_MODES or workspace_metadata.get("transfer_mode") != mode:
                raise AgentMoveError("package transfer mode is invalid or inconsistent")
            if TRANSFER_MODES_CAPABILITY not in manifest.get("required_receiver_capabilities", []):
                raise AgentMoveError("package omits explicit transfer mode capability")
            actual_bytes = sum(info.file_size for info in infos if info.filename.startswith("workspace/") or info.filename == "identity/agent.md")
            if actual_bytes > WORKSPACE_LIMIT_BYTES:
                raise AgentMoveError("Agent transfer payload exceeds the 1 GB limit")
            if mode == "workspace" and int(workspace_metadata.get("total_workspace_bytes", -1)) not in range(WORKSPACE_LIMIT_BYTES + 1):
                raise AgentMoveError("workspace inventory exceeds the 1 GB limit")
            if mode == "identity_memory" and any(name.startswith("workspace/") and not is_portable_memory_path(name[len("workspace/"):]) for name in names):
                raise AgentMoveError("identity-memory package contains files outside PCM scope")

        pcm_text = _read_required(archive, "identity/agent.md").decode("utf-8")
        try:
            parse_pcm_text(pcm_text, path=Path("agent.md"))
        except ValueError as exc:
            raise AgentMoveError(f"packaged agent.md is invalid: {exc}") from exc
        target_kind = target_platform or detect_environment_kind()
        if str(target_kind).lower() == "windows":
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
        checksums={str(k): str(v) for k, v in checksums.items()},
        names=tuple(names),
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


def archive_snapshot_fingerprint(
    package: AgentMoveArchive,
    *,
    secret_passphrase: str | None = None,
) -> str:
    """Return a stable digest of the durable source state in an archive.

    Creation timestamps, randomized encryption, exclusion diagnostics, and the
    append-only slash-command audit are intentionally omitted.  The audit file
    remains in the package; it alone is excluded from freshness because the
    move confirmation callback appends its own record. File contents, portable
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
        "deletion_inventory": [item for item in package.workspace_metadata.get("inventory", [])
                               if str(item.get("path", "")).casefold() not in _FRESHNESS_EXCLUDED_WORKSPACE_PATHS],
    }
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


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
    if not path.is_file():
        return {"version": 1, "heartbeats": [], "crons": [], "nudges": []}
    data = _load_json_file(path)
    result: dict[str, Any] = {
        "version": data.get("version", 1) if isinstance(data, dict) else 1
    }
    for section in ("heartbeats", "crons", "nudges"):
        rows = data.get(section, []) if isinstance(data, dict) else []
        selected = []
        for row in rows:
            if not isinstance(row, dict) or row.get("agent") != agent_id:
                continue
            item = dict(row)
            item["enabled"] = False
            item["import_state"] = "disabled_review_draft"
            selected.append(item)
        result[section] = selected
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


def _workspace_inventory(workspace: Path) -> list[dict[str, Any]]:
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
            if len(entries) > MAX_ARCHIVE_MEMBERS:
                raise AgentMoveError("workspace inventory exceeds the file-count limit")
    return sorted(entries, key=lambda item: item["path"])


def _scan_workspace(
    workspace: Path,
    *,
    max_workspace_bytes: int,
    transfer_mode: str | None = None,
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
            normalized_directory = directory.casefold()
            reason = ""
            if normalized_directory == PCM_FILENAME.casefold() and (not transfer_mode or current_path == workspace):
                raise AgentMoveError(
                    f"workspace/{relative} conflicts with the reserved Agent identity; "
                    "only exact root AGENT.md may be retained as a non-authoritative attachment"
                )
            if normalized_directory in _SKIP_DIR_NAMES and (
                not transfer_mode or current_path == workspace
                or normalized_directory not in {"state", "tmp", "undelivered", "backend_state"}
            ):
                reason = "ephemeral_or_environment_directory"
            elif child.is_symlink() or _is_windows_junction(child):
                reason = "directory_symlink_not_portable"
            elif not transfer_mode and (child / ".git").exists():
                reason = "nested_project_not_agent_state"
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
            is_environment_secret = normalized_filename.startswith(
                ".env."
            ) and not normalized_filename.endswith((".example", ".sample", ".template"))
            is_credential_material = (
                normalized_filename
                in {
                    ".env",
                    ".git-credentials",
                    ".netrc",
                    ".npmrc",
                    ".pypirc",
                    "auth.json",
                    "credentials.json",
                    "id_ed25519",
                    "id_rsa",
                    "oauth.json",
                    "secrets.json",
                    "service-account.json",
                    "token.json",
                }
                or is_environment_secret
                or source.suffix.lower() in _SECRET_FILE_SUFFIXES
            )
            if (
                normalized_filename in _SKIP_FILE_NAMES
                or is_credential_material
                or any(
                    normalized_filename.endswith(suffix) for suffix in _SKIP_SUFFIXES
                )
            ):
                reason = (
                    "credential_material_requires_encrypted_rebind"
                    if is_credential_material
                    else "ephemeral_runtime_file"
                )
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
    entries: Iterable[WorkspaceEntry], temp_dir: Path
) -> list[WorkspaceEntry]:
    prepared: list[WorkspaceEntry] = []
    for index, item in enumerate(entries):
        if not item.sqlite_snapshot:
            prepared.append(item)
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
            prepared.append(
                WorkspaceEntry(
                    source=item.source,
                    relative_path=item.relative_path,
                    mode=item.mode,
                    size=item.size,
                    sqlite_snapshot=False,
                    materialized_symlink=item.materialized_symlink,
                )
            )
    return prepared


def _write_bytes(
    archive: zipfile.ZipFile,
    name: str,
    content: bytes,
    checksums: dict[str, str],
) -> None:
    _safe_member_name(name)
    archive.writestr(name, content)
    checksums[name] = hashlib.sha256(content).hexdigest()


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
