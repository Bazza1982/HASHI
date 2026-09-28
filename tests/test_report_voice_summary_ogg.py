from __future__ import annotations

import argparse
import os
from pathlib import Path
from types import SimpleNamespace

from tools import report_voice_summary_ogg


def test_send_telegram_injects_project_root_into_child_pythonpath(
    tmp_path: Path, monkeypatch
):
    captured: dict[str, object] = {}
    existing_pythonpath = os.pathsep.join(("existing-one", "existing-two"))
    monkeypatch.setenv("PYTHONPATH", existing_pythonpath)

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(report_voice_summary_ogg.subprocess, "run", fake_run)
    args = argparse.Namespace(
        telegram_type="voice",
        telegram_caption="",
        telegram_chat_id=None,
        telegram_agent="sunny",
    )

    report_voice_summary_ogg.send_telegram(tmp_path / "summary.ogg", args)

    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PYTHONPATH"].split(os.pathsep) == [
        str(report_voice_summary_ogg.ROOT),
        "existing-one",
        "existing-two",
    ]
    assert env["HASHI_AGENT_NAME"] == "sunny"
    assert captured["cwd"] == str(report_voice_summary_ogg.ROOT)
