#!/usr/bin/env python3

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.modules.setdefault(
    "zeroconf",
    SimpleNamespace(
        IPVersion=SimpleNamespace(V4Only=object()),
        InterfaceChoice=SimpleNamespace(All=object()),
        ServiceBrowser=object,
        ServiceInfo=object,
        ServiceListener=object,
        Zeroconf=object,
    ),
)

from remote.peer import lan  # noqa: E402
from remote.peer.base import PeerInfo  # noqa: E402
from remote.peer.registry import PeerRegistry  # noqa: E402
from remote.security.auth import get_shared_token, set_shared_token  # noqa: E402
from remote.security.shared_token import SharedTokenSnapshot  # noqa: E402
from remote.protocol_manager import ProtocolManager  # noqa: E402


def _peer(**changes):
    values = {
        "instance_id": "HASHI3",
        "display_name": "HASHI Three",
        "host": "127.0.0.1",
        "port": 38763,
        "workbench_port": 18803,
        "platform": "windows",
        "hashi_version": "test-version",
        "protocol_version": "2.0",
        "capabilities": [],
    }
    values.update(changes)
    return PeerInfo(**values)


def test_versioned_txt_field_round_trips_utf8_out_of_order_with_wire_budget():
    payload = json.dumps(
        [{"host": f"10.20.30.{index}", "scope": "lan", "source": "界面扫描"}
         for index in range(1, 31)],
        ensure_ascii=False,
        separators=(",", ":"),
    )

    records = lan._encode_versioned_txt_records("address_candidates_json", payload)

    assert len(records) > 2
    assert all(lan._txt_record_size(key, value) <= 255 for key, value in records.items())
    shuffled = dict(reversed(list(records.items())))
    assert lan._decode_versioned_txt_records(shuffled, "address_candidates_json") == payload


@pytest.mark.parametrize("damage", ["missing", "duplicate", "version", "length", "checksum"])
def test_versioned_txt_field_rejects_incomplete_or_malicious_fragments(damage):
    records = lan._encode_versioned_txt_records("address_candidates_json", "界" * 300)
    manifest = json.loads(records["address_candidates_json_meta"])
    if damage == "missing":
        records.pop("address_candidates_json_1")
    elif damage == "duplicate":
        records["address_candidates_json_00"] = records["address_candidates_json_0"]
    elif damage == "version":
        manifest["v"] = 999
        records["address_candidates_json_meta"] = json.dumps(manifest, separators=(",", ":"))
    elif damage == "length":
        manifest["l"] = lan._MDNS_TXT_FIELD_MAX_BYTES + 1
        records["address_candidates_json_meta"] = json.dumps(manifest, separators=(",", ":"))
    else:
        records["address_candidates_json_0"] += "x"

    with pytest.raises(lan.TxtRecordDecodeError):
        lan._decode_versioned_txt_records(records, "address_candidates_json")


