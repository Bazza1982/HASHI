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


def _canonical_windows_sddl(value: str) -> str:
    """Drop only Windows' non-authoritative auto-inherited observation bit."""
    dacl = value.find("D:")
    if dacl < 0:
        return value
    first_ace = value.find("(", dacl + 2)
    if first_ace < 0:
        return value
    flags = value[dacl + 2:first_ace]
    if "AI" not in flags or any(ch not in "PARI" for ch in flags):
        return value
    return value[:dacl + 2] + flags.replace("AI", "") + value[first_ace:]


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


def _windows_security():
    import ctypes
    from ctypes import wintypes as w
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security.GetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]
    security.GetFileSecurityW.restype = w.BOOL
    security.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, w.LPVOID]
    security.SetFileSecurityW.restype = w.BOOL
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        w.LPVOID, w.DWORD, w.DWORD, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.DWORD)]
    security.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = w.BOOL
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    security.GetSecurityDescriptorControl.argtypes = [w.LPVOID, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
    security.GetSecurityDescriptorControl.restype = w.BOOL
    kernel.LocalFree.argtypes = [w.LPVOID]
    kernel.LocalFree.restype = w.LPVOID
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    return ctypes, w, security, kernel


def _windows_get_sddl(path: Path) -> str:
    c, w, security, kernel = _windows_security()
    size = w.DWORD()
    security.GetFileSecurityW(str(path), 7, None, 0, c.byref(size))
    if not size.value:
        raise c.WinError(c.get_last_error())
    buffer = c.create_string_buffer(size.value)
    if not security.GetFileSecurityW(str(path), 7, buffer, size, c.byref(size)):
        raise c.WinError(c.get_last_error())
    text = w.LPWSTR()
    if not security.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            buffer, 1, 7, c.byref(text), None):
        raise c.WinError(c.get_last_error())
    try:
        # SetFileSecurityW preserves owner, group, ACEs and the protected bit,
        # but Windows recomputes/drops the informational SE_DACL_AUTO_INHERITED
        # marker. Do not turn that observation bit into a false rollback failure.
        return _canonical_windows_sddl(text.value)
    finally:
        kernel.LocalFree(c.cast(text, w.LPVOID))


