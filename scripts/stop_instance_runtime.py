"""Stop one captured HASHI process tree, retaining identity across PID reuse.

The POSIX fixed launcher uses the Functions interpreter for this operation.
Capture descendants before Core exits so detached Functions cannot become
untracked orphans. No process is selected by a machine-wide name search here.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

import psutil


def _belongs_to_home(process, home: Path) -> bool:
    argv = process.cmdline()
    if not any(Path(value).name == "main.py" for value in argv):
        return False
    try:
        value = argv[argv.index("--bridge-home") + 1]
    except (ValueError, IndexError):
        return False
    return Path(value).resolve() == home.resolve()


def _is_remote_for_home(process, home: Path) -> bool:
    argv = process.cmdline()
    if not any(argv[index:index + 2] == ["-m", "remote"] for index in range(len(argv) - 1)):
        return False
    try:
        value = argv[argv.index("--hashi-root") + 1]
    except (ValueError, IndexError):
        return False
    return Path(value).resolve() == home.resolve()


def stop_runtime(pid: int, home: Path, *, grace_seconds: float = 30.0) -> list[int]:
    if pid <= 0 or pid == os.getpid():
        raise ValueError("Refusing an invalid or current process")
    try:
        core = psutil.Process(pid)
        core.create_time()  # Freeze psutil's PID/create-time identity before signalling.
        if not _belongs_to_home(core, home):
            raise ValueError("Core process does not belong to this instance home")
        descendants = core.children(recursive=True)
    except psutil.NoSuchProcess:
        return []
    # A child-mode Remote is also a supported restart provider. Preserve that
    # exact-home service and its launcher tree even if its old parent exits.
    # In that topology Core can be our ancestor; the explicit home/argv check
    # above, rather than a blanket ancestor prohibition, identifies the target.
    preserved = set()
    for process in descendants:
        try:
            if _is_remote_for_home(process, home):
                preserved.update(p.pid for p in [process, *process.children(recursive=True)])
        except psutil.NoSuchProcess:
            continue
    processes = [core]
    for process in descendants:
        if process.pid in preserved:
            continue
        try:
            process.create_time()
            processes.append(process)
        except psutil.NoSuchProcess:
            continue

    # Let Core perform its normal Functions shutdown first.
    try:
        core.terminate()
    except psutil.NoSuchProcess:
        pass
    _, alive = psutil.wait_procs(processes, timeout=grace_seconds)
    for process in reversed(alive):
        try:
            process.terminate()  # psutil fences each signal against PID reuse.
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=5.0)
    for process in reversed(alive):
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=5.0)
    return [p.pid for p in alive if p.is_running() and p.status() != psutil.STATUS_ZOMBIE]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--bridge-home", type=Path, required=True)
    args = parser.parse_args()
    remaining = stop_runtime(args.pid, args.bridge_home)
    if remaining:
        print(f"Instance shutdown is incomplete: {remaining}", file=sys.stderr)
        return 1
    print("Captured instance Core and Functions processes have stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_084233cce8ca466dae271319d464fdd2
