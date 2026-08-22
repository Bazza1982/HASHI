from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
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

from orchestrator.runtime_defaults import DEFAULT_HASHI_REMOTE_PORT
from remote.local_http import local_http_hosts
from remote.runtime_identity import (
    configured_instance_id,
    pid_is_alive,
    read_runtime_claim,
    remove_runtime_claim,
)
from remote.supervisor_identity import resolve_supervisor_identity


_SYSTEMD_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
_SUPERVISOR_HEALTH_ATTEMPTS = 12
_SUPERVISOR_HEALTH_INTERVAL_SECONDS = 0.25


@dataclass(frozen=True)
class RemoteLifecycleSettings:
    root: Path
    enabled: bool
    supervised: bool
    disabled_path: Path
    port: int
    use_tls: bool
    backend: str


@dataclass(frozen=True)
class RemoteSupervisorInfo:
    instance_id: str
    service_name: str
    service_path: Path
    installed: bool
    declared_root: Path | None
    owns_root: bool
    valid_name: bool


def resolve_hashi_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser().resolve()
    env_root = os.getenv("HASHI_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def disabled_state_path(root: Path | str | None = None) -> Path:
    return resolve_hashi_root(root) / "state" / "remote_disabled.json"


def _systemd_user_dir() -> Path:
    config_home = os.getenv("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def _read_unit_working_directory(path: Path) -> Path | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for raw_line in lines:
        line = raw_line.strip()
        if not line.startswith("WorkingDirectory="):
            continue
        value = line.split("=", 1)[1].strip()
        try:
            parts = shlex.split(value)
        except ValueError:
            return None
        if len(parts) != 1:
            return None
        # systemd escapes a literal percent as %% in unit directives.
        return Path(parts[0].replace("%%", "%")).expanduser().resolve()
    return None


def remote_supervisor_info(root: Path | str | None = None) -> RemoteSupervisorInfo:
    resolved_root = resolve_hashi_root(root)
    identity = resolve_supervisor_identity(
        resolved_root,
        instance_id=os.getenv("HASHI_INSTANCE_ID"),
    )
    service_name = str(
        os.getenv("HASHI_REMOTE_SERVICE_NAME") or identity.systemd_service_name
    ).strip()
    valid_name = bool(_SYSTEMD_SERVICE_NAME_RE.fullmatch(service_name))
    service_path = _systemd_user_dir() / service_name if valid_name else _systemd_user_dir()
    installed = valid_name and service_path.is_file()
    declared_root = _read_unit_working_directory(service_path) if installed else None
    return RemoteSupervisorInfo(
        instance_id=identity.instance_id,
        service_name=service_name,
        service_path=service_path,
        installed=installed,
        declared_root=declared_root,
        owns_root=bool(installed and declared_root == resolved_root),
        valid_name=valid_name,
    )


async def control_remote_supervisor(
    root: Path | str | None,
    *,
    action: str,
) -> dict[str, Any]:
    if action not in {"start", "stop", "restart"}:
        raise ValueError(f"unsupported Remote supervisor action: {action}")
    info = remote_supervisor_info(root)
    common = {"supervisor": info, "service_name": info.service_name}
    if not info.valid_name:
        return {
            "ok": False,
            "action": "invalid_supervisor_name",
            "reason": f"invalid systemd service name: {info.service_name}",
            **common,
        }
    if not info.installed:
        return {
            "ok": False,
            "action": "supervisor_unavailable",
            "reason": f"per-instance supervisor is not installed at {info.service_path}",
            **common,
        }
    if not info.owns_root:
        declared = str(info.declared_root) if info.declared_root else "unknown"
        return {
            "ok": False,
            "action": "supervisor_root_mismatch",
            "reason": (
                f"refusing to control {info.service_name}: unit root {declared} "
                f"does not match {resolve_hashi_root(root)}"
            ),
            **common,
        }
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            "--user",
            action,
            info.service_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    except (FileNotFoundError, OSError) as exc:
        return {
            "ok": False,
            "action": "supervisor_control_unavailable",
            "reason": f"systemctl --user is unavailable: {type(exc).__name__}: {exc}",
            **common,
        }
    output = stdout.decode("utf-8", errors="replace").strip()
    error = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        return {
            "ok": False,
            "action": f"supervisor_{action}_failed",
            "reason": error or output or f"systemctl exited {process.returncode}",
            "exit_code": process.returncode,
            "stdout": output,
            "stderr": error,
            **common,
        }
    return {
        "ok": True,
        "action": f"supervisor_{action}ed" if action != "stop" else "supervisor_stopped",
        "exit_code": process.returncode,
        "stdout": output,
        "stderr": error,
        **common,
    }


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
        return int(server.get("port") or DEFAULT_HASHI_REMOTE_PORT)
    except Exception:
        return DEFAULT_HASHI_REMOTE_PORT


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
        control = await control_remote_supervisor(settings.root, action="start")
        if not control.get("ok"):
            return {**control, "settings": settings}
        for _attempt in range(_SUPERVISOR_HEALTH_ATTEMPTS):
            owned = await _find_owned_remote(settings)
            if owned:
                return {
                    **control,
                    "ok": True,
                    "action": "started_supervisor",
                    "settings": settings,
                    **owned,
                }
            await asyncio.sleep(_SUPERVISOR_HEALTH_INTERVAL_SECONDS)
        return {
            **control,
            "ok": False,
            "action": "supervisor_started_unhealthy",
            "reason": (
                f"{control.get('service_name')} started but Remote did not become "
                f"healthy on configured port {settings.port}"
            ),
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
