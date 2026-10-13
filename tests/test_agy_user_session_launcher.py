from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from xml.etree import ElementTree

from adapters import agy_user_session_launcher as launcher


def _payload(path: Path, args: list[str]) -> None:
    path.mkdir(mode=0o700)
    (path / "args.json").write_text(
        json.dumps({"args": args, "cwd": None}),
        encoding="utf-8",
    )


def test_scheduled_task_xml_is_manual_only_and_interactive() -> None:
    xml = launcher._manual_task_xml(
        "DOMAIN\\user",
        "S-1-5-21-111-222-333-1001",
        r"C:\Python\python.exe",
        r'"C:\HASHI\agy_user_session_launcher.py" --task-runner work task',
    )

    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ElementTree.fromstring(xml)
    triggers = root.find("task:Triggers", namespace)
    assert triggers is not None
    assert list(triggers) == []
    assert root.findtext(".//task:LogonType", namespaces=namespace) == "InteractiveToken"
    assert root.findtext(
        ".//task:AllowStartOnDemand", namespaces=namespace
    ) == "true"
    assert root.findtext(".//task:StartWhenAvailable", namespaces=namespace) == "false"
    assert root.findtext(
        "task:RegistrationInfo/task:SecurityDescriptor", namespaces=namespace
    ) == (
        "D:P(A;;FA;;;SY)(A;;FA;;;BA)"
        "(A;;FA;;;S-1-5-21-111-222-333-1001)"
    )


def test_current_console_identity_requires_same_session_and_exact_sid(
    monkeypatch,
) -> None:
    class _Kernel:
        @staticmethod
        def GetCurrentProcessId():
            return 123

        @staticmethod
        def GetCurrentProcess():
            return 456

        @staticmethod
        def ProcessIdToSessionId(_pid, output):
            output._obj.value = 8
            return True

        @staticmethod
        def CloseHandle(_handle):
            return True

    class _Advapi:
        @staticmethod
        def OpenProcessToken(_process, _access, output):
            output._obj.value = 789
            return True

    monkeypatch.setattr(
        launcher, "_token_user_sid", lambda *_args: "S-1-5-21-1-2-3-1001"
    )
    monkeypatch.setattr(
        launcher,
        "_wts_session_text",
        lambda _wts, _session, info: (
            "alice" if info == launcher.WTS_USER_NAME else "DOMAIN"
        ),
    )
    monkeypatch.setattr(
        launcher, "_account_sid", lambda *_args: "S-1-5-21-1-2-3-1001"
    )

    identity = launcher._current_console_identity(
        _Kernel(), _Advapi(), object(), 8
    )
    assert identity == ("DOMAIN\\alice", "S-1-5-21-1-2-3-1001")

    monkeypatch.setattr(
        launcher, "_account_sid", lambda *_args: "S-1-5-21-9-9-9-1001"
    )
    assert launcher._current_console_identity(
        _Kernel(), _Advapi(), object(), 8
    ) is None
    assert launcher._current_console_identity(
        _Kernel(), _Advapi(), object(), 9
    ) is None


def test_current_console_query_failure_never_uses_current_identity(
    monkeypatch,
) -> None:
    class _Kernel:
        @staticmethod
        def WTSGetActiveConsoleSessionId():
            return 8

    adjusted = []
    monkeypatch.setattr(
        launcher, "_bind", lambda: (_Kernel(), object(), object(), object())
    )
    monkeypatch.setattr(launcher, "_current_console_identity", lambda *_args: None)
    monkeypatch.setattr(
        launcher,
        "_adjust_privileges",
        lambda *_args: adjusted.append(_args[-1]) or False,
    )

    result = launcher.main(["--", "agy.exe", "--version"])

    assert result == launcher.EXIT_QUERY_TOKEN_FAILED
    assert adjusted == [("SeTcbPrivilege",)]


def test_verified_current_identity_spawns_without_privilege_adjustment(
    monkeypatch,
) -> None:
    class _Kernel:
        @staticmethod
        def WTSGetActiveConsoleSessionId():
            return 8

    spawned = []
    monkeypatch.setattr(
        launcher, "_bind", lambda: (_Kernel(), object(), object(), object())
    )
    monkeypatch.setattr(
        launcher,
        "_current_console_identity",
        lambda *_args: ("DOMAIN\\alice", "S-1-5-21-1-2-3-1001"),
    )
    monkeypatch.setattr(
        launcher,
        "_adjust_privileges",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("verified current identity must not request SeTcbPrivilege")
        ),
    )
    monkeypatch.setattr(
        launcher,
        "_spawn_as_current_identity",
        lambda exe, args, cwd: spawned.append((exe, args, cwd)) or 23,
    )

    result = launcher.main(["--cwd", r"C:\work", "--", "agy.exe", "--version"])

    assert result == 23
    assert spawned == [("agy.exe", ["--version"], r"C:\work")]


