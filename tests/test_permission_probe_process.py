"""The disposable sentinel runs only under its exact suspended test identity."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.permissions.windows_probe_process import ChildFacts, ProbeCleanupUnverified, run_owned_sentinel
from scripts.permissions.windows_probe_staging import StageError


class FakeProcessAPI:
    def __init__(self, sid):
        self.events = []
        self.child_facts = ChildFacts(sid, False, False, True)
        self.exit_code = 0
        self.write_result = True
        self.did_stop = True
        self.job_close_error = False
        self.sentinel = None
        self.nonce = None

    def create_job(self):
        self.events.append("create_job")
        return object()

    def start_suspended(self, identity, executable, scratch, sentinel, nonce, job):
        self.events.append("start_suspended")
        self.sentinel, self.nonce = sentinel, nonce
        return object()

    def facts(self, process, job):
        self.events.append("facts")
        return self.child_facts

    def resume(self, process):
        self.events.append("resume")
        if self.write_result:
            self.sentinel.write_bytes(self.nonce)

    def wait(self, process, milliseconds):
        self.events.append("wait")
        return self.exit_code

    def close_job(self, job):
        self.events.append("close_job")
        if self.job_close_error:
            raise OSError("injected job close failure")

    def stopped(self, process, milliseconds):
        self.events.append("stopped")
        return self.did_stop

    def close_process(self, process):
        self.events.append("close_process")


@pytest.fixture
def running_setup(tmp_path):
    python = tmp_path / "python"
    scratch = tmp_path / "scratch"
    python.mkdir()
    scratch.mkdir()
    (python / "python.exe").write_bytes(b"test executable")
    sid = "S-1-5-21-1-2-3-1101"
    identity = SimpleNamespace(name="HashiProbe_1234abcd", password="test password", sid=sid)
    stage = SimpleNamespace(
        sid=sid, python=python, scratch=scratch, _handle=object(), _check_identity=lambda: None,
    )
    return identity, stage, FakeProcessAPI(sid)


def test_sentinel_runs_after_job_and_identity_are_checked(running_setup):
    identity, stage, api = running_setup
    run_owned_sentinel(api, identity, stage)
    assert api.events == [
        "create_job", "start_suspended", "facts", "resume", "wait",
        "close_job", "stopped", "close_process",
    ]
    assert len(list(Path(stage.scratch).glob("sentinel-*.bin"))) == 1


@pytest.mark.parametrize("facts", [
    ChildFacts("S-1-5-21-1-2-3-9999", False, False, True),
    ChildFacts("S-1-5-21-1-2-3-1101", True, False, True),
    ChildFacts("S-1-5-21-1-2-3-1101", False, True, True),
    ChildFacts("S-1-5-21-1-2-3-1101", False, False, False),
])
def test_wrong_child_identity_or_job_is_never_resumed(running_setup, facts):
    identity, stage, api = running_setup
    api.child_facts = facts
    with pytest.raises(StageError, match="wrong identity or job"):
        run_owned_sentinel(api, identity, stage)
    assert "resume" not in api.events
    assert api.events[-3:] == ["close_job", "stopped", "close_process"]


def test_timeout_closes_owned_job_and_checks_the_child_stopped(running_setup):
    identity, stage, api = running_setup
    api.exit_code = None
    with pytest.raises(StageError, match="timed out"):
        run_owned_sentinel(api, identity, stage)
    assert api.events[-3:] == ["close_job", "stopped", "close_process"]


def test_failed_or_unknown_cleanup_is_never_reported_as_success(running_setup):
    identity, stage, api = running_setup
    api.did_stop = False
    with pytest.raises(ProbeCleanupUnverified, match="cleanup is unverified"):
        run_owned_sentinel(api, identity, stage)
    assert api.events[-1] == "close_process"


def test_job_close_failure_is_never_reported_as_success(running_setup):
    identity, stage, api = running_setup
    api.job_close_error = True
    with pytest.raises(ProbeCleanupUnverified, match="cleanup is unverified"):
        run_owned_sentinel(api, identity, stage)
    assert api.events[-1] == "close_process"


def test_missing_child_result_is_not_success(running_setup):
    identity, stage, api = running_setup
    api.write_result = False
    with pytest.raises(StageError, match="result is unavailable"):
        run_owned_sentinel(api, identity, stage)


def test_stage_and_identity_must_be_bound_before_process_creation(running_setup):
    identity, stage, api = running_setup
    stage.sid = "S-1-5-21-9-9-9-9999"
    with pytest.raises(StageError):
        run_owned_sentinel(api, identity, stage)
    assert api.events == []


def test_windows_start_failure_closes_only_the_owned_job(running_setup):
    identity, stage, api = running_setup

    def fail_start(*args):
        api.events.append("start_suspended")
        raise StageError("required Windows privilege unavailable")

    api.start_suspended = fail_start
    with pytest.raises(StageError, match="privilege unavailable"):
        run_owned_sentinel(api, identity, stage)
    assert api.events == ["create_job", "start_suspended", "close_job"]
