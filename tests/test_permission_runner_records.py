"""Runner reservations survive preparation failure without touching other data.

Native observers/profile removal are injected; these tests do not qualify OS
isolation and never create a profile, account or execution process.
"""
from dataclasses import replace
from pathlib import Path

import pytest

from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.permissions.runner_records import RunnerRecords
from orchestrator.permissions.windows_process import ProcessObservation
from tests.test_permission_pao_fence import setup_run


def environment(tmp_path):
    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    root = tmp_path / "runner"
    root.mkdir()
    jobs, profiles = [], []
    def stopped(name, sid, **kwargs):
        jobs.append((name, sid, kwargs))
        return {"zero_job": True}
    records = RunnerRecords(
        root, instance_id="INSTANCE", runner_sid="S-1-5-21-1", recover_job=stopped,
        observe_stopped=lambda process: True, remove_profile=lambda name: profiles.append(name),
    )
    return binding, records, jobs, profiles


def test_reserved_profile_and_job_recover_before_any_process_observation(tmp_path):
    binding, records, jobs, profiles = environment(tmp_path)
    value = records.reserve(binding)
    scratch = Path(value["scratch"])
    scratch.mkdir()
    (scratch / "owned").write_text("temporary task data", encoding="utf-8")
    sibling = records.root / "unrelated"
    sibling.mkdir()
    (sibling / "keep").write_text("preserve", encoding="utf-8")
    records.recover_all()
    assert len(jobs) == 1 and jobs[0][0] == value["job_name"]
    assert profiles == [value["profile"]]
    assert not scratch.exists() and (sibling / "keep").read_text(encoding="utf-8") == "preserve"
    assert not records.pending()
    with pytest.raises(ValueError, match="cannot be created again"):
        records.reserve(binding)


def test_failed_profile_cleanup_remains_owned_for_retry(tmp_path):
    binding, records, _, profiles = environment(tmp_path)
    value = records.reserve(binding)
    def unavailable(name):
        raise OSError("profile removal remains unverified")
    records._remove_profile = unavailable
    with pytest.raises(OSError):
        records.retire(binding)
    assert len(records.pending()) == 1
    records._remove_profile = lambda name: profiles.append(name)
    assert records.retire(binding) is True
    assert profiles == [value["profile"]] and not records.pending()


def test_wrong_instance_or_unrestricted_observation_cannot_bind_a_host(tmp_path):
    binding, records, _, _ = environment(tmp_path)
    foreign = replace(binding, context=replace(binding.context, instance_id="OTHER"))
    with pytest.raises(ValueError, match="another instance"):
        records.reserve(foreign)
    records.reserve(binding)
    wrong = ProcessObservation(100, "S-1-5-21-1", None, 0, False, False, True, "a" * 64, 12345)
    with pytest.raises(ValueError, match="restricted host"):
        records.observe(binding, wrong)
    assert records.pending()[0][1]["observation"] is None
