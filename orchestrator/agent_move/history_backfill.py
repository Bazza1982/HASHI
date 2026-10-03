"""Offline, owner-checked Session history backfill for already-moved Agents.

This is deliberately narrower than Agent Move: it imports schema-5 conversation
capsules into existing target Agents and never changes Agent configuration,
workspaces, credentials, lifecycle state, or source Sessions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from orchestrator.agent_move.package import (
    AgentMoveError,
    configured_conversation_owner_id,
    export_conversation_continuity_snapshot,
)
from orchestrator.agent_incarnation import valid_agent_lifecycle_id
from orchestrator.config_json import read_config_json, write_config_json
from orchestrator.instance_lock import InstanceLock
from orchestrator.pathing import build_bridge_paths
from orchestrator.session_store import (
    SessionStore,
    SessionStoreError,
    validate_conversation_continuity_capsule,
)
from tools.private_files import protect_private_file


MANIFEST_SCHEMA_VERSION = 1
_MANIFEST_KEYS = {
    "schema_version",
    "batch_id",
    "target_instance",
    "owner_id",
    "expected_entry_count",
    "entries",
}
_ENTRY_KEYS = {
    "source_instance",
    "source_agent_id",
    "source_agent_lifecycle_id",
    "target_agent_id",
    "target_agent_lifecycle_id",
}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise AgentMoveError("history backfill SessionStore snapshot source is missing")
    uri = source.resolve().as_uri() + "?mode=ro"
    with (
        closing(sqlite3.connect(uri, uri=True)) as source_db,
        closing(sqlite3.connect(target)) as target_db,
    ):
        source_db.backup(target_db)


def _has_nonterminal_runs(database: Path, *, instance_id: str) -> bool:
    """Inspect the canonical Run fence without initializing the live store."""

    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM runs AS r
            JOIN sessions AS s ON s.session_id=r.session_id
            WHERE s.instance_id=?
              AND r.state NOT IN (
                'completed','failed','stopped','superseded','interrupted'
              )
            LIMIT 1
            """,
            (instance_id,),
        ).fetchone()
    return row is not None


def _configured_instance_id(root: Path) -> str:
    payload = read_config_json(root / "agents.json")
    global_config = payload.get("global")
    if not isinstance(global_config, Mapping):
        raise AgentMoveError("target global configuration is missing")
    instance_id = str(global_config.get("instance_id") or "").strip().upper()
    if not instance_id:
        raise AgentMoveError("target instance identity is not configured")
    return instance_id


def _agent_lifecycle_ids(root: Path) -> dict[str, str]:
    payload = read_config_json(root / "agents.json")
    rows = payload.get("agents")
    if not isinstance(rows, list):
        raise AgentMoveError("target Agent registry is invalid")
    lifecycle_ids: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise AgentMoveError("target Agent registry contains an invalid row")
        name = str(row.get("name") or row.get("id") or "").strip()
        if not name:
            raise AgentMoveError("target Agent registry contains an unnamed row")
        folded = name.casefold()
        if folded in lifecycle_ids:
            raise AgentMoveError(f"target Agent registry duplicates {name!r}")
        lifecycle_ids[folded] = str(
            row.get("agent_lifecycle_id") or ""
        ).strip().casefold()
    return lifecycle_ids


def _is_external_link(path: Path) -> bool:
    try:
        info = path.lstat()
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        junction = getattr(path, "is_junction", None)
        return (
            stat.S_ISLNK(info.st_mode)
            or bool(attributes & reparse)
            or bool(callable(junction) and junction())
        )
    except OSError as exc:
        raise AgentMoveError(f"could not inspect path safely: {path}") from exc


def _session_database(root: Path, *, role: str) -> Path:
    state_root = root / "state"
    if (
        not state_root.is_dir()
        or _is_external_link(state_root)
    ):
        raise AgentMoveError(f"history backfill {role} state directory is unavailable")
    database = state_root / "sessions.sqlite3"
    if not database.is_file() or _is_external_link(database):
        raise AgentMoveError(f"history backfill {role} SessionStore is unavailable")
    return database


