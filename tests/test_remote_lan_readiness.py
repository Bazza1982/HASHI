from __future__ import annotations

import json
import time

import pytest

import starlette.requests as starlette_requests
from fastapi.testclient import TestClient

from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import build_auth_headers, canonical_request_target
from remote.terminal.executor import TerminalExecutor
from remote.api.lan_readiness import (
    CHECKPOINT_MIN_SECONDS,
    LanReadinessCache,
    MAX_REPORT_BYTES,
    SCHEMA_VERSION,
)


def _valid_report(**overrides):
    report = {
        "schema_version": SCHEMA_VERSION,
        "instance_id": "LANLAB1",
        "state": "degraded",
        "observed_at": time.time(),
        "source": "windows_doctor",
        "process": {"state": "ok"},
        "listener": {"state": "ok", "address": "0.0.0.0", "port": 8779, "pid": 1234},
        "firewall": {
            "state": "degraded",
            "reason_code": "firewall_rule_missing",
            "effective_policy_checked": True,
        },
        "mdns": {"advertising": "ok", "browsing": "ok", "external_visibility": "unknown"},
        "external_reachability": {"state": "unknown", "reason_code": "lan_reachability_unverified"},
        "reason_codes": ["firewall_rule_missing", "lan_reachability_unverified"],
    }
    report.update(overrides)
    return report


def _make_cache(tmp_path, instance_id="LANLAB1"):
    return LanReadinessCache(tmp_path, instance_id)


def test_empty_projection_is_unknown_not_green(tmp_path):
    cache = _make_cache(tmp_path)
    projection = cache.projection()
    assert projection["schema_version"] == SCHEMA_VERSION
    assert projection["state"] == "unknown"
    assert projection["stale"] is True
    assert projection["reason_codes"] == ["lan_reachability_unverified"]


def test_update_accepts_valid_report_and_projects_it(tmp_path):
    cache = _make_cache(tmp_path)
    result = cache.update(_valid_report())
    assert result["state"] == "degraded"
    assert result["firewall"]["reason_code"] == "firewall_rule_missing"
    assert result["stale"] is False
    projection = cache.projection()
    assert projection["state"] == "degraded"
    assert "instance_id" not in projection


def test_wrong_instance_report_is_rejected(tmp_path):
    cache = _make_cache(tmp_path)
    with pytest.raises(ValueError):
        cache.update(_valid_report(instance_id="HASHI6"))
    assert cache.projection()["state"] == "unknown"


def test_wrong_schema_report_is_rejected(tmp_path):
    cache = _make_cache(tmp_path)
    with pytest.raises(ValueError):
        cache.update(_valid_report(schema_version=2))


def test_non_object_report_is_rejected(tmp_path):
    cache = _make_cache(tmp_path)
    with pytest.raises(ValueError):
        cache.update(["not", "a", "report"])


def test_credential_material_is_redacted(tmp_path):
    cache = _make_cache(tmp_path)
    report = _valid_report()
    report["firewall"]["error"] = "shared token abc123 was rejected"
    report["listener"]["detail"] = "hmac proof failed"
    result = cache.update(report)
    assert result["firewall"]["error"] == "[redacted]"
    assert result["listener"]["detail"] == "[redacted]"


def test_unknown_reason_codes_are_preserved(tmp_path):
    cache = _make_cache(tmp_path)
    report = _valid_report(reason_codes=["firewall_rule_missing", "future_code_x"])
    result = cache.update(report)
    assert "future_code_x" in result["reason_codes"]


def test_identical_updates_do_not_rewrite_checkpoint(tmp_path):
    cache = _make_cache(tmp_path)
    cache.update(_valid_report())
    state_path = tmp_path / "state" / "lan_readiness.json"
    first_stat = state_path.stat()
    cache.update(_valid_report())
    second_stat = state_path.stat()
    assert first_stat.st_mtime_ns == second_stat.st_mtime_ns
    assert cache.activity()["persist_writes_suppressed"] >= 1


def test_material_change_persists_immediately(tmp_path):
    cache = _make_cache(tmp_path)
    cache.update(_valid_report())
    state_path = tmp_path / "state" / "lan_readiness.json"
    first_content = state_path.read_bytes()
    changed = _valid_report(firewall={
        "state": "ok",
        "reason_code": "",
        "effective_policy_checked": True,
    })
    cache.update(changed)
    second_content = state_path.read_bytes()
    assert second_content != first_content
    payload = json.loads(second_content)
    assert payload["firewall"]["state"] == "ok"


def test_checkpoint_reloaded_after_restart(tmp_path):
    cache = _make_cache(tmp_path)
    cache.update(_valid_report())
    reloaded = LanReadinessCache(tmp_path, "LANLAB1")
    projection = reloaded.projection()
    assert projection["state"] == "degraded"
    assert projection["source"] == "windows_doctor"


