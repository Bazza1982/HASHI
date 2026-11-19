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