def validate_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _MANIFEST_KEYS:
        raise AgentMoveError("history backfill manifest keys are invalid")
    if payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise AgentMoveError("unsupported history backfill manifest schema")
    batch_id = str(payload.get("batch_id") or "").strip()
    target_instance = str(payload.get("target_instance") or "").strip().upper()
    owner_id = str(payload.get("owner_id") or "").strip()
    if not _IDENTIFIER.fullmatch(batch_id):
        raise AgentMoveError("history backfill batch_id is invalid")
    if not _IDENTIFIER.fullmatch(target_instance):
        raise AgentMoveError("history backfill target_instance is invalid")
    if not owner_id.startswith("user:") or not owner_id[5:].isdigit():
        raise AgentMoveError("history backfill owner_id is invalid")
    entries = payload.get("entries")
    expected = payload.get("expected_entry_count")
    if not isinstance(expected, int) or expected < 1 or expected > 500:
        raise AgentMoveError("history backfill expected_entry_count is invalid")
    if not isinstance(entries, list) or len(entries) != expected:
        raise AgentMoveError("history backfill entry count does not match manifest")

    normalized_entries: list[dict[str, str]] = []
    seen_sources: set[tuple[str, str]] = set()
    seen_targets: set[str] = set()
    for raw in entries:
        if not isinstance(raw, Mapping) or set(raw) != _ENTRY_KEYS:
            raise AgentMoveError("history backfill entry keys are invalid")
        source_instance = str(raw.get("source_instance") or "").strip().upper()
        source_agent = str(raw.get("source_agent_id") or "").strip().lower()
        source_lifecycle = str(
            raw.get("source_agent_lifecycle_id") or ""
        ).strip().casefold()
        target_agent = str(raw.get("target_agent_id") or "").strip().lower()
        target_lifecycle = str(
            raw.get("target_agent_lifecycle_id") or ""
        ).strip().casefold()
        if (
            not _IDENTIFIER.fullmatch(source_instance)
            or not _IDENTIFIER.fullmatch(source_agent)
            or not _IDENTIFIER.fullmatch(target_agent)
        ):
            raise AgentMoveError("history backfill entry identity is invalid")
        if not valid_agent_lifecycle_id(
            source_lifecycle
        ) or not valid_agent_lifecycle_id(target_lifecycle):
            raise AgentMoveError("history backfill Agent lifecycle identity is invalid")
        if source_lifecycle != target_lifecycle:
            raise AgentMoveError(
                "history backfill source and target lifecycle identities differ"
            )
        if source_instance == target_instance:
            raise AgentMoveError("history backfill source and target instances must differ")
        source_key = (source_instance, source_agent)
        if source_key in seen_sources or target_agent in seen_targets:
            raise AgentMoveError("history backfill entries must be one-to-one")
        seen_sources.add(source_key)
        seen_targets.add(target_agent)
        identity = {
            "source_instance": source_instance,
            "source_agent_id": source_agent,
            "source_agent_lifecycle_id": source_lifecycle,
            "target_agent_id": target_agent,
            "target_agent_lifecycle_id": target_lifecycle,
        }
        identity_digest = hashlib.sha256(_canonical_json(identity)).hexdigest()[:20]
        normalized_entries.append(
            {
                **identity,
                "transfer_id": f"history-backfill:{batch_id}:{identity_digest}",
                "capsule_name": f"{identity_digest}.conversation-capsule.json",
            }
        )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "batch_id": batch_id,
        "target_instance": target_instance,
        "owner_id": owner_id,
        "expected_entry_count": expected,
        "entries": normalized_entries,
    }


def _validate_target(manifest: Mapping[str, Any], target_root: Path) -> None:
    _session_database(target_root, role="target")
    target_instance = _configured_instance_id(target_root)
    if target_instance != manifest["target_instance"]:
        raise AgentMoveError("history backfill target instance does not match manifest")
    target_owner = configured_conversation_owner_id(target_root)
    if target_owner != manifest["owner_id"]:
        raise AgentMoveError("history backfill target owner does not match manifest")
    targets = _agent_lifecycle_ids(target_root)
    for entry in manifest["entries"]:
        actual = targets.get(entry["target_agent_id"].casefold())
        if actual is None:
            raise AgentMoveError(
                f"target Agent {entry['target_agent_id']!r} does not exist exactly once"
            )
        if not valid_agent_lifecycle_id(actual) or actual != entry[
            "target_agent_lifecycle_id"
        ]:
            raise AgentMoveError(
                f"target Agent {entry['target_agent_id']!r} lifecycle does not match"
            )


