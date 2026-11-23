"""Regression tests for the 2026-11-23 production-pre-release PIT residuals.

Covers two minimal HASHI Remote fixes:
- unknown liveness is never converted into inactive in gateway failure
  payloads (only a confirmed probe may report inactive);
- a healthy /api/health answered by a *different* HASHI instance on the
  expected loopback port is not evidence that the controlled instance is
  active.
"""

from __future__ import annotations

from pathlib import Path

from remote.api import server as remote_server


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


def test_fetch_workbench_health_rejects_foreign_instance_id(monkeypatch):
    """A healthy /api/health from a *different* instance occupying the expected
    port must not be reported as this instance being active."""
    remote_server.reset_local_workbench_transport_state()

    monkeypatch.setattr(
        remote_server,
        "_fetch_workbench_json",
        lambda path, *, timeout: {"ok": True, "instance_id": "HASHI_FOREIGN"},
    )
    monkeypatch.setattr(
        remote_server, "_controlled_instance_id", lambda status=None: "HASHI_EXPECTED"
    )
    recorded: dict = {}
    monkeypatch.setattr(
        remote_server,
        "record_hashi_liveness",
        lambda *, hashi_state, health=None: recorded.update(
            {"hashi_state": hashi_state, "health": health}
        ),
    )

    result = remote_server._fetch_workbench_health(timeout=0.5)

    assert result is None
    assert recorded.get("health") is None
    assert recorded.get("hashi_state") != "active"


def test_fetch_workbench_health_accepts_expected_instance_id(monkeypatch):
    """The health of the controlled instance is accepted as active, and a
    health without instance_id keeps the previous (compatible) behaviour."""
    remote_server.reset_local_workbench_transport_state()

    recorded: dict = {}
    monkeypatch.setattr(
        remote_server,
        "record_hashi_liveness",
        lambda *, hashi_state, health=None: recorded.update(
            {"hashi_state": hashi_state, "health": health}
        ),
    )
    monkeypatch.setattr(
        remote_server, "_controlled_instance_id", lambda status=None: "HASHI_EXPECTED"
    )

    monkeypatch.setattr(
        remote_server,
        "_fetch_workbench_json",
        lambda path, *, timeout: {"ok": True, "instance_id": "HASHI_EXPECTED"},
    )
    health = remote_server._fetch_workbench_health(timeout=0.5)
    assert health is not None and health.get("instance_id") == "HASHI_EXPECTED"
    assert recorded.get("hashi_state") == "active"

    # No instance_id in health (old backend shape): accepted as before.
    monkeypatch.setattr(
        remote_server,
        "_fetch_workbench_json",
        lambda path, *, timeout: {"ok": True},
    )
    health = remote_server._fetch_workbench_health(timeout=0.5)
    assert health is not None
    assert recorded.get("hashi_state") == "active"


def test_fetch_workbench_health_skips_check_without_expected_identity(monkeypatch):
    """Remotes without a configured expected instance keep legacy behaviour:
    any healthy /api/health response counts as active (identity unknown)."""
    remote_server.reset_local_workbench_transport_state()

    recorded: dict = {}
    monkeypatch.setattr(
        remote_server,
        "record_hashi_liveness",
        lambda *, hashi_state, health=None: recorded.update(
            {"hashi_state": hashi_state, "health": health}
        ),
    )
    monkeypatch.setattr(remote_server, "_controlled_instance_id", lambda status=None: "")
    monkeypatch.setattr(
        remote_server,
        "_fetch_workbench_json",
        lambda path, *, timeout: {"ok": True, "instance_id": "HASHI_ANY"},
    )

    health = remote_server._fetch_workbench_health(timeout=0.5)
    assert health is not None
    assert recorded.get("hashi_state") == "active"
# HASHI-Fingerprint: hfp_2e30e35452f44f88b7a95f063c63454e
