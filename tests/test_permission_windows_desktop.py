"""Lease and failure boundaries; native desktop/DLL evidence is separate."""
from copy import deepcopy
import json

import pytest

from orchestrator.permissions.windows_desktop import DesktopAclCoordinator

A = "S-1-15-2-1-2-3-4-5-6-7"
B = "S-1-15-2-11-12-13-14-15-16-17"


class StationAPI:
    """Arrange native object state, not a simulated LPAC or DLL success."""
    def __init__(self, *, visible=False):
        self.description = {"name": "Service-0x0-12345$", "visible": visible, "session_id": 0}
        self.security = {"existing": ["SYSTEM", "service-owner"], "tasks": []}
        self.desktops = {}
        self.fail_create = self.fail_close = False
        self.writes = 0

    def describe(self):
        return dict(self.description)

    def read_security(self):
        return json.dumps(self.security, sort_keys=True).encode()

    def with_station_read(self, original, sid):
        value = json.loads(original)
        if sid in value["tasks"]:
            raise PermissionError("pre-existing task ACE")
        value["tasks"].append(sid)
        return json.dumps(value, sort_keys=True).encode()

    def write_security(self, value):
        self.security = json.loads(value)
        self.writes += 1
        return self.read_security()

    def create_desktop(self, name, sid):
        if self.fail_create:
            raise OSError("desktop creation failure")
        handle = object()
        self.desktops[handle] = (name, sid)
        return handle

    def close_desktop(self, handle):
        if self.fail_close:
            raise OSError("desktop close failure")
        del self.desktops[handle]


def test_distinct_leases_keep_the_other_task_live_and_restore_the_original_acl():
    api = StationAPI()
    original = api.read_security()
    owner = DesktopAclCoordinator(api)
    first, second = owner.open(A), owner.open(B)
    assert first.name != second.name
    assert first.name.startswith(api.description["name"] + "\\HashiTask_")
    first.close()
    assert api.security["tasks"] == [B]
    assert list(api.desktops.values()) == [(second.desktop_name, B)]
    second.close()
    assert api.read_security() == original and not api.desktops
    second.close()
    assert api.read_security() == original


def test_external_acl_change_is_preserved_and_blocks_new_acquisition():
    api = StationAPI()
    owner = DesktopAclCoordinator(api)
    lease = owner.open(A)
    api.security["existing"].append("independent-admin-change")
    changed = deepcopy(api.security)
    with pytest.raises(OSError, match="changed"):
        lease.close()
    assert api.security == changed
    assert not api.desktops
    with pytest.raises(OSError, match="changed"):
        owner.open(B)
    assert api.security == changed and not api.desktops


def test_failed_desktop_creation_releases_only_its_own_station_grant():
    api = StationAPI()
    owner = DesktopAclCoordinator(api)
    first = owner.open(A)
    expected = api.read_security()
    api.fail_create = True
    with pytest.raises(OSError, match="creation"):
        owner.open(B)
    assert api.read_security() == expected
    assert list(api.desktops.values()) == [(first.desktop_name, A)]
    api.fail_create = False
    first.close()
    assert not api.security["tasks"] and not api.desktops


def test_unverified_desktop_close_retains_the_lease_until_actual_cleanup():
    api = StationAPI()
    owner = DesktopAclCoordinator(api)
    lease = owner.open(A)
    api.fail_close = True
    with pytest.raises(OSError, match="close"):
        lease.close()
    assert api.security["tasks"] == [A] and len(api.desktops) == 1
    api.fail_close = False
    lease.close()
    assert not api.security["tasks"] and not api.desktops


@pytest.mark.parametrize("description", [
    {"name": "WinSta0", "visible": True, "session_id": 1},
    {"name": "OtherStation", "visible": False, "session_id": 0},
    {"name": "Service-0x0-12345$", "visible": False, "session_id": 1},
])
def test_interactive_or_unverified_stations_are_never_modified(description):
    api = StationAPI()
    api.description = description
    with pytest.raises(PermissionError):
        DesktopAclCoordinator(api).open(A)
    assert api.writes == 0 and not api.desktops


def test_process_stop_keeps_the_desktop_until_the_job_is_confirmed_empty():
    from types import SimpleNamespace
    from orchestrator.permissions.windows_process import OwnedWindowsProcess

    api = StationAPI()
    lease = DesktopAclCoordinator(api).open(A)
    class Job:
        blocked = True
        def stop(self, **options):
            if self.blocked:
                raise OSError("Job still contains a process")
    job = Job()
    kernel = SimpleNamespace(CloseHandle=lambda handle: True)
    process = OwnedWindowsProcess(SimpleNamespace(process=34, thread=12),
                                  job, None, kernel, desktop=lease)
    with pytest.raises(OSError, match="Job"):
        process.stop()
    assert api.security["tasks"] == [A] and len(api.desktops) == 1
    job.blocked = False
    process.stop()
    assert not api.security["tasks"] and not api.desktops
    process.stop()


def test_process_handle_cleanup_does_not_hide_an_unclosed_desktop():
    from types import SimpleNamespace
    from orchestrator.permissions.windows_process import WindowsProcessCleanupFailed

    api = StationAPI()
    lease = DesktopAclCoordinator(api).open(A)
    handles = SimpleNamespace(process=34, thread=12)
    kernel = SimpleNamespace(WaitForSingleObject=lambda *args: 0,
                             CloseHandle=lambda handle: True)
    failure = WindowsProcessCleanupFailed(handles, SimpleNamespace(stop=lambda **options: None), kernel)
    failure._desktop = lease
    api.fail_close = True
    with pytest.raises(OSError, match="close"):
        failure.retry_cleanup()
    assert handles.process is None and handles.thread is None
    assert api.security["tasks"] == [A] and len(api.desktops) == 1
    api.fail_close = False
    failure.retry_cleanup()
    assert not api.security["tasks"] and not api.desktops
# HASHI-Fingerprint: hfp_8152b6a434bb46cd8553e6cf628af4e0
