"""Prove a WSL DrvFS path against its actual Windows object.

This is filesystem identity evidence, not user authentication. The caller must
already be authenticated and must bind the result to its operation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
from pathlib import Path
from typing import Any


class WslPathIdentityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_POWERSHELL = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class HashiWslFileIdentity {
 [StructLayout(LayoutKind.Sequential)] public struct Info {
  public UInt32 attrs, creationLow, creationHigh, accessLow, accessHigh,
   writeLow, writeHigh, volume, sizeHigh, sizeLow, links, indexHigh, indexLow;
 }
 [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 private static extern SafeFileHandle CreateFileW(string name, UInt32 access,
  UInt32 share, IntPtr sec, UInt32 create, UInt32 flags, IntPtr template);
 [DllImport("kernel32.dll", SetLastError=true)]
 private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out Info info);
 public static string Read(string path) {
  using (var h=CreateFileW(path, 0x80, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero)) {
   if(h.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
   Info i; if(!GetFileInformationByHandle(h,out i)) throw new Win32Exception(Marshal.GetLastWin32Error());
   return i.volume.ToString()+":"+(((UInt64)i.indexHigh<<32)|i.indexLow).ToString();
  }
 }
}
'@
$paths = ConvertFrom-Json -InputObject ([Console]::In.ReadToEnd())
$result = New-Object System.Collections.Generic.List[object]
foreach ($item in @($paths)) {
 try {
  $pair = [HashiWslFileIdentity]::Read([string]$item).Split(":")
  $result.Add(@{device=$pair[0]; inode=$pair[1]})
 } catch { $result.Add($null) }
}
[Console]::Out.WriteLine((ConvertTo-Json -InputObject $result.ToArray() -Compress -Depth 3))
""".strip()


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def assert_matching_drvfs_mount(path: str, mountinfo: str | None = None) -> None:
    """Require the longest applicable mount to be the actual matching drive."""
    match = re.match(r"^/mnt/([a-z])(?:/|$)", path)
    if not match or not os.environ.get("WSL_DISTRO_NAME"):
        raise WslPathIdentityError("attachment_reselect_required", "Path is not on a verified Windows drive.")
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError as exc:
            raise WslPathIdentityError("attachment_identity_unavailable", "Drive identity is unavailable.") from exc
    candidates = []
    for line in mountinfo.splitlines():
        fields = line.split(" ")
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator < 6 or len(fields) <= separator + 3:
            continue
        mountpoint = _unescape_mount(fields[4])
        if path != mountpoint and not path.startswith(mountpoint.rstrip("/") + "/"):
            continue
        candidates.append((len(mountpoint), mountpoint, _unescape_mount(fields[3]),
                           fields[separator + 1], _unescape_mount(fields[separator + 2]),
                           _unescape_mount(" ".join(fields[separator + 3:]))))
    if not candidates:
        raise WslPathIdentityError("attachment_reselect_required", "Drive mount is unavailable.")
    _, mountpoint, root, kind, source, options = max(candidates)
    drive = match.group(1).upper() + ":\\"
    if (mountpoint != "/mnt/" + match.group(1) or root != "/" or kind not in {"9p", "drvfs"}
            or source.upper() != drive or (kind == "9p" and "aname=drvfs;" not in options)):
        raise WslPathIdentityError("attachment_reselect_required", "Path is not on the matching Windows drive.")


def _windows_system_executable(*parts: str) -> str:
    system_root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
    match = re.fullmatch(r"([A-Za-z]):[\\/](.+)", system_root)
    if not match or any(part in {"", ".", ".."} for part in re.split(r"[\\/]+", match.group(2))):
        raise WslPathIdentityError("attachment_identity_unavailable", "Windows system path is unavailable.")
    root = "/mnt/" + match.group(1).lower() + "/" + match.group(2).replace("\\", "/")
    executable = posixpath.join(root, "System32", *parts)
    assert_matching_drvfs_mount(executable)
    return executable


def _interop_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("HASHI", "BRIDGE", "KASUMI")) and key.upper() != "WSLENV"}


def windows_host_id() -> str:
    """Return the same one-way host fingerprint as native Windows, or empty."""
    try:
        executable = _windows_system_executable("reg.exe")
        result = subprocess.run(
            [executable, "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid", "/reg:64"],
            env=_interop_environment(), capture_output=True, text=True, timeout=15, check=True,
        )
        match = re.search(r"^\s*MachineGuid\s+REG_SZ\s+([^\r\n]+)$", result.stdout, re.MULTILINE | re.IGNORECASE)
        guid = match.group(1).strip().lower() if match else ""
        if not guid:
            return ""
        return hashlib.sha256(("hashi-physical-host-v1\nwindows\n" + guid).encode()).hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def windows_to_wsl(path: str) -> str:
    if not isinstance(path, str) or len(path) > 4096 or "\x00" in path:
        raise WslPathIdentityError("attachment_reselect_required", "Windows path is invalid.")
    match = re.fullmatch(r"([A-Za-z]):[\\/](.*)", path)
    if not match:
        raise WslPathIdentityError("attachment_reselect_required", "Windows path is invalid.")
    parts = re.split(r"[\\/]+", match.group(2))
    if any(part in {".", ".."} for part in parts):
        raise WslPathIdentityError("attachment_reselect_required", "Windows path is invalid.")
    return "/mnt/" + match.group(1).lower() + ("/" + "/".join(parts) if parts != [""] else "")


