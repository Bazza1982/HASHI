"""Rescue-hatch behaviour: interpreter selection and the stdlib fallback shell."""
from __future__ import annotations

import json
import subprocess
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

    monkeypatch.setattr(resolve_tui_python, "_functions_runtime", lambda root: ["C:/rt/functions.exe"])
    monkeypatch.setattr(resolve_tui_python, "_portable_python", lambda root: "C:/root/python.exe")
    monkeypatch.setattr(resolve_tui_python, "_venv_python", lambda root: "C:/root/.venv/python.exe")
    monkeypatch.setattr(resolve_tui_python, "_path_python", lambda: "C:/path/python.exe")

    executable, label = resolve_tui_python.resolve(tmp_path, probe=lambda exe: True)
    assert executable == "C:/rt/functions.exe"
    assert label == resolve_tui_python.LABEL_FUNCTIONS


def test_resolve_skips_an_interpreter_without_the_tui_packages(tmp_path, monkeypatch):
    """A broken venv must not win just because it exists."""

    monkeypatch.setattr(resolve_tui_python, "_functions_runtime", lambda root: [])
    monkeypatch.setattr(resolve_tui_python, "_portable_python", lambda root: None)
    monkeypatch.setattr(resolve_tui_python, "_venv_python", lambda root: "C:/root/.venv/python.exe")
    monkeypatch.setattr(resolve_tui_python, "_path_python", lambda: "C:/path/python.exe")

    executable, label = resolve_tui_python.resolve(
        tmp_path, probe=lambda exe: exe == "C:/path/python.exe"
    )
    assert executable == "C:/path/python.exe"
    assert label == resolve_tui_python.LABEL_PATH


def test_resolve_reports_failure_instead_of_guessing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(resolve_tui_python, "_functions_runtime", lambda root: [])
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


def test_installer_candidate_survives_a_broken_server_contract(tmp_path, monkeypatch):
    """A broken Core/Functions contract must not hide the TUI interpreter.

    Discovery reuses the installer catalogue only; the server contract is a
    separate question and must never remove a candidate the TUI can use.
    """

    from scripts import hashi_instance_cli

    executable = tmp_path / "functions-python.exe"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        hashi_instance_cli, "_prepared_role_candidates", lambda root: [[str(executable)]]
    )

    def _broken_contract(*_args, **_kwargs):
        raise RuntimeError("no installer-qualified independent Core/Functions pair is available")

    monkeypatch.setattr(hashi_instance_cli, "_select_core_runtime", _broken_contract)

    assert resolve_tui_python._functions_runtime(tmp_path) == [str(executable)]


def _stub_core_runtime(monkeypatch, executable: str = "C:/core/python.exe"):
    """Keep diagnosis tests off the real (slow, machine-specific) selector."""

    monkeypatch.setattr(
        rescue_cli, "core_runtime", lambda root: ([executable, "-I", "-B"], "")
    )


def test_diagnose_reports_a_broken_agents_json(tmp_path, monkeypatch):
    _stub_core_runtime(monkeypatch)
    home = tmp_path / "instance"
    home.mkdir()
    (home / "agents.json").write_text("{not json", encoding="utf-8")

    checks = {check["label"]: check for check in rescue_cli.diagnose(ROOT, home)}
    assert checks["agents.json"]["status"] == rescue_cli.CHECK_FAIL
    assert "invalid JSON" in checks["agents.json"]["detail"]
    # A broken config must not crash the diagnosis itself.
    assert "python" in checks
    assert "local backend" in checks


def test_diagnose_reads_the_instance_identity(tmp_path, monkeypatch):
    _stub_core_runtime(monkeypatch)
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


# --- The launchers start the resolver from outside the repository ----------


