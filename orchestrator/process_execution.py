"""Cross-platform process and shell execution contracts.

Every caller uses an explicit executable.  HASHI never relies on Python's
platform-dependent implicit shell selection.
"""

from __future__ import annotations

import asyncio
import base64
import locale
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


UTF8_ENCODING = "utf-8"
SUPPORTED_SHELLS = ("bash", "powershell", "cmd")


@dataclass(frozen=True)
class ShellInvocation:
    argv: tuple[str, ...]
    shell: str
    executable: str
    encoding: str = UTF8_ENCODING
    launcher: str = "direct"
    launcher_executable: str = ""


@dataclass(frozen=True)
class ArgvInvocation:
    argv: tuple[str, ...]
    requested_executable: str
    resolved_executable: str
    launcher: str
    launcher_executable: str
    encoding: str = UTF8_ENCODING


def is_wsl() -> bool:
    if os.name != "posix" or platform.system().casefold() != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(
            encoding="utf-8", errors="ignore"
        )
    except OSError:
        return False
    return "microsoft" in release.casefold()


def runtime_platform_name() -> str:
    if os.name == "nt":
        return "windows_native"
    if is_wsl():
        return "windows_wsl"
    if platform.system().casefold() == "darwin":
        return "macos"
    return "linux"


def default_shell_name() -> str:
    return "powershell" if os.name == "nt" else "bash"


def _which(candidates: Sequence[str]) -> str | None:
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return str(Path(resolved).resolve())
    return None


def _cmd_executable() -> str | None:
    configured = str(os.environ.get("COMSPEC") or "").strip()
    if configured:
        return configured
    return _which(("cmd.exe", "cmd"))


def _powershell_executable() -> str | None:
    candidates = (
        ("pwsh.exe", "powershell.exe", "pwsh", "powershell")
        if os.name == "nt"
        else ("pwsh", "powershell")
    )
    return _which(candidates)


def _bash_executable() -> str | None:
    return _which(("bash.exe", "bash")) if os.name == "nt" else _which(("bash",))


def _encoded_powershell_argv(executable: str, script: str) -> tuple[str, ...]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return (
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-OutputFormat",
        "Text",
        "-EncodedCommand",
        encoded,
    )


