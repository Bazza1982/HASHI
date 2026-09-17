from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import restart_provider


def _write_peer_state(path: Path, *, target: str = "HASHI2", handshake: str = "handshake_accepted", capabilities=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "peers": {
                    target: {
                        "canonical": {
                            "instance_id": target,
                            "display_name": target,
                            "host": "127.0.0.1",
                            "port": 8766,
                            "workbench_port": 8765,
                            "platform": "windows",
                            "protocol_version": "2.0",
                            "capabilities": list(capabilities if capabilities is not None else ["rescue_control", "rescue_restart"]),
                            "properties": {
                                "handshake_state": handshake,
                                "live_status": "online",
                            },
                        },
                        "observations": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _supported_probe(instance: str, **_kwargs):
    return {
        "ok": True,
        "instance": instance,
        "base_url": "http://127.0.0.1:8766",
        "capabilities": {"rescue_control": True, "rescue_restart": True},
        "remote_supervisor": {"mode": "supervised"},
    }


def _accepted_target_peer(*_args, **_kwargs):
    return {
        "instance_id": "HASHI1",
        "properties": {"handshake_state": "handshake_accepted"},
    }


def _patch_peer_success(monkeypatch, state: Path) -> None:
    monkeypatch.setattr(restart_provider, "local_instance_id", lambda: "HASHI1")
    monkeypatch.setattr(restart_provider, "_peer_state_path", lambda _source: state)
    monkeypatch.setattr(restart_provider.remote_rescue, "probe_capabilities", _supported_probe)
    monkeypatch.setattr(restart_provider, "_target_confirms_source_handshake", _accepted_target_peer)


def test_peer_restart_provider_requires_accepted_handshake(monkeypatch, tmp_path):
    state = tmp_path / "peers.json"
    _write_peer_state(state, handshake="handshake_rejected")
    _patch_peer_success(monkeypatch, state)

    with pytest.raises(restart_provider.RestartProviderError, match="not trusted"):
        restart_provider.peer_restart_provider("HASHI2")


def test_peer_restart_provider_requires_handshake_capability(monkeypatch, tmp_path):
    state = tmp_path / "peers.json"
    _write_peer_state(state, capabilities=["rescue_control"])
    _patch_peer_success(monkeypatch, state)

    with pytest.raises(restart_provider.RestartProviderError, match="did not advertise rescue_restart"):
        restart_provider.peer_restart_provider("HASHI2")


def test_peer_restart_provider_requires_live_rescue_restart(monkeypatch, tmp_path):
    state = tmp_path / "peers.json"
    _write_peer_state(state)
    _patch_peer_success(monkeypatch, state)
    monkeypatch.setattr(
        restart_provider.remote_rescue,
        "probe_capabilities",
        lambda instance, **_kwargs: {
            "ok": True,
            "instance": instance,
            "base_url": "http://127.0.0.1:8766",
            "capabilities": {"rescue_restart": False},
            "remote_supervisor": {"mode": "supervised"},
        },
    )

    with pytest.raises(restart_provider.RestartProviderError, match="L3_RESTART"):
        restart_provider.peer_restart_provider("HASHI2")


def test_peer_restart_provider_requires_supervised_remote(monkeypatch, tmp_path):
    state = tmp_path / "peers.json"
    _write_peer_state(state)
    _patch_peer_success(monkeypatch, state)
    monkeypatch.setattr(
        restart_provider.remote_rescue,
        "probe_capabilities",
        lambda instance, **_kwargs: {
            "ok": True,
            "instance": instance,
            "base_url": "http://127.0.0.1:8766",
            "capabilities": {"rescue_restart": True},
            "remote_supervisor": {"mode": "child"},
        },
    )

    with pytest.raises(restart_provider.RestartProviderError, match="not rescue-grade"):
        restart_provider.peer_restart_provider("HASHI2")


def test_peer_restart_provider_accepts_supported_bilateral_peer(monkeypatch, tmp_path):
    state = tmp_path / "peers.json"
    _write_peer_state(state)
    _patch_peer_success(monkeypatch, state)

    provider = restart_provider.peer_restart_provider("@hashi2")

    assert provider["kind"] == "peer_remote"
    assert provider["source_instance"] == "HASHI1"
    assert provider["target_instance"] == "HASHI2"
    assert provider["handshake_state"] == "handshake_accepted"
    assert provider["target_handshake_state"] == "handshake_accepted"


def test_target_confirms_source_handshake_requires_trusted_view(monkeypatch):
    monkeypatch.setattr(restart_provider, "local_instance_id", lambda: "HASHI1")
    monkeypatch.setattr(restart_provider, "_auth_kwargs", lambda: {"shared_token": "secret", "from_instance": "HASHI1"})
    monkeypatch.setattr(
        restart_provider.remote_rescue,
        "_request_json_status",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            body={
                "trusted_view": False,
                "instance": {"instance_id": "HASHI2"},
                "peers": [],
            },
        ),
    )

    with pytest.raises(restart_provider.RestartProviderError, match="authenticated trusted peer view"):
        restart_provider._target_confirms_source_handshake(
            "HASHI2", base_url="http://127.0.0.1:8766"
        )


def test_target_confirms_source_handshake_rejects_target_side_stale_trust(monkeypatch):
    monkeypatch.setattr(restart_provider, "local_instance_id", lambda: "HASHI1")
    monkeypatch.setattr(restart_provider, "_auth_kwargs", lambda: {"shared_token": "secret", "from_instance": "HASHI1"})
    monkeypatch.setattr(
        restart_provider.remote_rescue,
        "_request_json_status",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            body={
                "trusted_view": True,
                "instance": {"instance_id": "HASHI2"},
                "peers": [
                    {
                        "instance_id": "HASHI1",
                        "properties": {"handshake_state": "rehydrate_required"},
                    }
                ],
            },
        ),
    )

    with pytest.raises(restart_provider.RestartProviderError, match="does not currently trust"):
        restart_provider._target_confirms_source_handshake(
            "HASHI2", base_url="http://127.0.0.1:8766"
        )


def test_target_confirms_source_handshake_accepts_live_bilateral_trust(monkeypatch):
    monkeypatch.setattr(restart_provider, "local_instance_id", lambda: "HASHI1")
    monkeypatch.setattr(restart_provider, "_auth_kwargs", lambda: {"shared_token": "secret", "from_instance": "HASHI1"})
    monkeypatch.setattr(
        restart_provider.remote_rescue,
        "_request_json_status",
        lambda *_args, **_kwargs: SimpleNamespace(
            ok=True,
            body={
                "trusted_view": True,
                "instance": {"instance_id": "HASHI2"},
                "peers": [
                    {
                        "instance_id": "HASHI1",
                        "properties": {"handshake_state": "handshake_accepted"},
                    }
                ],
            },
        ),
    )

    peer = restart_provider._target_confirms_source_handshake(
        "HASHI2", base_url="http://127.0.0.1:8766"
    )

    assert peer["instance_id"] == "HASHI1"


def test_local_restart_provider_requires_supervised_remote(monkeypatch):
    monkeypatch.setattr(restart_provider, "local_instance_id", lambda: "HASHI1")
    monkeypatch.setattr(
        restart_provider.remote_rescue,
        "probe_capabilities",
        lambda instance, **_kwargs: {
            "ok": True,
            "instance": instance,
            "capabilities": {"rescue_restart": True},
            "remote_supervisor": {"mode": "child"},
        },
    )

    with pytest.raises(restart_provider.RestartProviderError, match="not rescue-grade"):
        restart_provider.local_restart_provider()


def test_restart_via_peer_revalidates_before_post(monkeypatch):
    provider = {
        "kind": "peer_remote",
        "source_instance": "HASHI1",
        "target_instance": "HASHI2",
        "provider_instance": "HASHI2",
    }
    calls = {"revalidated": 0, "restart": 0}

    def _revalidate(target):
        calls["revalidated"] += 1
        assert target == "HASHI2"
        return dict(provider)

    def _restart(target, **kwargs):
        calls["restart"] += 1
        assert target == "HASHI2"
        assert kwargs["reason"] == "test"
        assert (
            kwargs["timeout"]
            == restart_provider.remote_rescue.RESTART_REQUEST_TIMEOUT_SECONDS
        )
        return 0, {"ok": True, "restart_launched": True}

    monkeypatch.setattr(restart_provider, "peer_restart_provider", _revalidate)
    monkeypatch.setattr(restart_provider.remote_rescue, "rescue_restart", _restart)
    monkeypatch.setattr(restart_provider, "_auth_kwargs", lambda: {"shared_token": "secret", "from_instance": "HASHI1"})

    code, payload = restart_provider.restart_via_provider(provider, reason="test")

    assert code == 0
    assert payload["restart_launched"] is True
    assert calls == {"revalidated": 1, "restart": 1}
