"""Package writer and reader for HASHI Hermes agent transfer archives."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .schema import (
    DryRunReport,
    default_profile_policy,
    default_secrets_policy,
    validate_manifest,
    validate_normalized_agent,
)


class TransferPackageError(ValueError):
    """Raised when a transfer package cannot be read or verified."""


@dataclass(frozen=True)
class PackageBuildResult:
    package_path: Path
    manifest: dict[str, Any]
    checksums: dict[str, str]
    dry_run_plan: dict[str, Any]


@dataclass(frozen=True)
class TransferPackage:
    package_path: Path
    manifest: dict[str, Any]
    normalized_agent: dict[str, Any]
    profile_policy: dict[str, Any]
    secrets_policy: dict[str, Any]
    checksums: dict[str, str]
    dry_run_plan: dict[str, Any]
    names: list[str]


def create_transfer_package(
    output_path: Path | str,
    *,
    manifest: dict[str, Any],
    normalized_agent: dict[str, Any],
    files: Mapping[str, bytes | str | Path] | None = None,
    profile_policy: dict[str, Any] | None = None,
    secrets_policy: dict[str, Any] | None = None,
    dry_run_report: DryRunReport | dict[str, Any] | None = None,
    migration_report: str = "",
    post_migration_self_check: str = "",
) -> PackageBuildResult:
    """Create a `.hashi-hermes-agent` zip package.

    The caller provides already-normalized content. Runtime-specific exporters
    are intentionally outside Phase 1.
    """

    validate_manifest(manifest)
    validate_normalized_agent(normalized_agent)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    entries: dict[str, bytes] = {
        "manifest.json": _json_bytes(manifest),
        "normalized_agent.json": _json_bytes(normalized_agent),
        "profile_policy.json": _json_bytes(profile_policy or default_profile_policy()),
        "secrets.policy.json": _json_bytes(secrets_policy or default_secrets_policy()),
        "audit/dry_run_plan.json": _json_bytes(_dry_run_dict(dry_run_report, manifest)),
        "audit/migration_report.md": _text_bytes(migration_report),
        "audit/post_migration_self_check.md": _text_bytes(post_migration_self_check),
    }
    for name, value in (files or {}).items():
        safe_name = _safe_package_name(name)
        if safe_name in entries:
            raise TransferPackageError(f"duplicate package entry: {safe_name}")
        entries[safe_name] = _entry_bytes(value)

    checksums = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in sorted(entries.items())
        if name != "audit/checksums.json"
    }
    entries["audit/checksums.json"] = _json_bytes({"schema_version": 1, "files": checksums})

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            archive.writestr(name, content)

    return PackageBuildResult(
        package_path=output,
        manifest=dict(manifest),
        checksums=checksums,
        dry_run_plan=json.loads(entries["audit/dry_run_plan.json"].decode("utf-8")),
    )


def read_transfer_package(package_path: Path | str, *, verify: bool = True) -> TransferPackage:
    path = Path(package_path)
    if not path.exists():
        raise TransferPackageError(f"package not found: {path}")
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        for name in names:
            _safe_package_name(name)
        manifest = _read_json(archive, "manifest.json")
        normalized_agent = _read_json(archive, "normalized_agent.json")
        profile_policy = _read_json(archive, "profile_policy.json")
        secrets_policy = _read_json(archive, "secrets.policy.json")
        checksums_obj = _read_json(archive, "audit/checksums.json")
        dry_run_plan = _read_json(archive, "audit/dry_run_plan.json")
        checksums = checksums_obj.get("files", {})
        if not isinstance(checksums, dict):
            raise TransferPackageError("audit/checksums.json files must be an object")
        validate_manifest(manifest)
        validate_normalized_agent(normalized_agent)
        if verify:
            verify_package_checksums(path)
    return TransferPackage(
        package_path=path,
        manifest=manifest,
        normalized_agent=normalized_agent,
        profile_policy=profile_policy,
        secrets_policy=secrets_policy,
        checksums=checksums,
        dry_run_plan=dry_run_plan,
        names=names,
    )


def verify_package_checksums(package_path: Path | str) -> dict[str, str]:
    path = Path(package_path)
    with zipfile.ZipFile(path, "r") as archive:
        checksums_obj = _read_json(archive, "audit/checksums.json")
        expected = checksums_obj.get("files", {})
        if not isinstance(expected, dict):
            raise TransferPackageError("audit/checksums.json files must be an object")
        actual: dict[str, str] = {}
        for name in archive.namelist():
            _safe_package_name(name)
            if name == "audit/checksums.json":
                continue
            actual[name] = hashlib.sha256(archive.read(name)).hexdigest()
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(name for name in set(actual) & set(expected) if actual[name] != expected[name])
        raise TransferPackageError(
            f"package checksum mismatch missing={missing} extra={extra} changed={changed}"
        )
    return actual


def _dry_run_dict(report: DryRunReport | dict[str, Any] | None, manifest: dict[str, Any]) -> dict[str, Any]:
    if isinstance(report, DryRunReport):
        return report.to_dict()
    if isinstance(report, dict):
        return dict(report)
    return DryRunReport(
        operation="package",
        source_runtime=str(manifest["source_runtime"]),
        target_runtime=str(manifest["target_runtime"]),
        agent_id=str(manifest["agent_id"]),
    ).to_dict()


def _read_json(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise TransferPackageError(f"required package entry missing: {name}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise TransferPackageError(f"invalid JSON package entry: {name}") from exc
    if not isinstance(data, dict):
        raise TransferPackageError(f"JSON package entry must be an object: {name}")
    return data


def _entry_bytes(value: bytes | str | Path) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, Path):
        return value.read_bytes()
    return str(value).encode("utf-8")


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _text_bytes(value: str) -> bytes:
    text = str(value or "")
    if text and not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def _safe_package_name(name: str) -> str:
    normalized = str(name or "").strip().replace("\\", "/")
    parts = Path(normalized).parts
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.startswith("../")
        or ".." in parts
        or normalized.endswith("/")
    ):
        raise TransferPackageError(f"unsafe package entry name: {name!r}")
    return normalized