def _powershell_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def resolve_shell_invocation(
    command: str, requested_shell: str | None = None
) -> ShellInvocation:
    requested = str(requested_shell or "").strip().casefold()
    shell = default_shell_name() if requested in {"", "auto", "native"} else requested
    if shell in {"pwsh", "powershell.exe", "pwsh.exe"}:
        shell = "powershell"
    elif shell in {"cmd.exe", "command_prompt"}:
        shell = "cmd"
    elif shell in {"bash.exe"}:
        shell = "bash"
    if shell not in SUPPORTED_SHELLS:
        raise ValueError(
            f"unsupported shell {requested_shell!r}; expected one of "
            f"{', '.join(SUPPORTED_SHELLS)}"
        )

    if shell == "powershell":
        executable = _powershell_executable()
        if not executable:
            raise FileNotFoundError("PowerShell executable was not found")
        utf8_command = (
            "$OutputEncoding = [Console]::OutputEncoding = "
            "[System.Text.UTF8Encoding]::new($false);\n"
            "$ProgressPreference = 'SilentlyContinue';\n"
            "$global:LASTEXITCODE = 0;\n"
            "& {\n"
            f"{command}\n"
            "}\n"
            "$hashiCommandSucceeded = $?; $hashiNativeExit = $LASTEXITCODE;\n"
            "if ($null -ne $hashiNativeExit -and $hashiNativeExit -ne 0) "
            "{ exit $hashiNativeExit };\n"
            "if (-not $hashiCommandSucceeded) { exit 1 }"
        )
        return ShellInvocation(
            argv=(
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-OutputFormat",
                "Text",
                "-Command",
                utf8_command,
            ),
            shell=shell,
            executable=executable,
            launcher="powershell",
            launcher_executable=executable,
        )

    if shell == "cmd":
        if os.name != "nt":
            raise ValueError("cmd is available only in native Windows execution")
        executable = _cmd_executable()
        if not executable:
            raise FileNotFoundError("cmd.exe was not found")
        transport = _powershell_executable()
        if not transport:
            raise FileNotFoundError(
                "PowerShell executable was not found for safe CMD transport"
            )
        # Python's generic Windows argv quoting is the Microsoft C-runtime
        # dialect, not CMD's parser.  Passing a /c string containing quotes
        # through it changes the command (for example, quotes become literal
        # backslashes).  Carry the exact CMD text inside an encoded PowerShell
        # transport so CreateProcess receives no ambiguous nested quotes.
        cmd_payload = base64.b64encode(
            f"chcp 65001>nul & {command}".encode("utf-16-le")
        ).decode("ascii")
        transport_script = (
            "$OutputEncoding = [Console]::OutputEncoding = "
            "[System.Text.UTF8Encoding]::new($false);\n"
            "$ProgressPreference = 'SilentlyContinue';\n"
            "$global:LASTEXITCODE = 0;\n"
            "$hashiCmd = [Text.Encoding]::Unicode.GetString("
            f"[Convert]::FromBase64String('{cmd_payload}'));\n"
            "& $env:ComSpec /d /s /c $hashiCmd;\n"
            "exit $LASTEXITCODE"
        )
        return ShellInvocation(
            argv=_encoded_powershell_argv(transport, transport_script),
            shell=shell,
            executable=executable,
            launcher="powershell_encoded",
            launcher_executable=transport,
        )

    executable = _bash_executable()
    if not executable:
        raise FileNotFoundError("Bash executable was not found")
    return ShellInvocation(
        argv=(executable, "--noprofile", "--norc", "-c", command),
        shell=shell,
        executable=executable,
        launcher="bash",
        launcher_executable=executable,
    )


def resolve_argv_invocation(argv: Sequence[str]) -> ArgvInvocation:
    values = tuple(str(item) for item in argv)
    if not values or not values[0]:
        raise ValueError("argv requires a non-empty executable")
    requested = values[0]
    if os.name != "nt":
        return ArgvInvocation(
            argv=values,
            requested_executable=requested,
            resolved_executable=requested,
            launcher="direct",
            launcher_executable=requested,
        )

    resolved = shutil.which(requested)
    if resolved is None and Path(requested).exists():
        resolved = str(Path(requested).resolve())
    resolved = str(resolved or requested)
    suffix = Path(resolved).suffix.casefold()
    if suffix in {".cmd", ".bat"}:
        cmd = _cmd_executable()
        if not cmd:
            raise FileNotFoundError("cmd.exe was not found for .cmd/.bat execution")
        return ArgvInvocation(
            argv=(
                cmd,
                "/d",
                "/c",
                "chcp",
                "65001>nul",
                "&",
                "call",
                resolved,
                *values[1:],
            ),
            requested_executable=requested,
            resolved_executable=resolved,
            launcher="cmd",
            launcher_executable=cmd,
        )
    if suffix == ".ps1":
        powershell = _powershell_executable()
        if not powershell:
            raise FileNotFoundError(
                "PowerShell executable was not found for .ps1 execution"
            )
        command = " ".join(
            _powershell_literal(value) for value in (resolved, *values[1:])
        )
        script = (
            "$OutputEncoding = [Console]::OutputEncoding = "
            "[System.Text.UTF8Encoding]::new($false);\n"
            "$ProgressPreference = 'SilentlyContinue';\n"
            "$global:LASTEXITCODE = 0;\n"
            f"& {command};\n"
            "$hashiCommandSucceeded = $?; $hashiNativeExit = $LASTEXITCODE;\n"
            "if ($null -ne $hashiNativeExit -and $hashiNativeExit -ne 0) "
            "{ exit $hashiNativeExit };\n"
            "if (-not $hashiCommandSucceeded) { exit 1 }"
        )
        return ArgvInvocation(
            argv=_encoded_powershell_argv(powershell, script),
            requested_executable=requested,
            resolved_executable=resolved,
            launcher="powershell",
            launcher_executable=powershell,
        )
    return ArgvInvocation(
        argv=(resolved, *values[1:]),
        requested_executable=requested,
        resolved_executable=resolved,
        launcher="direct",
        launcher_executable=resolved,
    )