def test_current_identity_spawn_binds_job_and_inherits_standard_handles(
    monkeypatch,
) -> None:
    calls = []

    class _Process:
        _handle = 123

        @staticmethod
        def wait(timeout=None):
            assert timeout is None
            return 17

    class _Job:
        active = True

        def __init__(self, process):
            assert process is child

        def close(self):
            calls.append("closed")

    child = _Process()

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return child

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "_WindowsChildJob", _Job)

    result = launcher._spawn_as_current_identity(
        "agy.exe", ["--version"], r"C:\work"
    )

    assert result == 17
    assert calls == [
        (["agy.exe", "--version"], {"cwd": r"C:\work"}),
        "closed",
    ]


def test_current_identity_spawn_fails_closed_when_job_binding_fails(
    monkeypatch,
) -> None:
    calls = []

    class _Process:
        _handle = 123

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            calls.append("terminated")

        @staticmethod
        def wait(timeout=None):
            calls.append(("waited", timeout))
            return 1

    class _Job:
        active = False

        def __init__(self, _process):
            pass

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_args, **_kwargs: _Process())
    monkeypatch.setattr(launcher, "_WindowsChildJob", _Job)

    result = launcher._spawn_as_current_identity("agy.exe", [], None)

    assert result == launcher.EXIT_CREATE_PROCESS_FAILED
    assert calls == ["terminated", ("waited", 10)]


def test_delete_scheduled_task_retries_while_exact_task_still_exists(
    monkeypatch,
) -> None:
    class _Result:
        def __init__(self, returncode):
            self.returncode = returncode

    commands = []
    results = iter([_Result(1), _Result(0), _Result(0)])

    def run(command, **_kwargs):
        commands.append(command)
        return next(results)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    assert launcher._delete_scheduled_task("RikaAgy-3333333333333333") is True
    assert [command[1] for command in commands] == ["/Delete", "/Query", "/Delete"]


def test_only_explicit_stream_json_contract_reads_launcher_stdin(monkeypatch) -> None:
    class _UnexpectedStdin:
        def isatty(self):
            return False

        @property
        def buffer(self):
            raise AssertionError("--version must not read stdin")

    monkeypatch.setattr(launcher.sys, "stdin", _UnexpectedStdin())
    assert launcher._read_scheduled_stdin(["--version"]) is None
    assert launcher._stream_json_stdin_requested(
        ["--input-format", "stream-json", "--output-format", "stream-json"]
    )


def test_task_runner_relays_private_stdin_and_self_cleans(tmp_path, monkeypatch) -> None:
    task_name = "RikaAgy-1111111111111111"
    work_dir = tmp_path / task_name
    _payload(
        work_dir,
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ],
    )
    expected = b'{"event":"user","message":{"content":"private"}}\n'
    (work_dir / "input.bin").write_bytes(expected)
    (work_dir / "heartbeat").write_text("alive", encoding="utf-8")
    deleted = []
    monkeypatch.setattr(launcher, "_delete_scheduled_task", deleted.append)
    monkeypatch.setattr(launcher, "_task_temp_root", lambda: tmp_path)
    observed = []

    def acknowledge() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                (work_dir / "heartbeat").touch()
                if (work_dir / "code.txt").exists():
                    observed.append((work_dir / "out.txt").read_bytes())
                    (work_dir / "ack").write_text("read", encoding="utf-8")
                    return
            except OSError:
                return
            time.sleep(0.02)

    thread = threading.Thread(target=acknowledge, daemon=True)
    thread.start()
    result = launcher._run_task_payload(work_dir, task_name)
    thread.join(timeout=2)

    assert result == 0
    assert observed == [expected]
    assert deleted == [task_name]
    assert not work_dir.exists()


def test_task_runner_terminates_its_owned_child_when_launcher_dies(
    tmp_path, monkeypatch
) -> None:
    task_name = "RikaAgy-2222222222222222"
    work_dir = tmp_path / task_name
    _payload(
        work_dir,
        [sys.executable, "-c", "import time; time.sleep(120)"],
    )
    deleted = []
    monkeypatch.setattr(launcher, "_delete_scheduled_task", deleted.append)
    monkeypatch.setattr(launcher, "_task_temp_root", lambda: tmp_path)

    started = time.monotonic()
    result = launcher._run_task_payload(work_dir, task_name)

    assert time.monotonic() - started < 10
    assert isinstance(result, int)
    assert deleted == [task_name]
    assert not work_dir.exists()


def test_internal_runner_rejects_unowned_paths_without_deleting_them(
    tmp_path, monkeypatch
) -> None:
    work_dir = tmp_path / "not-a-launcher-task"
    work_dir.mkdir()
    deleted = []
    monkeypatch.setattr(launcher, "_delete_scheduled_task", deleted.append)

    result = launcher._run_task_payload(work_dir, "OtherTask")

    assert result == launcher.EXIT_TASK_START_FAILED
    assert work_dir.exists()
    assert deleted == []