def test_unchecked_checkpoint_keeps_unknown(tmp_path):
    cache = _make_cache(tmp_path)
    cache.update(_valid_report())
    # A differently owned instance must not inherit this checkpoint.
    other = LanReadinessCache(tmp_path, "HASHI6")
    assert other.projection()["state"] == "unknown"


def test_stale_projection_after_timeout(tmp_path):
    cache = _make_cache(tmp_path)
    old = time.time() - 7200
    cache.update(_valid_report(observed_at=old))
    projection = cache.projection()
    assert projection["stale"] is True
    assert projection["state"] == "degraded"


def test_oversized_report_is_bounded_not_amplified(tmp_path):
    cache = _make_cache(tmp_path)
    report = _valid_report()
    report["process"] = {"blob": "x" * (MAX_REPORT_BYTES * 2)}


def test_persisted_checkpoint_is_bounded(tmp_path):
    cache = _make_cache(tmp_path)
    cache.update(_valid_report())
    raw = (tmp_path / "state" / "lan_readiness.json").read_bytes()
    assert len(raw) <= MAX_REPORT_BYTES
    payload = json.loads(raw)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["instance_id"] == "LANLAB1"


class _ProtocolStub:
    def get_protocol_status(self):
        return {"capabilities": []}


def _report_client(tmp_path):
    token = "shared-secret"
    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": token}),
        encoding="utf-8",
    )
    app = create_app(
        {
            "instance_id": "HASHI1",
            "display_name": "HASHI One",
            "remote_port": 8766,
            "workbench_port": 18800,
        },
        PairingManager(storage_dir=tmp_path / "pairing"),
        TerminalExecutor(),
        protocol_manager=_ProtocolStub(),
        hashi_root=str(tmp_path),
        workbench_port=18800,
        lan_readiness_cache=LanReadinessCache(tmp_path, "HASHI1"),
    )
    return TestClient(app), token


def _report_headers(token):
    # The endpoint authenticates against an empty-body contract before it
    # enforces the size bound; signing the payload bytes would never match.
    target = canonical_request_target("/lan-readiness/report", "")
    return build_auth_headers(
        shared_token=token,
        method="POST",
        path=target,
        from_instance="WORKBENCH",
        body_bytes=b"",
        timestamp=int(time.time()),
    )

def _signed_report_body(extra_blob=None):
    report = _valid_report(instance_id="HASHI1")
    if extra_blob:
        report["process"] = {"blob": extra_blob}
    return json.dumps(report).encode("utf-8")


