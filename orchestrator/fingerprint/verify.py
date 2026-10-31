"""Standalone exact-byte and linkage checks; never author authentication."""
from __future__ import annotations

from pathlib import Path

from .execution import observe
from .manifest import read_manifest, load_json
from .models import FileVersion, relative_target


def check_receipt(receipt: dict) -> None:
    required = {"schema", "event_id", "operation_id", "artifact_id", "root_id", "relative_path",
                "before", "after", "identity", "evidence_grade", "tool_call_id", "tool_name"}
    if not isinstance(receipt, dict) or not required <= set(receipt):
        raise ValueError("incomplete receipt")
    if type(receipt["schema"]) is not int or receipt["schema"] != 1:
        raise ValueError("unsupported receipt schema")
    for key in ("event_id", "operation_id", "artifact_id", "root_id", "tool_call_id", "tool_name"):
        value = receipt[key]
        if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(char in value for char in ("\\", "/", "\n", "\r", "\x00")):
            raise ValueError("invalid receipt association")
    relative_target(receipt["relative_path"])
    try:
        FileVersion(**receipt["before"])
        FileVersion(**receipt["after"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid file-version evidence") from exc
    if receipt["evidence_grade"] not in {"controlled_execution", "interval_observation", "engine_reported", "unknown"}:
        raise ValueError("invalid evidence grade")
    from datetime import datetime
    from .models import RunIdentity
    enumerations = {
        "action": {"create", "modify", "delete", "no_change", "unknown"},
        "business_status": {"completed", "failed", "cancelled", "unknown"},
        "evidence_status": {"persisted", "reconciled_observation"},
        "marker_status": {"inline", "manifest", "deleted", "pending", "conflict", "not_implemented"},
    }
    for key, choices in enumerations.items():
        if key in receipt and (not isinstance(receipt[key], str) or receipt[key] not in choices):
            raise ValueError("invalid receipt status")
    for key in ("started_at", "observed_at"):
        if key in receipt:
            value = receipt[key]
            if not isinstance(value, str) or len(value) > 64:
                raise ValueError("invalid receipt time")
            parsed = datetime.fromisoformat(value)
            if parsed.utcoffset() is None:
                raise ValueError("receipt time requires timezone")
    if "policy_version" in receipt:
        value = receipt["policy_version"]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("invalid receipt policy version")
    if receipt.get("prepared_event_id") is not None:
        value = receipt["prepared_event_id"]
        if not isinstance(value, str) or not value or len(value) > 512 or any(c in value for c in ("\\", "/", "\n", "\r", "\x00")):
            raise ValueError("invalid preparation association")
    for key in ("marker_changed_bytes", "business_changed_bytes"):
        if key in receipt and receipt[key] is not None and type(receipt[key]) is not bool:
            raise ValueError("invalid byte-change fact")
    try:
        RunIdentity(**receipt["identity"])
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid receipt identity") from exc


def inspect_entry(root: Path, relative: str, manifest: dict) -> dict:
    relative = relative_target(relative)
    entry = manifest["artifacts"][relative]
    receipt = manifest["receipts"].get(entry.get("latest_receipt"))
    result = {"relative_path": relative, "artifact_id": entry.get("artifact_id"),
        "version": "unknown", "evidence_link": "missing", "evidence_grade": "unknown",
        "authentication": "unsigned", "coverage": "declared_operations_only"}
    try:
        check_receipt(receipt)
        if (receipt["event_id"] != entry["latest_receipt"] or receipt["artifact_id"] != entry["artifact_id"]
                or receipt["relative_path"] != relative or receipt["root_id"] != manifest["root_id"]
                or receipt["after"].get("sha256") != entry.get("sha256")
                or receipt["after"]["state"] != entry["state"]):
            raise ValueError("receipt/manifest mismatch")
        for event_id in entry.get("receipts", []):
            history = manifest["receipts"].get(event_id)
            check_receipt(history)
            if history["event_id"] != event_id or history["relative_path"] != relative:
                raise ValueError("history link mismatch")
        result["evidence_link"] = "consistent"
        result["evidence_grade"] = receipt["evidence_grade"]
        result["business_status"] = receipt.get("business_status", "unknown")
        result["marker_status"] = receipt.get("marker_status", "unknown")
        result["attribution"] = "controlled_observation" if receipt["evidence_grade"] == "controlled_execution" else "unknown"
    except (TypeError, ValueError, KeyError):
        result["evidence_link"] = "corrupt"
        return result
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        result["version"] = "outside_root"
        return result
    try:
        actual = observe(path).version
        expected = FileVersion(**receipt["after"])
        if actual.state in {"unknown", "conflict"} or expected.state in {"unknown", "conflict"}:
            result["version"] = "unknown"
        else:
            result["version"] = "match" if (actual.state, actual.sha256) == (expected.state, expected.sha256) else "mismatch"
    except (OSError, ValueError, RuntimeError):
        result["version"] = "unavailable"
    return result


def verify(root: Path) -> dict:
    root = Path(root).resolve()
    manifest = read_manifest(root)
    if manifest is None:
        return {"status": "missing", "authentication": "unsigned", "files": [], "coverage_complete": False}
    rows = [inspect_entry(root, relative, manifest) for relative in manifest["artifacts"]]
    passed = bool(rows) and all(row["version"] == "match" and row["evidence_link"] == "consistent" for row in rows)
    return {"status": "consistent" if passed else "incomplete", "files": rows,
            "authentication": "unsigned", "coverage_complete": False,
            "scope": "listed artifact versions only; no complete audit-chain or author proof"}


def inspect(path: Path, *, root: Path | None = None) -> dict:
    path = Path(path).absolute()
    if root is not None:
        root = Path(root).resolve()
    else:
        root = next((parent for parent in path.parents if (parent / ".hashi-fingerprint" / "manifest.json").exists()), path.parent)
    manifest = read_manifest(root)
    if manifest is not None:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            return {"version": "outside_root", "authentication": "unsigned"}
        if relative in manifest["artifacts"]:
            return inspect_entry(root, relative, manifest)
    sidecar = path.with_name(path.name + ".hashi.json")
    if sidecar.exists():
        data = load_json(sidecar)
        if data.get("kind") != "hashi-fingerprint-sidecar" or data.get("target") != path.name:
            raise ValueError("unrecognized sidecar")
        receipt = data["receipt"]
        check_receipt(receipt)
        local = {**receipt, "relative_path": path.name}
        projected = {"root_id": local["root_id"], "artifacts": {path.name: {
            "artifact_id": local["artifact_id"], "state": local["after"]["state"],
            "sha256": local["after"].get("sha256"), "latest_receipt": local["event_id"],
            "receipts": [local["event_id"]]}}, "receipts": {local["event_id"]: local}}
        return inspect_entry(path.parent, path.name, projected)
    return {"version": "unknown", "evidence_link": "missing", "authentication": "unsigned"}