def test_advertisement_bounds_each_record_and_total_payload_to_fixed_size_hints(monkeypatch):
    captured = {}

    class CapturingServiceInfo:
        def __init__(self, *, type_, name, port, properties, server, addresses):
            del type_, name, addresses
            for key, value in properties.items():
                assert len(key.encode("utf-8")) + 1 + len(value) <= 255
            assert sum(
                1 + len(key.encode("utf-8")) + 1 + len(value)
                for key, value in properties.items()
            ) <= lan._MDNS_TXT_TOTAL_MAX_BYTES
            self.properties = properties
            self.port = port
            self.server = server
            captured["service"] = self

        def parsed_addresses(self):
            return ["127.0.0.1"]

    candidates = [
        {"host": f"192.168.{index // 250}.{index % 250 + 1}", "scope": "lan", "source": "interface_scan"}
        for index in range(80)
    ]
    monkeypatch.setattr(lan, "ServiceInfo", CapturingServiceInfo)
    monkeypatch.setattr(lan.socket, "gethostname", lambda: "hashi-host")
    monkeypatch.setattr(lan, "_get_local_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(
        lan,
        "build_local_network_profile",
        lambda _peer: {
            "host_identity": "hashi-host",
            "environment_kind": "windows",
            "address_candidates": candidates,
            "observed_candidates": list(reversed(candidates)),
        },
    )
    capabilities = [f"capability_{index:03d}_{'界' * 20}" for index in range(100)]
    peer = _peer(
        display_name="第三测试实例" * 100,
        hashi_version="版本" * 200,
        capabilities=capabilities,
    )

    lan.LanDiscovery("HASHI3")._service_info_for_peer(peer)
    decoded = lan._service_info_to_peer(captured["service"], "HASHI2")

    assert decoded is not None
    assert peer.display_name.startswith(decoded.display_name)
    assert peer.hashi_version.startswith(decoded.hashi_version)
    assert len(decoded.display_name.encode("utf-8")) <= lan._MDNS_TXT_HINT_MAX_BYTES
    assert len(decoded.hashi_version.encode("utf-8")) <= lan._MDNS_TXT_HINT_MAX_BYTES
    assert decoded.capabilities == []
    assert 0 < len(decoded.properties["address_candidates"]) <= lan._MDNS_ROUTE_HINT_LIMIT
    assert decoded.properties["capabilities_digest"] == lan._metadata_digest(capabilities)
    assert decoded.properties["address_candidates_digest"] == lan._metadata_digest(candidates)
    assert len(decoded.properties["identity_digest"]) == 64


def test_advertisement_bounds_each_route_hint_before_txt_encoding(monkeypatch):
    captured = {}

    class CapturingServiceInfo:
        def __init__(self, *, type_, name, port, properties, server, addresses):
            del type_, name, addresses
            self.properties = properties
            self.port = port
            self.server = server
            captured["service"] = self

        def parsed_addresses(self):
            return ["127.0.0.1"]

    oversized_host = "界" * 6000
    monkeypatch.setattr(lan, "ServiceInfo", CapturingServiceInfo)
    monkeypatch.setattr(lan.socket, "gethostname", lambda: "hashi-host")
    monkeypatch.setattr(lan, "_get_local_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(
        lan,
        "build_local_network_profile",
        lambda _peer: {
            "host_identity": "hashi-host",
            "environment_kind": "windows",
            "address_candidates": [
                {"host": oversized_host, "scope": "lan", "source": "scan"}
            ],
            "observed_candidates": [
                {"host": oversized_host, "scope": "lan", "source": "scan"}
            ],
        },
    )

    lan.LanDiscovery("HASHI3")._service_info_for_peer(_peer())
    decoded = lan._service_info_to_peer(captured["service"], "HASHI2")

    assert decoded is not None
    advertised_host = decoded.properties["address_candidates"][0]["host"]
    assert len(advertised_host.encode("utf-8")) <= lan._MDNS_TXT_HINT_MAX_BYTES


def test_discovery_rejects_aggregate_txt_payload_above_budget():
    raw = {
        f"field_{index}".encode(): ("x" * 200).encode()
        for index in range(50)
    }

    with pytest.raises(lan.TxtRecordDecodeError, match="payload"):
        lan._decode_service_properties(raw)