def _capsule_path(capsule_dir: Path, entry: Mapping[str, Any]) -> Path:
    return capsule_dir / str(entry["capsule_name"])


def export_history_capsules(
    source_root: Path | str,
    source_instance: str,
    manifest_payload: Mapping[str, Any],
    capsule_dir: Path | str,
) -> dict[str, Any]:
    """Export one source instance's entries using that instance's native SQLite."""

    root = Path(source_root).expanduser().resolve()
    manifest = validate_manifest(manifest_payload)
    expected_instance = str(source_instance or "").strip().upper()
    if _configured_instance_id(root) != expected_instance:
        raise AgentMoveError("history backfill source instance does not match config")
    if configured_conversation_owner_id(root) != manifest["owner_id"]:
        raise AgentMoveError("history backfill source owner does not match manifest")
    _session_database(root, role="source")
    entries = [
        entry
        for entry in manifest["entries"]
        if entry["source_instance"] == expected_instance
    ]
    if not entries:
        raise AgentMoveError("manifest has no entries for this source instance")
    source_lifecycles = _agent_lifecycle_ids(root)
    for entry in entries:
        actual = source_lifecycles.get(entry["source_agent_id"].casefold())
        if actual is None:
            raise AgentMoveError(
                f"source Agent {entry['source_agent_id']!r} does not exist exactly once"
            )
        if not valid_agent_lifecycle_id(actual) or actual != entry[
            "source_agent_lifecycle_id"
        ]:
            raise AgentMoveError(
                f"source Agent {entry['source_agent_id']!r} lifecycle does not match"
            )

    destination = Path(capsule_dir).expanduser().absolute()
    if destination.is_symlink():
        raise AgentMoveError("history backfill capsule directory must not be a symlink")
    destination.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for entry in entries:
        capsule = export_conversation_continuity_snapshot(
            root,
            source_instance=entry["source_instance"],
            agent_id=entry["source_agent_id"],
            transfer_id=entry["transfer_id"],
            history_mode="move",
            explicit_owner_id=manifest["owner_id"],
        )
        path = _capsule_path(destination, entry)
        replayed = False
        if path.exists():
            if path.is_symlink() or read_config_json(path) != capsule:
                raise AgentMoveError(
                    f"frozen capsule already exists with different content: {path.name}"
                )
            replayed = True
        else:
            write_config_json(path, capsule, expected_revision=None)
        rows.append(
            {
                "source_instance": entry["source_instance"],
                "source_agent_id": entry["source_agent_id"],
                "target_agent_id": entry["target_agent_id"],
                "transfer_id": entry["transfer_id"],
                "capsule_name": entry["capsule_name"],
                "capsule_digest": capsule["capsule_digest"],
                "replayed": replayed,
            }
        )
    return {
        "ok": True,
        "mode": "export",
        "batch_id": manifest["batch_id"],
        "source_instance": expected_instance,
        "entries": rows,
    }