def _wsl_to_windows(path: str) -> str:
    match = re.fullmatch(r"/mnt/([a-z])(?:/(.*))?", path)
    if not match:
        raise WslPathIdentityError("attachment_reselect_required", "Canonical path is not a Windows drive.")
    return match.group(1).upper() + ":\\" + str(match.group(2) or "").replace("/", "\\")


def _windows_file_identities(paths: list[str]) -> list[Any]:
    try:
        executable = _windows_system_executable("WindowsPowerShell", "v1.0", "powershell.exe")
        encoded = base64.b64encode(_POWERSHELL.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            input=json.dumps(paths, ensure_ascii=False).encode("utf-8"),
            env=_interop_environment(), capture_output=True, timeout=60, check=True,
        )
        if len(result.stdout) > max(2048, len(paths) * 160):
            raise ValueError("identity response is too large")
        identities = json.loads(result.stdout.decode("utf-8-sig"))
        if not isinstance(identities, list) or len(identities) != len(paths):
            raise ValueError("identity response is invalid")
        return identities
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        raise WslPathIdentityError("attachment_identity_unavailable", "Windows file identity is unavailable.") from exc


def verify_windows_paths(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve and compare each selected Windows object from this WSL target."""
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise WslPathIdentityError("attachment_manifest_invalid", "Path verification request is invalid.")
    seen: set[str] = set()
    observed = []
    for item in entries:
        attachment_id, windows_path, expected = item.get("attachment_id"), item.get("windows_path"), item.get("target_identity")
        if (not isinstance(attachment_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{16,64}", attachment_id)
                or attachment_id in seen or not isinstance(expected, dict)
                or expected.get("platform") != "win32"
                or not isinstance(expected.get("device"), str)
                or not re.fullmatch(r"\d{1,40}", expected["device"])
                or not isinstance(expected.get("inode"), str)
                or not re.fullmatch(r"\d{1,40}", expected["inode"])
                or expected["inode"] == "0"):
            raise WslPathIdentityError("attachment_manifest_invalid", "Selected target identity is invalid.")
        seen.add(attachment_id)
        wsl_path = windows_to_wsl(windows_path)
        assert_matching_drvfs_mount(wsl_path)
        try:
            canonical = os.path.realpath(wsl_path, strict=True)
            assert_matching_drvfs_mount(canonical)
            initial = os.stat(canonical)
        except WslPathIdentityError:
            raise
        except OSError as exc:
            raise WslPathIdentityError("attachment_unavailable", "Selected path is unavailable on the target.") from exc
        if not (stat.S_ISREG(initial.st_mode) or stat.S_ISDIR(initial.st_mode)):
            raise WslPathIdentityError("attachment_unsupported_type", "Selected path is not a file or directory.")
        observed.append((item, canonical, initial))
    if not observed:
        return []
    # Windows and WSL can resolve a link differently. Compare the Windows
    # object for both the selected path and the actual WSL canonical path.
    native_paths = [path for item, canonical, _ in observed
                    for path in (str(item["windows_path"]), _wsl_to_windows(canonical))]
    native = _windows_file_identities(native_paths)
    if len(native) != len(native_paths):
        raise WslPathIdentityError("attachment_identity_unavailable", "Selected target identity is unavailable.")
    results = []
    for index, (item, canonical, initial) in enumerate(observed):
        expected = item["target_identity"]
        for actual in native[index * 2:index * 2 + 2]:
            if (not isinstance(actual, dict) or not re.fullmatch(r"\d{1,40}", str(actual.get("device") or ""))
                    or not re.fullmatch(r"\d{1,40}", str(actual.get("inode") or ""))):
                raise WslPathIdentityError("attachment_identity_unavailable", "Selected target identity is unavailable.")
            if actual["device"] != expected["device"] or actual["inode"] != expected["inode"]:
                raise WslPathIdentityError("attachment_changed", "Selected target was replaced.")
        try:
            current_path = os.path.realpath(windows_to_wsl(item["windows_path"]), strict=True)
            assert_matching_drvfs_mount(current_path)
            current = os.stat(current_path)
        except OSError as exc:
            raise WslPathIdentityError("attachment_unavailable", "Selected path became unavailable.") from exc
        if current_path != canonical or (current.st_dev, current.st_ino, stat.S_IFMT(current.st_mode)) != (
            initial.st_dev, initial.st_ino, stat.S_IFMT(initial.st_mode)
        ):
            raise WslPathIdentityError("attachment_changed", "Selected target was replaced.")
        results.append({"attachment_id": item["attachment_id"], "accessible_path": canonical})
    return results