@pytest.mark.asyncio
async def test_lan_discovery_cleans_partial_state_and_reports_retry_after_browser_failure(monkeypatch):
    captured = {}

    class FakeZeroconf:
        def __init__(self, **_kwargs):
            captured["zc"] = self
            self.registered = False
            self.unregistered = False
            self.closed = False

        def register_service(self, _info):
            self.registered = True

        def unregister_service(self, _info):
            self.unregistered = True

        def close(self):
            self.closed = True

    monkeypatch.setattr(lan, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(lan, "IPVersion", SimpleNamespace(V4Only=object()))
    monkeypatch.setattr(lan, "InterfaceChoice", SimpleNamespace(All=object()))
    monkeypatch.setattr(lan, "ServiceBrowser", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("browser failed")))
    monkeypatch.setattr(lan.LanDiscovery, "_service_info_for_peer", lambda _self, _info: object())

    discovery = lan.LanDiscovery("HASHI3")
    ok = await discovery.advertise(_peer())
    status = discovery.get_status()

    assert ok is False
    assert captured["zc"].registered is True
    assert captured["zc"].unregistered is True
    assert captured["zc"].closed is True
    assert status["advertising"] is False
    assert status["browsing"] is False
    assert status["readiness"] == "degraded"
    assert status["retry_count"] == 1
    assert status["next_retry_at"] > status["last_attempt_at"]
    assert "browser failed" in status["last_error"]


@pytest.mark.asyncio
async def test_advertisement_update_failure_is_not_reported_as_refreshed(monkeypatch):
    from remote.main import HashiRemoteApplication

    class FailedDiscovery:
        backend_name = "test"

        def __init__(self):
            self.update_calls = 0

        async def update_advertisement(self, _peer):
            self.update_calls += 1
            return False

        async def advertise(self, _peer):
            return False

        def retry_due(self):
            return True

        def get_status(self):
            return {"backend": "test", "readiness": "degraded", "advertising": False, "browsing": False}

    app = HashiRemoteApplication(hashi_root=Path.cwd(), use_tls=False)
    app._protocol_manager = SimpleNamespace(
        get_local_agent_directory_state=lambda: {"version": "v2", "directory_state": "fresh"},
        reload_shared_token=lambda _token: True,
        record_shared_token_config_state=lambda _state, _error: None,
    )
    discovery = FailedDiscovery()
    app._discoveries = [discovery]
    app._last_advertised_agent_snapshot = "v1:fresh"
    monkeypatch.setattr(app, "_build_self_peer", lambda **_kwargs: _peer())
    monkeypatch.setattr("remote.main.write_live_endpoint", lambda *_args, **_kwargs: None)

    result = await app._maintain_discovery_once(
        instance_info={"display_name": "HASHI3", "platform": "windows", "hashi_version": "x"},
        instance_id="HASHI3",
        workbench_port=18803,
        local_capabilities=[],
    )

    assert result["refreshed"] is False
    assert result["failed_backends"] == ["test"]
    assert result["attempted_backends"] == ["test"]
    assert discovery.update_calls == 1
    assert app._last_advertised_agent_snapshot == "v1:fresh"


@pytest.mark.asyncio
async def test_advertisement_refresh_respects_backend_backoff_and_recovers_independently(monkeypatch):
    from remote.main import HashiRemoteApplication

    class Discovery:
        def __init__(self, name, *, ready, due):
            self.backend_name = name
            self.ready = ready
            self.due = due
            self.update_calls = 0

        async def update_advertisement(self, _peer):
            self.update_calls += 1
            if self.due:
                self.ready = True
                return True
            return False

        def retry_due(self):
            return self.due

        def get_status(self):
            return {
                "backend": self.backend_name,
                "readiness": "ready" if self.ready else "degraded",
                "advertising": self.ready,
                "browsing": self.ready,
            }

    healthy = Discovery("healthy", ready=True, due=True)
    recovering = Discovery("recovering", ready=False, due=False)
    app = HashiRemoteApplication(hashi_root=Path.cwd(), use_tls=False)
    app._protocol_manager = SimpleNamespace(
        get_local_agent_directory_state=lambda: {
            "version": "v2",
            "directory_state": "fresh",
        },
        reload_shared_token=lambda _token: True,
        record_shared_token_config_state=lambda _state, _error: None,
    )
    app._discoveries = [healthy, recovering]
    app._last_advertised_agent_snapshot = "v1:fresh"
    monkeypatch.setattr(app, "_build_self_peer", lambda **_kwargs: _peer())
    monkeypatch.setattr("remote.main.write_live_endpoint", lambda *_args, **_kwargs: None)

    first = await app._maintain_discovery_once(
        instance_info={"display_name": "HASHI3", "platform": "windows", "hashi_version": "x"},
        instance_id="HASHI3",
        workbench_port=18803,
        local_capabilities=[],
    )
    second = await app._maintain_discovery_once(
        instance_info={"display_name": "HASHI3", "platform": "windows", "hashi_version": "x"},
        instance_id="HASHI3",
        workbench_port=18803,
        local_capabilities=[],
    )
    recovering.due = True
    third = await app._maintain_discovery_once(
        instance_info={"display_name": "HASHI3", "platform": "windows", "hashi_version": "x"},
        instance_id="HASHI3",
        workbench_port=18803,
        local_capabilities=[],
    )

    assert first["refreshed"] is False
    assert first["attempted_backends"] == ["healthy"]
    assert second["attempted_backends"] == []
    assert healthy.update_calls == 1
    assert recovering.update_calls == 1
    assert third["refreshed"] is True
    assert third["attempted_backends"] == ["recovering"]
    assert app._last_advertised_agent_snapshot == "v2:fresh"


