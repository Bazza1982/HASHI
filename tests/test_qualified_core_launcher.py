from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from scripts import run_qualified_core as launcher
from tools.instance_registry import InstanceRegistryError


def test_unqualified_pair_cannot_start_any_child(monkeypatch, capsys):
    def refuse(_root):
        raise InstanceRegistryError("unqualified pair")

    monkeypatch.setattr(launcher, "_select_core_runtime", refuse)
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda *a, **k: pytest.fail("unqualified Core must never start"))
    assert launcher.main(["--bridge-home", "unused"]) == 78
    assert "unqualified pair" in capsys.readouterr().err


def test_delegated_core_preserves_isolation_and_exit_status(monkeypatch, tmp_path):
    (tmp_path / "main.py").write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps(sys.executable))\nraise SystemExit(17)\n",
        encoding="utf-8")
    poison = tmp_path / "ambient"
    poison.mkdir()
    (poison / "json.py").write_text("raise RuntimeError('ambient import')\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(poison))
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_select_core_runtime",
                        lambda _root: [sys.executable, "-I", "-B"])
    witness = tmp_path / "executed.json"
    assert launcher.main([str(witness)]) == 17
    import json
    assert Path(json.loads(witness.read_text())) == Path(sys.executable)
    assert not list(poison.rglob("*.pyc"))


def test_delegated_signal_failure_is_preserved(monkeypatch):
    monkeypatch.setattr(launcher, "_select_core_runtime",
                        lambda _root: ["qualified-core", "-I", "-B"])
    monkeypatch.setattr(launcher.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=-15))
    assert launcher.main([]) == 143
# HASHI-Fingerprint: hfp_508497018ba24b989ef8f8d48e147b14
