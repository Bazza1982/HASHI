from __future__ import annotations

import json
import sys

import pytest

from remote.restart_runtime import qualified_restart_runtime, selected_restart_python


def test_restart_precheck_preserves_selected_functions_venv_launcher(
    tmp_path, monkeypatch
):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hashi.runtime]\ncore-host-lock = "core.lock"\n', encoding="utf-8"
    )
    selected = tmp_path / "runtime" / "functions" / "Scripts" / "python.exe"
    selected.parent.mkdir(parents=True)
    selected.write_bytes(b"launcher")
    monkeypatch.setattr(
        "scripts.hashi_instance_cli._prepared_role_candidates",
        lambda root: [[str(selected)]],
    )

    assert selected_restart_python(tmp_path) == str(selected)


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
        lambda root, with_functions=False: (
            [sys.executable, "-I", "-B"], [sys.executable]
        ),
    )
    if reply == "qualified":
        assert qualified_restart_runtime(tmp_path) == functions
    else:
        with pytest.raises(ValueError):
            qualified_restart_runtime(tmp_path)


def test_legacy_restart_does_not_invent_a_v5_selection(tmp_path):
    assert qualified_restart_runtime(tmp_path) is None


def test_restart_qualification_rejects_selector_change_after_precheck(
    tmp_path, monkeypatch
):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hashi.runtime]\ncore-host-lock = "core.lock"\n', encoding="utf-8"
    )
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check_runtime_selection.py").write_text("pass\n", encoding="utf-8")
    first = tmp_path / "first-python.exe"
    second = tmp_path / "second-python.exe"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    monkeypatch.setattr(
        "scripts.hashi_instance_cli._select_core_runtime",
        lambda root, with_functions=False: (
            [sys.executable, "-I", "-B"], [str(second)]
        ),
    )

    with pytest.raises(ValueError, match="changed after fixed-task precheck"):
        qualified_restart_runtime(
            tmp_path, expected_functions_python=str(first)
        )
# HASHI-Fingerprint: hfp_1bd3321f5bac4f2d98cc02f7da71c220