@pytest.mark.asyncio
async def test_malformed_token_reload_keeps_active_credential_until_valid_revision(monkeypatch):
    from remote.main import HashiRemoteApplication

    snapshots = iter(
        [
            SharedTokenSnapshot(None, "invalid-revision", "invalid", "invalid_json"),
            SharedTokenSnapshot("rotated-secret", "valid-revision", "configured_file"),
        ]
    )
    reloads = []
    config_states = []
    app = HashiRemoteApplication(hashi_root=Path.cwd(), use_tls=False)
    app._protocol_manager = SimpleNamespace(
        get_local_agent_directory_state=lambda: {
            "version": "v1",
            "directory_state": "fresh",
        },
        reload_shared_token=lambda token: reloads.append(token) or True,
        record_shared_token_config_state=lambda state, error: config_states.append((state, error)),
    )
    app._last_shared_token_revision = "initial-revision"
    app._last_advertised_agent_snapshot = "v1:fresh"
    monkeypatch.setattr("remote.main.load_shared_token_snapshot", lambda _root: next(snapshots))
    monkeypatch.setattr(app, "_build_self_peer", lambda **_kwargs: _peer())
    set_shared_token("working-secret")
    try:
        await app._maintain_discovery_once(
            instance_info={"display_name": "HASHI3", "platform": "windows", "hashi_version": "x"},
            instance_id="HASHI3",
            workbench_port=18803,
            local_capabilities=[],
        )
        assert get_shared_token() == "working-secret"
        assert reloads == []

        await app._maintain_discovery_once(
            instance_info={"display_name": "HASHI3", "platform": "windows", "hashi_version": "x"},
            instance_id="HASHI3",
            workbench_port=18803,
            local_capabilities=[],
        )
        assert get_shared_token() == "rotated-secret"
        assert reloads == ["rotated-secret"]
        assert config_states == [
            ("invalid", "invalid_json"),
            ("configured_file", ""),
        ]
    finally:
        set_shared_token(None)


def test_shared_token_reload_forces_rehandshake_without_exposing_credential(tmp_path):
    peer = _peer(
        instance_id="HASHI2",
        properties={"discovery": "lan", "handshake_state": "handshake_accepted"},
    )

    class Registry:
        def __init__(self):
            self.states = []

        def get_peers(self):
            return [peer]

        def get_peer_state(self, _instance_id):
            return {"instance_id": "HASHI2"}

        def mark_handshake_result(self, instance_id, **kwargs):
            self.states.append((instance_id, kwargs))

    registry = Registry()
    manager = ProtocolManager(
        hashi_root=tmp_path,
        instance_info={"instance_id": "HASHI3", "workbench_port": 18803},
        peer_registry=registry,
        workbench_port=18803,
        use_tls=False,
    )

    assert manager.reload_shared_token("rotated-secret") is True
    status = manager.get_protocol_status()

    assert manager._force_handshake is True
    assert registry.states == [("HASHI2", {"state": "rehydrate_required"})]
    assert status["credential"]["configured"] is True
    serialized = json.dumps(status)
    assert "rotated-secret" not in serialized
    assert "digest" not in status["credential"]


