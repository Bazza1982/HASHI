import hmac
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

    mode = stat.S_IMODE((tmp_path / "paired_instances.json").stat().st_mode)

    assert mode == 0o600
