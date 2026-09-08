import hmac
import json
import os
import stat

from remote.security.pairing import PairingManager


def test_pairing_uses_constant_time_digest_compare(monkeypatch, tmp_path):
    calls = []
    real_compare = hmac.compare_digest

    def recording_compare(left, right):
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr("remote.security.pairing.hmac.compare_digest", recording_compare)
    manager = PairingManager(storage_dir=tmp_path)
    token = manager.approve_request_direct("client-1", "Client One")

    assert manager.verify_token(token) == "client-1"
    assert calls


def test_paired_instances_file_is_owner_only(tmp_path):
    manager = PairingManager(storage_dir=tmp_path)
    manager.approve_request_direct("client-1", "Client One")

    path = tmp_path / "paired_instances.json"
    assert path.is_file()

    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_pairing_token_expires_at_configured_ttl(monkeypatch, tmp_path):
    now = 1_000_000.0
    monkeypatch.setattr("remote.security.pairing.time.time", lambda: now)
    manager = PairingManager(
        storage_dir=tmp_path,
        token_ttl_seconds=7 * 24 * 60 * 60,
    )

    token = manager.approve_request_direct("client-7d", "Seven Day Client")
    paired = manager.get_paired_client("client-7d")

    assert paired is not None
    assert paired.expires_at == now + 604_800
    assert manager.verify_token(token) == "client-7d"

    now += 604_800
    assert manager.verify_token(token) is None
    assert manager.get_paired_client("client-7d") is None
    persisted = json.loads((tmp_path / "paired_instances.json").read_text())
    assert persisted["clients"] == []


def test_legacy_unexpired_pairing_without_expiry_remains_compatible(tmp_path):
    manager = PairingManager(storage_dir=tmp_path)
    token = manager.approve_request_direct("legacy", "Legacy Client")

    payload = json.loads((tmp_path / "paired_instances.json").read_text())
    payload["clients"][0].pop("expires_at")
    (tmp_path / "paired_instances.json").write_text(json.dumps(payload))

    reloaded = PairingManager(storage_dir=tmp_path)
    assert reloaded.verify_token(token) == "legacy"


def test_one_click_pairing_can_be_enabled_without_trusting_the_lan(tmp_path):
    manager = PairingManager(
        storage_dir=tmp_path,
        lan_mode=False,
        auto_approve=True,
    )

    assert manager.lan_mode is False
    assert manager.is_auto_approved() is True
