"""Round-2 regressions: target refresh pinning and open-first validation.

Fixes review items 4 and 5 for PR #175: an explicitly requested target must
never silently switch to an unrelated device/machine, and a failed open must
never evict an active session.
"""
from __future__ import annotations

import pytest

from orchestrator.desktop_api import DesktopSessionService
from orchestrator.desktop_contract import DesktopError

TARGET_NEW_GEN = {
    "instance_id": "INST",
    "capability_id": "cap-new",
    "device_id": "device-a",
    "user_session_id": "user-a",
    "worker_generation": "gen-2",
}
TARGET_OLD_GEN = {
    "instance_id": "INST",
    "capability_id": "cap-old",
    "device_id": "device-a",
    "user_session_id": "user-a",
    "worker_generation": "gen-1",
}
TARGET_OTHER_DEVICE = {
    "instance_id": "INST",
    "capability_id": "cap-x",
    "device_id": "device-b",
    "user_session_id": "user-b",
    "worker_generation": "gen-9",
}


def test_refresh_target_prefers_same_device_and_never_switches_machines():
    targets = [TARGET_NEW_GEN]
    # Same-device generation refresh (worker restart) is safe and returns the
    # live target even when capability_id and worker_generation changed.
    assert DesktopSessionService._refresh_target(TARGET_OLD_GEN, targets) is targets[0]
    # Capability-only or device-only refresh also resolves to the live target.
    assert DesktopSessionService._refresh_target({"capability_id": "cap-new"}, targets) is targets[0]
    assert DesktopSessionService._refresh_target({"device_id": "device-a"}, targets) is targets[0]
    # An explicitly requested device that is no longer registered must never
    # silently fall back to an unrelated machine: report the change instead.
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        DesktopSessionService._refresh_target(
            {"capability_id": "cap-old", "device_id": "device-gone"}, targets)
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        DesktopSessionService._refresh_target({"capability_id": "cap-missing"}, targets)
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        DesktopSessionService._refresh_target(TARGET_OTHER_DEVICE, targets)
    # Absent/empty requests still select the first registered target.
    assert DesktopSessionService._refresh_target(None, targets) is targets[0]
    assert DesktopSessionService._refresh_target({}, targets) is targets[0]
    with pytest.raises(DesktopError, match="desktop_worker_unavailable"):
        DesktopSessionService._refresh_target({"capability_id": "cap-old"}, [])


class FailingOpenBroker:
    def __init__(self):
        self.fail_next = False
        self.calls = []

    def desktop_targets(self):
        return [{"capability_id": "cap-a", "device_id": "device-a"}]

    async def invoke_desktop(self, target, *, actor_id, session_id, operation, args):
        self.calls.append(operation)
        if self.fail_next:
            raise DesktopError("desktop_worker_unavailable", 503)
        return {"displays": []}


async def test_failed_open_never_evicts_an_active_session():
    broker = FailingOpenBroker()
    service = DesktopSessionService(lambda: broker)
    target = {"capability_id": "cap-a", "device_id": "device-a"}
    first = await service.run("owner-a", {"operation": "open", "client_id": "client-1", "target": target})
    for index in range(2, 9):
        await service.run("owner-a", {"operation": "open", "client_id": f"client-{index}", "target": target})
    assert len(service.sessions) == 8
    before = dict(service.sessions)
    # A failed open (worker unavailable at desktop_info) must not evict or
    # otherwise change any existing session.
    broker.fail_next = True
    with pytest.raises(DesktopError, match="desktop_worker_unavailable"):
        await service.run("owner-a", {"operation": "open", "client_id": "client-9", "target": target})
    assert dict(service.sessions) == before
    assert len(service.sessions) == 8
    # A successful open validates first, then evicts only the least recently
    # touched session to make room.
    broker.fail_next = False
    opened = await service.run("owner-a", {"operation": "open", "client_id": "client-9", "target": target})
    assert len(service.sessions) == 8
    assert opened["session_id"] in service.sessions
    evicted = set(before) - set(service.sessions)
    assert evicted == {first["session_id"]}


async def test_open_with_gone_target_reports_change_without_touching_worker_or_sessions():
    class Broker:
        def desktop_targets(self):
            return [{"capability_id": "cap-b", "device_id": "device-b"}]

        async def invoke_desktop(self, *_args, **_kwargs):
            raise AssertionError("must not reach the worker")

    service = DesktopSessionService(Broker)
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        await service.run("owner-a", {"operation": "open", "client_id": "client-a",
                                      "target": {"capability_id": "cap-a", "device_id": "device-a"}})
    assert service.sessions == {}

