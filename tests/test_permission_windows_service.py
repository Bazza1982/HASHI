"""Observe SCM status ordering independently from payload and OS calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from orchestrator.permissions.windows_service import (
    ERROR_SERVICE_SPECIFIC_ERROR, INTERROGATE, RUNNING, SHUTDOWN,
    START_PENDING, STOP, STOPPED, STOP_PENDING, ServiceHost,
)


class RecordingSCM:
    def __init__(self):
        self.statuses = []
        self.started, self.running, self.stopping = [threading.Event() for _ in range(3)]
        self.control = None
        self.fail_state = None

    def dispatch(self, name, main):
        main()

    def register(self, name, control):
        self.control = control

    def set_status(self, status):
        if self.fail_state == status.state:
            self.fail_state = None
            raise OSError("SCM status unavailable")
        self.statuses.append(status)
        {START_PENDING: self.started, RUNNING: self.running,
         STOP_PENDING: self.stopping}.get(status.state, threading.Event()).set()


def test_scm_running_waits_for_ready_and_stopped_waits_for_cleanup():
    api = RecordingSCM()
    allow_start, allow_close, closing = [threading.Event() for _ in range(3)]

    async def serve(_path, *, stopped, on_ready):
        await asyncio.to_thread(allow_start.wait, 3)
        on_ready()
        await stopped.wait()
        closing.set()
        await asyncio.to_thread(allow_close.wait, 3)

    host = ServiceHost("HashiSafetyTest_lifecycle", serve, "private.json", api=api, checkpoint_seconds=.01)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(host.run)
        try:
            assert api.started.wait(2)
            assert not api.running.is_set()
            allow_start.set()
            assert api.running.wait(2)
            assert host.control(STOP) == 0
            assert closing.wait(2)
            assert all(item.state != STOPPED for item in api.statuses)
            # The SCM callback thread remains responsive while async cleanup waits.
            assert host.control(INTERROGATE) == 0
        finally:
            allow_start.set()
            allow_close.set()
        task.result(timeout=3)
    assert api.statuses[-1].state == STOPPED
    assert api.statuses[-1].win32_exit_code == 0
    assert [item.state for item in api.statuses if item.checkpoint <= 1][:3] == [
        START_PENDING, RUNNING, STOP_PENDING,
    ]


@pytest.mark.parametrize("failure", ["startup", "cleanup", "unexpected_exit", "status"])
def test_failures_never_publish_successful_stopped(failure):
    api = RecordingSCM()
    if failure == "status":
        api.fail_state = RUNNING

    async def serve(_path, *, stopped, on_ready):
        if failure == "startup":
            raise OSError("private payload startup failed")
        on_ready()
        if failure == "unexpected_exit":
            return
        await stopped.wait()
        if failure == "cleanup":
            raise OSError("child cleanup is unknown")

    host = ServiceHost("HashiRunnerTest_failure", serve, "private.json", api=api)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(host.run)
        if failure == "cleanup":
            assert api.running.wait(2)
            host.control(STOP)
        with pytest.raises(OSError, match="inspect SCM exit status"):
            task.result(timeout=3)
    assert api.statuses[-1].state == STOPPED
    assert api.statuses[-1].win32_exit_code == ERROR_SERVICE_SPECIFIC_ERROR
    assert api.statuses[-1].service_exit_code != 0


def test_stop_during_start_is_idempotent_and_never_transitions_to_running():
    api, release_start = RecordingSCM(), threading.Event()

    async def serve(_path, *, stopped, on_ready):
        await asyncio.to_thread(release_start.wait, 3)
        on_ready()  # A late payload must not undo the earlier SCM stop.
        assert stopped.is_set()

    host = ServiceHost("HashiSafetyTest_early_stop", serve, "private.json", api=api)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(host.run)
        try:
            assert api.started.wait(2)
            assert host.control(STOP) == host.control(SHUTDOWN) == 0
            before = list(api.statuses)
            assert host.control(128) == 120
            assert api.statuses == before
        finally:
            release_start.set()
        task.result(timeout=3)
    assert RUNNING not in [item.state for item in api.statuses]
    assert api.statuses[-1].state == STOPPED and api.statuses[-1].win32_exit_code == 0


def test_pending_checkpoints_continue_even_when_payload_blocks_the_event_loop():
    api, release = RecordingSCM(), threading.Event()

    async def serve(_path, *, stopped, on_ready):
        # Real installation readers are synchronous; SCM checkpoints must not
        # depend on that same asyncio loop making progress.
        release.wait(3)
        on_ready()
        await stopped.wait()

    host = ServiceHost("HashiRunnerTest_checkpoints", serve, "private.json",
                       api=api, checkpoint_seconds=.01)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(host.run)
        try:
            assert api.started.wait(2)
            observed = threading.Event()
            original = api.set_status

            def capture(status):
                original(status)
                if status.state == START_PENDING and status.checkpoint >= 3:
                    observed.set()

            api.set_status = capture
            assert observed.wait(2)
            assert host.control(STOP) == 0
        finally:
            release.set()
        task.result(timeout=3)
    assert any(item.checkpoint >= 3 for item in api.statuses)
    assert api.statuses[-1].state == STOPPED


def test_checkpoint_failure_requests_stop_and_remains_a_service_failure():
    api, release = RecordingSCM(), threading.Event()

    async def serve(_path, *, stopped, on_ready):
        await asyncio.to_thread(release.wait, 3)
        await stopped.wait()

    host = ServiceHost("HashiRunnerTest_status_failure", serve, "private.json",
                       api=api, checkpoint_seconds=.01)
    original = api.set_status

    def capture(status):
        if status.state == START_PENDING and status.checkpoint == 2:
            release.set()
            raise OSError("SCM lost")
        original(status)

    api.set_status = capture
    with pytest.raises(OSError, match="inspect SCM exit status"):
        host.run()
    assert api.statuses[-1].state == STOPPED
    assert api.statuses[-1].service_exit_code != 0


def test_invalid_name_is_rejected_before_dispatch():
    with pytest.raises(ValueError, match="explicit Windows service name"):
        ServiceHost("service name / not a command", None, "private.json")


def test_control_status_failure_is_preserved_after_payload_cleanup():
    api = RecordingSCM()

    async def serve(_path, *, stopped, on_ready):
        on_ready()
        await stopped.wait()

    host = ServiceHost("HashiSafetyTest_control_failure", serve, "private.json", api=api)
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(host.run)
        assert api.running.wait(2)
        api.fail_state = STOP_PENDING
        with pytest.raises(OSError, match="SCM status unavailable"):
            host.control(STOP)
        with pytest.raises(OSError, match="inspect SCM exit status"):
            task.result(timeout=3)
    assert api.statuses[-1].state == STOPPED and api.statuses[-1].service_exit_code != 0


def test_missing_scm_dispatch_does_not_claim_a_successful_service_run():
    api = RecordingSCM()
    api.dispatch = lambda *_args: None
    with pytest.raises(OSError, match="inspect SCM exit status"):
        ServiceHost("HashiRunnerTest_not_dispatched", None, "private.json", api=api).run()
