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


def _interface_ipv4_hosts() -> tuple[str, ...]:
    hosts: list[str] = []

    def _add(host: str) -> None:
        value = str(host or "").strip()
        if not value or value in hosts:
            return
        try:
            socket.inet_aton(value)
        except Exception:
            return
        if value.startswith("127."):
            return
        hosts.append(value)

    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            for addr in adapter.ips:
                if isinstance(addr.ip, str):
                    _add(addr.ip)
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["ip", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=1.0,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                _add(line.split()[1].split("/", 1)[0])
    except Exception:
        pass

    for value in (socket.gethostbyname_ex(socket.gethostname())[2] if socket.gethostname() else []):
        _add(value)

    return tuple(hosts)


@lru_cache(maxsize=1)
def local_http_hosts() -> tuple[str, ...]:
    """Return local HTTP hosts in the order most likely to work.

    WSL mirrored networking can leave 127.0.0.1 TCP connections hanging while
    the loopback alias continues to reach Linux listeners. Prefer that alias on
    WSL, and keep 127.0.0.1 as the fallback for older NAT installs.

    On native Windows installs the local Workbench may be bound to the machine's
    LAN interface instead of loopback. Include real interface IPv4 addresses as
    fallbacks so local Remote enqueue does not fail just because 127.0.0.1 is
    not listening.
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
    if not _is_wsl():
        hosts.append("127.0.0.1")
    hosts.extend(_interface_ipv4_hosts())
    if _is_wsl():
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
