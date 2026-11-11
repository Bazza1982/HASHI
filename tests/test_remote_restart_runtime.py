from __future__ import annotations

import json
import sys

import pytest

from remote.restart_runtime import qualified_restart_runtime


@pytest.mark.parametrize("reply", ["qualified", "rejected", "malformed", "wrong-role"])
def test_restart_destination_runs_qualified_isolated_checker(tmp_path, monkeypatch, reply):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hashi.runtime]\ncore-host-lock = "core.lock"\n', encoding="utf-8"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    functions = {"role": "functions", "core_source_digest": "sha256:approved"}
    if reply == "wrong-role":
        functions["role"] = "core-host"
    checker = (
        "import sys\nassert sys.flags.isolated and sys.dont_write_bytecode\n"
        + ("raise SystemExit(78)\n" if reply == "rejected" else
           "print('not-json')\n" if reply == "malformed" else
           f"print({json.dumps({'functions': functions})!r})\n")
    )
    (scripts / "check_runtime_selection.py").write_text(checker, encoding="utf-8")
    monkeypatch.setattr(
        "scripts.hashi_instance_cli._select_core_runtime",
        lambda root: [sys.executable, "-I", "-B"],
    )
    if reply == "qualified":
        assert qualified_restart_runtime(tmp_path) == functions
    else:
        with pytest.raises(ValueError):
            qualified_restart_runtime(tmp_path)


def test_legacy_restart_does_not_invent_a_v5_selection(tmp_path):
    assert qualified_restart_runtime(tmp_path) is None
# HASHI-Fingerprint: hfp_4dfd9a7a482f4a66a1fb7a1367fc67e9
