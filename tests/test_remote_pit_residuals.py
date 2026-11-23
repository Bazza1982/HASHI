"""Regression tests for the 2026-11-23 production-pre-release PIT residuals.

Covers the minimal HASHI Remote fixes:
- unknown liveness is never converted into inactive in gateway failure
  payloads (only a confirmed probe may report inactive);
- a healthy /api/health answered by a *different* HASHI instance on the
  expected loopback port is not evidence that the controlled instance is
  active;
- a healthy /api/health without an instance_id cannot confirm the
  controlled instance either;
- the health probe only caches a loopback host after verifying the
  response's instance identity, and keeps scanning bounded candidates when
  the first HTTP 200 belongs to a foreign instance.
"""

from __future__ import annotations

import json
from urllib import parse as urlparse

from remote.api import server as remote_server


class _FakeJsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _install_fake_opener(monkeypatch, by_host):
    def opener(req, *, timeout, connect_timeout=None):
        host = urlparse.urlsplit(req.full_url).hostname or ""
        outcome = by_host.get(host, by_host.get("*"))
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeJsonResponse(outcome)

    monkeypatch.setattr(remote_server, "_open_local_workbench_request", opener)


def _record_liveness(monkeypatch):
    recorded: dict = {}
    monkeypatch.setattr(
        remote_server,
        "record_hashi_liveness",
        lambda *, hashi_state, health=None, now=None: recorded.update(
            {"hashi_state": hashi_state, "health": health}
        ),
    )
    return recorded


def _use_expected_instance(monkeypatch, instance_id):
    monkeypatch.setattr(
        remote_server, "_controlled_instance_id", lambda status=None: instance_id
    )


def _use_hosts(monkeypatch, hosts):
    monkeypatch.setattr(
        remote_server,
        "_ordered_local_workbench_hosts",
        lambda *, max_fallbacks=2: tuple(hosts),
    )


def test_gateway_failure_payload_preserves_unknown_hashi_state(monkeypatch):
    """An unverified failure keeps hashi_status=unknown and must not become
    hashi_inactive, so Workbench cannot treat it as 'Core is down, start it'."""
    remote_server.reset_local_workbench_transport_state()
    with remote_server._HASHI_LIVENESS_LOCK:
        assert remote_server._HASHI_LIVENESS_STATE["hashi_state"] == "unknown"

    error = remote_server.WorkbenchGatewayError(
        "backend_unavailable", accepted=False, retryable=True, status=503
    )
    payload = remote_server._workbench_gateway_failure_payload(error, method="GET")

    assert payload["hashi_status"] == "unknown"
    assert payload["hashi_running"] is False
    assert payload["reason_code"] == "backend_unavailable"
    assert payload["reason_code"] != "hashi_inactive"
    # The Remote's own online state stays independent of HASHI state.
    assert payload["remote_status"] == "ready"
    assert payload["remote_alive"] is True


def test_gateway_failure_payload_confirmed_inactive_still_reports_inactive():
    """Confirmed inactive (probe ran, backend down) still maps to
    hashi_status=inactive / reason_code=hashi_inactive."""
    remote_server.reset_local_workbench_transport_state()
    remote_server.record_hashi_liveness(hashi_state="inactive", health=None)

    error = remote_server.WorkbenchGatewayError(
        "local_workbench_unavailable", accepted=False, retryable=True, status=503
    )
    payload = remote_server._workbench_gateway_failure_payload(error, method="GET")

    assert payload["hashi_status"] == "inactive"
    assert payload["reason_code"] == "hashi_inactive"


def test_fetch_workbench_health_active_when_identity_matches(monkeypatch):
    """Scenario A: expected HASHI3, response HASHI3 -> active."""
    remote_server.reset_local_workbench_transport_state()
    recorded = _record_liveness(monkeypatch)
    _use_expected_instance(monkeypatch, "HASHI3")
    _use_hosts(monkeypatch, ("127.0.0.1",))
    _install_fake_opener(monkeypatch, {"127.0.0.1": {"ok": True, "instance_id": "HASHI3"}})

    health = remote_server._fetch_workbench_health(timeout=0.5)

    assert health is not None
    assert health.get("instance_id") == "HASHI3"
    assert recorded.get("hashi_state") == "active"


