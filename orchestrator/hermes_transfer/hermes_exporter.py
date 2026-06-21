"""Hermes-side exporter for HASHI Hermes transfer packages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .package import PackageBuildResult, create_transfer_package
from .schema import (
    PACKAGE_EXT,
    DryRunReport,
    PlannedWrite,
    default_profile_policy,
    default_secrets_policy,
    new_manifest,
    validate_normalized_agent,
)


class HermesExportError(ValueError):
    """Raised when a Hermes profile cannot be exported."""


@dataclass(frozen=True)
class HermesExportOptions:
    include_skills: bool = True
    include_memories: bool = True
    include_cron: bool = True
    max_memory_chars: int = 2200
    max_file_bytes: int = 1_000_000


@dataclass(frozen=True)
class HermesExportPlan:
    profile_dir: Path
    bridge_home: Path
    agent_id: str
    package_path: Path
    manifest: dict[str, Any]
    normalized_agent: dict[str, Any]
    files: dict[str, bytes | str]
    dry_run_report: DryRunReport
    migration_report: str
    post_migration_self_check: str
    warnings: list[str]


def plan_hermes_export(
    profile_dir: Path | str,
    bridge_home: Path | str,
    agent_id: str,
    output_path: Path | str | None = None,
    *,
    options: HermesExportOptions | None = None,
) -> HermesExportPlan:
    """Build a dry-run Hermes export plan without writing a package."""

    opts = options or HermesExportOptions()
    profile = Path(profile_dir).resolve()
    bridge = Path(bridge_home).resolve()
    name = str(agent_id or "").strip()
    if not name:
        raise HermesExportError("agent_id is required")
    if not profile.exists():
        raise HermesExportError(f"Hermes profile directory not found: {profile}")
    if not bridge.exists():
        raise HermesExportError(f"Hermes bridge home not found: {bridge}")

    profile_config_path = profile / "config.yaml"
    profile_config = _read_yaml_file(profile_config_path) if profile_config_path.exists() else {}
    bridge_agents = _read_bridge_agents(bridge)
    bridge_entry = _bridge_agent_entry(bridge_agents, name)
    display_name = str(bridge_entry.get("display_name") or profile_config.get("display_name") or name)
    warnings: list[str] = []

    manifest = new_manifest(
        source_runtime="hermes",
        target_runtime="hashi",
        agent_id=name,
        display_name=display_name,
        contains_secrets=False,
        contains_memory=opts.include_memories,
        contains_workspace=True,
    )
    normalized = _normalized_agent(name, display_name, bridge_entry)
    files: dict[str, bytes | str] = {
        "source/hermes_profile_config.yaml": profile_config_path.read_text(encoding="utf-8", errors="replace") if profile_config_path.exists() else "",
        "source/hermes_agents_entry.yaml": yaml.safe_dump({name: bridge_entry}, sort_keys=True, allow_unicode=True),
        "source/raw_paths.json": _json_text({"profile_dir": str(profile), "bridge_home": str(bridge)}),
    }

    identity = _read_hermes_identity(profile, profile_config, name, warnings)
    files["identity/hermes_instructions.md"] = identity

    memory_notes = _collect_memories(files, profile, opts, warnings)
    files["memory/import_notes.json"] = _json_text(memory_notes)

    schedules = _collect_cron(files, profile, name, opts, warnings)
    files["schedules/tasks.json"] = _json_text(schedules)

    if opts.include_skills:
        _collect_skills(files, profile, opts, warnings)

    _warn_blocked_profile_state(profile, warnings)
    files["source/secrets_summary.json"] = _json_text({"included": False, "policy": "excluded_by_default"})
    warnings.append("Hermes credentials and delivery configuration excluded by default")

    planned = [
        PlannedWrite(path=entry, action="write_package_entry", description="package entry")
        for entry in sorted(files)
    ]
    package_path = Path(output_path) if output_path else _default_package_path(bridge, name)
    dry_run = DryRunReport(
        operation="hermes_export",
        source_runtime="hermes",
        target_runtime="hashi",
        agent_id=name,
        planned_writes=planned,
        warnings=warnings,
    )
    migration_report = _migration_report(name, display_name, files, schedules, memory_notes, warnings)
    self_check = _post_migration_self_check(name, display_name)
    validate_normalized_agent(normalized)

    return HermesExportPlan(
        profile_dir=profile,
        bridge_home=bridge,
        agent_id=name,
        package_path=package_path,
        manifest=manifest,
        normalized_agent=normalized,
        files=files,
        dry_run_report=dry_run,
        migration_report=migration_report,
        post_migration_self_check=self_check,
        warnings=warnings,
    )


def export_hermes_agent(
    profile_dir: Path | str,
    bridge_home: Path | str,
    agent_id: str,
    output_path: Path | str | None = None,
    *,
    options: HermesExportOptions | None = None,
) -> PackageBuildResult:
    """Export a Hermes profile into a `.hashi-hermes-agent` package."""

    plan = plan_hermes_export(profile_dir, bridge_home, agent_id, output_path, options=options)
    return create_transfer_package(
        plan.package_path,
        manifest=plan.manifest,
        normalized_agent=plan.normalized_agent,
        files=plan.files,
        profile_policy=default_profile_policy(),
        secrets_policy=default_secrets_policy(),
        dry_run_report=plan.dry_run_report,
        migration_report=plan.migration_report,
        post_migration_self_check=plan.post_migration_self_check,
    )


def _read_bridge_agents(bridge_home: Path) -> dict[str, Any]:
    agents_file = bridge_home / "agents.yaml"
    if not agents_file.exists():
        raise HermesExportError(f"agents.yaml not found: {agents_file}")
    data = _read_yaml_file(agents_file)
    raw = data.get("agents", {})
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {str(item.get("name")): item for item in raw if isinstance(item, dict) and item.get("name")}
    raise HermesExportError("agents.yaml agents must be a mapping or list")


def _bridge_agent_entry(agents: dict[str, Any], agent_id: str) -> dict[str, Any]:
    for name, entry in agents.items():
        if str(name).lower() == agent_id.lower():
            return dict(entry or {})
    raise HermesExportError(f"Hermes bridge agent not found: {agent_id}")


def _normalized_agent(agent_id: str, display_name: str, bridge_entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "display_name": display_name,
        "description": str(bridge_entry.get("description") or ""),
        "emoji": str(bridge_entry.get("emoji") or ""),
        "identity_text_path": "identity/hermes_instructions.md",
        "preferred_backend": {"engine": "codex-cli"},
        "capabilities": {
            "hchat": True,
            "remote": True,
            "workspace_write": False,
            "scheduled_jobs": True,
        },
        "memory": {
            "strategy": "portable_files_first",
            "notes_path": "memory/import_notes.json",
            "max_chars_per_item": 2200,
            "session_state_included": False,
        },
        "skills": [],
        "schedules": [],
        "secrets": {
            "included": False,
            "encrypted": False,
            "keys": [],
        },
    }


def _read_hermes_identity(profile: Path, profile_config: dict[str, Any], agent_id: str, warnings: list[str]) -> str:
    for rel in ("AGENT.md", "agent.md", "instructions.md", "system.md"):
        path = profile / rel
        if path.exists():
            return path.read_text(encoding="utf-8", errors="replace")
    prompt = profile_config.get("system_prompt") or profile_config.get("instructions")
    if prompt:
        return f"# {agent_id}\n\n{prompt}\n"
    warnings.append(f"Hermes identity file missing for {agent_id}; placeholder generated")
    return f"# {agent_id}\n\nHermes identity file was not found during export.\n"


def _collect_memories(
    files: dict[str, bytes | str],
    profile: Path,
    options: HermesExportOptions,
    warnings: list[str],
) -> dict[str, Any]:
    notes = {"strategy": "portable_files_first", "accepted": [], "skipped": [], "warnings": []}
    memories = profile / "memories"
    if not options.include_memories or not memories.exists():
        return notes
    for path in sorted(memories.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(memories).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > options.max_memory_chars:
            warnings.append(f"skipped Hermes memory {path}: {len(text)} chars exceeds {options.max_memory_chars}")
            notes["skipped"].append(rel)
            continue
        if path.stat().st_size > options.max_file_bytes:
            warnings.append(f"skipped Hermes memory {path}: file exceeds max_file_bytes={options.max_file_bytes}")
            notes["skipped"].append(rel)
            continue
        files[f"memory/files/{rel}"] = text
        notes["accepted"].append(rel)
    notes["warnings"] = list(warnings)
    return notes


def _collect_cron(
    files: dict[str, bytes | str],
    profile: Path,
    agent_id: str,
    options: HermesExportOptions,
    warnings: list[str],
) -> dict[str, Any]:
    result = {"version": 1, "heartbeats": [], "crons": [], "nudges": []}
    cron_dir = profile / "cron"
    if not options.include_cron or not cron_dir.exists():
        return result
    for path in sorted(cron_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(cron_dir).as_posix()
        if path.stat().st_size > options.max_file_bytes:
            warnings.append(f"skipped Hermes cron {path}: file exceeds max_file_bytes={options.max_file_bytes}")
            continue
        files[f"source/hermes_cron/{rel}"] = path.read_bytes()
        result["crons"].append(
            {
                "id": f"{agent_id}-hermes-cron-{path.stem}",
                "agent": agent_id,
                "enabled": False,
                "import_state": "paused_review_draft",
                "source_path": f"cron/{rel}",
            }
        )
    return result


def _collect_skills(
    files: dict[str, bytes | str],
    profile: Path,
    options: HermesExportOptions,
    warnings: list[str],
) -> None:
    skills_dir = profile / "skills"
    if not skills_dir.exists():
        return
    for path in sorted(skills_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skills_dir).as_posix()
        if path.stat().st_size > options.max_file_bytes:
            warnings.append(f"skipped Hermes skill {path}: file exceeds max_file_bytes={options.max_file_bytes}")
            continue
        files[f"skills/{rel}"] = path.read_bytes()


def _warn_blocked_profile_state(profile: Path, warnings: list[str]) -> None:
    for rel in ("sessions", "plugins/cache", "message_queue.json"):
        if (profile / rel).exists():
            warnings.append(f"blocked Hermes runtime state excluded: {profile / rel}")
    plugins = profile / "plugins"
    if plugins.exists():
        warnings.append("Hermes plugins are not exported except through explicit future allowlist adapters")


def _migration_report(
    agent_id: str,
    display_name: str,
    files: dict[str, bytes | str],
    schedules: dict[str, Any],
    memory_notes: dict[str, Any],
    warnings: list[str],
) -> str:
    cron_count = len(schedules.get("crons", []))
    accepted_memory = len(memory_notes.get("accepted", []))
    skipped_memory = len(memory_notes.get("skipped", []))
    lines = [
        "# HASHI Hermes Migration Report",
        "",
        "- source_runtime: hermes",
        "- target_runtime: hashi",
        f"- agent_id: {agent_id}",
        f"- display_name: {display_name}",
        f"- package_entries: {len(files)}",
        f"- paused_cron_drafts: {cron_count}",
        f"- memory_accepted: {accepted_memory}",
        f"- memory_skipped: {skipped_memory}",
        "- secrets_included: false",
        "",
        "## Warnings",
    ]
    lines.extend(f"- {warning}" for warning in warnings)
    if not warnings:
        lines.append("- none")
    return "\n".join(lines) + "\n"


def _post_migration_self_check(agent_id: str, display_name: str) -> str:
    return "\n".join(
        [
            "# Post-migration self-check",
            "",
            f"Imported HASHI agent `{agent_id}` (`{display_name}`) must confirm:",
            "",
            "1. Imported identity is readable.",
            "2. Skills are present or intentionally skipped.",
            "3. Memory accepted/skipped counts match `memory/import_notes.json`.",
            "4. Imported schedules are disabled review drafts.",
            "5. No Hermes secrets or delivery credentials were imported.",
            "6. HASHI HChat/Remote behavior is checked before activation.",
            "",
        ]
    )


def _default_package_path(bridge_home: Path, agent_id: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return bridge_home / "packages" / f"{agent_id}_{ts}{PACKAGE_EXT}"


def _read_yaml_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise HermesExportError(f"YAML file must contain a mapping: {path}")
    return data


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
