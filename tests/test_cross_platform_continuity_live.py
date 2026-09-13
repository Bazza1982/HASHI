from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = [pytest.mark.integration, pytest.mark.live, pytest.mark.platform]


def _wsl_path(distro: str, path: Path) -> str:
    result = subprocess.run(
        ["wsl.exe", "--distribution", distro, "--exec", "wslpath", "-a", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _decode_result(result: subprocess.CompletedProcess[bytes]) -> dict:
    if result.returncode != 0:
        raise AssertionError(
            "cross-platform continuity worker failed:\n"
            + result.stdout.decode("utf-8", errors="replace")
            + result.stderr.decode("utf-8", errors="replace")
        )
    lines = result.stdout.decode("utf-8", errors="strict").splitlines()
    return json.loads(next(line for line in reversed(lines) if line.strip()))


def _windows_worker(
    *,
    source_root: Path,
    worker: Path,
    operation: str,
    root: Path,
    package: Path,
    instance: str,
    question: str,
    answer: str,
    source_instance: str = "",
) -> dict:
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(root),
            "USERPROFILE": str(root),
            "PYTHONPATH": str(source_root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    command = [
        sys.executable,
        str(worker),
        operation,
        "--root",
        str(root),
        "--package",
        str(package),
        "--instance",
        instance,
        "--question",
        question,
        "--answer",
        answer,
    ]
    if source_instance:
        command.extend(["--source-instance", source_instance])
    return _decode_result(
        subprocess.run(
            command,
            cwd=source_root,
            env=environment,
            capture_output=True,
            timeout=45,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    )


def _wsl_worker(
    *,
    source_root: Path,
    worker: Path,
    distro: str,
    python: str,
    operation: str,
    root: Path,
    package: Path,
    instance: str,
    question: str,
    answer: str,
    source_instance: str = "",
) -> dict:
    wsl_source = _wsl_path(distro, source_root)
    wsl_worker = _wsl_path(distro, worker)
    wsl_root = _wsl_path(distro, root)
    command = [
        "wsl.exe",
        "--distribution",
        distro,
        "--cd",
        wsl_root,
        "--exec",
        "env",
        f"HOME={wsl_root}",
        f"PYTHONPATH={wsl_source}",
        "PYTHONDONTWRITEBYTECODE=1",
        python,
        wsl_worker,
        operation,
        "--root",
        wsl_root,
        "--package",
        _wsl_path(distro, package),
        "--instance",
        instance,
        "--question",
        question,
        "--answer",
        answer,
    ]
    if source_instance:
        command.extend(["--source-instance", source_instance])
    return _decode_result(
        subprocess.run(
            command,
            cwd=source_root,
            capture_output=True,
            timeout=45,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    )


def _assert_move(
    *,
    source: dict,
    target: dict,
    source_question: str,
    source_answer: str,
    target_question: str,
    target_answer: str,
) -> None:
    assert source["history_mode"] == "move"
    assert source["eligible_messages"] == 2
    assert source["source_messages"] == 3
    assert target["package_id"] == source["package_id"]
    assert target["status"] == "committed_inactive"
    assert target["replay_status"] == "committed_inactive"
    assert target["imported_messages"] == 2
    assert target["replayed_messages"] == 2
    assert target["workbench_session_id"] == target["telegram_session_id"]
    assert target["history_generation"] >= 2
    assert target["messages"] == [
        source_question,
        source_answer,
        target_question,
        target_answer,
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="cross-platform H2/H3 canary starts from Windows")
def test_session_continuity_move_round_trips_between_wsl_and_windows(tmp_path: Path) -> None:
    distro = os.getenv("HASHI_LIVE_WSL_DISTRO", "").strip()
    wsl_python = os.getenv("HASHI_LIVE_WSL_PYTHON", "").strip()
    if not distro or not wsl_python:
        pytest.skip("set HASHI_LIVE_WSL_DISTRO and HASHI_LIVE_WSL_PYTHON for the authorized canary")

    source_root = Path(__file__).resolve().parent.parent
    worker = Path(__file__).with_name("session_continuity_live_worker.py")

    wsl_to_windows = tmp_path / "wsl-to-windows"
    first_package = wsl_to_windows / "traveler.hashi-agent"
    first_source_question = "WSL source question 界"
    first_source_answer = "WSL source answer 界"
    first_target_question = "Windows target question 界"
    first_target_answer = "Windows target answer 界"
    first_source = _wsl_worker(
        source_root=source_root,
        worker=worker,
        distro=distro,
        python=wsl_python,
        operation="source",
        root=wsl_to_windows / "source",
        package=first_package,
        instance="CANARY-WSL-SOURCE",
        question=first_source_question,
        answer=first_source_answer,
    )
    first_target = _windows_worker(
        source_root=source_root,
        worker=worker,
        operation="target",
        root=wsl_to_windows / "target",
        package=first_package,
        instance="CANARY-WIN-TARGET",
        source_instance="CANARY-WSL-SOURCE",
        question=first_target_question,
        answer=first_target_answer,
    )
    _assert_move(
        source=first_source,
        target=first_target,
        source_question=first_source_question,
        source_answer=first_source_answer,
        target_question=first_target_question,
        target_answer=first_target_answer,
    )

    windows_to_wsl = tmp_path / "windows-to-wsl"
    second_package = windows_to_wsl / "traveler.hashi-agent"
    second_source_question = "Windows source question 界"
    second_source_answer = "Windows source answer 界"
    second_target_question = "WSL target question 界"
    second_target_answer = "WSL target answer 界"
    second_source = _windows_worker(
        source_root=source_root,
        worker=worker,
        operation="source",
        root=windows_to_wsl / "source",
        package=second_package,
        instance="CANARY-WIN-SOURCE",
        question=second_source_question,
        answer=second_source_answer,
    )
    second_target = _wsl_worker(
        source_root=source_root,
        worker=worker,
        distro=distro,
        python=wsl_python,
        operation="target",
        root=windows_to_wsl / "target",
        package=second_package,
        instance="CANARY-WSL-TARGET",
        source_instance="CANARY-WIN-SOURCE",
        question=second_target_question,
        answer=second_target_answer,
    )
    _assert_move(
        source=second_source,
        target=second_target,
        source_question=second_source_question,
        source_answer=second_source_answer,
        target_question=second_target_question,
        target_answer=second_target_answer,
    )