# ---------------------------------------------------------------------------
# Round-3 regressions: session-pinned target identity. One physical computer
# can host several Windows interactive sessions sharing a device_id; refresh
# and open must never substitute one session for another.

SESSION_A = {
    "instance_id": "HASHI1",
    "device_id": "device-host",
    "user_session_id": "user-session-A",
    "capability_id": "cap-A",
    "worker_generation": "gen-1",
}
SESSION_B = {
    "instance_id": "HASHI1",
    "device_id": "device-host",
    "user_session_id": "user-session-B",
    "capability_id": "cap-B",
    "worker_generation": "gen-1",
}


def test_refresh_selects_the_requested_session_when_the_device_is_shared():
    # Same device, two Windows sessions: the requested session must be chosen
    # even when it appears second in the registration order.
    targets = [SESSION_B, SESSION_A]
    assert DesktopSessionService._refresh_target(SESSION_A, targets) is targets[1]


def test_refresh_rejects_a_different_user_session_on_the_same_device():
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        DesktopSessionService._refresh_target(SESSION_A, [SESSION_B])


def test_refresh_rejects_a_different_instance_sharing_the_device_id():
    other_instance = {**SESSION_A, "instance_id": "HASHI2", "capability_id": "cap-C"}
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        DesktopSessionService._refresh_target(SESSION_A, [other_instance])


def test_refresh_reports_an_ambiguous_device_without_a_session_identity():
    # A device-only request cannot decide between two live sessions and must
    # never guess: the client re-discovers and sends the full identity.
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        DesktopSessionService._refresh_target({"device_id": "device-host"}, [SESSION_B, SESSION_A])


def test_refresh_verifies_optional_physical_and_interactive_identifiers():
    requested = {**SESSION_A, "physical_host_id": "host-1", "interactive_session_id": "sess-1"}
    # A mismatched physical host is rejected even though device and session match.
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        DesktopSessionService._refresh_target(requested, [{**SESSION_A, "physical_host_id": "host-2"}])
    # A live target that does not provide the identifier cannot falsify the
    # pinned (instance, device, session) identity and still resolves.
    assert DesktopSessionService._refresh_target(requested, [SESSION_A])["capability_id"] == "cap-A"
    # Matching identifiers resolve normally.
    resolved = DesktopSessionService._refresh_target(
        requested, [{**SESSION_A, "physical_host_id": "host-1", "interactive_session_id": "sess-1"}])
    assert resolved["user_session_id"] == "user-session-A"


def test_refresh_allows_capability_and_generation_change_for_the_pinned_session():
    newer = {**SESSION_A, "capability_id": "cap-A-new", "worker_generation": "gen-7"}
    resolved = DesktopSessionService._refresh_target(SESSION_A, [newer])
    assert resolved["capability_id"] == "cap-A-new"
    assert resolved["user_session_id"] == "user-session-A"


def test_refresh_reports_change_when_the_requested_session_disappears():
    # Session A is no longer registered anywhere; session B on the same device
    # must not be substituted.
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        DesktopSessionService._refresh_target(SESSION_A, [SESSION_B])


class SessionPinnedBroker:
    """Synthetic broker with two Windows sessions on one device."""

    def __init__(self):
        self.opened_with = None

    def desktop_targets(self):
        return [dict(SESSION_B), dict(SESSION_A)]

    async def invoke_desktop(self, target, *, actor_id, session_id, operation, args):
        if operation == "desktop_info":
            self.opened_with = dict(target)
            return {"displays": [{"id": "disp-a"}]}
        return {"ok": True}


async def test_open_binds_the_intended_session_on_a_shared_device():
    broker = SessionPinnedBroker()
    service = DesktopSessionService(lambda: broker)
    result = await service.run("owner-a", {"operation": "open", "client_id": "client-a",
                                           "target": dict(SESSION_A)})
    assert broker.opened_with["user_session_id"] == "user-session-A"
    assert broker.opened_with["capability_id"] == "cap-A"
    assert result["binding"]["user_session_id"] == "user-session-A"
    assert result["binding"]["capability_id"] == "cap-A"
    # The stored session record keeps the intended binding for later operations.
    record = service.sessions[result["session_id"]]
    assert record.binding["user_session_id"] == "user-session-A"


async def test_open_with_requested_session_gone_reports_change_without_worker_call():
    class Broker:
        def desktop_targets(self):
            return [dict(SESSION_B)]

        async def invoke_desktop(self, *_args, **_kwargs):
            raise AssertionError("must not reach the worker")

    service = DesktopSessionService(Broker)
    with pytest.raises(DesktopError, match="desktop_target_changed"):
        await service.run("owner-a", {"operation": "open", "client_id": "client-a",
                                      "target": dict(SESSION_A)})
    assert service.sessions == {}
