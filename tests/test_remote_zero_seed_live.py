from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

import pytest

from remote.security.shared_token import build_auth_headers


pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.platform]

_REMOTE_BOOTSTRAP = (
    "import os; "
    "from remote.peer import lan; "
    "lan.HASHI_SERVICE_TYPE = os.environ['HASHI_LIVE_MDNS_SERVICE_TYPE']; "
    "from remote.main import main; "
    "raise SystemExit(main())"
)


@dataclass
class _LiveNode:
    instance_id: str
    root: Path
    runtime_root: str
    port: int
    process: subprocess.Popen[bytes]
    log_handle: BinaryIO
    log_path: Path
    platform: str
    started_after: float
    wsl_distro: str = ""


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_root(
    root: Path,
    *,
    instance_id: str,
    display_name: str,
    workbench_port: int,
    token: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agents.json").write_text(
        json.dumps(
            {
                "version": "zero-seed-live-canary",
                "global": {
                    "instance_id": instance_id,
                    "display_name": display_name,
                    "workbench_port": workbench_port,
                },
                "agents": [
                    {
                        "name": "probe",
                        "display_name": f"Probe on {instance_id}",
                        "is_active": True,
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_token(root, token)


def _write_token(root: Path, token: str) -> None:
    (root / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": token}, indent=2) + "\n",
        encoding="utf-8",
    )


def _wsl_path(distro: str, path: Path) -> str:
    result = subprocess.run(
        ["wsl.exe", "--distribution", distro, "--exec", "wslpath", "-a", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _remote_args(
    *,
    runtime_root: str,
    instance_id: str,
    display_name: str,
    port: int,
    workbench_port: int,
) -> list[str]:
    return [
        "-c",
        _REMOTE_BOOTSTRAP,
        "--hashi-root",
        runtime_root,
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--no-tls",
        "--no-lan-mode",
        "--discovery",
        "lan",
        "--instance-id",
        instance_id,
        "--display-name",
        display_name,
        "--workbench-port",
        str(workbench_port),
    ]


def _start_windows_node(
    *,
    source_root: Path,
    root: Path,
    instance_id: str,
    display_name: str,
    port: int,
    workbench_port: int,
    service_type: str,
) -> _LiveNode:
    log_path = root / "remote.log"
    log_handle = log_path.open("wb")
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(root),
            "USERPROFILE": str(root),
            "PYTHONPATH": str(source_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HASHI_LIVE_MDNS_SERVICE_TYPE": service_type,
        }
    )
    started_after = time.time() - 1
    process = subprocess.Popen(
        [
            sys.executable,
            *_remote_args(
                runtime_root=str(root),
                instance_id=instance_id,
                display_name=display_name,
                port=port,
                workbench_port=workbench_port,
            ),
        ],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return _LiveNode(
        instance_id=instance_id,
        root=root,
        runtime_root=str(root.resolve()),
        port=port,
        process=process,
        log_handle=log_handle,
        log_path=log_path,
        platform="windows",
        started_after=started_after,
    )


def _start_wsl_node(
    *,
    source_root: Path,
    root: Path,
    distro: str,
    python: str,
    instance_id: str,
    display_name: str,
    port: int,
    workbench_port: int,
    service_type: str,
) -> _LiveNode:
    runtime_root = _wsl_path(distro, root)
    wsl_source = _wsl_path(distro, source_root)
    log_path = root / "remote.log"
    log_handle = log_path.open("wb")
    started_after = time.time() - 1
    process = subprocess.Popen(
        [
            "wsl.exe",
            "--distribution",
            distro,
            "--cd",
            runtime_root,
            "--exec",
            "env",
            f"HOME={runtime_root}",
            f"PYTHONPATH={wsl_source}",
            "PYTHONDONTWRITEBYTECODE=1",
            f"HASHI_LIVE_MDNS_SERVICE_TYPE={service_type}",
            python,
            *_remote_args(
                runtime_root=runtime_root,
                instance_id=instance_id,
                display_name=display_name,
                port=port,
                workbench_port=workbench_port,
            ),
        ],
        cwd=source_root,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return _LiveNode(
        instance_id=instance_id,
        root=root,
        runtime_root=runtime_root,
        port=port,
        process=process,
        log_handle=log_handle,
        log_path=log_path,
        platform="wsl",
        started_after=started_after,
        wsl_distro=distro,
    )


def _signed_health(node: _LiveNode, token: str) -> dict:
    headers = build_auth_headers(
        shared_token=token,
        method="GET",
        path="/health",
        from_instance="LIVE-CANARY-PROBE",
        body_bytes=b"",
    )
    request = urllib.request.Request(
        f"http://127.0.0.1:{node.port}/health",
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _peer(health: dict, instance_id: str) -> dict | None:
    return next(
        (
            peer
            for peer in health.get("peers", [])
            if str(peer.get("instance_id") or "").upper() == instance_id.upper()
        ),
        None,
    )


def _is_fully_trusted(
    health: dict,
    *,
    peer_id: str,
    peer_display_name: str,
) -> bool:
    discovery = dict(health.get("discovery") or {})
    backends = list(discovery.get("backends") or [])
    peer = _peer(health, peer_id)
    properties = dict((peer or {}).get("properties") or {})
    return bool(
        health.get("trusted_view") is True
        and health.get("status") == "ready"
        and discovery.get("state") == "ready"
        and discovery.get("static_seed_fallback_active") is False
        and backends
        and all(item.get("advertising") and item.get("browsing") for item in backends)
        and peer
        and peer.get("display_name") == peer_display_name
        and properties.get("handshake_state") == "handshake_accepted"
        and properties.get("preferred_backend") == "lan"
        and "handshake_v2" in list(peer.get("capabilities") or [])
        and any(
            item.get("agent_name") == "probe"
            for item in list(properties.get("remote_agents") or [])
        )
    )


def _has_rejected_trust(health: dict, *, peer_id: str) -> bool:
    peer = _peer(health, peer_id)
    properties = dict((peer or {}).get("properties") or {})
    return bool(
        peer
        and properties.get("handshake_state") == "handshake_rejected"
        and not peer.get("capabilities")
        and not properties.get("remote_agents")
        and not properties.get("remote_agent_directory")
        and not properties.get("remote_supervisor")
    )


def _wait_for(
    predicate: Callable[[], object],
    *,
    nodes: list[_LiveNode],
    timeout: float = 45,
) -> object:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        for node in nodes:
            if node.process.poll() is not None:
                raise AssertionError(
                    f"{node.instance_id} exited with {node.process.returncode}:\n{_log_tail(node)}"
                )
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # A starting HTTP listener is expected to refuse briefly.
            last_error = exc
        time.sleep(0.25)
    detail = f"; last error={type(last_error).__name__}: {last_error}" if last_error else ""
    logs = "\n".join(f"[{node.instance_id}]\n{_log_tail(node)}" for node in nodes)
    raise AssertionError(f"live Remote condition did not settle within {timeout}s{detail}\n{logs}")


def _log_tail(node: _LiveNode, lines: int = 80) -> str:
    node.log_handle.flush()
    try:
        return "\n".join(
            node.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    except OSError:
        return "<log unavailable>"


def _validated_claim(node: _LiveNode) -> dict | None:
    path = node.root / "state" / "remote_runtime_claim.json"
    if not path.exists():
        return None
    try:
        claim = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if str(claim.get("instance_id") or "") != node.instance_id:
        return None
    if str(claim.get("root") or "") != node.runtime_root:
        return None
    if float(claim.get("started_at") or 0) < node.started_after:
        return None
    pid = int(claim.get("pid") or 0)
    return claim if pid > 0 else None


def _stop_node(node: _LiveNode) -> None:
    claim = _validated_claim(node)
    if claim:
        pid = str(int(claim["pid"]))
        if node.platform == "wsl":
            subprocess.run(
                [
                    "wsl.exe",
                    "--distribution",
                    node.wsl_distro,
                    "--exec",
                    "kill",
                    "-TERM",
                    pid,
                ],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
    try:
        node.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        node.process.terminate()
        try:
            node.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            node.process.kill()
            node.process.wait(timeout=5)
    finally:
        node.log_handle.close()


@pytest.mark.skipif(sys.platform != "win32", reason="cross-platform H2/H3 canary starts from Windows")
def test_zero_seed_wsl_windows_discovery_trust_and_token_rotation(tmp_path: Path) -> None:
    distro = os.getenv("HASHI_LIVE_WSL_DISTRO", "").strip()
    wsl_python = os.getenv("HASHI_LIVE_WSL_PYTHON", "").strip()
    if not distro or not wsl_python:
        pytest.skip("set HASHI_LIVE_WSL_DISTRO and HASHI_LIVE_WSL_PYTHON for the authorized canary")

    source_root = Path(__file__).resolve().parent.parent
    suffix = secrets.token_hex(4).upper()
    windows_id = f"CANARY-WIN-{suffix}"
    wsl_id = f"CANARY-WSL-{suffix}"
    service_type = f"_hc{suffix.lower()}._tcp.local."
    initial_token = secrets.token_urlsafe(32)
    rotated_token = secrets.token_urlsafe(32)
    windows_display = "Windows production handshake metadata " + "界" * 80
    wsl_display = "WSL production handshake metadata " + "界" * 80
    windows_root = tmp_path / "windows-node"
    wsl_root = tmp_path / "wsl-node"
    windows_port = _reserve_port()
    wsl_port = _reserve_port()

    _write_root(
        windows_root,
        instance_id=windows_id,
        display_name=windows_display,
        workbench_port=_reserve_port(),
        token=initial_token,
    )
    _write_root(
        wsl_root,
        instance_id=wsl_id,
        display_name=wsl_display,
        workbench_port=_reserve_port(),
        token=initial_token,
    )
    assert not (windows_root / "instances.json").exists()
    assert not (wsl_root / "instances.json").exists()

    nodes: list[_LiveNode] = []
    try:
        windows = _start_windows_node(
            source_root=source_root,
            root=windows_root,
            instance_id=windows_id,
            display_name=windows_display,
            port=windows_port,
            workbench_port=json.loads((windows_root / "agents.json").read_text())["global"]["workbench_port"],
            service_type=service_type,
        )
        nodes.append(windows)
        wsl = _start_wsl_node(
            source_root=source_root,
            root=wsl_root,
            distro=distro,
            python=wsl_python,
            instance_id=wsl_id,
            display_name=wsl_display,
            port=wsl_port,
            workbench_port=json.loads((wsl_root / "agents.json").read_text())["global"]["workbench_port"],
            service_type=service_type,
        )
        nodes.append(wsl)

        initial = _wait_for(
            lambda: (
                _signed_health(windows, initial_token),
                _signed_health(wsl, initial_token),
            )
            if _is_fully_trusted(
                _signed_health(windows, initial_token),
                peer_id=wsl_id,
                peer_display_name=wsl_display,
            )
            and _is_fully_trusted(
                _signed_health(wsl, initial_token),
                peer_id=windows_id,
                peer_display_name=windows_display,
            )
            else None,
            nodes=nodes,
        )
        windows_health, wsl_health = initial
        assert windows_health["instance"]["platform"] == "windows"
        assert wsl_health["instance"]["platform"] == "wsl"
        assert not (windows_root / "instances.json").exists()
        assert not (wsl_root / "instances.json").exists()

        (windows_root / "secrets.json").write_text("{ invalid json\n", encoding="utf-8")
        malformed = _wait_for(
            lambda: health
            if (health := _signed_health(windows, initial_token)).get("credential", {}).get(
                "configuration_state"
            )
            == "invalid"
            else None,
            nodes=nodes,
        )
        assert malformed["credential"]["configuration_error"] == "invalid_json"
        assert _peer(malformed, wsl_id)["properties"]["handshake_state"] == "handshake_accepted"

        _write_token(windows_root, initial_token)
        _wait_for(
            lambda: health
            if (health := _signed_health(windows, initial_token)).get("credential", {}).get(
                "configuration_state"
            )
            == "configured_file"
            else None,
            nodes=nodes,
        )

        _write_token(windows_root, rotated_token)
        def both_sides_reject_old_trust():
            windows_health = _signed_health(windows, rotated_token)
            wsl_health = _signed_health(wsl, initial_token)
            if (
                windows_health.get("credential", {}).get("generation", 0) >= 2
                and _has_rejected_trust(windows_health, peer_id=wsl_id)
                and _has_rejected_trust(wsl_health, peer_id=windows_id)
            ):
                return windows_health, wsl_health
            return None

        untrusted = _wait_for(
            both_sides_reject_old_trust,
            nodes=nodes,
        )
        assert untrusted[0]["credential"]["rehandshake_required"] is True

        _write_token(wsl_root, rotated_token)
        restored = _wait_for(
            lambda: (
                _signed_health(windows, rotated_token),
                _signed_health(wsl, rotated_token),
            )
            if _is_fully_trusted(
                _signed_health(windows, rotated_token),
                peer_id=wsl_id,
                peer_display_name=wsl_display,
            )
            and _is_fully_trusted(
                _signed_health(wsl, rotated_token),
                peer_id=windows_id,
                peer_display_name=windows_display,
            )
            else None,
            nodes=nodes,
        )
        assert restored[0]["credential"]["generation"] >= 2
        assert restored[1]["credential"]["generation"] >= 2
        assert not (windows_root / "instances.json").exists()
        assert not (wsl_root / "instances.json").exists()
    finally:
        for node in reversed(nodes):
            _stop_node(node)
