from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PORT = 8876


def _paths(root: Path) -> dict[str, Path]:
    return {
        "pid": root / "state" / "oll_gateway.pid",
        "log": root / "logs" / "oll_gateway.log",
        "audit": root / "logs" / "oll_gateway.audit.jsonl",
        "db": root / "state" / "browser_gateway.sqlite",
    }


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status(root: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> dict:
    paths = _paths(root)
    running = False
    pid = None
    if paths["pid"].exists():
        try:
            pid = int(paths["pid"].read_text(encoding="utf-8").strip())
            running = _pid_alive(pid)
        except Exception:
            pid = None
    if pid and not running and paths["pid"].exists():
        paths["pid"].unlink(missing_ok=True)
    return {
        "running": running,
        "pid": pid if running else None,
        "pid_file": str(paths["pid"]),
        "log_file": str(paths["log"]),
        "audit_file": str(paths["audit"]),
        "state_db": str(paths["db"]),
        "base_url": f"http://{host}:{port}",
    }


def start(root: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT, workbench_url: str = "http://127.0.0.1:18800", public_base_url: str = "") -> dict:
    current = status(root, host=host, port=port)
    if current["running"]:
        return current
    paths = _paths(root)
    paths["pid"].parent.mkdir(parents=True, exist_ok=True)
    paths["log"].parent.mkdir(parents=True, exist_ok=True)
    python_bin = root / ".venv" / "bin" / "python3"
    if not python_bin.exists():
        python_bin = Path(sys.executable)
    with paths["log"].open("ab") as log_fh:
        proc = subprocess.Popen(
            [
                str(python_bin),
                "-m",
                "browser_gateway",
                "--host",
                host,
                "--port",
                str(port),
                "--workbench-url",
                workbench_url,
                "--state-db",
                str(paths["db"]),
                "--audit-log",
                str(paths["audit"]),
                "--public-base-url",
                public_base_url,
            ],
            cwd=str(root),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    paths["pid"].write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.5)
    return status(root, host=host, port=port)


def stop(root: Path, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> dict:
    current = status(root, host=host, port=port)
    pid = current.get("pid")
    paths = _paths(root)
    if not pid:
        return current
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + 5
    while time.time() < deadline:
        if not _pid_alive(pid):
            break
        time.sleep(0.2)
    if _pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    paths["pid"].unlink(missing_ok=True)
    return status(root, host=host, port=port)
