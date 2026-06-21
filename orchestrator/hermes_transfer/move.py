"""Move-finalization helpers for HASHI Hermes transfer packages."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


class TransferMoveError(ValueError):
    """Raised when a transfer move cannot be finalized safely."""


@dataclass(frozen=True)
class MoveFinalizeOptions:
    target_verified: bool = False
    reason: str = "moved_by_hashi_hermes_transfer"
    package_id: str | None = None


@dataclass(frozen=True)
class MoveFinalizeResult:
    source_runtime: str
    source_id: str
    changed_paths: list[Path]
    warnings: list[str]


def finalize_hashi_to_hermes_move_source(
    hashi_root: Path | str,
    agent_id: str,
    *,
    options: MoveFinalizeOptions | None = None,
) -> MoveFinalizeResult:
    """Disable a HASHI source agent after a verified HASHI -> Hermes move."""

    opts = _require_verified(options)
    root = Path(hashi_root).resolve()
    name = str(agent_id or "").strip()
    if not name:
        raise TransferMoveError("agent_id is required")
    agents_file = root / "agents.json"
    if not agents_file.exists():
        raise TransferMoveError(f"agents.json not found: {agents_file}")
    data = _read_json_file(agents_file)
    agents = data if isinstance(data, list) else data.get("agents", [])
    changed = False
    for agent in agents:
        if isinstance(agent, dict) and (agent.get("name") == name or agent.get("id") == name):
            agent["is_active"] = False
            agent["transfer_disabled"] = True
            agent["transfer_disabled_reason"] = opts.reason
            agent["transfer_disabled_at"] = datetime.now().isoformat()
            if opts.package_id:
                agent["transfer_package_id"] = opts.package_id
            changed = True
            break
    if not changed:
        raise TransferMoveError(f"HASHI agent not found: {name}")
    agents_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    changed_paths = [agents_file]
    tasks_file = root / "tasks.json"
    if tasks_file.exists():
        tasks = _read_json_file(tasks_file)
        if _disable_hashi_tasks(tasks, name, opts):
            tasks_file.write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            changed_paths.append(tasks_file)
    return MoveFinalizeResult("hashi", name, changed_paths, [])


def finalize_hermes_to_hashi_move_source(
    profile_dir: Path | str,
    bridge_home: Path | str,
    profile_name: str,
    *,
    options: MoveFinalizeOptions | None = None,
) -> MoveFinalizeResult:
    """Disable a Hermes source profile after a verified Hermes -> HASHI move."""

    opts = _require_verified(options)
    profile = Path(profile_dir).resolve()
    bridge = Path(bridge_home).resolve()
    name = str(profile_name or "").strip()
    if not name:
        raise TransferMoveError("profile_name is required")
    if not bridge.exists():
        raise TransferMoveError(f"Hermes bridge home not found: {bridge}")
    if not profile.exists():
        raise TransferMoveError(f"Hermes profile directory not found: {profile}")

    changed_paths: list[Path] = []
    agents_file = bridge / "agents.yaml"
    if agents_file.exists():
        agents_data = _read_yaml_file(agents_file)
    else:
        agents_data = {"agents": {}}
    _disable_hermes_bridge_entry(agents_data, name, opts)
    _write_yaml_file(agents_file, agents_data)
    changed_paths.append(agents_file)

    config_file = profile / "config.yaml"
    config = _read_yaml_file(config_file) if config_file.exists() else {}
    transfer = dict(config.get("hashi_transfer") or {})
    transfer.update(
        {
            "source_disabled": True,
            "source_disabled_reason": opts.reason,
            "source_disabled_at": datetime.now().isoformat(),
        }
    )
    if opts.package_id:
        transfer["package_id"] = opts.package_id
    config["hashi_transfer"] = transfer
    _write_yaml_file(config_file, config)
    changed_paths.append(config_file)

    marker = profile / "DISABLED_BY_HASHI_TRANSFER.md"
    marker.write_text(
        "\n".join(
            [
                "# Disabled by HASHI Hermes transfer",
                "",
                f"- profile: {name}",
                f"- reason: {opts.reason}",
                f"- package_id: {opts.package_id or ''}",
                f"- disabled_at: {datetime.now().isoformat()}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    changed_paths.append(marker)
    return MoveFinalizeResult("hermes", name, changed_paths, [])


def _require_verified(options: MoveFinalizeOptions | None) -> MoveFinalizeOptions:
    opts = options or MoveFinalizeOptions()
    if not opts.target_verified:
        raise TransferMoveError("target_verified=True is required before disabling a source runtime")
    return opts


def _disable_hashi_tasks(tasks: Any, agent_id: str, opts: MoveFinalizeOptions) -> bool:
    changed = False
    if not isinstance(tasks, dict):
        return False
    for section in ("heartbeats", "crons", "nudges"):
        for item in tasks.get(section, []):
            if not isinstance(item, dict) or item.get("agent") != agent_id:
                continue
            if item.get("enabled") is not False:
                changed = True
            item["enabled"] = False
            item["transfer_disabled"] = True
            item["transfer_disabled_reason"] = opts.reason
            if opts.package_id:
                item["transfer_package_id"] = opts.package_id
    return changed


def _disable_hermes_bridge_entry(data: dict[str, Any], profile_name: str, opts: MoveFinalizeOptions) -> None:
    agents = data.setdefault("agents", {})
    entry: dict[str, Any]
    if isinstance(agents, dict):
        entry = dict(agents.get(profile_name) or {})
        agents[profile_name] = entry
    elif isinstance(agents, list):
        for item in agents:
            if isinstance(item, dict) and item.get("name") == profile_name:
                entry = item
                break
        else:
            entry = {"name": profile_name}
            agents.append(entry)
    else:
        raise TransferMoveError("agents.yaml agents must be a mapping or list")
    entry["enabled"] = False
    entry["transfer_disabled"] = True
    entry["transfer_disabled_reason"] = opts.reason
    entry["transfer_disabled_at"] = datetime.now().isoformat()
    if opts.package_id:
        entry["transfer_package_id"] = opts.package_id


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _read_yaml_file(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    if not isinstance(data, dict):
        raise TransferMoveError(f"YAML file must contain a mapping: {path}")
    return data


def _write_yaml_file(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=True), encoding="utf-8")
