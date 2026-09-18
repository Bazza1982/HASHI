"""HASHI-owned agent deletion service.

Orchestrates the safe, destructive removal of an Agent and its managed workspace:
1. Preview phase: read-only evaluation of lifecycle, configuration revision,
   managed workspace metrics, and blocking conditions. Issues short-lived preview tokens.
2. Commit phase: verifies preview token, typed confirmation, revision consistency,
   and lifecycle invariants before journaled execution.
3. Cleanup phase: removes configuration, local group memberships, scheduled tasks,
   agent-specific capabilities, and unshared secrets; archives conversation history;
   and moves the workspace into quarantine for a safe recovery window.
4. Resumable cleanup: marks receipts with 'cleanup_pending' on partial failure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from orchestrator.agent_directory import AgentDirectory
from orchestrator.agent_incarnation import (
    AGENT_LIFECYCLE_FIELD,
    valid_agent_lifecycle_id,
)
from orchestrator.telegram_delivery_state import retire_agent_state
from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDocument,
    ConfigDurabilityError,
    read_config_json,
    write_config_json,
)
from orchestrator.flexible_backend_registry import get_secret_lookup_order
from orchestrator.pathing import BridgePaths

logger = logging.getLogger("BridgeU.AgentDeletion")

PREVIEW_TTL_SECONDS = 300.0
QUARANTINE_RETENTION_DAYS = 7


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class AgentDeletionError(Exception):
    """Base exception for agent deletion domain errors."""

    error_code: str = "deletion_failed"
    status_code: int = 400

    def __init__(self, message: str, *, error_code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        if error_code:
            self.error_code = error_code
        if status_code:
            self.status_code = status_code


class AgentNotFoundError(AgentDeletionError):
    error_code = "agent_not_found"
    status_code = 404


class AgentBusyError(AgentDeletionError):
    error_code = "agent_busy"
    status_code = 409


class LastActiveAgentDeletionError(AgentDeletionError):
    error_code = "last_active_agent"
    status_code = 409


class DelayedMessagesBlockingDeletionError(AgentDeletionError):
    error_code = "delayed_work_pending"
    status_code = 409


class BackgroundJobBlockingDeletionError(AgentDeletionError):
    error_code = "background_jobs_pending"
    status_code = 409


class TransferBlockingDeletionError(AgentDeletionError):
    error_code = "transfer_in_progress"
    status_code = 409


class UnmanagedWorkspaceError(AgentDeletionError):
    error_code = "unmanaged_workspace"
    status_code = 400


class SharedWorkspaceError(AgentDeletionError):
    error_code = "shared_workspace"
    status_code = 400


class PreviewExpiredError(AgentDeletionError):
    error_code = "preview_expired"
    status_code = 410


class PreviewInvalidError(AgentDeletionError):
    error_code = "preview_invalid"
    status_code = 400


class ConfirmationMismatchError(AgentDeletionError):
    error_code = "confirmation_mismatch"
    status_code = 400


class ConfigConflictDeletionError(AgentDeletionError):
    error_code = "config_conflict"
    status_code = 409


class CleanupPendingError(AgentDeletionError):
    error_code = "cleanup_pending"
    status_code = 500

    def __init__(self, operation_id: str, message: str):
        super().__init__(message, error_code="cleanup_pending", status_code=500)
        self.operation_id = operation_id


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------


@dataclass
class AgentDeletionPreview:
    ok: bool
    agent_id: str
    display_name: str
    is_active: bool
    lifecycle_id: str
    config_revision: str
    workspace: dict[str, Any]
    blocked_reasons: list[str]
    will_delete: list[str]
    will_retain: list[str]
    recovery_available: bool
    recovery_retention_days: int
    preview_token: str
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# In-memory preview token store (bounded, auto-expiring)
_PREVIEW_TOKENS: dict[str, dict[str, Any]] = {}


def _store_preview_token(
    agent_id: str, lifecycle_id: str, config_revision: str, ttl: float = PREVIEW_TTL_SECONDS
) -> str:
    now = time.time()
    # Prune expired tokens
    expired = [k for k, v in _PREVIEW_TOKENS.items() if v.get("expires_at", 0) < now]
    for k in expired:
        _PREVIEW_TOKENS.pop(k, None)

    token = f"prev_{uuid.uuid4().hex}"
    _PREVIEW_TOKENS[token] = {
        "agent_id": agent_id,
        "lifecycle_id": lifecycle_id,
        "config_revision": config_revision,
        "created_at": now,
        "expires_at": now + ttl,
    }
    return token


def _verify_preview_token(token: str, agent_id: str) -> dict[str, Any]:
    record = _PREVIEW_TOKENS.get(token)
    if not record:
        raise PreviewInvalidError("preview token is invalid or was not found")
    if record.get("expires_at", 0) < time.time():
        _PREVIEW_TOKENS.pop(token, None)
        raise PreviewExpiredError("preview has expired; please request a new preview")
    if record.get("agent_id") != agent_id:
        raise PreviewInvalidError("preview token does not belong to this agent")
    return record


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _agent_rows(doc: Any) -> list[dict[str, Any]]:
    if isinstance(doc, dict):
        rows = doc.get("agents", [])
        return [r for r in rows if isinstance(r, dict)]
    if isinstance(doc, list):
        return [r for r in doc if isinstance(r, dict)]
    return []


def _value_references_secret(value: Any, secret_key: str) -> bool:
    """Check runtime credential consumers, never arbitrary config strings."""
    for row in _agent_rows(value):
        name = str(row.get("name") or row.get("id") or "")
        if str(row.get("telegram_token_key") or name) == secret_key:
            return True
        engines = {str(row.get("active_backend") or row.get("engine") or "")}
        for backend in row.get("allowed_backends", []) or []:
            if isinstance(backend, Mapping):
                engines.add(str(backend.get("engine") or ""))
        if any(
            secret_key in get_secret_lookup_order(engine, name)
            for engine in engines
            if engine
        ):
            return True
    return False


def _resolve_workspace_path(paths: BridgePaths, row: Mapping[str, Any], agent_id: str) -> Path:
    raw = str(
        row.get("workspace_dir")
        or row.get("workspace")
        or f"workspaces/{agent_id}"
    ).strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = paths.bridge_home / path
    return path.resolve()


def _validate_workspace_safety(
    paths: BridgePaths,
    ws_path: Path,
    agent_id: str,
    all_rows: list[dict[str, Any]],
) -> None:
    workspaces_root = paths.workspaces_root.resolve()
    resolved_home = paths.bridge_home.resolve()
    code_root = paths.code_root.resolve()
    user_home = Path.home().resolve()
    drive_anchor = Path(ws_path.anchor).resolve()

    protected = {workspaces_root, resolved_home, code_root, user_home, drive_anchor}
    if ws_path in protected:
        raise UnmanagedWorkspaceError(f"refusing to remove protected path: {ws_path}")
    if ws_path.is_symlink():
        raise UnmanagedWorkspaceError("refusing to remove a symlinked workspace")

    try:
        ws_path.relative_to(workspaces_root)
    except ValueError:
        raise UnmanagedWorkspaceError(
            f"workspace path {ws_path} is outside the managed workspaces root {workspaces_root}"
        )

    # Check shared workspace
    for other in all_rows:
        other_id = str(other.get("name") or other.get("id") or "")
        if other_id == agent_id:
            continue
        other_ws = _resolve_workspace_path(paths, other, other_id)
        if other_ws == ws_path:
            raise SharedWorkspaceError(
                f"workspace {ws_path} is shared with agent '{other_id}'"
            )


def _calc_workspace_stats(ws_path: Path) -> tuple[int, int]:
    if not ws_path.exists() or not ws_path.is_dir():
        return 0, 0
    file_count = 0
    total_bytes = 0
    try:
        for entry in ws_path.rglob("*"):
            if entry.is_file() and not entry.is_symlink():
                file_count += 1
                try:
                    total_bytes += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return file_count, total_bytes


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AgentDeletionService:
    """PAO-owned domain service for agent deletion."""

    def __init__(
        self,
        paths: BridgePaths,
        *,
        orchestrator: Any | None = None,
        session_store: Any | None = None,
    ):
        self.paths = paths
        self.orchestrator = orchestrator
        self.session_store = session_store
        self.receipts_dir = paths.bridge_home / "state" / "agent_deletions"
        self.quarantine_root = paths.bridge_home / ".quarantined_workspaces"

    def preview(self, agent_id: str) -> AgentDeletionPreview:
        agent_name = str(agent_id or "").strip()
        if not agent_name:
            raise AgentDeletionError("agent ID is required", error_code="invalid_agent_id", status_code=400)

        config_doc = read_config_json(self.paths.config_path)
        rows = _agent_rows(config_doc)
        matches = [
            r for r in rows if str(r.get("name") or r.get("id") or "") == agent_name
        ]
        if not matches:
            raise AgentNotFoundError(f"agent '{agent_name}' was not found")

        row = matches[0]
        lifecycle_id = str(row.get(AGENT_LIFECYCLE_FIELD) or "")
        display_name = str(row.get("display_name") or row.get("displayName") or agent_name)
        is_active = row.get("is_active", True) is not False

        # Validate workspace safety
        ws_path = _resolve_workspace_path(self.paths, row, agent_name)
        _validate_workspace_safety(self.paths, ws_path, agent_name, rows)
        file_count, total_bytes = _calc_workspace_stats(ws_path)

        blocked_reasons: list[str] = []

        # Last active agent guard
        active_count = sum(1 for r in rows if r.get("is_active", True) is not False)
        if is_active and active_count <= 1:
            blocked_reasons.append("LAST_ACTIVE_AGENT")

        # Running / busy / active guard
        if is_active:
            blocked_reasons.append("AGENT_ACTIVE")

        if self.orchestrator is not None:
            runtime_map = getattr(self.orchestrator, "_runtime_map", None)
            if callable(runtime_map) and agent_name in runtime_map():
                blocked_reasons.append("AGENT_RUNNING")

            # Check delayed messages
            try:
                from orchestrator.runtime_pending import delayed_count_now
                delayed = delayed_count_now(self.orchestrator, agent_name=agent_name)
                if delayed > 0:
                    blocked_reasons.append("AGENT_HAS_DELAYED_MESSAGES")
            except Exception:
                pass

            # Check background jobs
            try:
                bg_manager = getattr(self.orchestrator, "background_job_manager", None)
                if bg_manager and hasattr(bg_manager, "list"):
                    active_jobs = bg_manager.list(
                        agent=agent_name,
                        states={"created", "starting", "running", "cancel_requested"},
                        limit=5,
                    )
                    if active_jobs:
                        blocked_reasons.append("AGENT_HAS_BACKGROUND_JOBS")
            except Exception:
                pass

        # Check transfer in progress
        if row.get("transfer_state") or row.get("transfer_package_id"):
            blocked_reasons.append("AGENT_TRANSFER_IN_PROGRESS")

        will_delete = [
            "configuration in agents.json",
            "managed workspace files",
            "scheduled tasks in tasks.json",
        ]
        if self.paths.secrets_path.exists():
            will_delete.append("agent-owned unshared secrets in secrets.json")
        if (self.paths.bridge_home / "agent_capabilities.json").exists():
            will_delete.append("agent-specific capabilities")

        will_retain = [
            "archived conversation history",
            "operation audit receipt",
        ]

        preview_token = _store_preview_token(
            agent_id=agent_name,
            lifecycle_id=lifecycle_id,
            config_revision=config_doc.revision or "",
            ttl=PREVIEW_TTL_SECONDS,
        )

        return AgentDeletionPreview(
            ok=True,
            agent_id=agent_name,
            display_name=display_name,
            is_active=is_active,
            lifecycle_id=lifecycle_id,
            config_revision=config_doc.revision or "",
            workspace={
                "managed": True,
                "file_count": file_count,
                "bytes": total_bytes,
            },
            blocked_reasons=sorted(set(blocked_reasons)),
            will_delete=will_delete,
            will_retain=will_retain,
            recovery_available=True,
            recovery_retention_days=QUARANTINE_RETENTION_DAYS,
            preview_token=preview_token,
            expires_at=time.time() + PREVIEW_TTL_SECONDS,
        )

    def delete(
        self,
        agent_id: str,
        *,
        preview_token: str,
        confirmed_agent_id: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        agent_name = str(agent_id or "").strip()
        if not agent_name:
            raise AgentDeletionError("agent ID is required", error_code="invalid_agent_id", status_code=400)

        # Idempotency check
        if idempotency_key:
            existing_receipt = self._find_receipt_by_idempotency_key(idempotency_key)
            if existing_receipt:
                return existing_receipt

        # Verify exact typed confirmation
        if str(confirmed_agent_id or "").strip() != agent_name:
            raise ConfirmationMismatchError(
                f"confirmed_agent_id '{confirmed_agent_id}' does not match agent '{agent_name}'"
            )

        # Verify preview token
        token_record = _verify_preview_token(preview_token, agent_name)

        # Read config with revision check
        config_doc = read_config_json(self.paths.config_path)
        if config_doc.revision != token_record["config_revision"]:
            raise ConfigConflictDeletionError(
                "agents configuration changed since deletion preview was generated"
            )

        rows = _agent_rows(config_doc)
        matches = [
            r for r in rows if str(r.get("name") or r.get("id") or "") == agent_name
        ]
        if not matches:
            raise AgentNotFoundError(f"agent '{agent_name}' was not found in configuration")

        row = matches[0]
        lifecycle_id = str(row.get(AGENT_LIFECYCLE_FIELD) or "")
        if token_record["lifecycle_id"] and lifecycle_id != token_record["lifecycle_id"]:
            raise ConfigConflictDeletionError(
                "agent lifecycle incarnation changed since preview"
            )

        is_active = row.get("is_active", True) is not False
        active_count = sum(1 for r in rows if r.get("is_active", True) is not False)
        if is_active and active_count <= 1:
            raise LastActiveAgentDeletionError("cannot delete the last active agent")

        if is_active:
            raise AgentBusyError("agent is currently active; please deactivate it before deletion")

        if self.orchestrator is not None:
            runtime_map = getattr(self.orchestrator, "_runtime_map", None)
            if callable(runtime_map) and agent_name in runtime_map():
                raise AgentBusyError("agent runtime is currently running; stop it before deletion")

            try:
                from orchestrator.runtime_pending import delayed_count_now
                if delayed_count_now(self.orchestrator, agent_name=agent_name) > 0:
                    raise DelayedMessagesBlockingDeletionError(
                        "agent has delayed messages in scheduler"
                    )
            except (DelayedMessagesBlockingDeletionError, AgentBusyError):
                raise
            except Exception:
                pass

            try:
                bg_manager = getattr(self.orchestrator, "background_job_manager", None)
                if bg_manager and hasattr(bg_manager, "list"):
                    active_jobs = bg_manager.list(
                        agent=agent_name,
                        states={"created", "starting", "running", "cancel_requested"},
                        limit=1,
                    )
                    if active_jobs:
                        raise BackgroundJobBlockingDeletionError(
                            "agent has active background jobs"
                        )
            except (BackgroundJobBlockingDeletionError, AgentBusyError):
                raise
            except Exception:
                pass

        if row.get("transfer_state") or row.get("transfer_package_id"):
            raise TransferBlockingDeletionError("agent has a transfer or move in progress")

        # Resolve & validate workspace safety
        ws_path = _resolve_workspace_path(self.paths, row, agent_name)
        _validate_workspace_safety(self.paths, ws_path, agent_name, rows)

        # Build journal receipt
        operation_id = f"del_{uuid.uuid4().hex[:16]}"
        journal: dict[str, Any] = {
            "operation_id": operation_id,
            "idempotency_key": idempotency_key or None,
            "agent_id": agent_name,
            "lifecycle_id": lifecycle_id,
            "config_revision": config_doc.revision,
            "status": "started",
            "created_at": _utc_now_iso(),
            "updated_at": _utc_now_iso(),
            "completed_at": None,
            "steps_completed": [],
            "workspace_path": str(ws_path),
            "workspace_quarantined": False,
            "quarantine_path": None,
            "error": None,
        }
        self._write_journal(operation_id, journal)

        # Destructive execution boundary
        try:
            # 1. Mutate config_doc and publish atomically
            if isinstance(config_doc.get("agents"), list):
                config_doc["agents"] = [
                    r for r in config_doc["agents"]
                    if not (isinstance(r, dict) and str(r.get("name") or r.get("id") or "") == agent_name)
                ]
            AgentDirectory.remove_local_memberships(config_doc, agent_name)
            write_config_json(self.paths.config_path, config_doc)
            journal["steps_completed"].append("config_removed")
            journal["status"] = "config_removed"
            self._write_journal(operation_id, journal)
        except Exception as exc:
            journal["status"] = "failed"
            journal["error"] = str(exc)
            self._write_journal(operation_id, journal)
            raise

        # Post-config mutations: any failure here becomes 'cleanup_pending'
        try:
            # 2. Retire delivery state
            try:
                from orchestrator.telegram_delivery_failover import (
                    retire_agent_delivery_state,
                )
                retire_agent_delivery_state(
                    self.paths.bridge_home,
                    agent_name,
                    lifecycle_id=lifecycle_id,
                    reason="agent_deleted",
                )
            except Exception as exc:
                logger.warning(
                    "Delivery state retirement error (ignored): agent=%s %s",
                    agent_name,
                    exc,
                )

            if valid_agent_lifecycle_id(lifecycle_id):
                try:
                    retire_agent_state(
                        self.paths.bridge_home,
                        agent_name,
                        lifecycle_id=lifecycle_id,
                        reason="agent_deleted",
                    )
                except Exception as exc:
                    logger.warning(
                        "Lifecycle state retirement error (ignored): agent=%s %s",
                        agent_name,
                        exc,
                    )
            journal["steps_completed"].append("delivery_state_retired")

            # 3. Clean tasks.json
            self._clean_tasks(agent_name)
            journal["steps_completed"].append("tasks_removed")

            # 4. Clean agent_capabilities.json
            self._clean_capabilities(agent_name)
            journal["steps_completed"].append("capabilities_removed")

            # 5. Clean secrets.json (unshared secrets only)
            self._clean_unshared_secrets(agent_name, row, remaining_doc=config_doc)
            journal["steps_completed"].append("secrets_cleaned")

            # 6. Archive conversation history
            self._archive_conversations(agent_name)
            journal["steps_completed"].append("conversations_archived")

            # 7. Quarantine managed workspace
            quarantine_path = self._quarantine_workspace(agent_name, ws_path)
            journal["workspace_quarantined"] = quarantine_path is not None
            journal["quarantine_path"] = str(quarantine_path) if quarantine_path else None
            journal["steps_completed"].append("workspace_quarantined")

            # 8. Succeeded
            journal["status"] = "succeeded"
            journal["completed_at"] = _utc_now_iso()
            journal["updated_at"] = _utc_now_iso()
            self._write_journal(operation_id, journal)

            # Invalidate preview token
            _PREVIEW_TOKENS.pop(preview_token, None)

            return {
                "ok": True,
                "operation_id": operation_id,
                "status": "succeeded",
                "agent_id": agent_name,
                "lifecycle_id": lifecycle_id,
                "workspace_quarantined": journal["workspace_quarantined"],
                "recovery_available": True,
                "recovery_retention_days": QUARANTINE_RETENTION_DAYS,
            }

        except Exception as exc:
            journal["status"] = "cleanup_pending"
            journal["error"] = str(exc)
            journal["updated_at"] = _utc_now_iso()
            self._write_journal(operation_id, journal)
            logger.error(
                "Agent deletion cleanup pending for agent=%s op=%s error=%s",
                agent_name,
                operation_id,
                exc,
            )
            raise CleanupPendingError(operation_id, f"cleanup pending: {exc}") from exc

    def get_receipt(self, operation_id: str) -> dict[str, Any] | None:
        receipt_path = self.receipts_dir / f"{operation_id}.json"
        if not receipt_path.is_file():
            return None
        try:
            return json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _find_receipt_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        if not self.receipts_dir.is_dir():
            return None
        for path in self.receipts_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("idempotency_key") == key:
                    return {
                        "ok": data.get("status") == "succeeded",
                        "operation_id": data.get("operation_id"),
                        "status": data.get("status"),
                        "agent_id": data.get("agent_id"),
                        "lifecycle_id": data.get("lifecycle_id"),
                        "workspace_quarantined": data.get("workspace_quarantined", False),
                        "recovery_available": True,
                        "recovery_retention_days": QUARANTINE_RETENTION_DAYS,
                        "error": data.get("error"),
                    }
            except Exception:
                continue
        return None

    def _write_journal(self, operation_id: str, journal: dict[str, Any]) -> None:
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        path = self.receipts_dir / f"{operation_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(journal, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _clean_tasks(self, agent_id: str) -> None:
        if not self.paths.tasks_path.is_file():
            return
        try:
            tasks_doc = read_config_json(self.paths.tasks_path)
            for section in ("heartbeats", "crons", "nudges"):
                if isinstance(tasks_doc.get(section), list):
                    tasks_doc[section] = [
                        item
                        for item in tasks_doc[section]
                        if not (isinstance(item, dict) and item.get("agent") == agent_id)
                    ]
            write_config_json(self.paths.tasks_path, tasks_doc)
        except Exception as exc:
            logger.warning("Tasks cleanup warning for agent=%s: %s", agent_id, exc)

    def _clean_capabilities(self, agent_id: str) -> None:
        cap_path = self.paths.bridge_home / "agent_capabilities.json"
        if not cap_path.is_file():
            return
        try:
            cap_doc = read_config_json(cap_path)
            entries = cap_doc.get("agents")
            if isinstance(entries, list):
                cap_doc["agents"] = [
                    row
                    for row in entries
                    if not (
                        isinstance(row, dict)
                        and str(row.get("name") or row.get("id") or "") == agent_id
                    )
                ]
            elif isinstance(entries, dict):
                entries.pop(agent_id, None)
            write_config_json(cap_path, cap_doc)
        except Exception as exc:
            logger.warning("Capabilities cleanup warning for agent=%s: %s", agent_id, exc)

    def _clean_unshared_secrets(
        self, agent_id: str, deleted_row: Mapping[str, Any], remaining_doc: ConfigDocument
    ) -> None:
        if not self.paths.secrets_path.is_file():
            return
        try:
            secrets_doc = read_config_json(self.paths.secrets_path)
            # Candidate secret keys belonging to deleted agent
            token_key = deleted_row.get("telegram_token_key") or agent_id
            candidates = {str(token_key), agent_id}
            engines = {
                str(deleted_row.get("active_backend") or deleted_row.get("engine") or "")
            }
            for b in deleted_row.get("allowed_backends", []) or []:
                if isinstance(b, Mapping):
                    engines.add(str(b.get("engine") or ""))
            for engine in engines:
                if engine:
                    candidates.update(get_secret_lookup_order(engine, agent_id))

            removed_any = False
            for key in candidates:
                if key in secrets_doc:
                    # Check if any remaining agent references this secret
                    if not _value_references_secret(remaining_doc, key):
                        secrets_doc.pop(key, None)
                        removed_any = True

            if removed_any:
                write_config_json(self.paths.secrets_path, secrets_doc)
        except Exception as exc:
            logger.warning("Secrets cleanup warning for agent=%s: %s", agent_id, exc)

    def _archive_conversations(self, agent_id: str) -> None:
        if self.session_store is None:
            return
        try:
            if hasattr(self.session_store, "archive_agent_sessions"):
                self.session_store.archive_agent_sessions(agent_id)
        except Exception as exc:
            logger.warning("Conversation archive warning for agent=%s: %s", agent_id, exc)

    def _quarantine_workspace(self, agent_id: str, ws_path: Path) -> Path | None:
        if not ws_path.exists():
            return None
        self.quarantine_root.mkdir(parents=True, exist_ok=True)
        target = self.quarantine_root / f"{agent_id}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        shutil.move(str(ws_path), str(target))
        return target