def test_shared_token_reload_invalidates_cached_trusted_peer_metadata(tmp_path):
    root = tmp_path / "token-reload"
    root.mkdir()
    registry = PeerRegistry(root, "HASHI3")
    registry._state_path = tmp_path / "token-peer-state.json"
    registry.on_peers_changed(
        [
            _peer(
                instance_id="HASHI2",
                capabilities=["agent_directory_v1"],
                properties={
                    "discovery": "lan",
                    "handshake_state": "handshake_accepted",
                    "remote_agents": [{"agent_id": "private-agent"}],
                    "remote_agent_directory": {
                        "version": "private-directory",
                        "directory_state": "fresh",
                    },
                    "remote_supervisor": {"mode": "supervised"},
                },
            )
        ]
    )
    manager = ProtocolManager(
        hashi_root=root,
        instance_info={"instance_id": "HASHI3", "workbench_port": 18803},
        peer_registry=registry,
        workbench_port=18803,
        use_tls=False,
    )

    assert manager.reload_shared_token("new-token") is True
    peer = registry.get_peer("HASHI2")

    assert peer is not None
    assert peer.properties["handshake_state"] == "rehydrate_required"
    assert peer.capabilities == []
    assert "remote_agents" not in peer.properties
    assert "remote_agent_directory" not in peer.properties
    assert "remote_supervisor" not in peer.properties


def test_handshake_rejection_invalidates_cached_trusted_peer_metadata(tmp_path):
    root = tmp_path / "token-rejected"
    root.mkdir()
    registry = PeerRegistry(root, "HASHI3")
    registry._state_path = tmp_path / "rejected-peer-state.json"
    registry.on_peers_changed(
        [
            _peer(
                instance_id="HASHI2",
                capabilities=["agent_directory_v1"],
                properties={
                    "discovery": "lan",
                    "handshake_state": "handshake_accepted",
                    "remote_agents": [{"agent_id": "private-agent"}],
                    "remote_agent_directory": {"version": "private-directory"},
                    "remote_supervisor": {"mode": "supervised"},
                },
            )
        ]
    )

    registry.mark_handshake_result(
        "HASHI2",
        state="handshake_rejected",
        last_error="auth_required",
    )
    peer = registry.get_peer("HASHI2")

    assert peer is not None
    assert peer.properties["handshake_state"] == "handshake_rejected"
    assert peer.capabilities == []
    assert "remote_agents" not in peer.properties
    assert "remote_agent_directory" not in peer.properties
    assert "remote_supervisor" not in peer.properties

    manager = ProtocolManager(
        hashi_root=root,
        instance_info={"instance_id": "HASHI3", "workbench_port": 18803},
        peer_registry=registry,
        workbench_port=18803,
        use_tls=False,
    )
    manager._shared_token = "configured-token"
    manager._force_handshake = False
    assert manager.get_protocol_status()["credential"]["rehandshake_required"] is True


