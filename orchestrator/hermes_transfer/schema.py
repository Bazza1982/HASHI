"""Schema helpers for HASHI Hermes agent transfer packages."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = 1
PACKAGE_TYPE = "hashi-hermes-agent"
PACKAGE_EXT = ".hashi-hermes-agent"

RUNTIMES = {"hashi", "hermes"}
TRANSFER_MODES = {"copy", "move"}
SOURCE_DISABLE_POLICIES = {"never", "after_verified_import"}
TARGET_ENABLE_POLICIES = {"manual_review", "enable_after_import"}
PROFILE_DIRECTORY_POLICIES = {"whitelist_only"}
CRON_IMPORT_POLICIES = {"paused_review_drafts", "skip"}
MEMORY_IMPORT_POLICIES = {"portable_notes_only", "validate_size_age"}
SESSION_IMPORT_POLICIES = {"never"}


class TransferSchemaError(ValueError):
    """Raised when a transfer package schema object is invalid."""


@dataclass(frozen=True)
class PlannedWrite:
    """One filesystem write planned by an import/export operation."""

    path: str
    kind: str = "file"
    action: str = "create"
    required: bool = True
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "action": self.action,
            "required": self.required,
            "description": self.description,
        }


@dataclass(frozen=True)
class DryRunReport:
    """Portable dry-run report stored under audit/dry_run_plan.json."""

    operation: str
    source_runtime: str
    target_runtime: str
    agent_id: str
    planned_writes: list[PlannedWrite] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": self.operation,
            "source_runtime": self.source_runtime,
            "target_runtime": self.target_runtime,
            "agent_id": self.agent_id,
            "created_at": self.created_at,
            "planned_writes": [item.to_dict() for item in self.planned_writes],
            "warnings": list(self.warnings),
        }


def new_manifest(
    *,
    source_runtime: str,
    target_runtime: str,
    agent_id: str,
    display_name: str | None = None,
    transfer_mode: str = "copy",
    created_by: str = "hashi",
    contains_secrets: bool = False,
    secrets_encrypted: bool = False,
    contains_memory: bool = True,
    contains_workspace: bool = True,
    package_id: str | None = None,
) -> dict[str, Any]:
    """Build a conservative v1 manifest with Hermes-safe defaults."""

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "package_type": PACKAGE_TYPE,
        "package_id": package_id or f"pkg-{uuid4().hex}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
        "source_runtime": source_runtime,
        "target_runtime": target_runtime,
        "agent_id": agent_id,
        "display_name": display_name or agent_id,
        "transfer_mode": transfer_mode,
        "contains_secrets": bool(contains_secrets),
        "secrets_encrypted": bool(secrets_encrypted),
        "contains_memory": bool(contains_memory),
        "contains_workspace": bool(contains_workspace),
        "source_disable_policy": "never",
        "target_enable_policy": "manual_review",
        "profile_directory_policy": "whitelist_only",
        "cron_import_policy": "paused_review_drafts",
        "memory_import_policy": "validate_size_age",
        "session_import_policy": "never",
        "secrets_policy_file": "secrets.policy.json",
        "checksums_file": "audit/checksums.json",
    }
    validate_manifest(manifest)
    return manifest


def default_profile_policy() -> dict[str, Any]:
    """Return the default Hermes profile whitelist policy."""

    return {
        "schema_version": SCHEMA_VERSION,
        "runtime": "hermes",
        "allowed_profile_subdirs": ["skills", "memories"],
        "blocked_profile_subdirs": ["sessions", "cron/runtime", "plugins/cache"],
        "plugin_policy": {
            "mode": "explicit_allowlist",
            "allowed_plugins": ["hashi_hchat"],
            "blocked_reason": "plugins may contain local paths, caches, OAuth state, or provider credentials",
        },
        "memory_policy": {
            "max_chars_per_item": 2200,
            "stale_item_action": "skip_with_warning",
            "oversize_item_action": "convert_to_portable_note",
        },
        "cron_policy": {
            "default_state": "paused",
            "blocked_fields": ["deliver", "profile", "context_from"],
        },
    }


def default_secrets_policy() -> dict[str, Any]:
    """Return the default no-secrets policy for Hermes transfers."""

    return {
        "schema_version": SCHEMA_VERSION,
        "default": "exclude",
        "included": False,
        "encryption": "none",
        "allowed_secret_classes": [],
        "blocked_secret_classes": [
            "telegram_token",
            "telegram_chat_id",
            "webhook",
            "oauth_token",
            "voice_model_key",
            "media_generation_key",
            "hashi_remote_shared_token",
        ],
        "target_decryption_allowed": False,
        "operator_approval_required": True,
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "package_type",
        "package_id",
        "created_at",
        "created_by",
        "source_runtime",
        "target_runtime",
        "agent_id",
        "display_name",
        "transfer_mode",
        "contains_secrets",
        "secrets_encrypted",
        "contains_memory",
        "contains_workspace",
        "source_disable_policy",
        "target_enable_policy",
        "profile_directory_policy",
        "cron_import_policy",
        "memory_import_policy",
        "session_import_policy",
        "secrets_policy_file",
        "checksums_file",
    }
    _require_keys(manifest, required, "manifest")
    _require_equal(manifest["schema_version"], SCHEMA_VERSION, "manifest.schema_version")
    _require_equal(manifest["package_type"], PACKAGE_TYPE, "manifest.package_type")
    _require_in(manifest["source_runtime"], RUNTIMES, "manifest.source_runtime")
    _require_in(manifest["target_runtime"], RUNTIMES, "manifest.target_runtime")
    if manifest["source_runtime"] == manifest["target_runtime"]:
        raise TransferSchemaError("manifest source_runtime and target_runtime must differ")
    _require_in(manifest["transfer_mode"], TRANSFER_MODES, "manifest.transfer_mode")
    _require_in(manifest["source_disable_policy"], SOURCE_DISABLE_POLICIES, "manifest.source_disable_policy")
    _require_in(manifest["target_enable_policy"], TARGET_ENABLE_POLICIES, "manifest.target_enable_policy")
    _require_in(manifest["profile_directory_policy"], PROFILE_DIRECTORY_POLICIES, "manifest.profile_directory_policy")
    _require_in(manifest["cron_import_policy"], CRON_IMPORT_POLICIES, "manifest.cron_import_policy")
    _require_in(manifest["memory_import_policy"], MEMORY_IMPORT_POLICIES, "manifest.memory_import_policy")
    _require_in(manifest["session_import_policy"], SESSION_IMPORT_POLICIES, "manifest.session_import_policy")
    if not str(manifest["agent_id"]).strip():
        raise TransferSchemaError("manifest.agent_id must be non-empty")
    if not str(manifest["package_id"]).startswith("pkg-"):
        raise TransferSchemaError("manifest.package_id must start with 'pkg-'")
    for key in ("contains_secrets", "secrets_encrypted", "contains_memory", "contains_workspace"):
        if not isinstance(manifest[key], bool):
            raise TransferSchemaError(f"manifest.{key} must be boolean")
    if manifest["session_import_policy"] != "never":
        raise TransferSchemaError("manifest.session_import_policy must be 'never'")


def validate_normalized_agent(agent: dict[str, Any]) -> None:
    required = {
        "agent_id",
        "display_name",
        "identity_text_path",
        "capabilities",
        "memory",
        "secrets",
    }
    _require_keys(agent, required, "normalized_agent")
    if not str(agent["agent_id"]).strip():
        raise TransferSchemaError("normalized_agent.agent_id must be non-empty")
    memory = agent.get("memory")
    if not isinstance(memory, dict):
        raise TransferSchemaError("normalized_agent.memory must be an object")
    if int(memory.get("max_chars_per_item", 2200)) > 2200:
        raise TransferSchemaError("normalized_agent.memory.max_chars_per_item must not exceed 2200")
    if memory.get("session_state_included") is not False:
        raise TransferSchemaError("normalized_agent.memory.session_state_included must be false")
    secrets = agent.get("secrets")
    if not isinstance(secrets, dict):
        raise TransferSchemaError("normalized_agent.secrets must be an object")
    if secrets.get("included") and not secrets.get("encrypted"):
        raise TransferSchemaError("normalized_agent.secrets must be encrypted when included")


def _require_keys(obj: dict[str, Any], keys: set[str], label: str) -> None:
    missing = sorted(key for key in keys if key not in obj)
    if missing:
        raise TransferSchemaError(f"{label} missing required fields: {missing}")


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise TransferSchemaError(f"{label} must be {expected!r}, got {actual!r}")


def _require_in(actual: Any, allowed: set[str], label: str) -> None:
    if actual not in allowed:
        raise TransferSchemaError(f"{label} must be one of {sorted(allowed)}, got {actual!r}")
