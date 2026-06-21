"""Hermes-side importer for HASHI Hermes transfer packages."""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .package import TransferPackage, read_transfer_package
from .schema import DryRunReport, PlannedWrite


class HermesImportError(ValueError):
    """Raised when a transfer package cannot be imported into Hermes."""


@dataclass(frozen=True)
class HermesImportOptions:
    target_profile_name: str | None = None
    overwrite: bool = False
    include_memory: bool = True
    include_schedules: bool = True
    include_skills: bool = True


@dataclass(frozen=True)
class HermesImportPlan:
    profile_dir: Path
    bridge_home: Path
    package_path: Path
    target_profile_name: str
    rollback_dir: Path
    transfer_package: TransferPackage
    bridge_agent_entry: dict[str, Any]
    profile_config: dict[str, Any]
    dry_run_report: DryRunReport
    warnings: list[str]


def plan_hermes_import(
    profile_dir: Path | str,
    bridge_home: Path | str,
    package_path: Path | str,
    *,
    options: HermesImportOptions | None = None,
) -> HermesImportPlan:
    """Build a Hermes import plan without writing files."""

    opts = options or HermesImportOptions()
    profile = Path(profile_dir).resolve()
    bridge = Path(bridge_home).resolve()
    package = read_transfer_package(package_path)
    manifest = package.manifest
    if manifest.get("target_runtime") != "hermes":
        raise HermesImportError("package target_runtime must be 'hermes' for Hermes import")
    if manifest.get("source_runtime") != "hashi":
        raise HermesImportError("package source_runtime must be 'hashi' for Hermes import")
    if not bridge.exists():
        raise HermesImportError(f"Hermes bridge home not found: {bridge}")

    target_profile = str(opts.target_profile_name or package.normalized_agent.get("agent_id") or manifest["agent_id"]).strip()
    if not target_profile:
        raise HermesImportError("target profile name is required")
    if profile.exists() and any(profile.iterdir()) and not opts.overwrite:
        existing_agent = profile / "AGENT.md"
        config = profile / "config.yaml"
        if existing_agent.exists() or config.exists():
            raise HermesImportError(f"target Hermes profile already exists: {profile}")

    rollback_dir = _rollback_dir(bridge, manifest, target_profile)
    bridge_entry = _build_bridge_agent_entry(package.normalized_agent, manifest, target_profile)
    profile_config = _merged_profile_config(profile, package, target_profile)
    warnings = [
        "target Hermes profile is imported in disabled review mode",
        "Hermes secrets and delivery credentials are not imported",
        "Hermes schedules are written as paused review drafts",
    ]
    if "schedules/tasks.json" not in package.names:
        warnings.append("package does not contain schedules/tasks.json")
    if not any(name.startswith("skills/") for name in package.names):
        warnings.append("package does not contain Hermes skill files; hchat skill must be installed separately if needed")

    planned = [
        PlannedWrite(path=str(profile / "AGENT.md"), action="write", description="write imported Hermes instructions"),
        PlannedWrite(path=str(profile / "config.yaml"), action="merge", description="write disabled review-mode profile config"),
        PlannedWrite(path=str(bridge / "agents.yaml"), action="update", description="upsert disabled bridge agent entry"),
        PlannedWrite(path=str(rollback_dir), kind="directory", action="create", description="rollback snapshot"),
    ]
    if opts.include_memory:
        planned.append(PlannedWrite(path=str(profile / "imported_memory"), kind="directory", action="create", description="write portable memory files"))
    if opts.include_schedules:
        planned.append(PlannedWrite(path=str(profile / "imported_schedules"), kind="directory", action="create", description="write paused schedule drafts"))
    if opts.include_skills:
        planned.append(PlannedWrite(path=str(profile / "skills"), kind="directory", action="create", description="copy package skills when present"))

    dry_run = DryRunReport(
        operation="hermes_import",
        source_runtime="hashi",
        target_runtime="hermes",
        agent_id=target_profile,
        planned_writes=planned,
        warnings=warnings,
    )
    return HermesImportPlan(
        profile_dir=profile,
        bridge_home=bridge,
        package_path=Path(package_path),
        target_profile_name=target_profile,
        rollback_dir=rollback_dir,
        transfer_package=package,
        bridge_agent_entry=bridge_entry,
        profile_config=profile_config,
        dry_run_report=dry_run,
        warnings=warnings,
    )


def import_hermes_agent(
    profile_dir: Path | str,
    bridge_home: Path | str,
    package_path: Path | str,
    *,
    options: HermesImportOptions | None = None,
) -> HermesImportPlan:
    """Import a HASHI-origin package into a disabled Hermes profile."""

    plan = plan_hermes_import(profile_dir, bridge_home, package_path, options=options)
    opts = options or HermesImportOptions()
    _write_rollback(plan)
    plan.profile_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(plan.package_path, "r") as archive:
        identity = _read_first_text(
            archive,
            ["identity/agent.md", "identity/hermes_instructions.md"],
            fallback=f"# {plan.target_profile_name}\n\nImported from HASHI.\n",
        )
        (plan.profile_dir / "AGENT.md").write_text(identity, encoding="utf-8")
        _write_import_audit(archive, plan.profile_dir / "hashi_import")
        if opts.include_memory:
            _write_prefix(archive, "memory/", plan.profile_dir / "imported_memory" / _package_slug(plan.transfer_package.manifest))
        if opts.include_schedules:
            _write_paused_schedules(archive, plan.profile_dir / "imported_schedules" / _package_slug(plan.transfer_package.manifest))
        if opts.include_skills:
            _write_prefix(archive, "skills/", plan.profile_dir / "skills")

    _write_yaml_file(plan.profile_dir / "config.yaml", plan.profile_config)
    _upsert_bridge_agent(plan.bridge_home / "agents.yaml", plan.target_profile_name, plan.bridge_agent_entry)
    return plan


