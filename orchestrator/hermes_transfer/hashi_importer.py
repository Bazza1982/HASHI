"""HASHI-side importer for HASHI Hermes transfer packages."""

from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .package import TransferPackage, read_transfer_package
from .schema import DryRunReport, PlannedWrite


class HashiImportError(ValueError):
    """Raised when a transfer package cannot be imported into HASHI."""


@dataclass(frozen=True)
class HashiImportOptions:
    target_agent_id: str | None = None
    overwrite: bool = False
    enable: bool = False
    include_schedules: bool = True


@dataclass(frozen=True)
class HashiImportPlan:
    root: Path
    package_path: Path
    target_agent_id: str
    workspace_dir: Path
    rollback_dir: Path
    transfer_package: TransferPackage
    agent_config: dict[str, Any]
    dry_run_report: DryRunReport
    warnings: list[str]


def plan_hashi_import(
    root: Path | str,
    package_path: Path | str,
    *,
    options: HashiImportOptions | None = None,
) -> HashiImportPlan:
    """Build a HASHI import plan without writing files."""

    opts = options or HashiImportOptions()
    root_path = Path(root).resolve()
    package = read_transfer_package(package_path)
    manifest = package.manifest
    if manifest.get("target_runtime") != "hashi":
        raise HashiImportError("package target_runtime must be 'hashi' for HASHI import")
    if manifest.get("source_runtime") != "hermes":
        raise HashiImportError("package source_runtime must be 'hermes' for HASHI import")

    agents_file = root_path / "agents.json"
    if not agents_file.exists():
        raise HashiImportError(f"agents.json not found: {agents_file}")
    target_agent = str(opts.target_agent_id or package.normalized_agent.get("agent_id") or manifest["agent_id"]).strip()
    if not target_agent:
        raise HashiImportError("target agent id is required")
    agents_data = _read_json_file(agents_file)
    existing = _find_agent_config(agents_data, target_agent)
    if existing and not opts.overwrite:
        raise HashiImportError(f"target agent already exists: {target_agent}")

    workspace = root_path / "workspaces" / target_agent
    rollback_dir = _rollback_dir(root_path, manifest)
    agent_config = _build_hashi_agent_config(package.normalized_agent, target_agent, enable=opts.enable)
    planned = [
        PlannedWrite(path="agents.json", action="update", description="upsert disabled HASHI agent config"),
        PlannedWrite(path=f"workspaces/{target_agent}/agent.md", action="create", description="write imported identity"),
        PlannedWrite(path=f"workspaces/{target_agent}/hermes_import/", kind="directory", action="create", description="write source evidence and reports"),
        PlannedWrite(path=str(rollback_dir.relative_to(root_path)), kind="directory", action="create", description="rollback snapshot"),
    ]
    if opts.include_schedules and "schedules/tasks.json" in package.names:
        planned.append(PlannedWrite(path="tasks.json", action="update", description="append disabled imported schedule drafts"))
    warnings = [
        "imported HASHI agent is disabled by default" if not opts.enable else "import requested with enable=True",
        "secrets are not imported in Phase 3",
    ]
    dry_run = DryRunReport(
        operation="hashi_import",
        source_runtime="hermes",
        target_runtime="hashi",
        agent_id=target_agent,
        planned_writes=planned,
        warnings=warnings,
    )
    return HashiImportPlan(
        root=root_path,
        package_path=Path(package_path),
        target_agent_id=target_agent,
        workspace_dir=workspace,
        rollback_dir=rollback_dir,
        transfer_package=package,
        agent_config=agent_config,
        dry_run_report=dry_run,
        warnings=warnings,
    )


def import_hashi_agent(
    root: Path | str,
    package_path: Path | str,
    *,
    options: HashiImportOptions | None = None,
) -> HashiImportPlan:
    """Import a Hermes-origin package into HASHI as a disabled agent by default."""

    plan = plan_hashi_import(root, package_path, options=options)
    opts = options or HashiImportOptions()
    _write_rollback(plan)
    if plan.workspace_dir.exists() and opts.overwrite:
        shutil.rmtree(plan.workspace_dir)
    plan.workspace_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(plan.package_path, "r") as archive:
        identity = _read_first_text(
            archive,
            ["identity/agent.md", "identity/hermes_instructions.md"],
            fallback=f"# {plan.target_agent_id}\n\nImported from Hermes.\n",
        )
        (plan.workspace_dir / "agent.md").write_text(identity, encoding="utf-8")
        _write_hermes_import_files(archive, plan.workspace_dir / "hermes_import")

    _upsert_agent(plan.root / "agents.json", plan.agent_config)
    if opts.include_schedules:
        _import_schedules(plan)
    return plan


