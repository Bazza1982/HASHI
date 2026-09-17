"""Native read-only deployment controls for exact live HASHI targets."""
from __future__ import annotations

import csv
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from orchestrator.live_runtime_protection import LiveRuntimePolicy


_WINDOWS_SID = re.compile(r"S-1(?:-\d+)+", re.IGNORECASE)


@dataclass(frozen=True)
class NativeProtectionTarget:
    path: Path
    recursive: bool
    kind: str


def build_native_protection_targets(
    policy: LiveRuntimePolicy,
) -> tuple[NativeProtectionTarget, ...]:
    """Return exact existing targets without widening to a repository root."""

    recursive_roots = {
        _resolved(path)
        for path in policy.runtime_roots
    }
    for root in recursive_roots:
        if root.parent == root or not _is_python_environment_root(root):
            raise ValueError(
                f"live runtime root is not a validated Python environment: {root}"
            )
    targets: dict[str, NativeProtectionTarget] = {}
    for path in (*policy.protected_write_paths, *policy.protected_read_paths):
        resolved = _resolved(path)
        _require_narrow_existing_target(
            resolved,
            forbidden_roots=(policy.code_root, policy.bridge_home),
        )
        targets[_path_key(resolved)] = NativeProtectionTarget(
            path=resolved,
            recursive=resolved.is_dir(),
            kind="protected_path",
        )
    for root in recursive_roots:
        _require_narrow_existing_target(
            root,
            forbidden_roots=(policy.code_root, policy.bridge_home),
        )
        targets[_path_key(root)] = NativeProtectionTarget(
            path=root,
            recursive=True,
            kind="runtime",
        )
    return tuple(sorted(targets.values(), key=lambda item: _path_key(item.path)))