def test_authenticated_handshake_hydrates_bounded_mdns_metadata_until_digest_changes(tmp_path):
    root = tmp_path / "hashi"
    root.mkdir()
    registry = PeerRegistry(root, "HASHI99")
    registry._state_path = tmp_path / "peer-state.json"
    advertised = _peer(
        instance_id="HASHI2",
        display_name="bounded name",
        hashi_version="bounded version",
        capabilities=[],
        properties={
            "discovery": "lan",
            "metadata_schema": "2",
            "identity_digest": "identity-v1",
            "capabilities_digest": "capabilities-v1",
            "address_candidates_digest": "addresses-v1",
            "agent_snapshot_version": "directory-v1",
            "address_candidates": [{"host": "10.0.0.2", "scope": "lan"}],
        },
    )
    registry.on_peers_changed([advertised])
    full_addresses = [
        {"host": f"10.20.0.{index}", "scope": "lan", "source": "handshake"}
        for index in range(1, 9)
    ]
    registry.mark_handshake_result(
        "HASHI2",
        state="handshake_accepted",
        display_name="Full authenticated HASHI Two",
        display_handle="@hashi-two",
        hashi_version="full-build-identifier",
        capabilities=["handshake_v2", "agent_directory_v1"],
        address_candidates=full_addresses,
        remote_agent_directory={"version": "directory-v1", "directory_state": "fresh"},
    )

    registry.on_peers_changed([advertised])
    hydrated = registry.get_peer("HASHI2")

    assert hydrated is not None
    assert hydrated.display_name == "Full authenticated HASHI Two"
    assert hydrated.hashi_version == "full-build-identifier"
    assert hydrated.capabilities == ["handshake_v2", "agent_directory_v1"]
    assert hydrated.properties["address_candidates"] == full_addresses
    assert hydrated.properties["handshake_state"] == "handshake_accepted"

    changed = _peer(
        instance_id="HASHI2",
        display_name="bounded name",
        hashi_version="bounded version",
        capabilities=[],
        properties={
            **advertised.properties,
            "capabilities_digest": "capabilities-v2",
        },
    )
    registry.on_peers_changed([changed])
    pending = registry.get_peer("HASHI2")

    assert pending is not None
    assert pending.capabilities == []
    assert pending.properties["handshake_state"] == "rehydrate_required"
    assert "remote_agent_directory" not in pending.properties

    registry.mark_handshake_result(
        "HASHI2",
        state="handshake_accepted",
        capabilities=["handshake_v2"],
        remote_agent_directory={"version": "directory-v2"},
    )
    digest_removed = _peer(
        instance_id="HASHI2",
        capabilities=[],
        properties={
            **{
                key: value
                for key, value in changed.properties.items()
                if key != "identity_digest"
            },
            "agent_snapshot_version": "directory-v2",
        },
    )
    registry.on_peers_changed([digest_removed])
    downgraded = registry.get_peer("HASHI2")

    assert downgraded is not None
    assert downgraded.capabilities == []
    assert downgraded.properties["handshake_state"] == "rehydrate_required"
    assert "remote_agent_directory" not in downgraded.properties


def test_empty_lan_snapshot_prunes_only_lan_observations(tmp_path, monkeypatch):
    root = tmp_path / "empty-lan-snapshot"
    root.mkdir()
    monkeypatch.setattr(PeerRegistry, "_load_state", lambda self: None)
    registry = PeerRegistry(root, "HASHI3")
    registry._state_path = tmp_path / "empty-lan-peer-state.json"
    registry.on_peers_changed(
        [
            _peer(
                instance_id="HASHI2",
                host="192.168.50.2",
                properties={"discovery": "lan"},
            )
        ],
        backend="lan",
    )
    registry.on_peers_changed(
        [
            _peer(
                instance_id="HASHI2",
                host="127.0.0.1",
                properties={"discovery": "bootstrap_fallback"},
            )
        ]
    )

    registry.on_peers_changed([], backend="lan")
    fallback = registry.get_peer("HASHI2")

    assert fallback is not None
    assert fallback.host == "127.0.0.1"
    assert fallback.properties["preferred_backend"] == "bootstrap_fallback"
    registry = PeerRegistry(root, "HASHI3")
    registry._state_path = tmp_path / "lan-only-peer-state.json"
    registry.on_peers_changed(
        [_peer(instance_id="HASHI9", properties={"discovery": "lan"})],
        backend="lan",
    )
    registry.on_peers_changed([], backend="lan")
    assert registry.get_peer("HASHI9") is None