def test_fetch_workbench_health_rejects_foreign_instance_id(monkeypatch):
    """Scenario B: expected HASHI3, response HASHI4 -> not active."""
    remote_server.reset_local_workbench_transport_state()
    recorded = _record_liveness(monkeypatch)
    _use_expected_instance(monkeypatch, "HASHI3")
    _use_hosts(monkeypatch, ("127.0.0.1",))
    _install_fake_opener(monkeypatch, {"127.0.0.1": {"ok": True, "instance_id": "HASHI4"}})

    health = remote_server._fetch_workbench_health(timeout=0.5)

    assert health is None
    assert recorded.get("health") is None
    assert recorded.get("hashi_state") != "active"


def test_fetch_workbench_health_unverified_when_instance_id_missing(monkeypatch):
    """Scenario C: expected HASHI3, response without instance_id -> not active."""
    remote_server.reset_local_workbench_transport_state()
    recorded = _record_liveness(monkeypatch)
    _use_expected_instance(monkeypatch, "HASHI3")
    _use_hosts(monkeypatch, ("127.0.0.1",))
    _install_fake_opener(monkeypatch, {"127.0.0.1": {"ok": True}})

    health = remote_server._fetch_workbench_health(timeout=0.5)

    assert health is None
    assert recorded.get("health") is None
    assert recorded.get("hashi_state") != "active"


def test_fetch_workbench_health_legacy_without_expected_identity(monkeypatch):
    """Scenario G: no configured expected instance keeps legacy behaviour
    (any healthy response counts, identity unknown)."""
    remote_server.reset_local_workbench_transport_state()
    recorded = _record_liveness(monkeypatch)
    _use_expected_instance(monkeypatch, "")
    _use_hosts(monkeypatch, ("127.0.0.1",))
    _install_fake_opener(monkeypatch, {"127.0.0.1": {"ok": True, "instance_id": "HASHI_ANY"}})

    health = remote_server._fetch_workbench_health(timeout=0.5)

    assert health is not None
    assert recorded.get("hashi_state") == "active"


def test_fetch_workbench_json_skips_foreign_candidate(monkeypatch):
    """Scenario D: first candidate is a foreign instance, second is the
    controlled instance -> identify the second and cache only its address."""
    remote_server.reset_local_workbench_transport_state()
    _use_hosts(monkeypatch, ("127.0.0.1", "127.0.0.2"))
    _install_fake_opener(
        monkeypatch,
        {
            "127.0.0.1": {"ok": True, "instance_id": "HASHI4"},
            "127.0.0.2": {"ok": True, "instance_id": "HASHI3"},
        },
    )

    value = remote_server._fetch_workbench_json(
        "/api/health", timeout=0.5, required_instance_id="HASHI3"
    )

    assert value is not None
    assert value.get("instance_id") == "HASHI3"
    assert remote_server._CACHED_LOCAL_WORKBENCH_HOST == "127.0.0.2"


def test_fetch_workbench_json_returns_none_when_all_hosts_fail(monkeypatch):
    """Scenario E: every bounded candidate fails -> fast None, no stale cache."""
    remote_server.reset_local_workbench_transport_state()
    _use_hosts(monkeypatch, ("127.0.0.1", "127.0.0.2"))
    _install_fake_opener(monkeypatch, {"*": OSError("connection refused")})

    value = remote_server._fetch_workbench_json(
        "/api/health", timeout=0.5, required_instance_id="HASHI3"
    )

    assert value is None
    assert remote_server._CACHED_LOCAL_WORKBENCH_HOST is None
# HASHI-Fingerprint: hfp_2e30e35452f44f88b7a95f063c63454e