def windows_current_sid() -> str:
    if os.name != "nt":
        raise OSError("Windows SID resolution is only available on Windows")
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    result = subprocess.run(
        [str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    rows = list(csv.reader(result.stdout.strip().splitlines()))
    sid = rows[0][-1].strip() if len(rows) == 1 and len(rows[0]) == 2 else ""
    return _validated_sid(sid)


def apply_windows_read_only(
    targets: Sequence[NativeProtectionTarget],
    *,
    runtime_sid: str,
    lock_owner: bool,
) -> tuple[dict[str, object], ...]:
    """Replace target ACLs so the runtime SID has read/execute but no write."""

    if os.name != "nt":
        raise OSError("Windows ACL protection is only available on Windows")
    sid = _validated_sid(runtime_sid)
    reports: list[dict[str, object]] = []
    applied: list[NativeProtectionTarget] = []
    try:
        for target in targets:
            path = _resolved(target.path)
            _require_narrow_existing_target(path, forbidden_roots=())
            applied.append(target)
            inheritance = "(OI)(CI)" if target.recursive else ""
            commands: list[list[str]] = []
            if lock_owner:
                owner_command = [
                    "icacls.exe",
                    str(path),
                    "/setowner",
                    "*S-1-5-32-544",
                ]
                if target.recursive:
                    owner_command.extend(["/T", "/C"])
                commands.append(owner_command)
            # Install the complete replacement DACL in one icacls operation.
            # A separate `/inheritance:r` call can remove the caller's only ACE
            # before the replacement grants run, locking out apply and rollback.
            commands.append(
                [
                    "icacls.exe",
                    str(path),
                    "/grant:r",
                    f"*{sid}:{inheritance}(RX)",
                    f"*S-1-5-18:{inheritance}(F)",
                    f"*S-1-5-32-544:{inheritance}(F)",
                    "/inheritance:r",
                ]
            )
            outputs = [_run_windows_acl(command) for command in commands]
            reports.append(
                {
                    "path": str(path),
                    "recursive": target.recursive,
                    "kind": target.kind,
                    "owner_locked": lock_owner,
                    "outputs": outputs,
                }
            )
    except Exception:
        restore_windows_access(tuple(reversed(applied)), runtime_sid=sid)
        raise
    return tuple(reports)


def restore_windows_access(
    targets: Sequence[NativeProtectionTarget],
    *,
    runtime_sid: str,
) -> tuple[dict[str, object], ...]:
    """Remove this helper's deny ACE and restore runtime-owner full control."""

    if os.name != "nt":
        raise OSError("Windows ACL restoration is only available on Windows")
    sid = _validated_sid(runtime_sid)
    reports: list[dict[str, object]] = []
    for target in targets:
        path = _resolved(target.path)
        if not path.exists():
            continue
        inheritance = "(OI)(CI)" if target.recursive else ""
        commands = [
            ["icacls.exe", str(path), "/remove:d", f"*{sid}"],
            [
                "icacls.exe",
                str(path),
                "/grant:r",
                f"*{sid}:{inheritance}(F)",
            ],
            ["icacls.exe", str(path), "/inheritance:e"],
        ]
        outputs = [_run_windows_acl(command) for command in commands]
        reports.append({"path": str(path), "outputs": outputs})
    return tuple(reports)


def apply_posix_read_only(
    targets: Sequence[NativeProtectionTarget],
    *,
    runtime_user: str,
    runtime_group: str,
    immutable: bool = True,
) -> tuple[dict[str, object], ...]:
    """Make exact POSIX targets root-owned and runtime-readable/executable."""

    if os.name == "nt":
        raise OSError("POSIX protection is unavailable on Windows")
    if os.geteuid() != 0:
        raise PermissionError("POSIX live protection must run as root")
    import grp
    import pwd

    uid = pwd.getpwnam(runtime_user).pw_uid
    gid = grp.getgrnam(runtime_group).gr_gid
    reports: list[dict[str, object]] = []
    applied: list[NativeProtectionTarget] = []
    try:
        for target in targets:
            path = _resolved(target.path)
            _require_narrow_existing_target(path, forbidden_roots=())
            applied.append(target)
            changed = 0
            for entry in _target_entries(target):
                if entry.is_symlink():
                    os.lchown(entry, 0, gid)
                    changed += 1
                    continue
                mode = entry.stat().st_mode
                os.chown(entry, 0, gid)
                if entry.is_dir():
                    entry.chmod(0o550)
                elif mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                    entry.chmod(0o550)
                else:
                    entry.chmod(0o440)
                changed += 1
            if immutable:
                _run_posix_chattr("+i", path)
            reports.append(
                {
                    "path": str(path),
                    "recursive": target.recursive,
                    "kind": target.kind,
                    "runtime_uid": uid,
                    "runtime_gid": gid,
                    "immutable_root": immutable,
                    "entries": changed,
                }
            )
    except Exception:
        restore_posix_access(
            tuple(reversed(applied)),
            runtime_user=runtime_user,
            runtime_group=runtime_group,
            immutable=immutable,
        )
        raise
    return tuple(reports)


def restore_posix_access(
    targets: Sequence[NativeProtectionTarget],
    *,
    runtime_user: str,
    runtime_group: str,
    immutable: bool = True,
) -> tuple[dict[str, object], ...]:
    """Return canary targets to their runtime owner after a failed rollout."""

    if os.name == "nt":
        raise OSError("POSIX restoration is unavailable on Windows")
    if os.geteuid() != 0:
        raise PermissionError("POSIX live restoration must run as root")
    import grp
    import pwd

    uid = pwd.getpwnam(runtime_user).pw_uid
    gid = grp.getgrnam(runtime_group).gr_gid
    reports: list[dict[str, object]] = []
    for target in targets:
        path = _resolved(target.path)
        if not path.exists():
            continue
        if immutable:
            _run_posix_chattr("-i", path)
        changed = 0
        for entry in _target_entries(target):
            if entry.is_symlink():
                os.lchown(entry, uid, gid)
                changed += 1
                continue
            previous = stat.S_IMODE(entry.stat().st_mode)
            os.chown(entry, uid, gid)
            entry.chmod(previous | stat.S_IWUSR)
            changed += 1
        reports.append({"path": str(path), "entries": changed})
    return tuple(reports)


def _target_entries(target: NativeProtectionTarget) -> Iterable[Path]:
    path = _resolved(target.path)
    if not target.recursive or not path.is_dir():
        yield path
        return
    for root, directories, files in os.walk(path, topdown=False, followlinks=False):
        base = Path(root)
        for name in files:
            yield base / name
        for name in directories:
            yield base / name
    yield path


def _is_python_environment_root(root: Path) -> bool:
    """Accept a venv or an interpreter that reports this exact prefix."""

    if (root / "pyvenv.cfg").is_file():
        return True
    candidates = (
        root / "python.exe",
        root / "Scripts" / "python.exe",
        root / "bin" / "python3",
        root / "bin" / "python",
    )
    for executable in candidates:
        if not executable.is_file():
            continue
        kwargs: dict[str, object] = {
            "capture_output": True,
            "text": True,
            "timeout": 10,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            result = subprocess.run(
                [
                    str(executable),
                    "-I",
                    "-c",
                    "import pathlib, sys; print(pathlib.Path(sys.prefix).resolve())",
                ],
                **kwargs,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        reported = result.stdout.strip().splitlines()
        if reported and _path_key(Path(reported[-1])) == _path_key(root):
            return True
    return False


def _run_windows_acl(argv: Sequence[str]) -> str:
    result = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    output = "\n".join(
        value.strip() for value in (result.stdout, result.stderr) if value.strip()
    )
    if result.returncode != 0:
        raise OSError(
            f"Windows ACL command failed ({result.returncode}): "
            f"{' '.join(argv)}\n{output}"
        )
    return output


def _run_posix_chattr(action: str, path: Path) -> None:
    result = subprocess.run(
        ["chattr", action, "--", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        output = "\n".join(
            value.strip()
            for value in (result.stdout, result.stderr)
            if value.strip()
        )
        raise OSError(f"chattr {action} failed for {path}: {output}")


def _validated_sid(value: str) -> str:
    sid = str(value or "").strip().upper()
    if not _WINDOWS_SID.fullmatch(sid):
        raise ValueError(f"invalid Windows SID: {value!r}")
    return sid


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(_resolved(path)))


def _require_narrow_existing_target(
    path: Path,
    *,
    forbidden_roots: Sequence[Path],
) -> None:
    if not path.is_absolute() or path.parent == path:
        raise ValueError(f"refusing broad live protection target: {path}")
    if any(_path_key(path) == _path_key(root) for root in forbidden_roots):
        raise ValueError(f"refusing repository/instance root as a protection target: {path}")
    if not path.exists():
        raise FileNotFoundError(f"live protection target does not exist: {path}")
