from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


STATE_FILENAME = "runtime_port_assignments.json"
LOCK_FILENAME = ".runtime_port_assignments.lock"
DEFAULT_POOL_MIN = 20000
DEFAULT_POOL_MAX = 60999
DEFAULT_POOL_ATTEMPTS = 96
SERVICE_HASHI_REMOTE = "hashi_remote"

POPULAR_PORTS = {
    22,
    25,
    53,
    80,
    110,
    143,
    443,
    465,
    587,
    993,
    995,
    1433,
    1521,
    2049,
    2375,
    2376,
    3000,
    3306,
    5000,
    5173,
    5432,
    5601,
    5672,
    5900,
    6379,
    8000,
    8080,
    8443,
    8765,
    8766,
    8767,
    8768,
    8769,
    8770,
    8888,
    9200,
    9300,
    27017,
}


class PortAllocationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PortAssignment:
    service: str
    port: int
    source: str
    state_path: Path
    attempted_ports: list[int]
    persisted: bool


def is_port_available(host: str, port: int) -> bool:
    bind_host = "" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((bind_host, int(port)))
        except OSError:
            return False
    return True


def _expand_range(start: int, end: int) -> set[int]:
    if end < start:
        start, end = end, start
    return set(range(start, end + 1))


def read_linux_ephemeral_range(
    *,
    range_path: Path = Path("/proc/sys/net/ipv4/ip_local_port_range"),
    read_text: Callable[[Path], str] | None = None,
) -> set[int]:
    if sys.platform == "win32":
        return set()
    reader = read_text or (lambda path: path.read_text(encoding="utf-8"))
    try:
        raw = reader(range_path).strip()
    except OSError:
        return set()
    parts = raw.split()
    if len(parts) != 2:
        return set()
    try:
        start, end = int(parts[0]), int(parts[1])
    except ValueError:
        return set()
    return _expand_range(start, end)


def _run_command(args: list[str]) -> str:
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    return (completed.stdout or completed.stderr or "").strip()


def read_windows_excluded_ports(
    *,
    runner: Callable[[list[str]], str] | None = None,
) -> set[int]:
    output = (runner or _run_command)(
        ["netsh", "interface", "ipv4", "show", "excludedportrange", "protocol=tcp"]
    )
    reserved: set[int] = set()
    for line in output.splitlines():
        tokens = line.replace("*", " ").split()
        numbers = [token for token in tokens if token.isdigit()]
        if len(numbers) < 2:
            continue
        reserved.update(_expand_range(int(numbers[0]), int(numbers[1])))
    return reserved


def read_windows_dynamic_ports(
    *,
    runner: Callable[[list[str]], str] | None = None,
) -> set[int]:
    output = (runner or _run_command)(["netsh", "int", "ipv4", "show", "dynamicport", "tcp"])
    start = None
    count = None
    for line in output.splitlines():
        lowered = line.lower()
        if "start port" in lowered:
            with suppress(ValueError):
                start = int(line.split(":")[-1].strip())
        elif "number of ports" in lowered:
            with suppress(ValueError):
                count = int(line.split(":")[-1].strip())
    if start is None or count is None or count <= 0:
        return set()
    return _expand_range(start, start + count - 1)


def os_reserved_ports(
    *,
    platform_name: str | None = None,
    linux_range_reader: Callable[[Path], str] | None = None,
    windows_runner: Callable[[list[str]], str] | None = None,
) -> set[int]:
    platform_key = (platform_name or sys.platform).lower()
    if platform_key.startswith("win"):
        return read_windows_excluded_ports(runner=windows_runner) | read_windows_dynamic_ports(runner=windows_runner)
    if "linux" in platform_key or "wsl" in platform_key:
        return read_linux_ephemeral_range(read_text=linux_range_reader)
    return set()


def candidate_ports(
    requested_port: int | None,
    configured_port: int | None,
    *,
    reserved_ports: set[int] | None = None,
    rng: Callable[[int, int], int] | None = None,
    pool_min: int = DEFAULT_POOL_MIN,
    pool_max: int = DEFAULT_POOL_MAX,
    attempts: int = DEFAULT_POOL_ATTEMPTS,
) -> list[int]:
    candidates: list[int] = []
    reserved = set(reserved_ports or set())
    randint = rng or secrets.SystemRandom().randint

    def add(value: int | None, *, skip_common: bool = False) -> None:
        if not value:
            return
        try:
            port = int(value)
        except Exception:
            return
        if port <= 0 or port in reserved:
            return
        if skip_common and port in POPULAR_PORTS:
            return
        if port not in candidates:
            candidates.append(port)

    add(requested_port)
    add(configured_port)
    guard = 0
    while len(candidates) < attempts + 2 and guard < attempts * 32:
        guard += 1
        add(randint(pool_min, pool_max), skip_common=True)
    return candidates


def _read_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"version": 1, "assignments": {}}
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "assignments": {}}
    if not isinstance(payload, dict):
        return {"version": 1, "assignments": {}}
    assignments = payload.get("assignments")
    if not isinstance(assignments, dict):
        payload["assignments"] = {}
    payload.setdefault("version", 1)
    return payload


