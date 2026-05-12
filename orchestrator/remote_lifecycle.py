from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency fallback
    yaml = None


@dataclass(frozen=True)
class RemoteLifecycleSettings:
    root: Path
    enabled: bool
    supervised: bool
    disabled_path: Path
    port: int
    use_tls: bool
    backend: str


def resolve_hashi_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    env_root = os.getenv("HASHI_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def disabled_state_path(root: Path | str | None = None) -> Path:
    return resolve_hashi_root(root) / "state" / "remote_disabled.json"


def _load_remote_config(root: Path) -> dict[str, Any]:
    path = root / "remote" / "config.yaml"
    if not path.exists() or yaml is None:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def load_settings(root: Path | str | None = None) -> RemoteLifecycleSettings:
    hashi_root = resolve_hashi_root(root)
    data = _load_remote_config(hashi_root)
    lifecycle = data.get("lifecycle") or {}
    server = data.get("server") or {}
    discovery = data.get("discovery") or {}
    return RemoteLifecycleSettings(
        root=hashi_root,
        enabled=_as_bool(lifecycle.get("remote_enabled", data.get("remote_enabled")), True),
        supervised=_as_bool(lifecycle.get("remote_supervised", data.get("remote_supervised")), False),
        disabled_path=disabled_state_path(hashi_root),
        port=int(server.get("port") or 8766),
        use_tls=_as_bool(server.get("use_tls"), True),
        backend=str(discovery.get("backend") or "lan"),
    )


def read_disabled_state(root: Path | str | None = None) -> dict[str, Any] | None:
    path = disabled_state_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {
            "disabled": True,
            "disabled_by": "unknown",
            "reason": "invalid disabled state file",
            "path": str(path),
        }
    if isinstance(data, dict) and data.get("disabled"):
        data.setdefault("path", str(path))
        return data
    return None


def write_disabled_state(
    root: Path | str | None = None,
    *,
    disabled_by: str = "operator",
    reason: str = "manual /remote off",
) -> Path:
    path = disabled_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "disabled": True,
        "disabled_at": datetime.now(timezone.utc).isoformat(),
        "disabled_by": disabled_by,
        "reason": reason,
    }
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    return path


def clear_disabled_state(root: Path | str | None = None) -> bool:
    path = disabled_state_path(root)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def find_python(root: Path) -> Path | None:
    candidates = [
        root / ".venv" / "bin" / "python3",
        root / ".venv" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def build_child_command(settings: RemoteLifecycleSettings) -> list[str]:
    python = find_python(settings.root)
    if python is None:
        raise FileNotFoundError("No Python interpreter found for Hashi Remote")
    cmd = [str(python), "-m", "remote", "--port", str(settings.port)]
    if not settings.use_tls:
        cmd.append("--no-tls")
    if settings.backend in {"lan", "tailscale", "both"}:
        cmd.extend(["--discovery", settings.backend])
    return cmd


async def ensure_remote_started(root: Path | str | None = None) -> dict[str, Any]:
    settings = load_settings(root)
    disabled = read_disabled_state(settings.root)
    if not settings.enabled:
        return {"ok": False, "action": "skipped", "reason": "remote_enabled=false", "settings": settings}
    if disabled:
        return {"ok": False, "action": "skipped", "reason": "remote explicitly disabled", "disabled": disabled, "settings": settings}
    if await _is_port_open("127.0.0.1", settings.port):
        return {"ok": True, "action": "already_running", "settings": settings}
    if settings.supervised:
        return {
            "ok": False,
            "action": "supervisor_unavailable",
            "reason": "OS supervisor integration is not installed in this build",
            "settings": settings,
        }
    cmd = build_child_command(settings)
    log_path = settings.root / "tmp" / "hashi_remote_startup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab")
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(settings.root),
            stdout=log_handle,
            stderr=log_handle,
        )
    finally:
        log_handle.close()
    return {
        "ok": True,
        "action": "started_child",
        "pid": process.pid,
        "process": process,
        "log_path": log_path,
        "settings": settings,
    }


async def _is_port_open(host: str, port: int) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=0.5)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True
