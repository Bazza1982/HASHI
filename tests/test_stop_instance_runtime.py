from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import psutil
import pytest

from scripts.stop_instance_runtime import stop_runtime


@pytest.mark.skipif(os.name != "posix", reason="POSIX fixed launcher process ownership")
@pytest.mark.parametrize("child_remote", [False, True])
def test_stop_reaps_detached_functions_and_preserves_another_instance(tmp_path, child_remote):
    home = tmp_path / "instance"
    other = tmp_path / "other"
    home.mkdir()
    other.mkdir()
    ready = tmp_path / "ready.json"
    remote_package = tmp_path / "remote"
    remote_package.mkdir()
    (remote_package / "__main__.py").write_text("import time; time.sleep(60)\n", encoding="utf-8")
    script = tmp_path / "main.py"
    script.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
        "from pathlib import Path\n"
        + ("remote = subprocess.Popen([sys.executable, '-m', 'remote', '--hashi-root', sys.argv[2]], cwd=" + repr(str(tmp_path)) + ")\n"
           if child_remote else "remote = None\n")
        + "Path(sys.argv[3]).write_text(json.dumps({'child': child.pid, 'remote': remote.pid if remote else None}))\n"
        "while True: time.sleep(1)\n", encoding="utf-8",
    )
    peer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    core = subprocess.Popen([
        sys.executable, str(script), "--bridge-home", str(home), str(ready),
    ])
    child = None
    remote_process = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        identities = json.loads(ready.read_text())
        child = psutil.Process(identities["child"])
        if identities["remote"]:
            remote_process = psutil.Process(identities["remote"])
        with pytest.raises(ValueError, match="instance home"):
            stop_runtime(core.pid, other, grace_seconds=.1)
        assert core.poll() is None and peer.poll() is None
        assert stop_runtime(core.pid, home, grace_seconds=.15) == []
        core.wait(timeout=2)
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        assert peer.poll() is None
        if remote_process is not None:
            assert remote_process.is_running() and remote_process.status() != psutil.STATUS_ZOMBIE
    finally:
        for process in (core, peer):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        for process in (child, remote_process):
            if process is not None and process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                process.kill()


def test_stop_refuses_self_and_an_unrelated_ancestor(tmp_path):
    for pid in (os.getpid(), os.getppid(), 0, -1):
        with pytest.raises(ValueError, match="invalid or current|instance home"):
            stop_runtime(pid, tmp_path, grace_seconds=0)
# HASHI-Fingerprint: hfp_df07edbe02aa4a5eac38be2b3139abe9
