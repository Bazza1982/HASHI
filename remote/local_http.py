from __future__ import annotations

import socket
import subprocess
from functools import lru_cache


def _is_wsl() -> bool:
    try:
        release = open("/proc/sys/kernel/osrelease", encoding="utf-8").read().lower()
    except Exception:
        return False
    return "microsoft" in release or "wsl" in release


@lru_cache(maxsize=1)
def local_http_hosts() -> tuple[str, ...]:
    """Return local HTTP hosts in the order most likely to work.

    WSL mirrored networking can leave 127.0.0.1 TCP connections hanging while
    the loopback alias continues to reach Linux listeners. Prefer that alias on
    WSL, and keep 127.0.0.1 as the fallback for older NAT installs.
    """
    hosts: list[str] = []
    if _is_wsl():
        try:
            output = subprocess.check_output(
                ["ip", "-brief", "-4", "addr", "show", "lo"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=0.5,
            )
            for token in output.split():
                if "/" not in token:
                    continue
                host = token.split("/", 1)[0]
                if host and host != "127.0.0.1":
                    hosts.append(host)
        except Exception:
            pass
    hosts.append("127.0.0.1")
    deduped: list[str] = []
    for host in hosts:
        if host not in deduped:
            deduped.append(host)
    return tuple(deduped)


def local_http_url(port: int, path: str, *, host: str | None = None) -> str:
    selected = host or local_http_hosts()[0]
    normalized_path = path if str(path).startswith("/") else f"/{path}"
    return f"http://{selected}:{int(port)}{normalized_path}"


def is_local_http_host(host: str | None) -> bool:
    value = str(host or "").strip().lower()
    if value in {"localhost", "::1"}:
        return True
    try:
        socket.inet_aton(value)
    except Exception:
        return False
    return value in set(local_http_hosts()) | {"127.0.0.1"}