def _build_hashi_agent_config(normalized_agent: dict[str, Any], agent_id: str, *, enable: bool) -> dict[str, Any]:
    preferred = normalized_agent.get("preferred_backend") or {}
    engine = preferred.get("engine") or "codex-cli"
    config: dict[str, Any] = {
        "name": agent_id,
        "display_name": normalized_agent.get("display_name") or agent_id,
        "emoji": normalized_agent.get("emoji") or "",
        "type": "flex",
        "active_backend": engine,
        "engine": engine,
        "system_md": f"workspaces/{agent_id}/agent.md",
        "workspace_dir": f"workspaces/{agent_id}",
        "is_active": bool(enable),
        "access_scope": "project",
        "background_mode": True,
        "background_detach_after": 150,
        "process_timeout": 600,
        "imported_from": "hermes",
        "import_review_required": not enable,
    }
    if preferred.get("model"):
        config["model"] = preferred["model"]
    return config


def _write_rollback(plan: HashiImportPlan) -> None:
    plan.rollback_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in ("agents.json", "tasks.json"):
        src = plan.root / name
        if src.exists():
            shutil.copy2(src, plan.rollback_dir / name)
            copied.append(name)
    if plan.workspace_dir.exists():
        shutil.copytree(plan.workspace_dir, plan.rollback_dir / "workspace", dirs_exist_ok=True)
        copied.append(f"workspaces/{plan.target_agent_id}")
    manifest = {
        "created_at": datetime.now().isoformat(),
        "package": str(plan.package_path),
        "target_agent_id": plan.target_agent_id,
        "copied": copied,
    }
    (plan.rollback_dir / "rollback_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_hermes_import_files(archive: zipfile.ZipFile, target: Path) -> None:
    prefixes = (
        "source/",
        "memory/",
        "schedules/",
        "audit/",
    )
    exact = {
        "manifest.json",
        "normalized_agent.json",
        "profile_policy.json",
        "secrets.policy.json",
    }
    target.mkdir(parents=True, exist_ok=True)
    for name in archive.namelist():
        if name in exact or any(name.startswith(prefix) for prefix in prefixes):
            dest = target / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(archive.read(name))


def _upsert_agent(agents_file: Path, agent_config: dict[str, Any]) -> None:
    data = _read_json_file(agents_file)
    is_list = isinstance(data, list)
    agents = data if is_list else data.setdefault("agents", [])
    for idx, existing in enumerate(agents):
        if isinstance(existing, dict) and (existing.get("name") == agent_config["name"] or existing.get("id") == agent_config["name"]):
            agents[idx] = agent_config
            break
    else:
        agents.append(agent_config)
    agents_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _import_schedules(plan: HashiImportPlan) -> None:
    with zipfile.ZipFile(plan.package_path, "r") as archive:
        if "schedules/tasks.json" not in archive.namelist():
            return
        schedules = json.loads(archive.read("schedules/tasks.json").decode("utf-8"))
    tasks_file = plan.root / "tasks.json"
    if tasks_file.exists():
        tasks = _read_json_file(tasks_file)
    else:
        tasks = {"version": 1, "heartbeats": [], "crons": [], "nudges": []}
    for section in ("heartbeats", "crons", "nudges"):
        tasks.setdefault(section, [])
        existing_ids = {item.get("id") for item in tasks[section] if isinstance(item, dict)}
        for item in schedules.get(section, []):
            if not isinstance(item, dict):
                continue
            imported = dict(item)
            imported["agent"] = plan.target_agent_id
            imported["enabled"] = False
            imported["import_state"] = "disabled_review_draft"
            if imported.get("id") in existing_ids:
                imported["id"] = f"{plan.target_agent_id}-{imported['id']}"
            tasks[section].append(imported)
    tasks_file.write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _find_agent_config(agents_data: Any, agent_id: str) -> dict[str, Any] | None:
    agents = agents_data if isinstance(agents_data, list) else agents_data.get("agents", [])
    for agent in agents:
        if isinstance(agent, dict) and (agent.get("name") == agent_id or agent.get("id") == agent_id):
            return agent
    return None


def _read_first_text(archive: zipfile.ZipFile, names: list[str], *, fallback: str) -> str:
    archive_names = set(archive.namelist())
    for name in names:
        if name in archive_names:
            return archive.read(name).decode("utf-8", errors="replace")
    return fallback


def _rollback_dir(root: Path, manifest: dict[str, Any]) -> Path:
    package_id = str(manifest.get("package_id") or "pkg-unknown").replace("/", "-")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / "private" / "hermes_transfer" / "rollback" / f"{package_id}-{ts}"


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))
