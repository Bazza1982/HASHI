from __future__ import annotations

import subprocess

from scripts import check_pip_audit as audit


def _argv(monkeypatch, project_root):
    monkeypatch.setattr(
        audit.sys,
        "argv",
        ["check_pip_audit.py", "--project-root", str(project_root)],
    )


def test_main_falls_back_to_current_interpreter_when_executable_is_missing(
    tmp_path, monkeypatch, capsys
):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout_s: int):
        assert timeout_s == 20
        calls.append(cmd)
        if cmd[0] == "pip-audit":
            raise FileNotFoundError("pip-audit")
        return subprocess.CompletedProcess(
            cmd, 0, stdout="No known vulnerabilities found\n", stderr=""
        )

    _argv(monkeypatch, tmp_path)
    monkeypatch.setattr(audit, "_run", fake_run)

    assert audit.main() == 0
    assert calls == [
        ["pip-audit", "--desc", "--progress-spinner", "off"],
        [
            audit.sys.executable,
            "-m",
            "pip_audit",
            "--desc",
            "--progress-spinner",
            "off",
        ],
    ]
    assert "No known vulnerabilities found" in capsys.readouterr().out


def test_module_fallback_keeps_requirement_file_precedence(
    tmp_path, monkeypatch
):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout_s: int):
        assert timeout_s == 20
        calls.append(cmd)
        if cmd[0] == "pip-audit":
            raise OSError("not executable")
        return subprocess.CompletedProcess(
            cmd, 1, stdout="example 1.0 CVE-EXAMPLE\n", stderr=""
        )

    _argv(monkeypatch, tmp_path)
    monkeypatch.setattr(audit, "_run", fake_run)

    assert audit.main() == 1
    assert calls[:2] == [
        [
            "pip-audit",
            "-r",
            str(requirements),
            "--desc",
            "--progress-spinner",
            "off",
        ],
        ["pip-audit", "--desc", "--progress-spinner", "off"],
    ]
    assert calls[2] == [
        audit.sys.executable,
        "-m",
        "pip_audit",
        "-r",
        str(requirements),
        "--desc",
        "--progress-spinner",
        "off",
    ]
