from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import logging
import os
from pathlib import Path
import stat
from typing import Any
import uuid

from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDocument,
    ConfigDurabilityError,
    file_write_lock,
    read_config_json,
    write_config_json,
)

from orchestrator.pathing import BridgePaths
from orchestrator.pcm import atomic_write_pcm, render_pcm_document
from orchestrator.config import default_agent_mode_for_backend
from orchestrator.agent_incarnation import (
    AGENT_LIFECYCLE_FIELD,
    new_agent_lifecycle_id,
)

logger = logging.getLogger("BridgeU.ConfigAdmin")


class AgentWorkspaceConflictError(RuntimeError):
    """A normal create found a workspace that it does not own."""


@dataclass(frozen=True)
class AgentScaffoldReceipt:
    created: bool
    config_revision: str | None
    durability_confirmed: bool


_SCAFFOLD_OWNER_MARKER = ".hashi-agent-create-owner"


def _agent_rows(document: dict) -> list[dict]:
    rows = document.get("agents", [])
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("name"), str)
        for row in rows
    ):
        raise ValueError("cannot establish Agent ownership from malformed config")
    return rows


def _agent_name_collides(rows: list[dict], name: str) -> bool:
    normalize = str.casefold if os.name == "nt" else lambda value: value
    wanted = normalize(name)
    return any(normalize(row["name"]) == wanted for row in rows)


def _agent_creation_lock_target(config_path: Path, name: str) -> Path:
    normalized_name = name.casefold() if os.name == "nt" else name
    name_digest = hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()[:24]
    return config_path.resolve().parent / f"agent-create-{name_digest}"


def _directory_identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("workspace is no longer the created directory")
    return info.st_dev, info.st_ino


def _regular_file_state(path: Path) -> tuple[tuple[int, int], str]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("scaffold file is not a single regular owned file")
    return (info.st_dev, info.st_ino), hashlib.sha256(path.read_bytes()).hexdigest()


def _write_owner_marker(path: Path, token: str) -> tuple[int, int]:
    with path.open("x", encoding="utf-8", newline="") as stream:
        stream.write(token)
        stream.flush()
        os.fsync(stream.fileno())
    return _regular_file_state(path)[0]


def _owned_marker_matches(
    path: Path,
    *,
    identity: tuple[int, int],
    token: str,
) -> bool:
    current_identity, _digest = _regular_file_state(path)
    return current_identity == identity and path.read_text(encoding="utf-8") == token


