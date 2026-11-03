"""Job views report file/schema failures and never mutate schedule declarations."""
from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_jobs
from orchestrator.skill_manager import SkillManager


def listing(manager, buttons):
    if buttons:
        text, markup = runtime_jobs._build_jobs_with_buttons(
            SimpleNamespace(_ui_callback_tokens={}), "qa", manager, filter_agent="qa",
        )
        return text, markup
    return runtime_jobs._build_jobs_text("qa", manager), None


@pytest.mark.parametrize("buttons", [False, True])
@pytest.mark.parametrize("raw", [
    b'{"heartbeats":', b'[]', b'{"heartbeats":{}}',
    b'{"heartbeats":[{"agent":"qa"}]}',
    b'{"heartbeats":[{"id":"qa-task","agent":"qa","interval_seconds":"bad"}]}',
])
def test_invalid_job_data_has_parse_reason_and_no_action_buttons(tmp_path, buttons, raw):
    path = tmp_path / "tasks.json"
    path.write_bytes(raw)
    manager = SkillManager(tmp_path, path)
    text, markup = listing(manager, buttons)
    assert "jobs_parse_configuration_invalid" in text and "operr_" in text
    assert markup is None and "No scheduled jobs" not in text
    assert path.read_bytes() == raw


@pytest.mark.parametrize("buttons", [False, True])
def test_job_file_access_failure_is_distinct_from_bad_json(tmp_path, monkeypatch, buttons):
    path = tmp_path / "tasks.json"
    path.write_text('{"heartbeats":[],"crons":[]}', encoding="utf-8")
    manager = SkillManager(tmp_path, path)
    original = Path.open
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError(errno.EACCES, "private-job-path-and-content")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    text, markup = listing(manager, buttons)
    assert "jobs_read_access_denied" in text and "operr_" in text
    assert "private-" not in text and markup is None


@pytest.mark.parametrize("buttons", [False, True])
def test_job_views_read_bom_and_do_not_migrate_legacy_heartbeat_records(tmp_path, monkeypatch, buttons):
    path = tmp_path / "tasks.json"
    raw = json.dumps({
        "version": 1, "heartbeats": [{
            "id": "qa-active-heartbeat", "agent": "qa", "enabled": False,
            "interval_seconds": 600, "managed_by": "active-command",
        }], "crons": [],
    }).encode("utf-8-sig")
    path.write_bytes(raw)
    manager = SkillManager(tmp_path, path)
    monkeypatch.setattr(manager, "_save_json", lambda *a, **kw: pytest.fail("Listing must not migrate/write schedules"))
    text, _ = listing(manager, buttons)
    assert "qa-active-heartbeat" in text
    assert path.read_bytes() == raw
    assert sorted(item.name for item in tmp_path.iterdir()) == ["tasks.json"]


@pytest.mark.parametrize("buttons", [False, True])
def test_missing_job_file_remains_an_empty_read_without_creating_a_file(tmp_path, buttons):
    path = tmp_path / "tasks.json"
    manager = SkillManager(tmp_path, path)
    text, markup = listing(manager, buttons)
    assert "operr_" not in text and markup is None
    assert not list(tmp_path.iterdir())