def _windows_set_sddl(path: Path, sddl: str) -> None:
    c, w, security, kernel = _windows_security()
    descriptor = w.LPVOID()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, c.byref(descriptor), None):
        raise c.WinError(c.get_last_error())
    try:
        control, revision = w.WORD(), w.DWORD()
        if not security.GetSecurityDescriptorControl(descriptor, c.byref(control), c.byref(revision)):
            raise c.WinError(c.get_last_error())
        # Preserve the original owner, group and exact DACL.  Request explicit
        # protection only when the saved descriptor was protected.  Passing
        # UNPROTECTED_DACL_SECURITY_INFORMATION for an ordinary saved DACL asks
        # Windows to recompute inherited ACEs and is not an exact restoration.
        flags = 7 | (0x80000000 if control.value & 0x1000 else 0)
        if not security.SetFileSecurityW(str(path), flags, descriptor):
            raise c.WinError(c.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _windows_can_open(path: Path, rights: int) -> bool:
    """Request rights on an existing object; never create/write/delete it."""
    c, w, _security, kernel = _windows_security()
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID,
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    handle = kernel.CreateFileW(str(path), rights, 7, None, 3, 0x02200000, None)
    if handle == w.HANDLE(-1).value:
        error = c.get_last_error()
        if error == 5:
            return False
        # Sharing errors and missing objects are not proof of ACL denial.
        raise c.WinError(error)
    kernel.CloseHandle(handle)
    return True


def _windows_entries(targets, *, anchor: Path | None = None):
    entries = {}
    parents = set()
    for target in targets:
        for path in _target_entries(target):
            info = path.stat(follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("redirected runtime targets cannot receive ACL protection")
            entries[_path_key(path)] = (path, False)
        parent = _resolved(target.path).parent
        if anchor is not None and not parent.is_relative_to(anchor):
            raise ValueError("protection anchor does not contain the target parent")
        while True:
            parents.add(parent)
            if anchor is None or parent == anchor:
                break
            parent = parent.parent
    for path in parents:
        info = path.stat(follow_symlinks=False)
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("redirected protection parents are not allowed")
        entries.setdefault(_path_key(path), (path, True))
    return sorted(entries.values(), key=lambda entry: (len(entry[0].parts), _path_key(entry[0])))


def apply_windows_read_only(
    targets: Sequence[NativeProtectionTarget],
    *,
    runtime_sid: str,
    lock_owner: bool,
    snapshot_path: Path | None = None,
    maintenance_sid: str | None = None,
    runtime_pid: int | None = None,
    protection_anchor: Path | None = None,
    canary: bool = False,
) -> tuple[dict[str, object], ...]:
    """Deny runtime mutation, with an exact durable pre-change ACL snapshot.

    Strong protection needs a distinct, existing maintenance token and a
    non-writable installer anchor. Canary mode deliberately retains ownership
    and is never reported as account isolation.
    """
    if os.name != "nt":
        raise OSError("Windows ACL protection is only available on Windows")
    sid = _validated_sid(runtime_sid)
    maintainer = _validated_sid(maintenance_sid or windows_current_sid())
    if not canary and maintainer == sid:
        raise PermissionError("strong protection requires a distinct maintenance/recovery identity")
    if snapshot_path is None:
        raise ValueError("an ACL recovery snapshot path is required")
    if not canary:
        if not lock_owner or maintainer != windows_current_sid():
            raise PermissionError("run strong protection as the named maintenance identity")
        if runtime_pid is None or protection_anchor is None:
            raise ValueError("strong protection requires the runtime PID and installer protection anchor")
    elif lock_owner:
        raise ValueError("canary mode cannot claim a locked owner")
    anchor = _resolved(protection_anchor) if protection_anchor is not None else None
    if not canary:
        identity = windows_process_identity(runtime_pid)
        if identity["sid"] != sid:
            raise ValueError("runtime PID does not have the configured runtime SID")
        with _windows_runtime_token(runtime_pid):
            if any(_windows_can_open(anchor, mask) for mask in (0x10000, 0x40000, 0x80000)):
                raise PermissionError("installer anchor remains replaceable by the runtime token")
            if _windows_can_open(anchor.parent, 0x40):
                raise PermissionError("installer anchor parent permits replacement")
            recovery_parent = _resolved(snapshot_path).parent
            if any(_windows_can_open(recovery_parent, mask) for mask in (2, 4, 0x40, 0x10000, 0x40000, 0x80000)):
                raise PermissionError("recovery snapshot directory is writable by the runtime token")

    entries = _windows_entries(targets, anchor=anchor)
    records = []
    for path, parent_guard in entries:
        info = path.stat(follow_symlinks=False)
        records.append({"path": str(path), "sddl": _windows_get_sddl(path),
                        "identity": [info.st_dev, info.st_ino], "parent_guard": parent_guard})
    snapshot = {"schema": 1, "runtime_sid": sid, "maintenance_sid": maintainer,
                "canary": canary, "records": records}
    from orchestrator.config_json import new_config_json, write_config_json
    snapshot_path = _resolved(snapshot_path)
    if snapshot_path.exists():
        raise FileExistsError("refusing to overwrite an ACL recovery snapshot")
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    document = new_config_json(snapshot_path)
    document.update(snapshot)
    write_config_json(snapshot_path, document)
    applied = []
    try:
        for record in records:
            path = Path(record["path"])
            parent_guard = record["parent_guard"]
            # Explicit deny on every existing object also wins against explicit
            # per-child grants and protected (non-inheriting) child DACLs.
            if parent_guard:
                rights = "(DC)" if canary else "(DC,WDAC,WO)"
            else:
                rights = "(WD,AD,WEA,WA)" if canary else "(WD,AD,WEA,WA,WDAC,WO)"
            commands = []
            if lock_owner:
                commands.append(["icacls.exe", str(path), "/setowner", "*" + maintainer])
            commands.append(["icacls.exe", str(path), "/deny", f"*{sid}:{rights}"])
            # New files cannot be created by this SID; maintenance-created
            # descendants inherit the same deny without replacing sibling ACLs.
            if path.is_dir() and not parent_guard:
                inherited_rights = (
                    "(WD,AD,WEA,WA)"
                    if canary
                    else "(WD,AD,WEA,WA,WDAC,WO)"
                )
                commands.append(["icacls.exe", str(path), "/deny",
                                 f"*{sid}:(OI)(CI)(IO){inherited_rights}"])
            applied.append(record)
            for command in commands:
                _run_windows_acl(command)
        return tuple({"path": str(target.path), "recursive": target.recursive,
                      "kind": target.kind, "owner_locked": lock_owner,
                      "assurance": "canary" if canary else "maintenance_owned",
                      "snapshot_path": str(snapshot_path)}
                     for target in targets)
    except Exception as error:
        try:
            _restore_windows_records(records)
        except Exception as recovery_error:
            error.add_note("ACL rollback incomplete; retain recovery snapshot: " + str(recovery_error))
        raise


def _restore_windows_records(records):
    reports = []
    # Parents first, then exact children: inherited propagation cannot overwrite
    # the child's final saved descriptor.
    for record in sorted(records, key=lambda row: len(Path(row["path"]).parts)):
        path = Path(record["path"])
        info = path.stat(follow_symlinks=False)
        if [info.st_dev, info.st_ino] != record["identity"]:
            raise ValueError("ACL recovery target was replaced; do not restore onto another object")
        _windows_set_sddl(path, record["sddl"])
        reports.append({"path": str(path), "acl_restored": True})
    return tuple(reports)


def restore_windows_access(
    targets: Sequence[NativeProtectionTarget], *, runtime_sid: str,
    snapshot_path: Path | None = None,
) -> tuple[dict[str, object], ...]:
    """Restore exact saved ACLs; never grant a blanket Full Control fallback."""
    if os.name != "nt":
        raise OSError("Windows ACL restoration is only available on Windows")
    if snapshot_path is None:
        raise ValueError("exact ACL restoration requires its recovery snapshot")
    from orchestrator.config_json import read_config_json
    snapshot = read_config_json(_resolved(snapshot_path))
    if snapshot.get("schema") != 1 or snapshot.get("runtime_sid") != _validated_sid(runtime_sid):
        raise ValueError("ACL recovery snapshot does not match the runtime identity")
    if not snapshot.get("canary") and windows_current_sid() != snapshot.get("maintenance_sid"):
        raise PermissionError("ACL recovery requires the original maintenance identity")
    records = snapshot.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("ACL recovery snapshot has no saved descriptors")
    # Only explicitly supplied targets and their ancestors may be restored.
    target_paths = tuple(_resolved(target.path) for target in targets)
    for row in records:
        path = _resolved(row["path"])
        if not isinstance(row.get("sddl"), str) or not any(
                path == target or path.is_relative_to(target) or target.is_relative_to(path)
                for target in target_paths):
            raise ValueError("ACL recovery snapshot exceeds the named target scope")
    return _restore_windows_records(records)


def windows_process_identity(process_id: int | None = None) -> dict[str, object]:
    """Read token identity, elevation and integrity without exposing credentials."""
    c, w, security, kernel = _windows_security()
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetCurrentProcess.restype = w.HANDLE
    security.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    security.OpenProcessToken.restype = w.BOOL
    security.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, w.LPVOID, w.DWORD, c.POINTER(w.DWORD)]
    security.GetTokenInformation.restype = w.BOOL
    security.ConvertSidToStringSidW.argtypes = [w.LPVOID, c.POINTER(w.LPWSTR)]
    security.ConvertSidToStringSidW.restype = w.BOOL
    process = kernel.OpenProcess(0x1000, False, process_id) if process_id else kernel.GetCurrentProcess()
    if not process:
        raise c.WinError(c.get_last_error())
    token = w.HANDLE()
    try:
        if not security.OpenProcessToken(process, 8, c.byref(token)):
            raise c.WinError(c.get_last_error())
        def info(kind):
            size = w.DWORD()
            security.GetTokenInformation(token, kind, None, 0, c.byref(size))
            buffer = c.create_string_buffer(size.value)
            if not security.GetTokenInformation(token, kind, buffer, size, c.byref(size)):
                raise c.WinError(c.get_last_error())
            return buffer
        def sid_text(buffer):
            sid = c.cast(buffer, c.POINTER(w.LPVOID))[0]
            text = w.LPWSTR()
            if not security.ConvertSidToStringSidW(sid, c.byref(text)):
                raise c.WinError(c.get_last_error())
            try:
                return text.value
            finally:
                kernel.LocalFree(c.cast(text, w.LPVOID))
        sid = sid_text(info(1))
        integrity = sid_text(info(25))
        elevated = bool(c.cast(info(20), c.POINTER(w.DWORD))[0])
        return {"sid": sid, "elevated": elevated, "integrity_sid": integrity,
                "process_id": process_id or os.getpid()}
    finally:
        if token:
            kernel.CloseHandle(token)
        if process_id:
            kernel.CloseHandle(process)


@__import__("contextlib").contextmanager
def _windows_runtime_token(process_id: int | None):
    if process_id is None:
        yield
        return
    c, w, security, kernel = _windows_security()
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    security.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    security.OpenProcessToken.restype = w.BOOL
    security.DuplicateToken.argtypes = [w.HANDLE, c.c_int, c.POINTER(w.HANDLE)]
    security.DuplicateToken.restype = w.BOOL
    security.ImpersonateLoggedOnUser.argtypes = [w.HANDLE]
    security.ImpersonateLoggedOnUser.restype = w.BOOL
    process = kernel.OpenProcess(0x1000, False, process_id)
    if not process:
        raise c.WinError(c.get_last_error())
    token, duplicate = w.HANDLE(), w.HANDLE()
    impersonating = False
    try:
        if not security.OpenProcessToken(process, 0xA, c.byref(token)):
            raise c.WinError(c.get_last_error())
        if not security.DuplicateToken(token, 2, c.byref(duplicate)):
            raise c.WinError(c.get_last_error())
        if not security.ImpersonateLoggedOnUser(duplicate):
            raise c.WinError(c.get_last_error())
        impersonating = True
        yield
    finally:
        if impersonating and not security.RevertToSelf():
            # Continuing with a borrowed identity is unsafe.
            raise c.WinError(c.get_last_error())
        if duplicate:
            kernel.CloseHandle(duplicate)
        if token:
            kernel.CloseHandle(token)
        kernel.CloseHandle(process)


def verify_windows_access(targets, runtime_sid, *, runtime_pid: int | None = None):
    identity = windows_process_identity(runtime_pid)
    if identity["sid"] != _validated_sid(runtime_sid):
        raise ValueError("Windows verification token differs from the runtime SID")
    reports = []
    with _windows_runtime_token(runtime_pid):
        for target in targets:
            failures = []
            readable = True
            content_denied = True
            entries = tuple(_target_entries(target))
            for path in entries:
                readable = _windows_can_open(path, 1) and readable
                rights = {"write": 2, "append": 4, "write_ea": 0x10, "write_attributes": 0x100,
                          "delete": 0x10000, "change_acl": 0x40000, "change_owner": 0x80000}
                if path.is_dir():
                    rights["delete_child"] = 0x40
                for name, mask in rights.items():
                    if _windows_can_open(path, mask):
                        failures.append({"path": str(path), "right": name})
                        if name in {"write", "append", "write_ea", "write_attributes"}:
                            content_denied = False
            parent_denied = not _windows_can_open(_resolved(target.path).parent, 0x40)
            reports.append({"path": str(target.path), "readable": readable,
                            "write_denied": not failures and parent_denied,
                            "content_write_denied": content_denied,
                            "parent_replace_denied": parent_denied,
                            "entries_verified": len(entries), "allowed_mutations": failures[:8],
                            "runtime_token": identity})
    return reports


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
                    "-B",
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
# HASHI-Fingerprint: hfp_28d23d31323749878cfdc3431b47dd91
