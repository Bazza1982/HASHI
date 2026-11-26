"""Rescue-hatch behaviour: interpreter selection and the stdlib fallback shell."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import resolve_tui_python  # noqa: E402
from tui import rescue_cli  # noqa: E402


def _write_agents(home: Path, *, instance_id: str = "HASHI9", port: int = 18899) -> None:
    (home / "agents.json").write_text(
        json.dumps({"global": {"instance_id": instance_id, "workbench_port": port}}),
        encoding="utf-8",
    )


def test_resolve_prefers_the_installer_runtime(tmp_path, monkeypatch):
    """Candidate order: Functions runtime, portable, venv, PATH."""

    monkeypatch.setattr(resolve_tui_python, "_functions_runtime", lambda root: "C:/rt/functions.exe")
    monkeypatch.setattr(resolve_tui_python, "_portable_python", lambda root: "C:/root/python.exe")
    monkeypatch.setattr(resolve_tui_python, "_venv_python", lambda root: "C:/root/.venv/python.exe")
    monkeypatch.setattr(resolve_tui_python, "_path_python", lambda: "C:/path/python.exe")

    executable, label = resolve_tui_python.resolve(tmp_path, probe=lambda exe: True)
    assert executable == "C:/rt/functions.exe"
    assert label == resolve_tui_python.LABEL_FUNCTIONS


def test_resolve_skips_an_interpreter_without_the_tui_packages(tmp_path, monkeypatch):
    """A broken venv must not win just because it exists."""

    monkeypatch.setattr(resolve_tui_python, "_functions_runtime", lambda root: None)
    monkeypatch.setattr(resolve_tui_python, "_portable_python", lambda root: None)
    monkeypatch.setattr(resolve_tui_python, "_venv_python", lambda root: "C:/root/.venv/python.exe")
    monkeypatch.setattr(resolve_tui_python, "_path_python", lambda: "C:/path/python.exe")

    executable, label = resolve_tui_python.resolve(
        tmp_path, probe=lambda exe: exe == "C:/path/python.exe"
    )
    assert executable == "C:/path/python.exe"
    assert label == resolve_tui_python.LABEL_PATH


def test_resolve_reports_failure_instead_of_guessing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(resolve_tui_python, "_functions_runtime", lambda root: None)
    monkeypatch.setattr(resolve_tui_python, "_portable_python", lambda root: "C:/root/python.exe")
    monkeypatch.setattr(resolve_tui_python, "_venv_python", lambda root: None)
    monkeypatch.setattr(resolve_tui_python, "_path_python", lambda: None)

    executable, label = resolve_tui_python.resolve(tmp_path, probe=lambda exe: False)
    assert executable is None
    assert label == ""

    code = resolve_tui_python.main(["--code-root", str(tmp_path)])
    assert code == 78
    stderr = capsys.readouterr().err
    assert "TUI needs only: textual, rich, aiohttp" in stderr
    assert "not a Core/Functions failure" in stderr


def test_diagnose_reports_a_broken_agents_json(tmp_path):
    home = tmp_path / "instance"
    home.mkdir()
    (home / "agents.json").write_text("{not json", encoding="utf-8")

    checks = {check["label"]: check for check in rescue_cli.diagnose(ROOT, home)}
    assert checks["agents.json"]["status"] == rescue_cli.CHECK_FAIL
    assert "invalid JSON" in checks["agents.json"]["detail"]
    # A broken config must not crash the diagnosis itself.
    assert "python" in checks
    assert "local backend" in checks


def test_diagnose_reads_the_instance_identity(tmp_path):
    home = tmp_path / "instance"
    home.mkdir()
    _write_agents(home)
    (home / "secrets.json").write_text("{}", encoding="utf-8")

    checks = {check["label"]: check for check in rescue_cli.diagnose(ROOT, home)}
    assert "HASHI9" in checks["agents.json"]["detail"]
    assert "18899" in checks["agents.json"]["detail"]
    assert checks["secrets.json"]["status"] == rescue_cli.CHECK_OK


def test_logs_tail_explains_when_nothing_was_logged(tmp_path):
    home = tmp_path / "instance"
    home.mkdir()
    text = rescue_cli.logs_tail(ROOT, home)
    assert "No launcher or bridge logs were found." in text


def test_logs_tail_returns_the_newest_lines(tmp_path):
    home = tmp_path / "instance"
    logs = home / "logs"
    logs.mkdir(parents=True)
    (logs / "bridge.log").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    text = rescue_cli.logs_tail(ROOT, home, lines=2)
    assert "three" in text and "four" in text
    assert "one" not in text


def test_format_checks_summarises_failures():
    report = rescue_cli.format_checks(
        [
            {"label": "python", "status": rescue_cli.CHECK_OK, "detail": "fine"},
            {"label": "tui packages", "status": rescue_cli.CHECK_FAIL, "detail": "missing textual"},
            {"label": "logs", "status": rescue_cli.CHECK_WARN, "detail": "none yet"},
        ]
    )
    assert "[fail] tui packages: missing textual" in report
    assert "1 failure(s), 1 warning(s)." in report


def test_command_catalog_degrades_instead_of_failing(monkeypatch):
    """Without the server registry the palette degrades; the TUI still starts."""

    import builtins

    from tui import command_catalog

    real_import = builtins.__import__

    def _fake(name, *args, **kwargs):
        if name == "orchestrator.command_registry":
            raise ImportError("No module named 'telegram'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake)
    commands, notice = command_catalog.load_runtime_command_info()
    assert commands == []
    assert notice.startswith("runtime_command_catalog_unavailable")


def test_instance_identity_falls_back_when_the_port_is_not_a_number(tmp_path):
    home = tmp_path / "instance"
    home.mkdir()
    (home / "agents.json").write_text(
        json.dumps({"global": {"instance_id": "hashi7", "workbench_port": "eighty"}}),
        encoding="utf-8",
    )
    instance_id, port, error = rescue_cli.instance_identity(home)
    assert instance_id == "HASHI7"
    assert port == rescue_cli.DEFAULT_PORT
    assert "not a number" in error


def _entry_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("tui_entry_rescue", ROOT / "tui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rescue_flag_enters_the_stdlib_shell(monkeypatch, capsys):
    """`python tui.py --rescue` must work even when the TUI cannot import."""

    entry = _entry_module()

    def _raise(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert entry.main(["--rescue"]) == 0
    output = capsys.readouterr().out
    assert "HASHI rescue shell (standard library only)" in output
    assert "requested with --rescue" in output


def test_entry_point_falls_back_when_the_tui_import_is_broken(monkeypatch, capsys):
    """A broken TUI module yields a rescue shell, not a traceback and exit."""

    entry = _entry_module()
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _broken(name, *args, **kwargs):
        if name == "tui.app":
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _broken)

    def _raise(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise)
    assert entry.main([]) == 0
    output = capsys.readouterr().out
    assert "TUI could not start" in output
    assert "textual" in output
# HASHI-Fingerprint: hfp_695d915b6fcf4830bc89b78ee2c975ab