def _load_capsules(
    manifest: Mapping[str, Any], capsule_dir: Path | str
) -> list[dict[str, Any]]:
    directory = Path(capsule_dir).expanduser().absolute()
    if directory.is_symlink() or not directory.is_dir():
        raise AgentMoveError("history backfill capsule directory is invalid")
    expected_names = {entry["capsule_name"] for entry in manifest["entries"]}
    observed_names = {
        path.name
        for path in directory.glob("*.conversation-capsule.json")
        if path.is_file()
    }
    if observed_names != expected_names:
        raise AgentMoveError("history backfill capsule set does not match manifest")

    capsules: list[dict[str, Any]] = []
    for entry in manifest["entries"]:
        path = _capsule_path(directory, entry)
        if path.is_symlink():
            raise AgentMoveError("history backfill capsule must not be a symlink")
        try:
            capsule = dict(read_config_json(path))
            validate_conversation_continuity_capsule(
                capsule,
                owner_id=manifest["owner_id"],
                source_agent_id=entry["source_agent_id"],
                transfer_id=entry["transfer_id"],
                history_mode="move",
            )
        except (OSError, ValueError, SessionStoreError) as exc:
            raise AgentMoveError(
                f"history backfill capsule is invalid: {path.name}"
            ) from exc
        if str(capsule.get("source_instance") or "").upper() != entry[
            "source_instance"
        ]:
            raise AgentMoveError(
                f"history backfill capsule source does not match: {path.name}"
            )
        capsules.append(capsule)
    return capsules


def _message_count(store: SessionStore, *, owner_id: str, agent_id: str) -> int:
    count = 0
    sessions = store.list_sessions(
        owner_id=owner_id,
        agent_id=agent_id,
        include_archived=True,
        limit=500,
    )
    for session in sessions:
        after = 0
        while True:
            rows = store.messages(
                session["session_id"],
                owner_id=owner_id,
                after_ordinal=after,
                limit=1000,
            )
            count += len(rows)
            if len(rows) < 1000:
                break
            after = int(rows[-1]["ordinal"])
    return count


def _message_projection(
    store: SessionStore, *, session_id: str, owner_id: str
) -> list[dict[str, Any]]:
    projection: list[dict[str, Any]] = []
    after = 0
    while True:
        rows = store.messages(
            session_id,
            owner_id=owner_id,
            after_ordinal=after,
            limit=1000,
        )
        projection.extend(
            {
                "message_id": message["message_id"],
                "ordinal": int(message["ordinal"]),
                "content_hash": message["content_hash"],
            }
            for message in rows
        )
        if len(rows) < 1000:
            break
        after = int(rows[-1]["ordinal"])
    return projection


def _plan_from_snapshot(
    manifest: Mapping[str, Any],
    capsules: list[dict[str, Any]],
    snapshot_path: Path,
) -> dict[str, Any]:
    store = SessionStore(snapshot_path, instance_id=manifest["target_instance"])
    target_state: list[dict[str, Any]] = []
    for entry in manifest["entries"]:
        sessions = store.list_sessions(
            owner_id=manifest["owner_id"],
            agent_id=entry["target_agent_id"],
            include_archived=True,
            limit=500,
        )
        target_capsule = store.export_conversation_continuity(
            owner_id=manifest["owner_id"],
            agent_id=entry["target_agent_id"],
            source_instance=manifest["target_instance"],
            transfer_id=(
                f"history-backfill-plan:{manifest['batch_id']}:"
                f"{entry['target_agent_id']}"
            ),
            history_mode="move",
        )
        bindings_by_session = {
            item["source_session_id"]: sorted(
                (
                    {
                        "surface": binding["surface"],
                        "channel_key": binding["channel_key"],
                    }
                    for binding in item["bindings"]
                ),
                key=lambda binding: (binding["surface"], binding["channel_key"]),
            )
            for item in target_capsule["sessions"]
        }
        for session in sorted(sessions, key=lambda item: item["session_id"]):
            messages = _message_projection(
                store,
                session_id=session["session_id"],
                owner_id=manifest["owner_id"],
            )
            target_state.append(
                {
                    "agent_id": entry["target_agent_id"],
                    "session_id": session["session_id"],
                    "status": session["status"],
                    "is_default": bool(session["is_default"]),
                    "revision": int(session["revision"]),
                    "history_generation": int(session["history_generation"]),
                    "next_message_ordinal": int(session["next_message_ordinal"]),
                    "bindings": bindings_by_session.get(session["session_id"], []),
                    "messages": messages,
                }
            )
    target_state_digest = hashlib.sha256(
        _canonical_json({"sessions": target_state})
    ).hexdigest()
    entries: list[dict[str, Any]] = []
    for entry, capsule in zip(manifest["entries"], capsules, strict=True):
        before = _message_count(
            store, owner_id=manifest["owner_id"], agent_id=entry["target_agent_id"]
        )
        result = store.import_conversation_continuity(
            capsule,
            owner_id=manifest["owner_id"],
            agent_id=entry["target_agent_id"],
            transfer_id=entry["transfer_id"],
            history_mode="move",
        )
        after = _message_count(
            store, owner_id=manifest["owner_id"], agent_id=entry["target_agent_id"]
        )
        summary = capsule.get("summary") or {}
        entries.append(
            {
                "source_instance": entry["source_instance"],
                "source_agent_id": entry["source_agent_id"],
                "target_agent_id": entry["target_agent_id"],
                "transfer_id": entry["transfer_id"],
                "capsule_digest": capsule["capsule_digest"],
                "source_sessions": int(summary.get("session_count") or 0),
                "eligible_messages": int(summary.get("eligible_message_count") or 0),
                "excluded_messages": int(summary.get("excluded_message_count") or 0),
                "target_messages_before": before,
                "target_messages_after": after,
                "imported_messages": int(result.get("imported_messages") or 0),
                "deduplicated_messages": int(
                    result.get("deduplicated_messages") or 0
                ),
                "target_session_count": len(result.get("target_session_ids") or []),
            }
        )
    return {"entries": entries, "target_state_digest": target_state_digest}


