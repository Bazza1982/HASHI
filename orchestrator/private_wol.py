from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from orchestrator.ticket_manager import detect_instance

WOL_CONFIG_REL = Path("private/wol_targets.json")


def _config_path(project_root: Path) -> Path:
    return project_root / WOL_CONFIG_REL


def _load_config(project_root: Path) -> dict[str, Any]:
    path = _config_path(project_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _current_instance(project_root: Path) -> str:
    return str(detect_instance(project_root) or "").upper() or "HASHI1"


def _targets_map(project_root: Path) -> dict[str, dict[str, Any]]:
    data = _load_config(project_root)
    raw = data.get("targets") or {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            result[key.strip().lower()] = value
    return result


def private_wol_available(project_root: Path) -> bool:
    data = _load_config(project_root)
    allowed = [str(x).upper() for x in (data.get("allowed_instances") or []) if str(x).strip()]
    if allowed and _current_instance(project_root) not in allowed:
        return False
    return bool(_targets_map(project_root))


def describe_wol_targets(project_root: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name, cfg in sorted(_targets_map(project_root).items()):
        rows.append(
            {
                "name": name,
                "label": str(cfg.get("label") or name),
                "description": str(cfg.get("description") or "").strip(),
            }
        )
    return rows


def _windows_to_wsl_path(path_str: str) -> Path | None:
    raw = str(path_str or "").strip()
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
    if not m:
        return None
    drive = m.group(1).lower()
    rest = m.group(2).replace("\\", "/")
    return Path("/mnt") / drive / rest


def _resolve_script_exists(script_path: str) -> bool:
    raw = str(script_path or "").strip()
    if not raw:
        return False
    candidate = Path(raw)
    if candidate.exists():
        return True
    wsl_path = _windows_to_wsl_path(raw)
    return bool(wsl_path and wsl_path.exists())


def _build_command(target_cfg: dict[str, Any]) -> list[str]:
    runner = str(target_cfg.get("runner") or "powershell_file").strip().lower()
    if runner == "powershell_file":
        script_path = str(target_cfg.get("script_path") or "").strip()
        if not script_path:
            raise ValueError("missing script_path")
        if not _resolve_script_exists(script_path):
            raise FileNotFoundError(f"script not found: {script_path}")
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_path,
        ]
        for arg in target_cfg.get("args") or []:
            cmd.append(str(arg))
        return cmd

    if runner == "command":
        raw = target_cfg.get("command") or []
        if not isinstance(raw, list) or not raw:
            raise ValueError("runner=command requires non-empty command list")
        return [str(x) for x in raw]

    raise ValueError(f"unsupported runner: {runner}")


def run_private_wol(project_root: Path, target_name: str, timeout_s: int = 45) -> dict[str, Any]:
    instance = _current_instance(project_root)
    data = _load_config(project_root)
    allowed = [str(x).upper() for x in (data.get("allowed_instances") or []) if str(x).strip()]
    if allowed and instance not in allowed:
        return {
            "ok": False,
            "error": f"/wol is disabled on instance {instance}",
            "instance": instance,
        }

    targets = _targets_map(project_root)
    target_key = str(target_name or "").strip().lower()
    if not target_key:
        return {"ok": False, "error": "missing target name", "instance": instance}
    target_cfg = targets.get(target_key)
    if not target_cfg:
        return {
            "ok": False,
            "error": f"unknown target: {target_key}",
            "instance": instance,
            "available_targets": sorted(targets.keys()),
        }

    cmd = _build_command(target_cfg)
    env = os.environ.copy()
    env["HASHI_WOL_TARGET"] = target_key
    env["HASHI_INSTANCE_ID"] = instance
    result = subprocess.run(
        cmd,
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    return {
        "ok": result.returncode == 0,
        "instance": instance,
        "target": target_key,
        "label": str(target_cfg.get("label") or target_key),
        "description": str(target_cfg.get("description") or "").strip(),
        "runner": str(target_cfg.get("runner") or "powershell_file"),
        "command": cmd,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }
