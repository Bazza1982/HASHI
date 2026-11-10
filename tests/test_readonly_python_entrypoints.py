from __future__ import annotations

import subprocess
import sys

from scripts import hashi_instance_cli as cli
from tools import ocr


def test_runtime_probe_keeps_the_candidate_installation_readonly(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check_runtime_contract.py").write_text(
        "import sys\nraise SystemExit(0 if sys.dont_write_bytecode else 73)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_runtime_candidates", lambda root: [[sys.executable]])
    assert cli._select_runtime(tmp_path) == [sys.executable, "-B"]


def test_ocr_readiness_probe_keeps_the_candidate_installation_readonly(monkeypatch):
    run = subprocess.run
    monkeypatch.setattr(ocr, "_paddle_python_override", lambda: sys.executable)
    def probe(command, **kwargs):
        index = command.index("-c")
        return run(
            [*command[:index], "-c",
             "import sys; raise SystemExit(0 if sys.dont_write_bytecode else 73)"],
            **kwargs,
        )
    monkeypatch.setattr(ocr.subprocess, "run", probe)
    assert ocr._paddle_runtime_ready()
# HASHI-Fingerprint: hfp_df059b78ff6347899079521ba0dd021c