def _build_bridge_agent_entry(
    normalized_agent: dict[str, Any],
    manifest: dict[str, Any],
    target_profile: str,
) -> dict[str, Any]:
    return {
        "display_name": normalized_agent.get("display_name") or manifest.get("display_name") or target_profile,
        "emoji": normalized_agent.get("emoji") or "",
        "description": normalized_agent.get("description") or f"Imported from HASHI agent {manifest.get('agent_id')}",
        "profile": target_profile,
        "enabled": False,
        "review_required": True,
        "imported_from": "hashi",
        "source_agent_id": manifest.get("agent_id"),
        "package_id": manifest.get("package_id"),
    }


def _merged_profile_config(profile: Path, package: TransferPackage, target_profile: str) -> dict[str, Any]:
    existing = _read_yaml_file(profile / "config.yaml") if (profile / "config.yaml").exists() else {}
    merged = dict(existing)
    platforms = dict(merged.get("platforms") or {})
    hashi_hchat = dict(platforms.get("hashi_hchat") or {})
    hashi_hchat.update(
        {
            "enabled": False,
            "review_required": True,
            "imported_from": "hashi",
            "source_agent_id": package.manifest.get("agent_id"),
        }
    )
    platforms["hashi_hchat"] = hashi_hchat
    merged["platforms"] = platforms
    merged["hashi_transfer"] = {
        "profile": target_profile,
        "source_runtime": "hashi",
        "source_agent_id": package.manifest.get("agent_id"),
        "package_id": package.manifest.get("package_id"),
        "review_required": True,
        "disabled_until_review": True,
        "secrets_imported": False,
        "session_state_imported": False,
    }
    return merged


def _write_rollback(plan: HermesImportPlan) -> None:
    plan.rollback_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    agents_file = plan.bridge_home / "agents.yaml"
    if agents_file.exists():
        shutil.copy2(agents_file, plan.rollback_dir / "agents.yaml")
        copied.append("agents.yaml")
    if plan.profile_dir.exists():
        shutil.copytree(plan.profile_dir, plan.rollback_dir / "profile", dirs_exist_ok=True)
        copied.append(str(plan.profile_dir))
    manifest = {
        "created_at": datetime.now().isoformat(),
        "package": str(plan.package_path),
        "target_profile_name": plan.target_profile_name,
        "copied": copied,
    }
    (plan.rollback_dir / "rollback_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_import_audit(archive: zipfile.ZipFile, target: Path) -> None:
    prefixes = ("source/", "audit/")
    exact = {"manifest.json", "normalized_agent.json", "profile_policy.json", "secrets.policy.json"}
    target.mkdir(parents=True, exist_ok=True)
    for name in archive.namelist():
        if name in exact or any(name.startswith(prefix) for prefix in prefixes):
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(name))


def _write_prefix(archive: zipfile.ZipFile, prefix: str, target: Path) -> None:
    for name in archive.namelist():
        if not name.startswith(prefix):
            continue
        rel = name[len(prefix) :]
        if not rel or rel.endswith("/"):
            continue
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(archive.read(name))


def _write_paused_schedules(archive: zipfile.ZipFile, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if "schedules/tasks.json" not in archive.namelist():
        return
    schedules = json.loads(archive.read("schedules/tasks.json").decode("utf-8"))
    for section in ("heartbeats", "crons", "nudges"):
        for item in schedules.get(section, []):
            if isinstance(item, dict):
                item["enabled"] = False
                item["import_state"] = "paused_review_draft"
    (target / "tasks.paused.json").write_text(
        json.dumps(schedules, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _upsert_bridge_agent(agents_file: Path, target_profile: str, entry: dict[str, Any]) -> None:
    data = _read_yaml_file(agents_file) if agents_file.exists() else {}
    raw_agents = data.setdefault("agents", {})
    if isinstance(raw_agents, list):
        replaced = False
        for idx, existing in enumerate(raw_agents):
            if isinstance(existing, dict) and existing.get("name") == target_profile:
                raw_agents[idx] = {"name": target_profile, **entry}
                replaced = True
                break
        if not replaced:
            raw_agents.append({"name": target_profile, **entry})
    elif isinstance(raw_agents, dict):
        raw_agents[target_profile] = entry
    else:
        raise HermesImportError("agents.yaml agents must be a mapping or list")
    _write_yaml_file(agents_file, data)


def _read_first_text(archive: zipfile.ZipFile, names: list[str], *, fallback: str) -> str:
    archive_names = set(archive.namelist())
    for name in names:
        if name in archive_names:
            return archive.read(name).decode("utf-8", errors="replace")
    return fallback


def _rollback_dir(bridge: Path, manifest: dict[str, Any], target_profile: str) -> Path:
    return bridge / "private" / "hermes_transfer" / "rollback" / f"{_package_slug(manifest)}-{target_profile}-{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _package_slug(manifest: dict[str, Any]) -> str:
    return str(manifest.get("package_id") or "pkg-unknown").replace("/", "-")


def _read_yaml_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise HermesImportError(f"YAML file must contain a mapping: {path}")
    return data


def _write_yaml_file(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=True), encoding="utf-8")
