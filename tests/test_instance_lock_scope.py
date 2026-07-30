from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.instance_lock import InstanceLock
from orchestrator.pathing import build_bridge_paths

ROOT = Path(__file__).resolve().parent.parent
LOCK_HOLDER = """
import sys
from pathlib import Path
from orchestrator.instance_lock import InstanceLock

lock = InstanceLock(Path(sys.argv[1]), pid_path=Path(sys.argv[2]), instance_id=sys.argv[3])
lock.acquire()
print("READY", flush=True)
sys.stdin.readline()
lock.release()
"""


def _write_config(home: Path, instance_id: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "agents.json").write_text(
        json.dumps({"global": {"instance_id": instance_id}}),
        encoding="utf-8",
    )


def test_process_paths_are_scoped_by_instance_home(tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    _write_config(home_a, "HASHI1")
    _write_config(home_b, "HASHI2")

    paths_a = build_bridge_paths(tmp_path, bridge_home=home_a)
    paths_b = build_bridge_paths(tmp_path, bridge_home=home_b)

    assert paths_a.instance_id == "HASHI1"
    assert paths_b.instance_id == "HASHI2"
    assert paths_a.lock_path.name == paths_b.lock_path.name == "process.lock"
    assert paths_a.lock_path != paths_b.lock_path
    assert paths_a.pid_path != paths_b.pid_path


def test_different_instances_can_hold_locks_at_the_same_time(tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    _write_config(home_a, "HASHI1")
    _write_config(home_b, "HASHI2")
    paths_a = build_bridge_paths(tmp_path, bridge_home=home_a)
    paths_b = build_bridge_paths(tmp_path, bridge_home=home_b)
    lock_a = InstanceLock(
        paths_a.lock_path,
        pid_path=paths_a.pid_path,
        instance_id=paths_a.instance_id,
    )
    lock_b = InstanceLock(
        paths_b.lock_path,
        pid_path=paths_b.pid_path,
        instance_id=paths_b.instance_id,
    )

    try:
        lock_a.acquire()
        lock_b.acquire()
        assert paths_a.pid_path.exists()
        assert paths_b.pid_path.exists()
    finally:
        lock_b.release()
        lock_a.release()


def test_separate_bridge_homes_do_not_share_a_lock_even_with_same_display_id(tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    _write_config(home_a, "HASHI")
    _write_config(home_b, "HASHI")

    paths_a = build_bridge_paths(tmp_path, bridge_home=home_a)
    paths_b = build_bridge_paths(tmp_path, bridge_home=home_b)

    assert paths_a.lock_path != paths_b.lock_path
    assert paths_a.pid_path != paths_b.pid_path


def test_process_path_stays_stable_if_display_identity_changes(tmp_path):
    home = tmp_path / "home"
    _write_config(home, "HASHI-OLD")
    before = build_bridge_paths(tmp_path, bridge_home=home)

    _write_config(home, "HASHI-RENAMED")
    after = build_bridge_paths(tmp_path, bridge_home=home)

    assert before.instance_id != after.instance_id
    assert before.lock_path == after.lock_path
    assert before.pid_path == after.pid_path


def test_second_process_lock_for_same_instance_is_rejected(tmp_path):
    home = tmp_path / "home"
    _write_config(home, "HASHI1")
    paths = build_bridge_paths(tmp_path, bridge_home=home)
    first = InstanceLock(
        paths.lock_path,
        pid_path=paths.pid_path,
        instance_id=paths.instance_id,
    )
    second = InstanceLock(
        paths.lock_path,
        pid_path=paths.pid_path,
        instance_id=paths.instance_id,
    )

    try:
        first.acquire()
        with pytest.raises(RuntimeError, match="HASHI1"):
            second.acquire()
        second.release()
        assert paths.pid_path.exists()
    finally:
        second.release()
        first.release()


def test_same_instance_lock_is_enforced_across_processes(tmp_path):
    home = tmp_path / "home"
    _write_config(home, "HASHI1")
    paths = build_bridge_paths(tmp_path, bridge_home=home)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            LOCK_HOLDER,
            str(paths.lock_path),
            str(paths.pid_path),
            paths.instance_id,
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "READY"
        contender = InstanceLock(
            paths.lock_path,
            pid_path=paths.pid_path,
            instance_id=paths.instance_id,
        )
        with pytest.raises(RuntimeError, match="HASHI1"):
            contender.acquire()
        assert holder.poll() is None
    finally:
        if holder.poll() is None:
            holder.communicate("release\n", timeout=5)
        assert holder.returncode == 0
