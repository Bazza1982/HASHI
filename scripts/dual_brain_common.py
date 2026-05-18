"""Shared helpers for HASHI dual-brain sidecar scripts."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BackendContext:
    backend: str
    model: str
    command: str
    source: str


def load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require_config(cfg: dict[str, Any], key: str) -> Any:
    if key not in cfg:
        raise RuntimeError(f"config missing required key: {key}")
    value = cfg[key]
    if value is None or value == "":
        raise RuntimeError(f"config has empty required key: {key}")
    return value


def load_config(path: Path) -> dict[str, Any]:
    cfg = load_json(path, None)
    if not isinstance(cfg, dict):
        raise RuntimeError(f"config not found or invalid JSON: {path}")
    for key in ("continuity_file", "output_dir"):
        require_config(cfg, key)
    if "wiki_roots" not in cfg:
        require_config(cfg, "wiki_root")
    return cfg


def resolve_backend(
    hashi_root: Path,
    agent: str,
    cfg: dict[str, Any],
    *,
    role: str,
) -> BackendContext:
    override_backend = str(cfg.get(f"{role}_backend") or "").strip()
    override_model = str(cfg.get(f"{role}_model") or "").strip()
    source = f"config:{role}"

    if override_backend or override_model:
        if not override_backend or not override_model:
            raise RuntimeError(f"{role}_backend and {role}_model must be set together")
        backend = override_backend
        model = override_model
    else:
        state_path = hashi_root / "workspaces" / agent / "state.json"
        state = load_json(state_path, {})
        backend = str(state.get("active_backend") or "").strip()
        model = str(state.get("active_model") or "").strip()
        source = str(state_path)
        if not backend or not model:
            raise RuntimeError(f"cannot resolve active backend/model from {state_path}")

    allowed = cfg.get("allowed_cli_backends") or ["codex-cli", "claude-cli", "gemini-cli"]
    if backend not in allowed:
        raise RuntimeError(f"backend not allowed for {role}: {backend}")

    command = command_for_backend(hashi_root, backend)
    return BackendContext(backend=backend, model=model, command=command, source=source)


def command_for_backend(hashi_root: Path, backend: str) -> str:
    agents = load_json(hashi_root / "agents.json", {})
    global_cfg = agents.get("global") or {}
    if backend == "codex-cli":
        return str(global_cfg.get("codex_cmd") or os.environ.get("CODEX_BIN") or "codex")
    if backend == "claude-cli":
        return str(global_cfg.get("claude_cmd") or os.environ.get("CLAUDE_BIN") or "claude")
    if backend == "gemini-cli":
        return str(global_cfg.get("gemini_cmd") or os.environ.get("GEMINI_BIN") or "gemini")
    raise RuntimeError(f"unsupported backend: {backend}")


def git_check_ignored(hashi_root: Path, path: Path) -> tuple[bool, str]:
    try:
        rel = path.resolve().relative_to(hashi_root.resolve())
    except ValueError:
        return False, "outside_hashi_root"
    result = subprocess.run(
        ["git", "check-ignore", "-q", str(rel)],
        cwd=str(hashi_root),
        text=True,
        capture_output=True,
    )
    if result.returncode == 0:
        return True, "ignored"
    if result.returncode == 1:
        return False, "not_ignored"
    return False, f"check_failed:{(result.stderr or '').strip()}"
