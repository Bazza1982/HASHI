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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from orchestrator.pcm import parse_pcm_text
from orchestrator.process_execution import is_wsl

PACKAGE_TYPE = "hashi-agent-move"
PACKAGE_SCHEMA_VERSION = 1
AGENT_MOVE_CAPABILITY = "agent_move_receive_v1"
PACKAGE_EXTENSION = ".hashi-agent"
MAX_ARCHIVE_MEMBERS = 100_000
MAX_UNPACKED_BYTES = 2 * 1024 * 1024 * 1024
MAX_WORKSPACE_BYTES = MAX_UNPACKED_BYTES - (16 * 1024 * 1024)
_CONTROL_MEMBER_LIMITS = {
    "manifest.json": 1024 * 1024,
    "identity/agent.json": 4 * 1024 * 1024,
    "identity/agent.md": 16 * 1024 * 1024,
    "access/requirements.json": 4 * 1024 * 1024,
    "schedules/tasks.json": 32 * 1024 * 1024,
    "metadata/workspace.json": 64 * 1024 * 1024,
    "secrets/agent.enc": 16 * 1024 * 1024,
    "checksums.json": 64 * 1024 * 1024,
}

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
) -> AgentMoveArchive:
    """Create one checksummed Agent move archive.

    ``secret_passphrase`` is normally the paired HASHI Remote shared token.
    Secret values are never emitted unless encryption is available and a
    passphrase is supplied.
    """

    root = Path(hashi_root).expanduser().resolve()
    name = normalize_agent_id(agent_id)
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
    pcm_path = workspace / "agent.md"
    if not pcm_path.is_file():
        raise AgentMoveError(f"canonical agent.md is missing for Agent '{name}'")
    pcm_text = pcm_path.read_text(encoding="utf-8")
    try:
        parse_pcm_text(pcm_text, path=pcm_path)
    except ValueError as exc:
        raise AgentMoveError(f"canonical agent.md is invalid: {exc}") from exc

    agent_config, config_warnings = _portable_agent_config(raw_config, name)
    schedules = _collect_schedules(root, name)
    secret_values, required_secret_keys = _collect_agent_secrets(root, raw_config, name)
    if include_agent_secrets and secret_values and not secret_passphrase:
        raise AgentMoveError(
            "an encryption passphrase is required to include Agent credentials"
        )
    encrypted_secrets = (
        _encrypt_json(secret_values, str(secret_passphrase))
        if include_agent_secrets and secret_values
        else None
    )

    entries: list[WorkspaceEntry] = []
    exclusions: list[dict[str, str]] = []
    if include_workspace:
        entries, exclusions = _scan_workspace(
            workspace,
            max_workspace_bytes=MAX_WORKSPACE_BYTES,
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
    manifest = {
        "package_type": PACKAGE_TYPE,
        "schema_version": PACKAGE_SCHEMA_VERSION,
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
            "workspace": bool(include_workspace),
            "memory": True,
            "schedules": any(
                schedules.get(section) for section in ("heartbeats", "crons", "nudges")
            ),
            "access_requirements": True,
            "encrypted_agent_secrets": bool(encrypted_secrets),
        },
        "required_receiver_capabilities": [AGENT_MOVE_CAPABILITY],
        "compatibility_floor": "agent-move-v1",
        "warnings": config_warnings,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{package_key}.tmp")
    temporary.unlink(missing_ok=True)
    checksums: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="hashi-agent-move-") as temp_name:
        temp_dir = Path(temp_name)
        prepared_entries = _prepare_workspace_entries(entries, temp_dir)
        prepared_bytes = sum(item.size for item in prepared_entries)
        if prepared_bytes > MAX_WORKSPACE_BYTES:
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
        schedules = _read_json(archive, "schedules/tasks.json")
        workspace_metadata = _read_json(archive, "metadata/workspace.json")
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

    return AgentMoveArchive(
        package_path=path,
        manifest=manifest,
        agent_config=agent_config,
        access_requirements=access,
        schedules=schedules,
        workspace_metadata=workspace_metadata,
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

    Creation timestamps, randomized encryption, and exclusion diagnostics are
    intentionally omitted. File contents, portable modes, Agent configuration,
    schedules, access requirements, and Agent-owned credential values remain
    covered so a staged move cannot silently cut over from a stale snapshot.
    """

    stable_control = {
        "identity/agent.json",
        "identity/agent.md",
        "access/requirements.json",
        "schedules/tasks.json",
    }
    file_checksums = {
        name: digest
        for name, digest in package.checksums.items()
        if name in stable_control or name.startswith("workspace/")
    }
    portable_files = []
    for item in package.workspace_metadata.get("files", []) or []:
        if not isinstance(item, Mapping):
            continue
        portable_files.append(
            {
                "path": str(item.get("path") or ""),
                "mode": int(item.get("mode") or 0),
                "sqlite_snapshot": bool(item.get("sqlite_snapshot")),
                "materialized_symlink": bool(item.get("materialized_symlink")),
            }
        )
    credentials = decrypt_agent_secrets(package, secret_passphrase)
    payload = {
        "schema_version": 1,
        "file_checksums": dict(sorted(file_checksums.items())),
        "portable_files": sorted(portable_files, key=lambda item: item["path"]),
        "agent_credentials": credentials,
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
) -> tuple[dict[str, Any], list[str]]:
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
    return values, required


def _scan_workspace(
    workspace: Path,
    *,
    max_workspace_bytes: int,
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
            if normalized_directory in _SKIP_DIR_NAMES:
                reason = "ephemeral_or_environment_directory"
            elif child.is_symlink() or _is_windows_junction(child):
                reason = "directory_symlink_not_portable"
            elif (child / ".git").exists():
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
                sqlite3.connect(source_uri, uri=True) as source_db,
                sqlite3.connect(snapshot) as target_db,
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
                is_sqlite = item.source.read_bytes()[:16] == b"SQLite format 3\x00"
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
    if manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION:
        raise AgentMoveError(
            f"unsupported Agent move schema {manifest.get('schema_version')!r}; "
            f"this receiver accepts schema {PACKAGE_SCHEMA_VERSION}"
        )
    normalize_agent_id(str(manifest.get("agent_id") or ""))
    _validate_package_id(str(manifest.get("package_id") or ""))
    if not str(manifest.get("source_instance") or "").strip():
        raise AgentMoveError("manifest source_instance is required")
    required = manifest.get("required_receiver_capabilities")
    if not isinstance(required, list) or AGENT_MOVE_CAPABILITY not in required:
        raise AgentMoveError("manifest does not require the agent-move-v1 receiver")


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
        if relative.casefold() == "agent.md":
            raise AgentMoveError(
                "workspace/agent.md is reserved for the canonical Agent identity"
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
        if path.casefold() == "agent.md":
            raise AgentMoveError(
                "workspace/agent.md is reserved for the canonical Agent identity"
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
