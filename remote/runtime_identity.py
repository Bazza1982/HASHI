from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def runtime_claim_path(root: Path | str) -> Path:
    return Path(root).expanduser().resolve() / "state" / "remote_runtime_claim.json"


def configured_instance_id(root: Path | str) -> str:
    path = Path(root).expanduser().resolve() / "agents.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return ""
    return str(((data or {}).get("global") or {}).get("instance_id") or "").strip()


def validate_launch_context(*, hashi_root: Path, cwd: Path | None = None) -> None:
    """Refuse launches where Python loaded Remote from one HASHI root but cwd is another.

    This catches the dangerous case where a copied workspace runs `python -m remote`
    through an environment that imports the original workspace's `remote` package.
    """
    root = hashi_root.expanduser().resolve()
    working = (cwd or Path.cwd()).expanduser().resolve()
    if working == root:
        return
    if not ((working / "agents.json").exists() or (working / "remote").exists()):
        return
    root_id = configured_instance_id(root) or "unknown"
    cwd_id = configured_instance_id(working) or "unknown"
    raise RuntimeError(
        "Refusing Hashi Remote launch: imported root "
        f"{root} ({root_id}) differs from working HASHI root {working} ({cwd_id}). "
        "Use the matching virtualenv/package or pass --hashi-root explicitly."
    )


def write_runtime_claim(
    *,
    root: Path | str,
    instance_id: str,
    port: int,
    bind_host: str,
    code_root: Path | str,
    supervised: bool,
) -> dict[str, Any]:
    path = runtime_claim_path(root)
    now = time.time()
    claim = {
        "instance_id": str(instance_id),
        "root": str(Path(root).expanduser().resolve()),
        "code_root": str(Path(code_root).expanduser().resolve()),
        "pid": os.getpid(),
        "port": int(port),
        "bind_host": str(bind_host),
        "supervised": bool(supervised),
        "started_at": now,
        "updated_at": now,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(claim, indent=2, sort_keys=True), encoding="utf-8")
    return claim


def read_runtime_claim(root: Path | str) -> dict[str, Any] | None:
    path = runtime_claim_path(root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def remove_runtime_claim(root: Path | str, *, pid: int | None = None) -> bool:
    path = runtime_claim_path(root)
    current = read_runtime_claim(root)
    if pid is not None and current and int(current.get("pid") or 0) not in {0, int(pid)}:
        return False
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def pid_is_alive(pid: int | str | None) -> bool:
    try:
        value = int(pid or 0)
    except Exception:
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
