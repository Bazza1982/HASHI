from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency fallback
    yaml = None

from remote.local_http import local_http_hosts
from remote.runtime_identity import (
    configured_instance_id,
    pid_is_alive,
    read_runtime_claim,
    remove_runtime_claim,
)


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


def _load_agents_config(root: Path) -> dict[str, Any]:
    path = root / "agents.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_remote_port(root: Path, data: dict[str, Any]) -> int:
    agents = _load_agents_config(root)
    global_cfg = agents.get("global") or {}
    instance_id = str(global_cfg.get("instance_id") or "").strip().lower()
    instances_path = root / "instances.json"
    if instance_id and instances_path.exists():
        try:
            instances = json.loads(instances_path.read_text(encoding="utf-8")).get("instances", {}) or {}
        except Exception:
            instances = {}
        value = (instances.get(instance_id) or {}).get("remote_port")
        if value:
            try:
                return int(value)
            except Exception:
                pass
    value = global_cfg.get("remote_port")
    if value:
        try:
            return int(value)
        except Exception:
            pass
    server = data.get("server") or {}
    try:
        return int(server.get("port") or 8766)
    except Exception:
        return 8766


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
        port=_resolve_remote_port(hashi_root, data),
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
    cmd = [str(python), "-m", "remote", "--hashi-root", str(settings.root), "--port", str(settings.port)]
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
    owned = await _find_owned_remote(settings)
    if owned:
        return {"ok": True, "action": "already_running", "settings": settings, **owned}
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


async def _find_owned_remote(settings: RemoteLifecycleSettings) -> dict[str, Any] | None:
    expected_id = configured_instance_id(settings.root).upper()
    claim = read_runtime_claim(settings.root)
    ports: list[int] = []
    if claim:
        try:
            ports.append(int(claim.get("port") or 0))
        except Exception:
            pass
    ports.append(settings.port)
    ports = [port for index, port in enumerate(ports) if port > 0 and port not in ports[:index]]

    for port in ports:
        for host in local_http_hosts():
            health = await _fetch_remote_health(host, port)
            if not health:
                continue
            instance = health.get("instance") or {}
            actual_id = str(instance.get("instance_id") or "").strip().upper()
            runtime_claim = instance.get("runtime_claim") or {}
            claim_root = str(runtime_claim.get("root") or "").strip()
            root_matches = not claim_root or Path(claim_root).expanduser().resolve() == settings.root
            if actual_id == expected_id and root_matches:
                return {"port": port, "health": health, "health_host": host}
    if claim and not pid_is_alive(claim.get("pid")):
        remove_runtime_claim(settings.root)
    return None


async def _fetch_remote_health(host: str, port: int) -> dict[str, Any] | None:
    url = f"http://{host}:{int(port)}/health"
    try:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: _fetch_json(url))
    except Exception:
        return None


def _fetch_json(url: str) -> dict[str, Any] | None:
    with urllib.request.urlopen(url, timeout=0.8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data if isinstance(data, dict) else None


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