def _write_state(state_path: Path, payload: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(state_path)


class AllocationLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fh = None

    def __enter__(self) -> "AllocationLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            fh.seek(0)
            fh.truncate(0)
            fh.write(str(os.getpid()).encode("utf-8"))
            fh.flush()
        except Exception:
            fh.close()
            raise
        self._fh = fh
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


class StablePortAllocator:
    def __init__(
        self,
        *,
        bridge_home: Path,
        service: str,
        host: str,
        state_path: Path | None = None,
        lock_path: Path | None = None,
        availability_probe: Callable[[str, int], bool] | None = None,
        platform_name: str | None = None,
        linux_range_reader: Callable[[Path], str] | None = None,
        windows_runner: Callable[[list[str]], str] | None = None,
        rng: Callable[[int, int], int] | None = None,
    ):
        self.bridge_home = bridge_home
        self.service = service
        self.host = host
        self.state_path = state_path or (bridge_home / STATE_FILENAME)
        self.lock_path = lock_path or (bridge_home / LOCK_FILENAME)
        self.availability_probe = availability_probe or is_port_available
        self.platform_name = platform_name
        self.linux_range_reader = linux_range_reader
        self.windows_runner = windows_runner
        self.rng = rng

    def _reserved_ports(self) -> set[int]:
        return os_reserved_ports(
            platform_name=self.platform_name,
            linux_range_reader=self.linux_range_reader,
            windows_runner=self.windows_runner,
        )

    def _persist(self, payload: dict) -> None:
        _write_state(self.state_path, payload)

    def reset(self) -> bool:
        with AllocationLock(self.lock_path):
            payload = _read_state(self.state_path)
            assignments = payload.setdefault("assignments", {})
            removed = assignments.pop(self.service, None) is not None
            if removed:
                self._persist(payload)
            return removed

    def status(self) -> dict:
        payload = _read_state(self.state_path)
        entry = (payload.get("assignments") or {}).get(self.service)
        if not isinstance(entry, dict):
            return {
                "service": self.service,
                "state_path": str(self.state_path),
                "assigned": False,
            }
        port = int(entry.get("port") or 0)
        available = bool(port and self.availability_probe(self.host, port))
        return {
            "service": self.service,
            "state_path": str(self.state_path),
            "assigned": port > 0,
            "port": port,
            "source": entry.get("source"),
            "available": available,
        }

    def reserve_configured_port(self, configured_port: int) -> PortAssignment:
        return self._assign(configured_port=configured_port, allow_fallback=True)

    def validate_explicit_port(self, port: int) -> PortAssignment:
        attempted = [int(port)]
        if not self.availability_probe(self.host, int(port)):
            raise PortAllocationError(
                f"Explicit {self.service} port {port} is unavailable on {self.host}. "
                f"Choose another port or release the conflicting listener."
            )
        return PortAssignment(
            service=self.service,
            port=int(port),
            source="explicit",
            state_path=self.state_path,
            attempted_ports=attempted,
            persisted=False,
        )

    def _assign(self, *, configured_port: int, allow_fallback: bool) -> PortAssignment:
        with AllocationLock(self.lock_path):
            payload = _read_state(self.state_path)
            assignments = payload.setdefault("assignments", {})
            entry = assignments.get(self.service)
            if isinstance(entry, dict) and int(entry.get("port") or 0) > 0:
                port = int(entry["port"])
                if not self.availability_probe(self.host, port):
                    raise PortAllocationError(
                        f"Persisted {self.service} port {port} from {self.state_path} is unavailable on {self.host}. "
                        f"Release the conflicting listener or run the reset flow before restarting."
                    )
                return PortAssignment(
                    service=self.service,
                    port=port,
                    source=str(entry.get("source") or "persisted"),
                    state_path=self.state_path,
                    attempted_ports=[port],
                    persisted=True,
                )

            attempted: list[int] = [int(configured_port)]
            if self.availability_probe(self.host, int(configured_port)):
                assignments[self.service] = {"port": int(configured_port), "source": "configured"}
                self._persist(payload)
                return PortAssignment(
                    service=self.service,
                    port=int(configured_port),
                    source="configured",
                    state_path=self.state_path,
                    attempted_ports=attempted,
                    persisted=True,
                )

            if not allow_fallback:
                raise PortAllocationError(
                    f"Configured {self.service} port {configured_port} is unavailable on {self.host}. "
                    f"Release the port or configure another explicit port."
                )

            reserved_ports = self._reserved_ports()
            for port in candidate_ports(
                configured_port,
                configured_port,
                reserved_ports=reserved_ports,
                rng=self.rng,
            ):
                if port not in attempted:
                    attempted.append(port)
                if self.availability_probe(self.host, port):
                    assignments[self.service] = {"port": int(port), "source": "allocated"}
                    self._persist(payload)
                    return PortAssignment(
                        service=self.service,
                        port=int(port),
                        source="allocated",
                        state_path=self.state_path,
                        attempted_ports=attempted,
                        persisted=True,
                    )

        raise PortAllocationError(
            f"No available {self.service} port found for {self.host}; attempted={attempted}. "
            f"Run the reset flow or widen the allocation pool."
        )
