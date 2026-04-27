#!/usr/bin/env python3

import sys
import time
import json
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.modules.setdefault("edge_tts", SimpleNamespace())
sys.modules.setdefault(
    "zeroconf",
    SimpleNamespace(
        IPVersion=object,
        InterfaceChoice=object,
        ServiceBrowser=object,
        ServiceInfo=object,
        ServiceListener=object,
        Zeroconf=object,
    ),
)

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from remote.peer.registry import PeerRegistry
from remote.protocol_manager import ProtocolManager


class _PresenceDummy:
    _format_remote_age = FlexibleAgentRuntime._format_remote_age


class _RemoteStartDummy:
    _read_remote_start_log_excerpt = FlexibleAgentRuntime._read_remote_start_log_excerpt


class _RemoteRenderDummy:
    _render_remote_peer_endpoints = FlexibleAgentRuntime._render_remote_peer_endpoints
    _load_remote_instances = FlexibleAgentRuntime._load_remote_instances
    _peer_network_hosts = FlexibleAgentRuntime._peer_network_hosts


def test_registry_derives_offline_for_timed_out_peer_without_live_fields(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text('{"instances": {}}', encoding="utf-8")
    registry = PeerRegistry(hashi_root, "HASHI2")

    status = registry._derive_live_status(
        {
            "handshake_state": "handshake_timed_out",
            "last_handshake_at": int(time.time()),
            "last_error": "all hosts unreachable",
        }
    )

    assert status == "offline"


def test_remote_peer_presence_shows_offline_for_timed_out_peer_without_live_status():
    dummy = _PresenceDummy()
    peer = {
        "properties": {
            "handshake_state": "handshake_timed_out",
            "last_handshake_at": int(time.time()),
            "last_error": "all hosts unreachable",
        }
    }

    rank, presence, state = FlexibleAgentRuntime._remote_peer_presence(dummy, peer)

    assert rank == 3
    assert presence == "🔴 offline"
    assert state == "handshake_timed_out"


def test_remote_peer_presence_shows_offline_for_legacy_in_progress_peer_with_last_error():
    dummy = _PresenceDummy()
    peer = {
        "properties": {
            "handshake_state": "handshake_in_progress",
            "last_handshake_at": int(time.time()),
            "last_error": "all hosts unreachable",
        }
    }

    rank, presence, state = FlexibleAgentRuntime._remote_peer_presence(dummy, peer)

    assert rank == 3
    assert presence == "🔴 offline"
    assert state == "handshake_in_progress"


def test_registry_keeps_recently_healthy_peer_online_across_refresh_window(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text('{"instances": {}}', encoding="utf-8")
    registry = PeerRegistry(hashi_root, "HASHI2")

    now = int(time.time())
    status = registry._derive_live_status(
        {
            "handshake_state": "handshake_accepted",
            "last_seen_ok": now - 45,
            "consecutive_failures": 0,
        },
        now=now,
    )

    assert status == "online"


def test_registry_marks_healthy_peer_stale_after_refresh_window_expires(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text('{"instances": {}}', encoding="utf-8")
    registry = PeerRegistry(hashi_root, "HASHI2")

    now = int(time.time())
    status = registry._derive_live_status(
        {
            "handshake_state": "handshake_accepted",
            "last_seen_ok": now - 120,
            "consecutive_failures": 0,
        },
        now=now,
    )

    assert status == "stale"


def test_bootstrap_dedupes_legacy_aliases_on_same_endpoint():
    manager = object.__new__(ProtocolManager)
    manager._instance_info = {"instance_id": "HASHI2", "platform": "wsl"}
    manager._load_instances = lambda: {}

    instances = {
        "msi": {
            "instance_id": "MSI",
            "display_name": "MSI (Barry's Main Gaming Rig)",
            "platform": "windows",
            "api_host": "192.168.0.41",
            "lan_ip": "192.168.0.41",
            "remote_port": 8766,
            "workbench_port": 8779,
            "protocol_version": "2.0",
            "capabilities": ["handshake_v2", "protocol_message_v1"],
            "host_identity": "desktopvn0amd7",
            "environment_kind": "windows",
        },
        "hashi-desktop": {
            "instance_id": "HASHI-DESKTOP",
            "display_name": "HASHI Desktop (5950X/3090)",
            "platform": "windows",
            "api_host": "192.168.0.41",
            "lan_ip": "192.168.0.41",
            "remote_port": 8766,
            "workbench_port": 8779,
            "protocol_version": "1.0",
            "capabilities": [],
        },
    }

    deduped = ProtocolManager._dedupe_bootstrap_instances(manager, instances)

    assert "msi" in deduped
    assert "hashi-desktop" not in deduped


def test_remote_start_failure_message_includes_exit_code_and_log_excerpt(tmp_path):
    dummy = _RemoteStartDummy()
    dummy.global_config = SimpleNamespace(project_root=tmp_path)
    dummy.config = SimpleNamespace(agent_name="lin_yueru")
    log_path = tmp_path / "tmp" / "lin_yueru_remote_startup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("Traceback\nModuleNotFoundError: No module named 'uvicorn'\n", encoding="utf-8")

    message = FlexibleAgentRuntime._build_remote_start_failure_message(
        dummy,
        cfg={"port": 8766, "use_tls": False, "backend": "lan"},
        cmd=["/tmp/python", "-m", "remote", "--no-tls"],
        reason="process exited before /health became ready",
        log_path=log_path,
        exit_code=1,
    )

    assert "failed to start" in message
    assert "Exit code: <code>1</code>" in message
    assert "uvicorn" in message


def test_remote_start_failure_message_falls_back_to_log_path(tmp_path):
    dummy = _RemoteStartDummy()
    dummy.global_config = SimpleNamespace(project_root=tmp_path)
    dummy.config = SimpleNamespace(agent_name="lin_yueru")
    log_path = tmp_path / "tmp" / "lin_yueru_remote_startup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    message = FlexibleAgentRuntime._build_remote_start_failure_message(
        dummy,
        cfg={"port": 8767, "use_tls": True, "backend": "tailscale"},
        cmd=["/tmp/python", "-m", "remote"],
        reason="health endpoint did not become ready within timeout",
        log_path=log_path,
        exit_code=None,
    )

    assert "health endpoint did not become ready within timeout" in message
    assert str(log_path) in message


def test_render_remote_peer_endpoints_explains_same_host_loopback(tmp_path):
    (tmp_path / "instances.json").write_text(
        json.dumps(
            {
                "instances": {
                    "hashi9": {
                        "instance_id": "HASHI9",
                        "api_host": "127.0.0.1",
                        "lan_ip": "192.168.0.211",
                        "remote_port": 8768,
                        "same_host_loopback": "127.0.0.1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    dummy = _RemoteRenderDummy()
    dummy.global_config = SimpleNamespace(project_root=tmp_path)
    peer = {
        "instance_id": "HASHI9",
        "host": "127.0.0.1",
        "port": 8768,
        "properties": {},
    }

    lines = FlexibleAgentRuntime._render_remote_peer_endpoints(dummy, peer)

    assert lines == [
        "route: <code>127.0.0.1:8768</code>  ·  <code>same host</code>  ·  network: <code>192.168.0.211:8768</code>"
    ]


def test_render_remote_peer_endpoints_shows_route_and_network_when_they_differ(tmp_path):
    (tmp_path / "instances.json").write_text(
        json.dumps(
            {
                "instances": {
                    "msi": {
                        "instance_id": "MSI",
                        "api_host": "192.168.0.41",
                        "lan_ip": "192.168.0.41",
                        "remote_port": 8767,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    dummy = _RemoteRenderDummy()
    dummy.global_config = SimpleNamespace(project_root=tmp_path)
    peer = {
        "instance_id": "MSI",
        "host": "desktopvn0amd7",
        "port": 8767,
        "properties": {},
    }

    lines = FlexibleAgentRuntime._render_remote_peer_endpoints(dummy, peer)

    assert lines == [
        "route: <code>desktopvn0amd7:8767</code>",
        "network: <code>192.168.0.41:8767</code>",
    ]