def dry_run_history_backfill(
    target_root: Path | str,
    manifest_payload: Mapping[str, Any],
    *,
    capsule_dir: Path | str,
) -> dict[str, Any]:
    root = Path(target_root).expanduser().resolve()
    manifest = validate_manifest(manifest_payload)
    _validate_target(manifest, root)
    result, _capsule_rows = _prepare_plan(root, manifest, capsule_dir)
    return result


def _prepare_plan(
    root: Path,
    manifest: Mapping[str, Any],
    capsule_dir: Path | str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    capsules = _load_capsules(manifest, capsule_dir)
    with tempfile.TemporaryDirectory(prefix="hashi-history-backfill-plan-") as name:
        snapshot = Path(name) / "sessions.sqlite3"
        _snapshot_sqlite(root / "state" / "sessions.sqlite3", snapshot)
        if _has_nonterminal_runs(
            snapshot, instance_id=str(manifest["target_instance"])
        ):
            raise AgentMoveError(
                "target SessionStore contains non-terminal Runs; backfill is blocked"
            )
        plan = _plan_from_snapshot(manifest, capsules, snapshot)
    digest_payload = {
        "manifest": manifest,
        "target_state_digest": plan["target_state_digest"],
        "entries": plan["entries"],
    }
    result = {
        "ok": True,
        "mode": "dry-run",
        "batch_id": manifest["batch_id"],
        "target_instance": manifest["target_instance"],
        "owner_id": manifest["owner_id"],
        "target_state_digest": plan["target_state_digest"],
        "plan_digest": hashlib.sha256(_canonical_json(digest_payload)).hexdigest(),
        "target_writes": 0,
        **plan,
    }
    return result, capsules


def _reject_external_links(root: Path) -> None:
    if not root.exists():
        return
    if _is_external_link(root):
        raise AgentMoveError("history backfill backup tree contains a reparse point")
    try:
        root_mode = root.lstat().st_mode
    except OSError as exc:
        raise AgentMoveError("could not inspect history backfill backup tree") from exc
    if stat.S_ISREG(root_mode):
        return
    if not stat.S_ISDIR(root_mode):
        raise AgentMoveError(
            "history backfill backup tree contains a non-regular file"
        )

    def fail_walk(error: OSError) -> None:
        raise AgentMoveError("could not inspect history backfill backup tree") from error

    for current, directories, files in os.walk(
        root, followlinks=False, onerror=fail_walk
    ):
        current_path = Path(current)
        for name in directories:
            child = current_path / name
            if _is_external_link(child) or not child.is_dir():
                raise AgentMoveError(
                    "history backfill backup tree contains a reparse point"
                )
        for name in files:
            child = current_path / name
            try:
                regular = stat.S_ISREG(child.lstat().st_mode)
            except OSError as exc:
                raise AgentMoveError(
                    "could not inspect history backfill backup file"
                ) from exc
            if _is_external_link(child) or not regular:
                raise AgentMoveError(
                    "history backfill backup tree contains a non-regular file"
                )


def _reject_reparse_parents(path: Path) -> None:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    while current != current.parent:
        if _is_external_link(current):
            raise AgentMoveError("history backfill backup parent is a reparse point")
        current = current.parent


def _backup_target(
    root: Path,
    backup_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if backup_dir.exists():
        raise AgentMoveError("history backfill backup directory already exists")
    _session_database(root, role="target")
    state_root = (root / "state").absolute()
    workspace_source = (state_root / "session_workspaces").absolute()
    database_source = (state_root / "sessions.sqlite3").absolute()
    if (
        backup_dir.is_relative_to(state_root)
        or workspace_source.is_relative_to(backup_dir)
        or database_source.is_relative_to(backup_dir)
    ):
        raise AgentMoveError("history backfill backup directory overlaps target state")
    _reject_reparse_parents(backup_dir.parent)
    backup_dir.parent.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir()
    protect_private_file(backup_dir)
    database_backup = backup_dir / "sessions.sqlite3"
    _snapshot_sqlite(database_source, database_backup)
    _reject_external_links(database_backup)
    _reject_external_links(workspace_source)
    workspace_copied = workspace_source.is_dir()
    if workspace_copied:
        workspace_backup = backup_dir / "session_workspaces"
        shutil.copytree(workspace_source, workspace_backup, symlinks=True)
        _reject_external_links(workspace_backup)
    reusable_manifest = {
        key: value for key, value in manifest.items() if key != "entries"
    }
    reusable_manifest["entries"] = [
        {key: entry[key] for key in _ENTRY_KEYS}
        for entry in manifest["entries"]
    ]
    write_config_json(
        backup_dir / "manifest.json",
        reusable_manifest,
        expected_revision=None,
    )
    receipt = {
        "schema_version": 1,
        "batch_id": manifest["batch_id"],
        "target_instance": manifest["target_instance"],
        "sessions_sha256": _sha256(database_backup),
        "session_workspaces_copied": workspace_copied,
    }
    write_config_json(
        backup_dir / "receipt.json",
        receipt,
        expected_revision=None,
    )
    return receipt


@contextmanager
def _locked_target(root: Path) -> Iterator[None]:
    paths = build_bridge_paths(root, bridge_home=root, canonical_home=True)
    lock = InstanceLock(
        paths.lock_path,
        pid_path=paths.pid_path,
        instance_id=paths.instance_id,
    )
    try:
        lock.acquire()
    except RuntimeError as exc:
        raise AgentMoveError(
            "target instance is running; stop that exact instance before backfill"
        ) from exc
    try:
        yield
    finally:
        lock.release()


def apply_history_backfill(
    target_root: Path | str,
    manifest_payload: Mapping[str, Any],
    *,
    capsule_dir: Path | str,
    expected_plan_digest: str,
    backup_dir: Path | str,
) -> dict[str, Any]:
    root = Path(target_root).expanduser().resolve()
    manifest = validate_manifest(manifest_payload)
    _validate_target(manifest, root)
    expected = str(expected_plan_digest or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise AgentMoveError("expected_plan_digest must be a SHA-256 digest")
    backup = Path(backup_dir).expanduser().absolute()
    with _locked_target(root):
        _validate_target(manifest, root)
        plan, capsules = _prepare_plan(root, manifest, capsule_dir)
        if plan["plan_digest"] != expected:
            raise AgentMoveError("history backfill plan changed; run dry-run again")
        backup_receipt = _backup_target(root, backup, manifest)
        store = SessionStore(
            root / "state" / "sessions.sqlite3",
            instance_id=manifest["target_instance"],
        )
        results: list[dict[str, Any]] = []
        preexisting_claims = {
            entry["transfer_id"]
            for entry in manifest["entries"]
            if store.conversation_continuity_import_status(entry["transfer_id"])
        }
        attempted_transfers: list[str] = []
        try:
            for entry, capsule in zip(manifest["entries"], capsules, strict=True):
                attempted_transfers.append(entry["transfer_id"])
                result = store.import_conversation_continuity(
                    capsule,
                    owner_id=manifest["owner_id"],
                    agent_id=entry["target_agent_id"],
                    transfer_id=entry["transfer_id"],
                    history_mode="move",
                )
                results.append(
                    {
                        "target_agent_id": entry["target_agent_id"],
                        "transfer_id": entry["transfer_id"],
                        "imported_messages": int(result.get("imported_messages") or 0),
                        "deduplicated_messages": int(
                            result.get("deduplicated_messages") or 0
                        ),
                        "replayed": bool(result.get("replayed")),
                    }
                )
        except Exception as exc:
            rollback_errors: list[tuple[str, str]] = []
            for transfer_id in reversed(attempted_transfers):
                newly_persisted = (
                    transfer_id not in preexisting_claims
                    and store.conversation_continuity_import_status(transfer_id)
                )
                if newly_persisted:
                    try:
                        store.rollback_conversation_continuity(transfer_id)
                    except Exception as rollback_exc:
                        rollback_errors.append(
                            (transfer_id, type(rollback_exc).__name__)
                        )
            if rollback_errors:
                detail = ",".join(
                    f"{transfer_id}({error_type})"
                    for transfer_id, error_type in rollback_errors
                )
                raise AgentMoveError(
                    "history backfill failed; compensation incomplete for "
                    f"{detail}; manual recovery required"
                ) from exc
            raise AgentMoveError(
                f"history backfill failed and was compensated: {type(exc).__name__}"
            ) from exc
    return {
        "ok": True,
        "mode": "apply",
        "batch_id": manifest["batch_id"],
        "plan_digest": expected,
        "backup": backup_receipt,
        "entries": results,
    }


def history_backfill_status(
    target_root: Path | str,
    manifest_payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = Path(target_root).expanduser().resolve()
    manifest = validate_manifest(manifest_payload)
    _validate_target(manifest, root)
    with tempfile.TemporaryDirectory(prefix="hashi-history-backfill-status-") as name:
        snapshot = Path(name) / "sessions.sqlite3"
        _snapshot_sqlite(root / "state" / "sessions.sqlite3", snapshot)
        store = SessionStore(snapshot, instance_id=manifest["target_instance"])
        entries = []
        for entry in manifest["entries"]:
            status = store.conversation_continuity_import_status(entry["transfer_id"])
            entries.append(
                {
                    "target_agent_id": entry["target_agent_id"],
                    "transfer_id": entry["transfer_id"],
                    "status": "imported" if status else "not_imported",
                    "imported_messages": int(
                        (status or {}).get("imported_messages") or 0
                    ),
                }
            )
    return {
        "ok": True,
        "mode": "status",
        "batch_id": manifest["batch_id"],
        "entries": entries,
    }


def rollback_history_backfill(
    target_root: Path | str,
    manifest_payload: Mapping[str, Any],
) -> dict[str, Any]:
    root = Path(target_root).expanduser().resolve()
    manifest = validate_manifest(manifest_payload)
    _validate_target(manifest, root)
    results = []
    with _locked_target(root):
        _validate_target(manifest, root)
        if _has_nonterminal_runs(
            root / "state" / "sessions.sqlite3",
            instance_id=manifest["target_instance"],
        ):
            raise AgentMoveError(
                "target SessionStore contains non-terminal Runs; backfill is blocked"
            )
        store = SessionStore(
            root / "state" / "sessions.sqlite3",
            instance_id=manifest["target_instance"],
        )
        for entry in reversed(manifest["entries"]):
            result = store.rollback_conversation_continuity(entry["transfer_id"])
            results.append(
                {
                    "target_agent_id": entry["target_agent_id"],
                    "transfer_id": entry["transfer_id"],
                    "removed_messages": int(result.get("removed_messages") or 0),
                    "replayed": bool(result.get("replayed")),
                }
            )
    return {
        "ok": True,
        "mode": "rollback",
        "batch_id": manifest["batch_id"],
        "entries": results,
    }