class ConfigAdmin:
    """Small wrapper for mutable agents.json operations."""

    def __init__(self, paths: BridgePaths):
        self.paths = paths

    def load_raw_config(self) -> dict:
        return read_config_json(self.paths.config_path)

    def write_raw_config(self, raw_cfg: dict) -> str:
        # Loaded documents carry their revision outside the JSON. A concurrent
        # change raises ConfigConflictError rather than losing another update.
        return write_config_json(self.paths.config_path, raw_cfg)

    def read_fingerprint_policy(self):
        """Management read; never materializes defaults into configuration."""
        from orchestrator.fingerprint.models import FingerprintPolicy, PolicySnapshot

        raw = self.load_raw_config()
        source = raw.get("global", {})
        if not isinstance(source, dict):
            raise ValueError("global settings must be an object")
        policy = FingerprintPolicy.from_mapping(source.get("fingerprint", {}))
        return PolicySnapshot(policy, raw.revision)

    def write_fingerprint_policy(self, policy, *, expected_revision: str, actor_id: str, audit):
        """Persist an already-authorized management decision, never a model tool."""
        from dataclasses import asdict
        from orchestrator.fingerprint.models import FingerprintPolicy, PolicySnapshot
        from orchestrator.fingerprint.service import FingerprintAudit, FingerprintUnavailable

        if not isinstance(policy, FingerprintPolicy) or not isinstance(actor_id, str) or not actor_id.strip():
            raise ValueError("validated policy and authorized management actor required")
        if not isinstance(expected_revision, str) or len(expected_revision) != 64:
            raise ValueError("configuration revision required")
        raw = self.load_raw_config()
        if raw.revision != expected_revision:
            raise ConfigConflictError("fingerprint settings changed; re-read before applying")
        source = raw.setdefault("global", {})
        if not isinstance(source, dict):
            raise ValueError("global settings must be an object")
        previous = FingerprintPolicy.from_mapping(source.get("fingerprint", {}))
        writer = FingerprintAudit(audit)
        change_id = str(uuid.uuid4())
        prepared = writer.append("fingerprint_policy_prepared", {
            "change_id": change_id, "actor_id": actor_id,
            "before": previous.snapshot(), "after": policy.snapshot(),
            "config_revision": expected_revision,
        })
        source["fingerprint"] = asdict(policy)
        revision = self.write_raw_config(raw)
        try:
            writer.append("fingerprint_policy_changed", {
                "change_id": change_id, "prepared_event_id": prepared, "actor_id": actor_id,
                "before": previous.snapshot(), "after": policy.snapshot(), "config_revision": revision,
            })
        except FingerprintUnavailable as exc:
            raise ConfigDurabilityError("fingerprint configuration committed; audit outcome unconfirmed; do not replay") from exc
        return PolicySnapshot(policy, revision)

    @staticmethod
    def _permission_agent_row(raw: dict, agent_name: str) -> dict:
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("agent name is required")
        normalize = str.casefold if os.name == "nt" else lambda value: value
        matches = [
            row for row in _agent_rows(raw)
            if normalize(row["name"]) == normalize(agent_name)
        ]
        if not matches:
            raise KeyError("agent not found")
        if len(matches) != 1:
            raise ValueError("ambiguous agent name in configuration")
        return matches[0]

    @staticmethod
    def _permission_settings_source(row: dict):
        """Resolve the Agent-level placement without defaulting configuration errors."""
        from orchestrator.permissions.models import PermissionSettings

        extra = row.get("extra")
        if extra is not None and not isinstance(extra, dict):
            raise ValueError("agent extra settings must be an object")
        nested = isinstance(extra, dict) and "permission_settings" in extra
        top_level = "permission_settings" in row
        if top_level and nested:
            raise ValueError("conflicting permission settings")
        source = extra if nested else row
        if "permission_settings" not in source:
            return source, PermissionSettings()
        value = source["permission_settings"]
        if value is None:
            raise ValueError("explicit permission settings cannot be null")
        return source, PermissionSettings.from_mapping(value)

    def read_agent_permission_settings(self, agent_name: str):
        """Read without rewriting old configuration or enabling an authority."""
        from orchestrator.permissions.models import PermissionSettingsSnapshot

        raw = self.load_raw_config()
        row = self._permission_agent_row(raw, agent_name)
        _, settings = self._permission_settings_source(row)
        return PermissionSettingsSnapshot(row["name"], settings, raw.revision)

    def write_agent_permission_settings(
        self, agent_name: str, settings, *, expected_revision: str
    ):
        """Publish a previously authorized decision through the existing CAS writer.

        This persistence method does not authenticate users; the trusted
        permission-management ingress must authorize the exact change before
        invoking it. It is not exposed to model tools or generic worker RPC.
        """
        from orchestrator.permissions.models import PermissionSettings, PermissionSettingsSnapshot

        if not isinstance(settings, PermissionSettings):
            raise ValueError("validated permission settings are required")
        if not isinstance(expected_revision, str) or len(expected_revision) != 64:
            raise ValueError("a configuration revision is required")
        raw = self.load_raw_config()
        if raw.revision != expected_revision:
            raise ConfigConflictError("agents configuration changed")
        row = self._permission_agent_row(raw, agent_name)
        source, _ = self._permission_settings_source(row)
        source["permission_settings"] = {
            "requested_level": settings.requested_level.value,
            "task_default_seconds": settings.task_default_seconds,
            "request_ttl_seconds": settings.request_ttl_seconds,
            "audit_retention_seconds": settings.audit_retention_seconds,
        }
        revision = self.write_raw_config(raw)
        return PermissionSettingsSnapshot(row["name"], settings, revision)

    def get_all_agents_raw(self) -> list[dict]:
        raw = self.load_raw_config()
        return raw.get("agents", [])

    def set_agent_active(
        self,
        agent_name: str,
        active: bool,
        *,
        expected_revision: str | None = None,
        allow_last_active_deactivation: bool = False,
    ) -> bool:
        """Toggle is_active for an agent. Returns True if found and updated."""
        raw = self.load_raw_config()
        if expected_revision is not None and raw.revision != expected_revision:
            raise ConfigConflictError("agents configuration changed")
        for ag in raw.get("agents", []):
            if ag.get("name") == agent_name:
                if (
                    not active
                    and not allow_last_active_deactivation
                    and ag.get("is_active", True) is not False
                    and sum(
                        1
                        for row in raw.get("agents", [])
                        if isinstance(row, dict)
                        and row.get("is_active", True) is not False
                    ) <= 1
                ):
                    return False
                ag["is_active"] = active
                self.write_raw_config(raw)
                return True
        return False

    def update_agent_metadata(
        self,
        agent_name: str,
        *,
        display_name: str | None = None,
        emoji: str | None = None,
    ) -> dict[str, Any] | None:
        """Publish the PAO-owned public Agent metadata fields in one revision."""

        raw = self.load_raw_config()
        row = next(
            (
                item
                for item in raw.get("agents", [])
                if isinstance(item, dict) and item.get("name") == agent_name
            ),
            None,
        )
        if row is None:
            return None
        if display_name is not None:
            row["display_name"] = display_name
        if emoji is not None:
            row["emoji"] = emoji
        self.write_raw_config(raw)
        return dict(row)

    def delete_agent_from_config(self, agent_name: str) -> bool:
        """Remove an agent entry from config. Returns True if found and removed."""
        raw = self.load_raw_config()
        agents = raw.get("agents", [])
        removed = [ag for ag in agents if ag.get("name") == agent_name]
        remaining = [ag for ag in agents if ag.get("name") != agent_name]
        if (
            any(ag.get("is_active", True) is not False for ag in removed)
            and not any(
                isinstance(ag, dict) and ag.get("is_active", True) is not False
                for ag in remaining
            )
        ):
            return False
        orig_len = len(agents)
        raw["agents"] = remaining
        if len(raw["agents"]) < orig_len:
            self.write_raw_config(raw)
            # The config publication is authoritative.  Retire only the exact
            # deleted incarnation; a state error must never restore the row.
            try:
                from orchestrator.telegram_delivery_failover import (
                    retire_agent_delivery_state,
                )

                for row in removed:
                    retire_agent_delivery_state(
                        self.paths.config_path.parent,
                        agent_name,
                        lifecycle_id=row.get(AGENT_LIFECYCLE_FIELD),
                        reason="agent_deleted",
                    )
            except Exception as exc:
                # A subsequent watcher will quarantine the now-unowned record.
                logger.error(
                    "Agent delivery state retirement deferred agent=%s error=%s",
                    agent_name,
                    type(exc).__name__,
                )
            return True
        return False

    def _release_scaffold_marker(
        self,
        workspace: Path,
        *,
        marker_identity: tuple[int, int],
        token: str,
    ) -> None:
        marker = workspace / _SCAFFOLD_OWNER_MARKER
        try:
            if _owned_marker_matches(
                marker,
                identity=marker_identity,
                token=token,
            ):
                marker.unlink()
        except (FileNotFoundError, OSError, ValueError, UnicodeError) as exc:
            logger.warning(
                "agent_create.owner_marker retained workspace=%s error=%s",
                workspace.name,
                type(exc).__name__,
            )

    def _cleanup_failed_agent_scaffold(
        self,
        *,
        agent_name: str,
        owned_path: Path | None,
        workspace_identity: tuple[int, int] | None,
        marker_identity: tuple[int, int] | None,
        token: str,
        pcm_identity: tuple[int, int] | None,
        intended_digest: str,
        source_revision: str,
        candidate: ConfigDocument,
        publication_attempted: bool,
        failure: Exception,
    ) -> str:
        """Clean only a proven, unpublished scaffold while its owner is held."""

        if owned_path is None or workspace_identity is None or marker_identity is None:
            return "No cleanup authorized: scaffold ownership was not established"
        if publication_attempted and (
            getattr(failure, "committed", False)
            or candidate.revision != source_revision
        ):
            return "Scaffold retained: configuration publication was observed"
        try:
            latest = self.load_raw_config()
            if _agent_name_collides(_agent_rows(latest), agent_name):
                return "Scaffold retained: current configuration has this Agent"
            if (
                publication_attempted
                and not isinstance(failure, ConfigConflictError)
                and latest.revision != source_revision
            ):
                return "Scaffold retained: configuration outcome is unknown"
            if _directory_identity(owned_path) != workspace_identity:
                return "Scaffold retained: workspace directory identity changed"
            marker = owned_path / _SCAFFOLD_OWNER_MARKER
            if not _owned_marker_matches(
                marker,
                identity=marker_identity,
                token=token,
            ):
                return "Scaffold retained: ownership marker changed"
            agent_md = owned_path / "agent.md"
            try:
                current_pcm_identity, digest = _regular_file_state(agent_md)
            except FileNotFoundError:
                pass
            else:
                if pcm_identity is not None and current_pcm_identity != pcm_identity:
                    return "Scaffold retained: PCM file identity changed"
                if digest != intended_digest:
                    return "Scaffold retained: PCM bytes changed"
                agent_md.unlink()
            marker.unlink()
            try:
                owned_path.rmdir()
            except OSError:
                return "Owned scaffold files removed; directory retained for recovery"
            return "Unpublished owned scaffold removed"
        except Exception as cleanup_error:
            return (
                "Scaffold retained: cleanup ownership is unknown ("
                + type(cleanup_error).__name__
                + ")"
            )

    def _publish_agent_scaffold(
        self,
        agent_name: str,
        new_entry: dict,
        *,
        pcm_text: str,
        raw_config: ConfigDocument,
    ) -> AgentScaffoldReceipt:
        config_path = self.paths.config_path.resolve()
        workspaces_root = self.paths.workspaces_root.resolve()
        workspaces_root.mkdir(parents=True, exist_ok=True)
        workspace = workspaces_root / agent_name
        if workspace.resolve().parent != workspaces_root:
            raise ValueError("workspace escapes the configured root")
        if new_entry.get("name") != agent_name:
            raise ValueError("built Agent config and target name differ")
        if not isinstance(raw_config, ConfigDocument):
            raise TypeError("Agent creation requires its revision-bearing policy snapshot")
        if raw_config.source != config_path:
            raise ValueError("Agent creation policy belongs to another config file")

        intended_digest = hashlib.sha256(pcm_text.encode("utf-8")).hexdigest()
        lock_target = _agent_creation_lock_target(config_path, agent_name)
        name_digest = lock_target.name.removeprefix("agent-create-")

        with file_write_lock(lock_target):
            current = self.load_raw_config()
            if _agent_name_collides(_agent_rows(current), agent_name):
                return AgentScaffoldReceipt(False, current.revision, True)
            if current.revision != raw_config.revision:
                raise ConfigConflictError(
                    "Agent creation policy was computed from stale configuration"
                )
            if workspace.exists() or workspace.is_symlink():
                raise AgentWorkspaceConflictError(
                    f"workspace 'workspaces/{agent_name}' already exists"
                )

            source_revision = raw_config.revision
            candidate = raw_config.copy()
            candidate.clear()
            candidate.update(deepcopy(dict(raw_config)))
            candidate.setdefault("agents", []).append(deepcopy(new_entry))

            token = uuid.uuid4().hex
            staging = workspaces_root / f".agent-create-{name_digest}-{token}"
            owned_path: Path | None = None
            workspace_identity: tuple[int, int] | None = None
            marker_identity: tuple[int, int] | None = None
            pcm_identity: tuple[int, int] | None = None
            publication_attempted = False
            try:
                staging.mkdir(exist_ok=False)
                owned_path = staging
                workspace_identity = _directory_identity(staging)
                marker_identity = _write_owner_marker(
                    staging / _SCAFFOLD_OWNER_MARKER,
                    token,
                )
                agent_md = staging / "agent.md"
                atomic_write_pcm(agent_md, pcm_text)
                pcm_identity, written_digest = _regular_file_state(agent_md)
                if written_digest != intended_digest:
                    raise ValueError("PCM writer did not install the requested bytes")
                try:
                    os.rename(staging, workspace)
                except OSError as exc:
                    if workspace.exists() or workspace.is_symlink():
                        raise AgentWorkspaceConflictError(
                            f"workspace 'workspaces/{agent_name}' already exists"
                        ) from exc
                    raise
                owned_path = workspace
                if _directory_identity(workspace) != workspace_identity:
                    raise ValueError("workspace identity changed during publication")

                publication_attempted = True
                try:
                    revision = self.write_raw_config(candidate)
                except ConfigDurabilityError:
                    self._release_scaffold_marker(
                        workspace,
                        marker_identity=marker_identity,
                        token=token,
                    )
                    return AgentScaffoldReceipt(True, candidate.revision, False)
                self._release_scaffold_marker(
                    workspace,
                    marker_identity=marker_identity,
                    token=token,
                )
                return AgentScaffoldReceipt(
                    True,
                    revision or candidate.revision,
                    True,
                )
            except Exception as failure:
                outcome = self._cleanup_failed_agent_scaffold(
                    agent_name=agent_name,
                    owned_path=owned_path,
                    workspace_identity=workspace_identity,
                    marker_identity=marker_identity,
                    token=token,
                    pcm_identity=pcm_identity,
                    intended_digest=intended_digest,
                    source_revision=source_revision,
                    candidate=candidate,
                    publication_attempted=publication_attempted,
                    failure=failure,
                )
                if callable(getattr(failure, "add_note", None)):
                    failure.add_note(outcome)
                raise

    def add_agent_to_config(
        self,
        agent_name: str,
        agent_cfg: dict | str | None = None,
        token: str | None = None,
        *,
        raw_config: dict | None = None,
        return_receipt: bool = False,
    ):
        """Add a flex agent scaffold.

        The original API returned bool for dict/None input. Some runtime command
        paths pass a display-name string and optional token and expect (ok, msg).
        Keep both shapes for compatibility.
        """
        wants_message = isinstance(agent_cfg, str) or token is not None
        display_name = (
            agent_cfg
            if isinstance(agent_cfg, str)
            else str((agent_cfg or {}).get("display_name") or agent_name)
        )

        raw = raw_config if raw_config is not None else self.load_raw_config()
        if _agent_name_collides(_agent_rows(raw), agent_name):
            receipt = AgentScaffoldReceipt(False, getattr(raw, "revision", None), True)
            if return_receipt:
                return receipt
            return (False, f"Agent '{agent_name}' already exists.") if wants_message else False

        if isinstance(agent_cfg, dict):
            new_entry = dict(agent_cfg)
        else:
            new_entry = {
                "display_name": display_name,
                "telegram_token_key": agent_name if not token else f"{agent_name}_telegram_token",
            }
        new_entry.setdefault("name", agent_name)
        # Ordinary creation is a new incarnation even if a caller supplied a
        # stale row copied from a deleted Agent. Move has its own narrow import
        # path and is the only operation allowed to preserve this identity.
        new_entry[AGENT_LIFECYCLE_FIELD] = new_agent_lifecycle_id()
        new_entry.setdefault("workspace_dir", f"workspaces/{agent_name}")
        new_entry.pop("system_md", None)
        new_entry.setdefault("is_active", True)
        new_entry.setdefault("type", "flex")
        if not new_entry.get("allowed_backends") or not new_entry.get("active_backend"):
            template = next(
                (
                    row
                    for row in raw.get("agents", [])
                    if isinstance(row, dict)
                    and row.get("active_backend")
                    and row.get("allowed_backends")
                ),
                None,
            )
            active_backend = str(
                (template or {}).get("active_backend") or "codex-cli"
            )
            template_row = next(
                (
                    item
                    for item in (template or {}).get("allowed_backends", [])
                    if isinstance(item, dict) and item.get("engine") == active_backend
                ),
                {"engine": active_backend},
            )
            # Agent creation never inherits Tool grants. Copy only backend/model
            # selection fields needed for a valid, least-privilege scaffold.
            safe_backend = {
                key: value
                for key, value in template_row.items()
                if key in {"engine", "model", "default_model", "models", "provider"}
            }
            safe_backend["engine"] = active_backend
            new_entry.setdefault("allowed_backends", [safe_backend])
            new_entry.setdefault("active_backend", active_backend)
        new_entry.setdefault(
            "default_mode",
            default_agent_mode_for_backend(new_entry.get("active_backend")),
        )

        try:
            receipt = self._publish_agent_scaffold(
                agent_name,
                new_entry,
                pcm_text=render_pcm_document(
                    persona=f"You are {display_name}, a HASHI agent.",
                    system=(
                        "Follow HASHI infrastructure policy, active /sys rules, "
                        "and the authoritative current user request."
                    ),
                ),
                raw_config=raw,
            )
        except AgentWorkspaceConflictError:
            if wants_message and not return_receipt:
                return False, f"Workspace 'workspaces/{agent_name}' already exists."
            raise
        if return_receipt:
            return receipt
        if not receipt.created:
            return (False, f"Agent '{agent_name}' already exists.") if wants_message else False
        if wants_message:
            suffix = ""
            if token:
                suffix = " Telegram token was not written to secrets.json; add it before Telegram use."
            return True, f"Added agent '{agent_name}'.{suffix}"
        return True

    def configured_agent_names(self) -> list[str]:
        raw = self.load_raw_config()
        return [agent["name"] for agent in raw.get("agents", []) if agent.get("is_active", True)]

    def get_startable_agent_names(
        self,
        running: set[str],
        starting: set[str],
        exclude_name: str | None = None,
    ) -> list[str]:
        return [
            name for name in self.configured_agent_names()
            if name not in running and name not in starting and name != exclude_name
        ]
