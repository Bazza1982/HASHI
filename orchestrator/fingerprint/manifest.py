"""Portable projections. Canonical Audit remains the authoritative history."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from orchestrator.config_json import file_write_lock
from .models import SCHEMA, canonical_json, relative_target

KIND = "hashi-fingerprint-manifest"
OWNER_KIND = "hashi-fingerprint-namespace"
DIRECTORY = ".hashi-fingerprint"


def _regular(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or path.is_symlink():
        raise ValueError("evidence destination must be a single regular file")


def load_json(path: Path) -> dict:
    _regular(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("evidence projection exceeds the supported size")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("evidence must be an object")
    return value


def revision(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    _regular(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_projection(path: Path, payload: dict, *, expected_revision: str | None):
    if path.exists() or path.is_symlink():
        _regular(path)
    descriptor, name = tempfile.mkstemp(prefix=".fingerprint-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if revision(path) != expected_revision:
            raise ValueError("evidence projection changed; preserving external edit")
        os.replace(name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def namespace(root: Path, root_id: str) -> Path:
    with file_write_lock(root / DIRECTORY):
        return _namespace_locked(root, root_id)


def _namespace_locked(root: Path, root_id: str) -> Path:
    folder = root / DIRECTORY
    created = False
    try:
        folder.mkdir()
        created = True
    except FileExistsError:
        pass
    if folder.is_symlink() or getattr(folder, "is_junction", lambda: False)() or not folder.is_dir():
        raise ValueError("fingerprint namespace is not an owned directory")
    owner = folder / "owner.json"
    if created:
        with owner.open("xb") as stream:
            stream.write(canonical_json({"kind": OWNER_KIND, "schema": SCHEMA, "root_id": root_id}) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    # Existing unrelated directories are never adopted, even when empty.
    if load_json(owner) != {"kind": OWNER_KIND, "schema": SCHEMA, "root_id": root_id}:
        raise ValueError("fingerprint namespace belongs to another owner")
    return folder


def read_manifest(root: Path) -> dict | None:
    path = root / DIRECTORY / "manifest.json"
    if not path.exists():
        return None
    if path.parent.is_symlink() or getattr(path.parent, "is_junction", lambda: False)():
        raise ValueError("linked evidence directory is not supported")
    value = load_json(path)
    if value.get("kind") != KIND or value.get("schema") != SCHEMA:
        raise ValueError("unrecognized manifest; preserving existing evidence")
    if not isinstance(value.get("artifacts"), dict) or not isinstance(value.get("receipts"), dict):
        raise ValueError("invalid manifest")
    if not isinstance(value.get("root_id"), str) or not value["root_id"]:
        raise ValueError("invalid manifest root")
    for relative, entry in value["artifacts"].items():
        relative_target(relative)
        if not isinstance(entry, dict) or not isinstance(entry.get("receipts"), list):
            raise ValueError("invalid artifact index")
        if not entry["receipts"] or entry.get("latest_receipt") not in entry["receipts"]:
            raise ValueError("latest receipt must belong to the artifact history")
        if not all(isinstance(event_id, str) and event_id for event_id in entry["receipts"]):
            raise ValueError("invalid receipt history")
    return value


def file_token(file_id) -> str | None:
    return hashlib.sha256(canonical_json(list(file_id))).hexdigest() if file_id is not None else None


def existing_artifact(root: Path, relative: str, before, default: str) -> str:
    manifest = read_manifest(root)
    entry = (manifest or {}).get("artifacts", {}).get(relative)
    if (entry and entry.get("state") == "present" and before.version.state == "present"
            and entry.get("file_token") == file_token(before.file_id)):
        return str(entry["artifact_id"])
    return default


def public_receipt(receipt: dict) -> dict:
    """Explicit allow-list: no tool arguments, content, logs or machine paths."""
    keys = ("schema", "operation_id", "identity", "tool_call_id", "tool_name", "artifact_id",
            "root_id", "relative_path", "action", "before", "after", "business_status",
            "evidence_status", "marker_status", "evidence_grade", "policy_version",
            "started_at", "observed_at", "event_id", "prepared_event_id", "coverage",
            "marker_changed_bytes", "business_changed_bytes")
    result = {key: receipt[key] for key in keys if key in receipt}
    if "relative_path" in result:
        result["relative_path"] = relative_target(result["relative_path"])
    return result


def project(root: Path, receipt: dict, *, file_id=None) -> Path:
    folder = namespace(root, receipt["root_id"])
    path = folder / "manifest.json"
    with file_write_lock(path):
        expected_revision = revision(path)
        value = read_manifest(root) or {
            "kind": KIND, "schema": SCHEMA, "root_id": receipt["root_id"],
            "authentication": "unsigned", "artifacts": {}, "receipts": {},
            "coverage": {"scope": "listed operations only", "complete_repository": False},
        }
        if value["root_id"] != receipt["root_id"]:
            raise ValueError("manifest root mismatch")
        relative = relative_target(receipt["relative_path"])
        record = public_receipt(receipt)
        value["receipts"][receipt["event_id"]] = record
        previous = value["artifacts"].get(relative, {})
        history = list(previous.get("receipts", []))
        if receipt["event_id"] not in history:
            history.append(receipt["event_id"])
        previous_record = value["receipts"].get(previous.get("latest_receipt"), {})
        if previous_record.get("observed_at", "") > record.get("observed_at", ""):
            # Rebuilding one Agent's projection must not rewind a newer peer operation.
            value["artifacts"][relative] = {**previous, "receipts": history}
        else:
            value["artifacts"][relative] = {
                "artifact_id": receipt["artifact_id"], "state": receipt["after"]["state"],
                "sha256": receipt["after"].get("sha256"), "latest_receipt": receipt["event_id"],
                "receipts": history, "file_token": file_token(file_id),
            }
        atomic_projection(path, value, expected_revision=expected_revision)
    return path


def sidecar_for(path: Path, record: dict) -> dict:
    return {"kind": "hashi-fingerprint-sidecar", "schema": SCHEMA,
        "target": path.name, "authentication": "unsigned", "receipt": public_receipt(record)}


def write_sidecar(path: Path, record: dict) -> Path:
    destination = path.with_name(path.name + ".hashi.json")
    payload = sidecar_for(path, record)
    with file_write_lock(destination):
        expected_revision = revision(destination)
        if destination.exists() or destination.is_symlink():
            old = load_json(destination)
            if (old.get("kind") != payload["kind"] or old.get("target") != path.name
                    or old.get("receipt", {}).get("artifact_id") != record["artifact_id"]):
                raise ValueError("sidecar name is already in use")
        atomic_projection(destination, payload, expected_revision=expected_revision)
    return destination