def process_group_kwargs() -> dict[str, Any]:
    if os.name == "posix":
        return {"start_new_session": True}
    creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return {"creationflags": creation_flag} if creation_flag else {}


def process_is_alive(pid: int | str | None) -> bool:
    """Probe a PID without signalling it.

    ``os.kill(pid, 0)`` is a harmless existence check on POSIX. On Windows,
    signal 0 is ``CTRL_C_EVENT`` and can interrupt every process sharing the
    console. Use the kernel query API there instead.
    """

    try:
        value = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(value, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            value,
        )
        if not handle:
            # Access denied still proves the PID exists.
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


async def terminate_windows_process_tree(pid: int, *, force: bool) -> dict[str, Any]:
    """Terminate one native Windows process tree with the OS-owned utility."""

    if os.name != "nt":
        raise RuntimeError("Windows process-tree termination requested off Windows")
    executable = shutil.which("taskkill.exe") or shutil.which("taskkill")
    if not executable:
        return {
            "returncode": None,
            "output": "taskkill.exe was not found",
            "force": bool(force),
        }
    argv = [executable, "/PID", str(int(pid)), "/T"]
    if force:
        argv.append("/F")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _stderr = await proc.communicate()
    return {
        "returncode": proc.returncode,
        "output": decode_process_output(stdout),
        "force": bool(force),
    }


def decode_process_output(content: bytes, *, encoding: str = UTF8_ENCODING) -> str:
    if not content:
        return ""
    try:
        return content.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError):
        candidates: list[str] = []
        if os.name == "nt":
            try:
                import ctypes

                candidates.append(f"cp{int(ctypes.windll.kernel32.GetOEMCP())}")
            except Exception:
                candidates.append("mbcs")
        candidates.append(locale.getpreferredencoding(False) or UTF8_ENCODING)
        for candidate in candidates:
            try:
                return content.decode(candidate, errors="strict")
            except (LookupError, UnicodeDecodeError):
                continue
    return content.decode(encoding, errors="replace")


def execution_environment_descriptor(cwd: str | Path | None = None) -> dict[str, Any]:
    current = Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
    default_shell = default_shell_name()
    try:
        shell_invocation = resolve_shell_invocation("", default_shell)
        shell_executable = shell_invocation.executable
    except (FileNotFoundError, ValueError):
        shell_executable = ""
    available_shells = []
    for shell in SUPPORTED_SHELLS:
        try:
            resolve_shell_invocation("", shell)
        except (FileNotFoundError, ValueError):
            continue
        available_shells.append(shell)
    return {
        "schema_version": 1,
        "runtime_platform": runtime_platform_name(),
        "operating_system": platform.system() or os.name,
        "working_directory": str(current),
        "path_style": "windows" if os.name == "nt" else "posix",
        "path_separator": os.sep,
        "text_encoding": UTF8_ENCODING,
        "python_executable": str(Path(sys.executable).resolve()),
        "shell_tool": {
            "name": "shell",
            "legacy_alias": "bash",
            "default_shell": default_shell,
            "default_executable": shell_executable,
            "available_shells": available_shells,
            "selector_argument": "shell",
            "implicit_shell": False,
        },
        "argv_execution": {
            "implicit_shell": False,
            "windows_cmd_bat_auto_wrapped": os.name == "nt",
        },
    }