def test_report_endpoint_accepts_valid_signed_report(tmp_path):
    client, token = _report_client(tmp_path)
    body = _signed_report_body()
    response = client.post(
        "/lan-readiness/report",
        headers=_report_headers(token),
        content=body,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["lan_readiness"]["state"] == "degraded"


def test_report_endpoint_rejects_oversized_report_before_parse(tmp_path):
    client, token = _report_client(tmp_path)
    # A 40 KiB report exceeds the 32 KiB contract and must be refused with
    # 413; the payload is still valid JSON so the failure is the size bound,
    # not parsing.
    body = _signed_report_body("x" * (MAX_REPORT_BYTES + 8192))
    assert len(body) > MAX_REPORT_BYTES
    response = client.post(
        "/lan-readiness/report",
        headers=_report_headers(token),
        content=body,
    )
    assert response.status_code == 413
    assert response.json() == {"ok": False, "error": "lan_readiness_report_too_large"}


def test_report_endpoint_never_calls_unbounded_request_body(tmp_path, monkeypatch):
    # The endpoint must enforce the bound from the declared Content-Length or
    # a bounded stream read. Any direct call to the unbounded request.body()
    # (the previous implementation) must fail this guard.
    client, token = _report_client(tmp_path)

    def boom(_self):
        raise AssertionError("unbounded request.body() was called")

    monkeypatch.setattr(starlette_requests.Request, "body", boom)
    body = _signed_report_body()
    response = client.post(
        "/lan-readiness/report",
        headers=_report_headers(token),
        content=body,
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_report_endpoint_requires_authentication(tmp_path):
    client, _token = _report_client(tmp_path)
    response = client.post("/lan-readiness/report", content=_signed_report_body())
    assert response.status_code == 401

def test_checkpoint_min_seconds_is_sane():
    assert CHECKPOINT_MIN_SECONDS >= 60
# HASHI-Fingerprint: hfp_cca85abb5a48466094762d3c8a45d39f


def _write_checkpoint(tmp_path, text):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "lan_readiness.json").write_text(text, encoding="utf-8")
    return state_dir / "lan_readiness.json"


def test_corrupt_schema_string_checkpoint_does_not_crash(tmp_path):
    # A valid JSON file whose schema_version cannot be converted (the string
    # "invalid") previously raised ValueError from int() during cache
    # initialisation and could take the Remote down at startup.
    _write_checkpoint(tmp_path, json.dumps({
        "schema_version": "invalid",
        "instance_id": "LANLAB1",
        "state": "degraded",
    }))
    cache = LanReadinessCache(tmp_path, "LANLAB1")
    projection = cache.projection()
    assert projection["state"] == "unknown"
    assert projection["reason_codes"] == ["lan_reachability_unverified"]


def test_invalid_json_checkpoint_is_ignored_and_preserved(tmp_path):
    sibling = tmp_path / "state" / "unrelated.json"
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    sibling.write_text('{"keep": true}', encoding="utf-8")
    checkpoint = _write_checkpoint(tmp_path, "{not-json")
    cache = LanReadinessCache(tmp_path, "LANLAB1")
    assert cache.projection()["state"] == "unknown"
    # Corrupted optional diagnostics never delete unrelated state files.
    assert checkpoint.exists()
    assert sibling.exists()


def test_null_schema_checkpoint_is_ignored(tmp_path):
    _write_checkpoint(tmp_path, json.dumps({
        "schema_version": None,
        "instance_id": "LANLAB1",
        "state": "ok",
    }))
    cache = LanReadinessCache(tmp_path, "LANLAB1")
    assert cache.projection()["state"] == "unknown"


@pytest.mark.parametrize("schema_value", [[], {}, 1.5, True, "not-a-number"])
def test_unexpected_schema_shapes_are_ignored(tmp_path, schema_value):
    _write_checkpoint(tmp_path, json.dumps({
        "schema_version": schema_value,
        "instance_id": "LANLAB1",
        "state": "ok",
    }))
    cache = LanReadinessCache(tmp_path, "LANLAB1")
    assert cache.projection()["state"] == "unknown"


def test_schema_version_string_one_checkpoint_still_loads(tmp_path):
    # Historical leniency: the old int("1") parse accepted a string version.
    cache = _make_cache(tmp_path)
    cache.update(_valid_report())
    payload = json.loads((tmp_path / "state" / "lan_readiness.json").read_text(encoding="utf-8"))
    payload["schema_version"] = "1"
    (tmp_path / "state" / "lan_readiness.json").write_text(json.dumps(payload), encoding="utf-8")
    reloaded = LanReadinessCache(tmp_path, "LANLAB1")
    assert reloaded.projection()["state"] == "degraded"


def test_wrong_instance_checkpoint_is_ignored(tmp_path):
    _write_checkpoint(tmp_path, json.dumps({
        "schema_version": SCHEMA_VERSION,
        "instance_id": "HASHI6",
        "state": "ok",
    }))
    cache = LanReadinessCache(tmp_path, "LANLAB1")
    assert cache.projection()["state"] == "unknown"


def test_oversized_checkpoint_is_ignored_and_preserved(tmp_path):
    checkpoint = _write_checkpoint(tmp_path, json.dumps({
        "schema_version": SCHEMA_VERSION,
        "instance_id": "LANLAB1",
        "state": "ok",
        "blob": "x" * (MAX_REPORT_BYTES + 1024),
    }))
    cache = LanReadinessCache(tmp_path, "LANLAB1")
    assert cache.projection()["state"] == "unknown"
    assert checkpoint.exists()


def test_checkpoint_read_failure_is_ignored(tmp_path, monkeypatch):
    import pathlib
    _write_checkpoint(tmp_path, json.dumps({
        "schema_version": SCHEMA_VERSION,
        "instance_id": "LANLAB1",
        "state": "ok",
    }))
    original_read_bytes = pathlib.Path.read_bytes

    def raise_permission_error(self, *args, **kwargs):
        if self.name == "lan_readiness.json":
            raise PermissionError("simulated read failure")
        return original_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_bytes", raise_permission_error)
    cache = LanReadinessCache(tmp_path, "LANLAB1")
    assert cache.projection()["state"] == "unknown"


def test_corrupt_checkpoint_never_blocks_remote_startup(tmp_path):
    # The cache is constructed during Remote startup; corruption must yield
    # a healthy empty projection instead of raising.
    _write_checkpoint(tmp_path, json.dumps({
        "schema_version": "invalid",
        "instance_id": "LANLAB1",
        "state": "ok",
    }))
    cache = LanReadinessCache(tmp_path, "LANLAB1")
    assert cache.activity()["snapshot_updates"] == 0
    assert cache.projection()["diagnostics_available"] is False
