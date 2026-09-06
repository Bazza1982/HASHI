from __future__ import annotations

import argparse
import json

from tools import memory_plus_auto_debug as debug


def test_run_handles_initially_missing_notepad_and_uses_agent_report_dir(
    tmp_path, monkeypatch
):
    agent = "agent1"
    memory_dir = tmp_path / "workspaces" / agent / "memory"
    notepad = memory_dir / "memory_plus_notepad.md"
    calls = 0

    monkeypatch.setattr(debug, "ROOT", tmp_path)
    monkeypatch.setattr(
        debug,
        "_json_get",
        lambda *_args, **_kwargs: {"ok": True, "agents": [agent]},
    )

    def send_round(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            memory_dir.mkdir(parents=True)
            notepad.write_text("jasmine rice\neggplant\n", encoding="utf-8")
        return {
            "ok": True,
            "request_id": f"request-{calls}",
            "text": "jasmine rice and eggplant",
        }

    monkeypatch.setattr(debug, "_send_round", send_round)

    args = argparse.Namespace(
        agent=agent,
        base_url="http://127.0.0.1:18800",
        mode="run",
        dry_run=False,
        timeout_s=1,
        reset_wait_s=0,
        reset_before_seed=False,
        reset_after_seed=False,
        label="jasmine rice",
        shelf="eggplant",
        label_alias=[],
        shelf_alias=[],
        report_dir=None,
    )

    assert debug.run(args) == 0

    reports = list((tmp_path / "workspaces" / agent / "reports").glob("*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["pass"] is True
    assert payload["notepad_delta"] == "jasmine rice\neggplant\n"
