#!/usr/bin/env python3

import asyncio
import stat
import sys
import time
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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
from remote.peer.base import PeerInfo
from remote.peer.registry import PeerRegistry
from remote.peer.tailscale import TailscaleDiscovery
from remote.protocol_manager import ProtocolManager
from remote.live_endpoints import read_live_endpoints, remove_live_endpoint, write_live_endpoints
from tools.hchat_send import parse_hchat_message


class _PresenceDummy:
    _format_remote_age = FlexibleAgentRuntime._format_remote_age


class _RemoteStartDummy:
    _read_remote_start_log_excerpt = FlexibleAgentRuntime._read_remote_start_log_excerpt


class _RemoteRenderDummy:
    _render_remote_peer_endpoints = FlexibleAgentRuntime._render_remote_peer_endpoints
    _load_remote_instances = FlexibleAgentRuntime._load_remote_instances
    _peer_network_hosts = FlexibleAgentRuntime._peer_network_hosts


class _RemoteConfigDummy:
    _remote_config_snapshot = FlexibleAgentRuntime._remote_config_snapshot
    _remote_urls = FlexibleAgentRuntime._remote_urls


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


def test_registry_refresh_success_derives_live_status_via_common_path(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text('{"instances": {}}', encoding="utf-8")
    registry = PeerRegistry(hashi_root, "HASHI2")
    registry._peers["HASHI1"] = PeerInfo(
        instance_id="HASHI1",
        display_name="HASHI1",
        host="192.168.0.211",
        port=8766,
        workbench_port=18800,
        platform="wsl",
        properties={"handshake_state": "handshake_accepted"},
    )

    now = int(time.time())
    registry.mark_refresh_result("HASHI1", ok=True, checked_at=now)

    assert registry._peers["HASHI1"].properties["live_status"] == "online"


def test_registry_rebuild_keeps_observed_host_for_same_host_wsl_peer(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text(
        json.dumps(
            {
                "instances": {
                    "hashi2": {
                        "instance_id": "HASHI2",
                        "platform": "wsl",
                        "wsl_root_from_windows": r"\\\\wsl$\\Ubuntu-22.04\\home\\lily\\projects\\hashi2",
                    },
                    "hashi1": {
                        "instance_id": "HASHI1",
                        "platform": "wsl",
                        "wsl_root_from_windows": r"\\\\wsl$\\Ubuntu-22.04\\home\\lily\\projects\\hashi",
                        "host_identity": "a9max",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    registry = PeerRegistry(hashi_root, "HASHI2")
    registry._observations["HASHI1"] = {
        "lan": PeerInfo(
            instance_id="HASHI1",
            display_name="HASHI1",
            host="192.168.0.211",
            port=8766,
            workbench_port=18800,
            platform="wsl",
            properties={"discovery": "lan", "host_identity": "a9max", "environment_kind": "wsl"},
        )
    }

    registry._rebuild_canonical_peers()

    peer = registry.get_peer("HASHI1")
    assert peer is not None
    assert peer.host == "192.168.0.211"
    assert peer.properties["same_host_loopback"] == "127.0.0.1"


def test_registry_prefers_bootstrap_over_loopback_fallback_when_bootstrap_exists(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text(
        json.dumps(
            {
                "instances": {
                    "hashi9": {"instance_id": "HASHI9", "platform": "windows"},
                    "hashi1": {"instance_id": "HASHI1", "platform": "wsl"},
                }
            }
        ),
        encoding="utf-8",
    )
    registry = PeerRegistry(hashi_root, "HASHI9")
    registry._observations["HASHI1"] = {
        "bootstrap": PeerInfo(
            instance_id="HASHI1",
            display_name="HASHI1",
            host="127.0.0.1",
            port=8766,
            workbench_port=18800,
            platform="wsl",
            properties={"discovery": "bootstrap"},
        ),
        "bootstrap_fallback": PeerInfo(
            instance_id="HASHI1",
            display_name="HASHI1",
            host="127.0.0.1",
            port=8767,
            workbench_port=18800,
            platform="wsl",
            properties={"discovery": "bootstrap_fallback"},
        ),
    }

    registry._rebuild_canonical_peers()

    peer = registry.get_peer("HASHI1")
    assert peer is not None
    assert peer.port == 8766
    assert peer.properties["preferred_backend"] == "bootstrap"


def test_registry_prunes_legacy_alias_when_new_identity_shares_same_endpoint(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text('{"instances": {}}', encoding="utf-8")
    registry = PeerRegistry(hashi_root, "HASHI9")
    registry._observations["MSI"] = {
        "lan": PeerInfo(
            instance_id="MSI",
            display_name="MSI",
            host="192.168.0.41",
            port=8767,
            workbench_port=8779,
            platform="windows",
            protocol_version="2.0",
            capabilities=["handshake_v2"],
            properties={
                "discovery": "lan",
                "host_identity": "desktopvn0amd7",
                "address_candidates": [{"host": "192.168.0.41", "scope": "lan"}],
                "observed_candidates": [{"host": "192.168.0.41", "scope": "lan"}],
            },
        )
    }
    registry._observations["HASHI-DESKTOP"] = {
        "bootstrap": PeerInfo(
            instance_id="HASHI-DESKTOP",
            display_name="HASHI Desktop",
            host="192.168.0.41",
            port=8766,
            workbench_port=8779,
            platform="windows",
            protocol_version="1.0",
            capabilities=[],
            properties={"discovery": "bootstrap"},
        )
    }

    registry._rebuild_canonical_peers()

    assert registry.get_peer("MSI") is not None
    assert registry.get_peer("HASHI-DESKTOP") is None


def test_registry_load_state_prunes_legacy_alias_when_new_identity_exists(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text('{"instances": {}}', encoding="utf-8")
    state_dir = tmp_path / "state-home"
    state_dir.mkdir()
    original_home = Path.home
    Path.home = staticmethod(lambda: state_dir)
    try:
        state_path = state_dir / ".hashi-remote" / "peers_state_hashi9.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(
                {
                    "peers": {
                        "MSI": {
                            "canonical": {
                                "instance_id": "MSI",
                                "display_name": "MSI",
                                "display_handle": "@msi",
                                "host": "192.168.0.41",
                                "port": 8767,
                                "workbench_port": 8779,
                                "platform": "windows",
                                "version": "unknown",
                                "hashi_version": "unknown",
                                "protocol_version": "2.0",
                                "capabilities": ["handshake_v2"],
                                "properties": {
                                    "discovery": "lan",
                                    "preferred_backend": "lan",
                                    "host_identity": "desktopvn0amd7",
                                },
                            },
                            "observations": {
                                "lan": {
                                    "instance_id": "MSI",
                                    "display_name": "MSI",
                                    "display_handle": "@msi",
                                    "host": "192.168.0.41",
                                    "port": 8767,
                                    "workbench_port": 8779,
                                    "platform": "windows",
                                    "version": "unknown",
                                    "hashi_version": "unknown",
                                    "protocol_version": "2.0",
                                    "capabilities": ["handshake_v2"],
                                    "properties": {
                                        "discovery": "lan",
                                        "host_identity": "desktopvn0amd7",
                                    },
                                }
                            },
                        },
                        "HASHI-DESKTOP": {
                            "canonical": {
                                "instance_id": "HASHI-DESKTOP",
                                "display_name": "HASHI Desktop",
                                "display_handle": "@hashi-desktop",
                                "host": "192.168.0.41",
                                "port": 8766,
                                "workbench_port": 8779,
                                "platform": "windows",
                                "version": "unknown",
                                "hashi_version": "unknown",
                                "protocol_version": "1.0",
                                "capabilities": [],
                                "properties": {
                                    "discovery": "bootstrap",
                                    "preferred_backend": "bootstrap",
                                },
                            },
                            "observations": {
                                "bootstrap": {
                                    "instance_id": "HASHI-DESKTOP",
                                    "display_name": "HASHI Desktop",
                                    "display_handle": "@hashi-desktop",
                                    "host": "192.168.0.41",
                                    "port": 8766,
                                    "workbench_port": 8779,
                                    "platform": "windows",
                                    "version": "unknown",
                                    "hashi_version": "unknown",
                                    "protocol_version": "1.0",
                                    "capabilities": [],
                                    "properties": {
                                        "discovery": "bootstrap",
                                    },
                                }
                            },
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        registry = PeerRegistry(hashi_root, "HASHI9")

        assert registry.get_peer("MSI") is not None
        assert registry.get_peer("HASHI-DESKTOP") is None
    finally:
        Path.home = original_home


def test_registry_load_state_rebuilds_stale_canonical_from_fresher_observation(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text('{"instances": {}}', encoding="utf-8")
    state_dir = tmp_path / "state-home"
    state_dir.mkdir()
    original_home = Path.home
    Path.home = staticmethod(lambda: state_dir)
    try:
        state_path = state_dir / ".hashi-remote" / "peers_state_hashi9.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        state_path.write_text(
            json.dumps(
                {
                    "peers": {
                        "HASHI2": {
                            "canonical": {
                                "instance_id": "HASHI2",
                                "display_name": "HASHI2 (WSL)",
                                "display_handle": "@hashi2",
                                "host": "127.0.0.1",
                                "port": 8767,
                                "workbench_port": 18802,
                                "platform": "wsl",
                                "version": "1.0.0",
                                "hashi_version": "unknown",
                                "protocol_version": "2.0",
                                "capabilities": ["handshake_v2"],
                                "properties": {
                                    "discovery": "bootstrap_fallback",
                                    "preferred_backend": "bootstrap_fallback",
                                    "handshake_state": "handshake_accepted",
                                    "last_handshake_at": now,
                                    "last_seen_ok": now - 9 * 3600,
                                    "live_status": "offline",
                                },
                            },
                            "observations": {
                                "bootstrap_fallback": {
                                    "instance_id": "HASHI2",
                                    "display_name": "HASHI2 (WSL)",
                                    "display_handle": "@hashi2",
                                    "host": "127.0.0.1",
                                    "port": 8767,
                                    "workbench_port": 18802,
                                    "platform": "wsl",
                                    "version": "1.0.0",
                                    "hashi_version": "unknown",
                                    "protocol_version": "2.0",
                                    "capabilities": ["handshake_v2"],
                                    "properties": {
                                        "discovery": "bootstrap_fallback",
                                        "host_identity": "a9max",
                                        "environment_kind": "wsl",
                                        "handshake_state": "handshake_accepted",
                                        "last_seen_ok": now,
                                        "consecutive_failures": 0,
                                        "live_status": "online",
                                    },
                                },
                                "handshake_inbound": {
                                    "instance_id": "HASHI2",
                                    "display_name": "@hashi2",
                                    "display_handle": "@hashi2",
                                    "host": "127.0.0.1",
                                    "port": 8767,
                                    "workbench_port": 18802,
                                    "platform": "wsl",
                                    "version": "unknown",
                                    "hashi_version": "unknown",
                                    "protocol_version": "2.0",
                                    "capabilities": ["handshake_v2"],
                                    "properties": {
                                        "discovery": "handshake_inbound",
                                        "host_identity": "a9max",
                                        "environment_kind": "wsl",
                                    },
                                },
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        registry = PeerRegistry(hashi_root, "HASHI9")
        peer = registry.get_peer("HASHI2")

        assert peer is not None
        assert peer.properties["live_status"] == "online"
        assert peer.properties["last_seen_ok"] == now
    finally:
        Path.home = original_home


def test_registry_load_state_prunes_expired_peer_state(tmp_path, monkeypatch):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text('{"instances": {}}', encoding="utf-8")
    state_home = tmp_path / "state-home-expired"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: state_home))
    state_path = state_home / ".hashi-remote" / "peers_state_hashi1.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    old_seen = int(time.time()) - (25 * 3600)
    state_path.write_text(
        json.dumps(
            {
                "peers": {
                    "MSI": {
                        "canonical": {
                            "instance_id": "MSI",
                            "display_name": "MSI",
                            "host": "192.168.0.41",
                            "port": 8767,
                            "workbench_port": 8779,
                            "platform": "windows",
                            "properties": {
                                "discovery": "lan",
                                "live_status": "offline",
                                "last_seen_ok": old_seen,
                            },
                        },
                        "observations": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    registry = PeerRegistry(hashi_root, "HASHI1")

    assert registry.get_peer("MSI") is None
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["peers"] == {}


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


def test_same_machine_hint_recognizes_same_host_wsl_siblings():
    manager = object.__new__(ProtocolManager)
    manager._instance_info = {"instance_id": "HASHI2", "platform": "wsl"}
    manager._local_network_profile = lambda: {
        "host_identity": "a9max",
        "environment_kind": "wsl",
        "address_candidates": [{"host": "192.168.0.211", "scope": "lan"}],
    }
    manager._load_instances = lambda: {
        "hashi2": {
            "instance_id": "HASHI2",
            "platform": "wsl",
            "wsl_root_from_windows": r"\\\\wsl$\\Ubuntu-22.04\\home\\lily\\projects\\hashi2",
        }
    }

    entry = {
        "instance_id": "HASHI1",
        "platform": "wsl",
        "host_identity": "a9max",
        "wsl_root_from_windows": r"\\\\wsl$\\Ubuntu-22.04\\home\\lily\\projects\\hashi",
        "lan_ip": "192.168.0.211",
    }

    assert ProtocolManager._same_machine_hint(manager, entry) is True


def test_same_machine_hint_does_not_falsely_match_wsl_siblings_on_different_hosts():
    manager = object.__new__(ProtocolManager)
    manager._instance_info = {"instance_id": "HASHI2", "platform": "wsl"}
    manager._local_network_profile = lambda: {
        "host_identity": "a9max",
        "environment_kind": "wsl",
        "address_candidates": [{"host": "192.168.0.211", "scope": "lan"}],
    }
    manager._load_instances = lambda: {
        "hashi2": {
            "instance_id": "HASHI2",
            "platform": "wsl",
            "host_identity": "a9max",
            "wsl_root_from_windows": r"\\\\wsl$\\Ubuntu-22.04\\home\\lily\\projects\\hashi2",
        }
    }

    entry = {
        "instance_id": "HASHI-REMOTE",
        "platform": "wsl",
        "host_identity": "otherhost",
        "wsl_root_from_windows": r"\\\\wsl$\\Ubuntu-22.04\\home\\remote\\projects\\hashi",
        "lan_ip": "192.168.50.12",
    }

    assert ProtocolManager._same_machine_hint(manager, entry) is False


def test_resolve_peer_route_uses_loopback_candidate_for_live_same_host_peer():
    manager = object.__new__(ProtocolManager)
    peer = PeerInfo(
        instance_id="HASHI1",
        display_name="HASHI1",
        host="192.168.0.211",
        port=8766,
        workbench_port=18800,
        platform="wsl",
        properties={"address_candidates": [{"host": "127.0.0.1", "scope": "same_host"}]},
    )
    manager._peer_registry = SimpleNamespace(get_peer=lambda _iid: peer)
    manager._load_instances = lambda: {
        "hashi1": {"instance_id": "HASHI1", "same_host_loopback": "127.0.0.1", "remote_port": 8766}
    }
    manager._candidate_hosts_for_peer = lambda _peer: ["127.0.0.1", "192.168.0.211"]
    manager._probe_route = lambda host, port: host == "127.0.0.1" and port == 8766

    route = ProtocolManager._resolve_peer_route(manager, "HASHI1")

    assert route == {"host": "127.0.0.1", "port": 8766, "instance_id": "HASHI1"}


def test_protocol_status_includes_route_diagnostics(tmp_path):
    (tmp_path / "instances.json").write_text(
        json.dumps(
            {
                "instances": {
                    "hashi1": {
                        "instance_id": "HASHI1",
                        "platform": "wsl",
                        "host_identity": "a9max",
                        "remote_port": 8766,
                    },
                    "hashi2": {
                        "instance_id": "HASHI2",
                        "platform": "wsl",
                        "host_identity": "a9max",
                        "remote_port": 8766,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    manager = object.__new__(ProtocolManager)
    manager._hashi_root = tmp_path
    manager._instance_info = {"instance_id": "HASHI1", "platform": "wsl"}

    diagnostics = ProtocolManager.get_route_diagnostics(manager)

    assert diagnostics["local_instance"] == "HASHI1"
    assert diagnostics["port_conflicts"][0]["instances"] == ["HASHI1", "HASHI2"]


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


def test_remote_config_snapshot_prefers_instances_remote_port_over_agents_and_yaml(tmp_path):
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "config.yaml").write_text(
        "server:\n  port: 8767\n  use_tls: false\ndiscovery:\n  backend: lan\n",
        encoding="utf-8",
    )
    (tmp_path / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "HASHI1", "remote_port": 9999}}),
        encoding="utf-8",
    )
    (tmp_path / "instances.json").write_text(
        json.dumps({"instances": {"hashi1": {"instance_id": "HASHI1", "remote_port": 8766}}}),
        encoding="utf-8",
    )
    dummy = _RemoteConfigDummy()
    dummy.global_config = SimpleNamespace(project_root=tmp_path)

    cfg = FlexibleAgentRuntime._remote_config_snapshot(dummy)

    assert cfg["port"] == 8766


def test_remote_urls_use_local_http_hosts_for_wsl_alias(monkeypatch, tmp_path):
    dummy = _RemoteConfigDummy()
    dummy.global_config = SimpleNamespace(project_root=tmp_path)
    monkeypatch.setattr(
        "orchestrator.flexible_agent_runtime.local_http_hosts",
        lambda: ("10.255.255.254", "127.0.0.1"),
    )
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "config.yaml").write_text(
        "server:\n  port: 8766\n  use_tls: false\n",
        encoding="utf-8",
    )

    urls = FlexibleAgentRuntime._remote_urls(dummy, "peers")

    assert urls[:2] == [
        "http://10.255.255.254:8766/peers",
        "https://10.255.255.254:8766/peers",
    ]
    assert "http://127.0.0.1:8766/peers" in urls


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


def test_handshake_ignores_successful_response_from_wrong_instance():
    manager = object.__new__(ProtocolManager)
    peer = PeerInfo(
        instance_id="INTEL",
        display_name="INTEL",
        host="10.0.0.2",
        port=8767,
        workbench_port=18802,
        platform="windows",
        properties={"handshake_state": "handshake_pending"},
    )
    recorded: list[tuple[str, dict]] = []

    class _Registry:
        def get_peers(self):
            return [peer]

        def mark_handshake_result(self, instance_id, **kwargs):
            recorded.append((instance_id, kwargs))

    manager._peer_registry = _Registry()
    manager._instance_info = {"instance_id": "MSI", "remote_port": 8766, "workbench_port": 8779, "platform": "windows"}
    manager._handshake_timeout_seconds = 1
    manager._candidate_hosts_for_peer = lambda _peer: ["10.0.0.2"]
    manager._candidate_urls = lambda host, port, path: [f"http://{host}:{port}{path}"]
    manager._local_network_profile = lambda: {
        "host_identity": "desktopvn0amd7",
        "environment_kind": "windows",
        "address_candidates": [],
        "observed_candidates": [],
    }
    manager.get_local_agents_snapshot = lambda: []
    manager._post_json = lambda _url, _payload, timeout=0: {
        "status": "handshake_accept",
        "instance_id": "HASHI2",
        "protocol_version": "2.0",
        "capabilities": ["handshake_v2"],
        "agents": [{"agent_name": "rika"}],
    }

    asyncio.run(ProtocolManager._handshake_once(manager))

    assert recorded[0] == ("INTEL", {"state": "handshake_in_progress"})
    assert recorded[-1][0] == "INTEL"
    assert recorded[-1][1]["state"] == "handshake_timed_out"


def test_old_peer_without_hmac_is_marked_rejected_auth_required():
    manager = object.__new__(ProtocolManager)
    peer = PeerInfo(
        instance_id="HASHI2",
        display_name="HASHI2",
        host="10.0.0.2",
        port=8767,
        workbench_port=18802,
        platform="wsl",
        properties={"handshake_state": "handshake_pending"},
    )
    recorded: list[tuple[str, dict]] = []

    class _Registry:
        def get_peers(self):
            return [peer]

        def mark_handshake_result(self, instance_id, **kwargs):
            recorded.append((instance_id, kwargs))

    manager._peer_registry = _Registry()
    manager._instance_info = {"instance_id": "HASHI1", "remote_port": 8766, "workbench_port": 18800, "platform": "wsl"}
    manager._handshake_timeout_seconds = 1
    manager._candidate_hosts_for_peer = lambda _peer: ["10.0.0.2"]
    manager._candidate_urls = lambda host, port, path: [f"http://{host}:{port}{path}"]
    manager._local_network_profile = lambda: {"host_identity": "a9max", "environment_kind": "wsl", "address_candidates": [], "observed_candidates": []}
    manager.get_local_agents_snapshot = lambda: []
    manager.get_local_agent_directory_state = lambda: {"version": "", "directory_state": "fresh"}
    manager._post_json = lambda _url, _payload, timeout=0: {
        "status": "handshake_reject",
        "reason": "auth_required",
    }

    asyncio.run(ProtocolManager._handshake_once(manager))

    assert recorded[0] == ("HASHI2", {"state": "handshake_in_progress"})
    assert recorded[-1] == ("HASHI2", {"state": "handshake_rejected", "last_error": "auth_required"})


def test_control_loop_retries_bootstrap_after_startup_window():
    manager = object.__new__(ProtocolManager)
    manager._running = True
    manager._bootstrap_retry_seconds = 0
    manager._last_bootstrap_run = 0.0
    now = time.time()
    manager._last_refresh_run = now
    manager._last_handshake_run = now
    manager._poll_interval_seconds = 0

    calls: list[float] = []

    async def fake_bootstrap(*, initial_delay=0.0):
        calls.append(initial_delay)
        manager._last_bootstrap_run = time.time()

    async def fake_refresh():
        manager._last_refresh_run = time.time()

    async def fake_handshake():
        manager._last_handshake_run = time.time()

    async def fake_process():
        manager._running = False

    manager._bootstrap_known_peers = fake_bootstrap
    manager._refresh_peer_liveness_once = fake_refresh
    manager._handshake_once = fake_handshake
    manager._process_inflight_once = fake_process

    asyncio.run(ProtocolManager._control_loop(manager))

    assert calls == [0.0]


def test_candidate_urls_follow_protocol_tls_setting(tmp_path):
    manager = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI1", "remote_port": 30264, "workbench_port": 18800},
        peer_registry=None,
        workbench_port=18800,
        use_tls=True,
    )

    urls = manager._candidate_urls("10.0.0.1", 30264, "/health")

    assert urls[0] == "https://10.0.0.1:30264/health"


def test_candidate_urls_follow_protocol_plain_http_setting(tmp_path):
    manager = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI1", "remote_port": 30264, "workbench_port": 18800},
        peer_registry=None,
        workbench_port=18800,
        use_tls=False,
    )

    urls = manager._candidate_urls("10.0.0.1", 30264, "/health")

    assert urls[0] == "http://10.0.0.1:30264/health"


def test_bootstrap_probe_ports_prefers_live_endpoint(tmp_path):
    manager = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI1", "remote_port": 8766, "workbench_port": 18800},
        peer_registry=None,
        workbench_port=18800,
    )

    ports = manager._bootstrap_probe_ports({"remote_port": 8767}, {"port": 30264})

    assert ports == [30264, 8767]


def test_bootstrap_known_peers_logs_when_no_probe_ports(caplog, tmp_path):
    manager = object.__new__(ProtocolManager)
    manager._hashi_root = tmp_path
    manager._instance_info = {"instance_id": "HASHI1"}
    manager._peer_registry = None
    manager._load_instances = lambda: {"hashi2": {"instance_id": "HASHI2"}}
    manager._dedupe_bootstrap_instances = lambda instances: instances
    manager._bootstrap_probe_ports = lambda entry, live_entry=None: []

    caplog.set_level("DEBUG", logger="remote.protocol_manager")

    asyncio.run(manager._bootstrap_known_peers())

    assert "Bootstrap: HASHI2 has no live or fallback probe ports, skipping" in caplog.text


def test_live_endpoints_file_is_private(tmp_path):
    write_live_endpoints(
        tmp_path,
        [
            PeerInfo(
                instance_id="HASHI2",
                display_name="HASHI2",
                host="192.168.0.211",
                port=30264,
                workbench_port=18802,
                platform="wsl",
            )
        ],
    )

    mode = (tmp_path / "state" / "remote_live_endpoints.json").stat().st_mode

    assert stat.S_IMODE(mode) == 0o600


def test_remove_live_endpoint_removes_only_matching_instance(tmp_path):
    write_live_endpoints(
        tmp_path,
        [
            PeerInfo(
                instance_id="HASHI2",
                display_name="HASHI2",
                host="192.168.0.211",
                port=30264,
                workbench_port=18802,
                platform="wsl",
            ),
            PeerInfo(
                instance_id="HASHI3",
                display_name="HASHI3",
                host="192.168.0.212",
                port=30265,
                workbench_port=18803,
                platform="linux",
            ),
        ],
    )

    assert remove_live_endpoint(tmp_path, "hashi2") is True

    endpoints = read_live_endpoints(tmp_path)
    assert "hashi2" not in endpoints
    assert endpoints["hashi3"]["port"] == 30265
    mode = (tmp_path / "state" / "remote_live_endpoints.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_tailscale_discovery_uses_live_endpoint_port(monkeypatch, tmp_path):
    status_path = tmp_path / "tailscale.json"
    status_path.write_text(
        json.dumps(
            {
                "Peer": {
                    "node1": {
                        "Online": True,
                        "HostName": "hashi2",
                        "TailscaleIPs": ["100.64.0.2"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HASHI_TAILSCALE_STATUS_JSON", str(status_path))
    (tmp_path / "instances.json").write_text(
        json.dumps({"instances": {"hashi2": {"instance_id": "HASHI2", "remote_port": 8767}}}),
        encoding="utf-8",
    )
    write_live_endpoints(
        tmp_path,
        [
            PeerInfo(
                instance_id="HASHI2",
                display_name="HASHI2",
                host="192.168.0.211",
                port=30264,
                workbench_port=18802,
                platform="wsl",
            )
        ],
    )
    discovery = TailscaleDiscovery("HASHI1", tmp_path)

    peers = discovery._load_peers()

    assert peers[0].port == 30264
    assert peers[0].properties["live_endpoint_source"] == "cache"


def test_local_agents_snapshot_marks_fresh_directory_state(tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"agents": [{"name": "zelda", "display_name": "Zelda", "is_active": True}]}),
        encoding="utf-8",
    )
    manager = object.__new__(ProtocolManager)
    manager._hashi_root = tmp_path
    manager._instance_info = {"instance_id": "HASHI1"}
    manager._core_online = lambda: True

    snapshot = ProtocolManager.get_local_agents_snapshot(manager)

    assert snapshot[0]["agent_name"] == "zelda"
    assert snapshot[0]["directory_state"] == "fresh"
    assert snapshot[0]["agent_snapshot_version"]


def test_local_agents_snapshot_marks_stale_when_core_is_offline(tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"agents": [{"name": "zelda", "display_name": "Zelda", "is_active": True}]}),
        encoding="utf-8",
    )
    manager = object.__new__(ProtocolManager)
    manager._hashi_root = tmp_path
    manager._instance_info = {"instance_id": "HASHI1"}
    manager._core_online = lambda: False

    snapshot = ProtocolManager.get_local_agents_snapshot(manager)
    state = ProtocolManager.get_local_agent_directory_state(manager)

    assert snapshot[0]["directory_state"] == "stale"
    assert state["directory_state"] == "stale"


def test_agent_snapshot_change_forces_handshake(tmp_path):
    agents_path = tmp_path / "agents.json"
    agents_path.write_text(json.dumps({"agents": [{"name": "zelda", "is_active": True}]}), encoding="utf-8")
    manager = object.__new__(ProtocolManager)
    manager._hashi_root = tmp_path
    manager._instance_info = {"instance_id": "HASHI1"}
    manager._core_online = lambda: True

    assert ProtocolManager._refresh_local_agent_snapshot_if_changed(manager) is False
    agents_path.write_text(json.dumps({"agents": [{"name": "akane", "is_active": True}]}), encoding="utf-8")

    assert ProtocolManager._refresh_local_agent_snapshot_if_changed(manager) is True
    assert manager._force_handshake is True


def test_registry_prunes_legacy_alias_with_same_host_and_workbench(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    (hashi_root / "instances.json").write_text(
        json.dumps(
            {
                "instances": {
                    "hashi1": {"instance_id": "HASHI1", "platform": "wsl"},
                    "msi": {
                        "instance_id": "MSI",
                        "display_name": "MSI",
                        "platform": "windows",
                        "api_host": "192.168.0.41",
                        "lan_ip": "192.168.0.41",
                        "remote_port": 8767,
                        "workbench_port": 8779,
                        "protocol_version": "2.0",
                        "capabilities": ["handshake_v2"],
                        "host_identity": "desktopvn0amd7",
                        "environment_kind": "windows",
                        "handshake_state": "handshake_accepted",
                        "live_status": "online",
                    },
                    "hashi-desktop": {
                        "instance_id": "HASHI-DESKTOP",
                        "display_name": "HASHI Desktop",
                        "platform": "windows",
                        "api_host": "192.168.0.41",
                        "lan_ip": "192.168.0.41",
                        "remote_port": 8766,
                        "workbench_port": 8779,
                        "protocol_version": "1.0",
                        "capabilities": [],
                        "handshake_state": "handshake_timed_out",
                        "live_status": "offline",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    registry = PeerRegistry(hashi_root, "HASHI1")
    registry._peers = {
        "MSI": PeerInfo(
            instance_id="MSI",
            display_name="MSI",
            host="192.168.0.41",
            port=8767,
            workbench_port=8779,
            platform="windows",
            protocol_version="2.0",
            capabilities=["handshake_v2"],
            properties={
                "address_candidates": [{"host": "192.168.0.41", "scope": "lan"}],
                "handshake_state": "handshake_accepted",
                "live_status": "online",
                "host_identity": "desktopvn0amd7",
                "environment_kind": "windows",
            },
        ),
        "HASHI-DESKTOP": PeerInfo(
            instance_id="HASHI-DESKTOP",
            display_name="HASHI Desktop",
            host="192.168.0.41",
            port=8766,
            workbench_port=8779,
            platform="windows",
            protocol_version="1.0",
            capabilities=[],
            properties={
                "address_candidates": [{"host": "192.168.0.41", "scope": "peer"}],
                "handshake_state": "handshake_timed_out",
                "live_status": "offline",
            },
        ),
    }
    registry._observations = {
        "MSI": {"lan": registry._peers["MSI"]},
        "HASHI-DESKTOP": {"bootstrap": registry._peers["HASHI-DESKTOP"]},
    }

    assert registry._prune_duplicate_peer_aliases() is True
    assert set(registry._peers) == {"MSI"}

    instances = json.loads((hashi_root / "instances.json").read_text(encoding="utf-8"))["instances"]
    pruned, changed = registry._prune_duplicate_instance_aliases(instances)

    assert changed is True
    assert "msi" in pruned
    assert "hashi-desktop" not in pruned


def test_registry_prunes_stale_legacy_instance_not_in_live_peers(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    old_seen = int(time.time()) - (25 * 3600)
    (hashi_root / "instances.json").write_text(
        json.dumps(
            {
                "instances": {
                    "hashi1": {"instance_id": "HASHI1", "platform": "wsl"},
                    "msi": {
                        "instance_id": "MSI",
                        "display_name": "MSI",
                        "platform": "windows",
                        "lan_ip": "192.168.0.41",
                        "remote_port": 8767,
                        "_discovery": "lan",
                        "last_seen": old_seen,
                        "last_seen_ok": old_seen,
                        "last_seen_error": int(time.time()),
                        "last_handshake_at": int(time.time()),
                        "live_status": "offline",
                        "active": False,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    registry = PeerRegistry(hashi_root, "HASHI1")

    registry._sync_to_instances_json()

    instances = json.loads((hashi_root / "instances.json").read_text(encoding="utf-8"))["instances"]
    assert "hashi1" in instances
    assert "msi" not in instances


def test_registry_keeps_current_peer_even_when_legacy_timestamp_is_old(tmp_path):
    hashi_root = tmp_path / "hashi"
    hashi_root.mkdir()
    old_seen = int(time.time()) - (25 * 3600)
    (hashi_root / "instances.json").write_text(
        json.dumps(
            {
                "instances": {
                    "hashi1": {"instance_id": "HASHI1", "platform": "wsl"},
                    "intel": {
                        "instance_id": "INTEL",
                        "display_name": "INTEL",
                        "platform": "windows",
                        "lan_ip": "192.168.0.6",
                        "remote_port": 8766,
                        "_discovery": "lan",
                        "last_seen": old_seen,
                        "last_seen_ok": old_seen,
                        "live_status": "offline",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    registry = PeerRegistry(hashi_root, "HASHI1")
    registry._peers = {
        "INTEL": PeerInfo(
            instance_id="INTEL",
            display_name="INTEL",
            host="192.168.0.6",
            port=8766,
            workbench_port=18802,
            platform="windows",
            properties={"discovery": "lan", "live_status": "online", "last_seen_ok": int(time.time())},
        )
    }
    registry._observations = {"INTEL": {"lan": registry._peers["INTEL"]}}

    registry._sync_to_instances_json()

    instances = json.loads((hashi_root / "instances.json").read_text(encoding="utf-8"))["instances"]
    assert "intel" in instances
    assert instances["intel"]["live_status"] == "online"


def test_parse_hchat_message_exposes_reply_body_for_loop_guard():
    parsed = parse_hchat_message("[hchat from rain@HASHI2] [hchat reply from lily] hello")

    assert parsed is not None
    assert parsed["agent"] == "rain"
    assert parsed["instance_id"] == "HASHI2"
    assert parsed["body"] == "[hchat reply from lily] hello"


def test_flex_hchat_cross_instance_reply_is_tagged(monkeypatch):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "sakura"
    runtime.logger = Mock()
    runtime.orchestrator = None

    sent = {}

    def fake_send_hchat(to_agent, from_agent, text, target_instance=None, **kwargs):
        sent["to_agent"] = to_agent
        sent["from_agent"] = from_agent
        sent["text"] = text
        sent["target_instance"] = target_instance
        sent["kwargs"] = kwargs
        return True

    monkeypatch.setattr("tools.hchat_send.send_hchat", fake_send_hchat)
    monkeypatch.setattr("tools.hchat_send._load_config", lambda: {})
    monkeypatch.setattr("tools.hchat_send._get_instance_id", lambda _cfg: "HASHI1")

    item = SimpleNamespace(prompt="[hchat from rika@HASHI2] hello")

    asyncio.run(runtime._hchat_route_reply(item, "Roger that"))

    assert sent["to_agent"] == "rika"
    assert sent["from_agent"] == "sakura"
    assert sent["target_instance"] == "HASHI2"
    assert sent["text"] == "[hchat reply from sakura] Roger that"


def test_flex_hchat_reply_body_is_not_replied_again(monkeypatch):
    runtime = FlexibleAgentRuntime.__new__(FlexibleAgentRuntime)
    runtime.name = "sakura"
    runtime.logger = Mock()
    runtime.orchestrator = None

    send_mock = Mock(return_value=True)
    monkeypatch.setattr("tools.hchat_send.send_hchat", send_mock)

    item = SimpleNamespace(prompt="[hchat from rika@HASHI2] [hchat reply from rika] done")

    asyncio.run(runtime._hchat_route_reply(item, "ack"))

    send_mock.assert_not_called()