def test_resolver_imports_the_checkout_when_started_from_elsewhere(tmp_path):
    """A shortcut runs ``scripts/resolve_tui_python.py`` with only ``scripts/``
    on ``sys.path``; the installer runtime must still be found."""

    probe = f"""
import importlib.util, sys
from pathlib import Path
root = Path({str(ROOT)!r})
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != root]
spec = importlib.util.spec_from_file_location(
    "resolver", root / "scripts" / "resolve_tui_python.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module._functions_runtime(root)
print("scripts.hashi_instance_cli" in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert completed.stdout.strip().splitlines()[-1] == "True", completed.stderr[-2000:]


# --- Onboarding uses the same launch and rescue path -----------------------


def _onboarding_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tui_onboarding_entry", ROOT / "tui_onboarding.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _eof(_prompt=""):
    raise EOFError


def test_onboarding_falls_back_to_the_same_rescue_shell(monkeypatch, capsys):
    """A missing TUI package must not turn onboarding into a traceback."""

    onboarding = _onboarding_module()
    real_import = __import__

    def _broken(name, *args, **kwargs):
        if name == "tui.app":
            raise ImportError("No module named 'textual'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _broken)
    monkeypatch.setattr("builtins.input", _eof)
    assert onboarding.main() == 0
    output = capsys.readouterr().out
    assert "HASHI rescue shell (standard library only)" in output
    assert "textual" in output


# --- First install, legacy defaults and rejected endpoints ----------------


def _stub_app(monkeypatch, app_class):
    """Stand in for ``tui.app`` so the launch path runs without textual."""

    import types

    stub = types.ModuleType("tui.app")
    stub.HASHITuiApp = app_class
    monkeypatch.setitem(sys.modules, "tui.app", stub)


def test_first_install_enters_the_configuration_wizard(monkeypatch, tmp_path):
    """No agents.json is a first install: the wizard runs, not the rescue shell."""

    entry = _entry_module()
    monkeypatch.delenv("HASHI_LOCAL_ENDPOINT_FILE", raising=False)
    monkeypatch.delenv("HASHI_WORKBENCH_URL", raising=False)
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))

    recorded = {}

    class _FakeApp:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def run(self):
            recorded["ran"] = True

    _stub_app(monkeypatch, _FakeApp)
    assert entry.main([]) == 0
    assert recorded["ran"] is True
    assert recorded["onboarding_mode"] is True
    assert recorded["launch_instance_id"] == "HASHI"
    assert recorded["workbench_url"] == "http://127.0.0.1:18800"


def test_legacy_config_without_identity_fields_still_starts(monkeypatch, tmp_path):
    """A configuration that predates these fields keeps its legal defaults."""

    entry = _entry_module()
    (tmp_path / "agents.json").write_text(json.dumps({"global": {}}), encoding="utf-8")
    monkeypatch.delenv("HASHI_LOCAL_ENDPOINT_FILE", raising=False)
    monkeypatch.delenv("HASHI_WORKBENCH_URL", raising=False)
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))

    recorded = {}

    class _FakeApp:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def run(self):
            recorded["ran"] = True

    _stub_app(monkeypatch, _FakeApp)
    assert entry.main([]) == 0
    assert recorded["launch_instance_id"] == "HASHI"
    assert recorded["workbench_url"] == "http://127.0.0.1:18800"
    assert recorded["onboarding_mode"] is False


def test_failed_endpoint_verification_stays_offline(monkeypatch, capsys, tmp_path):
    """A rejected endpoint is never promoted into a guessed online session."""

    entry = _entry_module()
    _stub_app(monkeypatch, object)
    _write_agents(tmp_path, instance_id="hashi9", port=18899)
    endpoint = tmp_path / "elsewhere" / "local-endpoint.json"
    endpoint.parent.mkdir()
    endpoint.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))
    monkeypatch.setenv("HASHI_LOCAL_ENDPOINT_FILE", str(endpoint))
    monkeypatch.setenv("HASHI_WORKBENCH_URL", "http://127.0.0.1:18899")
    monkeypatch.setattr("builtins.input", _eof)

    assert entry.main([]) == 0
    output = capsys.readouterr().out
    assert "identity could not be verified" in output
    assert "refusing to connect to a guessed instance" in output
    assert "rescue shell" in output


def test_verified_identity_uses_the_declared_port(monkeypatch, tmp_path):
    entry = _entry_module()
    _write_agents(tmp_path, instance_id="hashi9", port=18899)
    monkeypatch.delenv("HASHI_LOCAL_ENDPOINT_FILE", raising=False)
    monkeypatch.delenv("HASHI_WORKBENCH_URL", raising=False)
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))

    instance_id, urls, primary = entry._get_workbench_urls()
    assert instance_id == "HASHI9"
    assert primary == "http://127.0.0.1:18899"
    assert urls == [primary]


def test_entry_point_passes_the_verified_instance_to_the_app(monkeypatch, tmp_path):
    entry = _entry_module()
    _write_agents(tmp_path, instance_id="hashi9", port=18899)
    monkeypatch.delenv("HASHI_LOCAL_ENDPOINT_FILE", raising=False)
    monkeypatch.delenv("HASHI_WORKBENCH_URL", raising=False)
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))

    recorded = {}

    class _FakeApp:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def run(self):
            recorded["ran"] = True

    _stub_app(monkeypatch, _FakeApp)
    assert entry.main([]) == 0
    assert recorded["ran"] is True
    assert recorded["workbench_url"] == "http://127.0.0.1:18899"
    assert recorded["launch_instance_id"] == "HASHI9"
    assert recorded["onboarding_mode"] is False


def test_onboarding_reaches_the_shared_launch_in_onboarding_mode(monkeypatch, tmp_path):
    onboarding = _onboarding_module()
    _write_agents(tmp_path, instance_id="hashi9", port=18899)
    monkeypatch.delenv("HASHI_LOCAL_ENDPOINT_FILE", raising=False)
    monkeypatch.delenv("HASHI_WORKBENCH_URL", raising=False)
    monkeypatch.setenv("BRIDGE_HOME", str(tmp_path))

    recorded = {}

    class _FakeApp:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def run(self):
            recorded["ran"] = True

    _stub_app(monkeypatch, _FakeApp)
    assert onboarding.main() == 0
    assert recorded["onboarding_mode"] is True
    assert recorded["workbench_url"] == "http://127.0.0.1:18899"


# --- A corrupt encoding must be reported, not raise -----------------------


def test_undecodable_config_keeps_the_rescue_shell_alive(tmp_path, monkeypatch, capsys):
    home = tmp_path / "instance"
    home.mkdir()
    (home / "agents.json").write_bytes(b"\xff\xfe{\x00b\x00a\x00d\x00")

    document, error = rescue_cli._read_json(home / "agents.json")
    assert document == {}
    assert "UTF-8" in error

    monkeypatch.setattr("builtins.input", _eof)
    assert rescue_cli.run(ROOT, home, "test") == 0
    output = capsys.readouterr().out
    assert "agents.json problem" in output
    assert "UTF-8" in output


# --- Health answers must belong to this instance --------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_health_rejects_an_answer_from_another_instance(monkeypatch):
    payloads = {"body": {"ok": True, "instance_id": "HASHI4"}}
    monkeypatch.setattr(
        rescue_cli.urllib.request,
        "urlopen",
        lambda url, timeout=0: _FakeResponse(payloads["body"]),
    )

    status, detail = rescue_cli._health(18806, expected_instance="HASHI5")
    assert status == rescue_cli.CHECK_FAIL
    assert "HASHI4" in detail and "HASHI5" in detail

    status, _detail = rescue_cli._health(18806, expected_instance="HASHI4")
    assert status == rescue_cli.CHECK_OK

    payloads["body"] = {"ok": True}
    status, detail = rescue_cli._health(18806, expected_instance="HASHI4")
    assert status == rescue_cli.CHECK_FAIL
    assert "without an instance identity" in detail


# --- /start runs Core through the qualified launcher ---------------------


def _instance_home(tmp_path, instance_id: str = "HASHI9", port: int = 18899) -> Path:
    home = tmp_path / "instance"
    home.mkdir()
    _write_agents(home, instance_id=instance_id, port=port)
    return home


def test_start_backend_uses_the_qualified_core_launcher(monkeypatch, tmp_path):
    home = _instance_home(tmp_path)
    captured = {}
    monkeypatch.setattr(
        rescue_cli.subprocess,
        "Popen",
        lambda argv, **kwargs: captured.setdefault("argv", argv),
    )
    monkeypatch.setattr(
        rescue_cli, "core_runtime", lambda root: (["C:/core/python.exe", "-I", "-B"], "")
    )
    monkeypatch.setattr(
        rescue_cli,
        "_health",
        lambda port, timeout=0, expected_instance=None: (
            rescue_cli.CHECK_OK,
            f"instance {expected_instance}",
        ),
    )

    message = rescue_cli.start_backend(ROOT, home)
    argv = captured["argv"]
    assert argv[0] == "C:/core/python.exe"
    assert argv[1:3] == ["-I", "-B"]
    assert argv[3].endswith("run_qualified_core.py")
    assert "--bridge-home" in argv
    assert "main.py" not in " ".join(argv)
    assert message.startswith("Backend responded")
    assert "HASHI9" in message


def test_start_backend_refuses_to_run_core_with_the_tui_interpreter(monkeypatch, tmp_path):
    home = _instance_home(tmp_path)
    spawned = []
    monkeypatch.setattr(rescue_cli.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(
        rescue_cli,
        "core_runtime",
        lambda root: (None, "no installer-qualified Core runtime is available"),
    )

    message = rescue_cli.start_backend(ROOT, home)
    assert spawned == []
    assert "TUI interpreter is not used for Core" in message


def test_diagnose_separates_the_tui_interpreter_from_the_server(monkeypatch, tmp_path):
    """The selector verdict is the server answer; no second role-blind check."""

    home = _instance_home(tmp_path)
    (home / "secrets.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        rescue_cli, "core_runtime", lambda root: (["C:/core/python.exe", "-I", "-B"], "")
    )

    checks = {check["label"]: check for check in rescue_cli.diagnose(ROOT, home)}
    assert checks["python"]["detail"].startswith("TUI interpreter")
    assert checks["server runtime"]["status"] == rescue_cli.CHECK_OK
    assert "C:/core/python.exe" in checks["server runtime"]["detail"]
    # A qualified Core/Functions pair must never be reported as dependency
    # drift: the Functions contract is not re-run from the TUI interpreter.
    assert "server dependency contract" not in checks


def test_diagnose_reports_a_missing_server_runtime_as_a_server_problem(monkeypatch, tmp_path):
    home = _instance_home(tmp_path)
    (home / "secrets.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        rescue_cli,
        "core_runtime",
        lambda root: (None, "no installer-qualified Core runtime is available"),
    )

    checks = {check["label"]: check for check in rescue_cli.diagnose(ROOT, home)}
    assert checks["server runtime"]["status"] == rescue_cli.CHECK_FAIL
    assert "no installer-qualified Core runtime" in checks["server runtime"]["detail"]
    assert checks["python"]["status"] == rescue_cli.CHECK_OK
# HASHI-Fingerprint: hfp_695d915b6fcf4830bc89b78ee2c975ab
