#!/usr/bin/env python3

import sys
import time
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
