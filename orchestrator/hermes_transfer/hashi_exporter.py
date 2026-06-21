"""HASHI-side exporter for HASHI Hermes transfer packages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .package import PackageBuildResult, create_transfer_package
from .schema import (
    PACKAGE_EXT,
    DryRunReport,
    PlannedWrite,
    default_secrets_policy,
    new_manifest,
    validate_normalized_agent,
)


class HashiExportError(ValueError):
    """Raised when a HASHI agent cannot be exported."""


@dataclass(frozen=True)
class HashiExportOptions:
    include_memory: bool = True
    include_schedules: bool = True
    include_workspace_state: bool = True
    include_secrets: bool = False
    max_file_bytes: int = 1_000_000


@dataclass(frozen=True)
class HashiExportPlan:
    root: Path
    agent_id: str
    package_path: Path
    manifest: dict[str, Any]
    normalized_agent: dict[str, Any]
    files: dict[str, bytes | str]
    dry_run_report: DryRunReport
    migration_report: str
    post_migration_self_check: str
    warnings: list[str]


def plan_hashi_export(
    root: Path | str,
    agent_id: str,
    output_path: Path | str | None = None,
    *,
    options: HashiExportOptions | None = None,
) -> HashiExportPlan:
    """Build a dry-run HASHI export plan without writing a package."""

    opts = options or HashiExportOptions()
    if opts.include_secrets:
        raise HashiExportError("HASHI -> Hermes secret export is not implemented in Phase 2")

    root_path = Path(root).resolve()
    agent_name = str(agent_id or "").strip()
    if not agent_name:
        raise HashiExportError("agent_id is required")
    agent_config = _find_agent(root_path, agent_name)
    workspace = _workspace_dir(root_path, agent_config)
    system_md = _system_md_path(root_path, agent_config, workspace)
    warnings: list[str] = []

    display_name = str(agent_config.get("display_name") or agent_name)
    manifest = new_manifest(
        source_runtime="hashi",
        target_runtime="hermes",
        agent_id=agent_name,
        display_name=display_name,
        contains_secrets=False,
        contains_memory=opts.include_memory,
        contains_workspace=opts.include_workspace_state,
    )
    normalized_agent = _normalized_agent(agent_config, agent_name, display_name)

    files: dict[str, bytes | str] = {
        "source/hashi_agent_config.json": _json_text(agent_config),
        "source/raw_paths.json": _json_text(
            {
                "root": str(root_path),
                "workspace_dir": str(workspace) if workspace else None,
                "system_md": str(system_md) if system_md else None,
            }
        ),
    }

    identity_text = _read_identity(system_md, agent_name, warnings)
    files["identity/agent.md"] = identity_text

    memory_notes = _collect_hashi_memory_files(files, workspace, opts, warnings)
    files["memory/import_notes.json"] = _json_text(memory_notes)

    if opts.include_schedules:
        schedules = _collect_schedules(root_path, agent_name)
        files["schedules/tasks.json"] = _json_text(schedules)
    else:
        schedules = {"version": 1, "heartbeats": [], "crons": [], "nudges": []}

    if opts.include_workspace_state and workspace:
        _include_optional_workspace_file(files, workspace, "state.json", "workspace/state.json", opts, warnings)
        _include_optional_workspace_file(files, workspace, "skill_state.json", "workspace/skill_state.json", opts, warnings)

    secrets_policy = default_secrets_policy()
    files["source/secrets_summary.json"] = _json_text(
        {
            "included": False,
            "policy": "excluded_by_default",
            "matching_keys": _agent_secret_key_names(root_path, agent_name),
        }
    )
    warnings.append("secrets excluded by default for HASHI -> Hermes export")

    planned_writes = [
        PlannedWrite(path=name, action="write_package_entry", description="package entry")
        for name in sorted(files)
    ]
    package_path = Path(output_path) if output_path else _default_package_path(root_path, agent_name)
    dry_run = DryRunReport(
        operation="hashi_export",
        source_runtime="hashi",
        target_runtime="hermes",
        agent_id=agent_name,
        planned_writes=planned_writes,
        warnings=warnings,
    )
    migration_report = _migration_report(agent_name, display_name, files, schedules, warnings, secrets_policy)
    self_check = _post_migration_self_check(agent_name, display_name)
    validate_normalized_agent(normalized_agent)

    return HashiExportPlan(
        root=root_path,
        agent_id=agent_name,
        package_path=package_path,
        manifest=manifest,
        normalized_agent=normalized_agent,
        files=files,
        dry_run_report=dry_run,
        migration_report=migration_report,
        post_migration_self_check=self_check,
        warnings=warnings,
    )


def export_hashi_agent(
    root: Path | str,
    agent_id: str,
    output_path: Path | str | None = None,
    *,
    options: HashiExportOptions | None = None,
) -> PackageBuildResult:
    """Export a HASHI agent into a `.hashi-hermes-agent` package."""

    plan = plan_hashi_export(root, agent_id, output_path, options=options)
    return create_transfer_package(
        plan.package_path,
        manifest=plan.manifest,
        normalized_agent=plan.normalized_agent,
        files=plan.files,
        dry_run_report=plan.dry_run_report,
        migration_report=plan.migration_report,
        post_migration_self_check=plan.post_migration_self_check,
    )


def _find_agent(root: Path, agent_id: str) -> dict[str, Any]:
    agents_file = root / "agents.json"
    if not agents_file.exists():
        raise HashiExportError(f"agents.json not found: {agents_file}")
    data = _read_json_file(agents_file)
    agents = data if isinstance(data, list) else data.get("agents", [])
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        if agent.get("name") == agent_id or agent.get("id") == agent_id:
            return dict(agent)
    raise HashiExportError(f"agent not found: {agent_id}")


def _workspace_dir(root: Path, agent_config: dict[str, Any]) -> Path | None:
    raw = agent_config.get("workspace_dir") or agent_config.get("workspace")
    if not raw:
        raw = f"workspaces/{agent_config.get('name') or agent_config.get('id') or ''}"
    path = Path(str(raw))
    if not path.is_absolute():
        path = root / path
    return path if path.exists() else None


def _system_md_path(root: Path, agent_config: dict[str, Any], workspace: Path | None) -> Path | None:
    raw = agent_config.get("system_md")
    candidates: list[Path] = []
    if raw:
        p = Path(str(raw))
        candidates.append(p if p.is_absolute() else root / p)
    if workspace:
        candidates.append(workspace / "agent.md")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def _read_identity(system_md: Path | None, agent_id: str, warnings: list[str]) -> str:
    if system_md and system_md.exists():
        return system_md.read_text(encoding="utf-8", errors="replace")
    warnings.append(f"identity file missing for {agent_id}; placeholder generated")
    return f"# {agent_id}\n\nIdentity file was not found during HASHI export.\n"


def _normalized_agent(agent_config: dict[str, Any], agent_id: str, display_name: str) -> dict[str, Any]:
    active_backend = agent_config.get("active_backend") or agent_config.get("engine")
    model = agent_config.get("model")
    preferred_backend = {"engine": active_backend or "unknown"}
    if model:
        preferred_backend["model"] = model
    return {
        "agent_id": agent_id,
        "display_name": display_name,
        "description": str(agent_config.get("description") or ""),
        "emoji": str(agent_config.get("emoji") or ""),
        "identity_text_path": "identity/agent.md",
        "preferred_backend": preferred_backend,
        "capabilities": {
            "hchat": True,
            "remote": True,
            "workspace_write": str(agent_config.get("access_scope") or "").lower() in {"project", "workspace"},
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


def _collect_hashi_memory_files(
    files: dict[str, bytes | str],
    workspace: Path | None,
    options: HashiExportOptions,
    warnings: list[str],
) -> dict[str, Any]:
    notes: dict[str, Any] = {
        "strategy": "portable_files_first",
        "accepted": [],
        "skipped": [],
        "warnings": [],
    }
    if not options.include_memory or not workspace:
        return notes

    memory_candidates = [
        ("MEMORY.md", "memory/files/MEMORY.md"),
        ("bridge_memory.sqlite", "memory/sqlite/bridge_memory.sqlite"),
    ]
    for source_name, package_name in memory_candidates:
        result = _include_optional_workspace_file(files, workspace, source_name, package_name, options, warnings)
        (notes["accepted"] if result else notes["skipped"]).append(source_name)

    memory_dir = workspace / "memory"
    if memory_dir.exists():
        for path in sorted(memory_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(memory_dir).as_posix()
            package_name = f"memory/files/{rel}"
            if _include_file(files, path, package_name, options, warnings):
                notes["accepted"].append(f"memory/{rel}")
            else:
                notes["skipped"].append(f"memory/{rel}")

    notes["warnings"] = list(warnings)
    return notes


def _include_optional_workspace_file(
    files: dict[str, bytes | str],
    workspace: Path,
    source_name: str,
    package_name: str,
    options: HashiExportOptions,
    warnings: list[str],
) -> bool:
    path = workspace / source_name
    if not path.exists() or not path.is_file():
        return False
    return _include_file(files, path, package_name, options, warnings)


def _include_file(
    files: dict[str, bytes | str],
    source_path: Path,
    package_name: str,
    options: HashiExportOptions,
    warnings: list[str],
) -> bool:
    size = source_path.stat().st_size
    if size > options.max_file_bytes:
        warnings.append(f"skipped {source_path}: size {size} exceeds max_file_bytes={options.max_file_bytes}")
        return False
    files[package_name] = source_path.read_bytes()
    return True


def _collect_schedules(root: Path, agent_id: str) -> dict[str, Any]:
    tasks_file = root / "tasks.json"
    if not tasks_file.exists():
        return {"version": 1, "heartbeats": [], "crons": [], "nudges": []}
    data = _read_json_file(tasks_file)
    result = {"version": data.get("version", 1) if isinstance(data, dict) else 1}
    for section in ("heartbeats", "crons", "nudges"):
        raw = data.get(section, []) if isinstance(data, dict) else []
        rows = []
        for item in raw:
            if isinstance(item, dict) and item.get("agent") == agent_id:
                paused = dict(item)
                paused["enabled"] = False
                paused["export_state"] = "paused_review_draft"
                rows.append(paused)
        result[section] = rows
    return result


def _agent_secret_key_names(root: Path, agent_id: str) -> list[str]:
    secrets_file = root / "secrets.json"
    if not secrets_file.exists():
        return []
    try:
        data = _read_json_file(secrets_file)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    return sorted(
        key
        for key in data
        if key == agent_id or key.startswith(f"{agent_id}_") or key.startswith(f"{agent_id}.")
    )


def _migration_report(
    agent_id: str,
    display_name: str,
    files: dict[str, bytes | str],
    schedules: dict[str, Any],
    warnings: list[str],
    secrets_policy: dict[str, Any],
) -> str:
    schedule_count = sum(len(schedules.get(section, [])) for section in ("heartbeats", "crons", "nudges"))
    lines = [
        "# HASHI Hermes Migration Report",
        "",
        f"- source_runtime: hashi",
        f"- target_runtime: hermes",
        f"- agent_id: {agent_id}",
        f"- display_name: {display_name}",
        f"- package_entries: {len(files)}",
        f"- schedule_drafts_paused: {schedule_count}",
        f"- secrets_included: {bool(secrets_policy.get('included'))}",
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
            f"Target Hermes profile for HASHI agent `{agent_id}` (`{display_name}`) must confirm:",
            "",
            "1. Imported identity is readable.",
            "2. Imported skills are present or intentionally skipped.",
            "3. Memory import counts match `memory/import_notes.json`.",
            "4. Schedules are paused and require manual resume.",
            "5. No secrets were imported unless explicitly approved.",
            "6. HASHI HChat bridge status is healthy before enabling the profile.",
            "",
        ]
    )


def _default_package_path(root: Path, agent_id: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / "private" / "hermes_transfer" / f"{agent_id}_{ts}{PACKAGE_EXT}"


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
