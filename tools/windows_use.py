"""
Windows desktop-use tools for HASHI.

Purpose:
  - Let HASHI agents running on Windows or inside WSL control the real
    Windows desktop via a thin tool layer.
  - Keep the public API parallel to the existing Linux `desktop_*` tier.

Initial backend:
  - `usecomputer` on the Windows host, invoked through PowerShell.

Design notes:
  - Works from WSL by shelling out to `powershell.exe`.
  - Future provider expansion can add `windows-mcp` without changing tool names.
  - This tool targets the interactive Windows desktop, so the host usually needs
    to be unlocked for reliable screenshots and focus-sensitive actions.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import subprocess
from pathlib import Path
from urllib import request as urllib_request
from urllib.error import URLError, HTTPError

logger = logging.getLogger("Tools.WindowsUse")
_WINDOWS_HELPER_ENV = "HASHI_WINDOWS_HELPER"
_WINDOWS_HELPER_PORT = int(os.environ.get("HASHI_WINDOWS_HELPER_PORT", "47831"))


def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _powershell_exe() -> str | None:
    if os.name == "nt":
        return "powershell.exe"
    if _is_wsl():
        return "powershell.exe"
    return None


def _normalize_provider(provider: str | None) -> str:
    value = (provider or "auto").strip().lower()
    if value in {"", "auto"}:
        return "auto"
    if value == "usecomputer":
        return value
    if value == "windows-mcp":
        return value
    return "invalid"


def _windows_helper_enabled() -> bool:
    return os.environ.get(_WINDOWS_HELPER_ENV, "1").strip().lower() not in {"0", "false", "off", "no"}


def _windows_helper_base_url() -> str:
    return f"http://127.0.0.1:{_WINDOWS_HELPER_PORT}"


def _resolve_windows_save_path(path_value: str | None) -> tuple[str | None, str | None]:
    if not path_value:
        return None, None

    if os.name == "nt":
        return str(Path(path_value)), None

    if _is_wsl():
        try:
            result = subprocess.run(
                ["wslpath", "-w", path_value],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return None, f"failed to convert save_path with wslpath: {exc}"
        converted = result.stdout.strip()
        if not converted:
            return None, "failed to convert save_path with wslpath: empty output"
        return converted, None

    return None, "windows_use is only supported on Windows hosts or inside WSL"


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_bool(flag: bool) -> str:
    return "$true" if flag else "$false"


def _build_prelude() -> str:
    return r"""
$ErrorActionPreference = "Stop"

function Resolve-UsecomputerPath {
    if ($env:HASHI_WINDOWS_USECOMPUTER_BIN -and (Test-Path $env:HASHI_WINDOWS_USECOMPUTER_BIN)) {
        return $env:HASHI_WINDOWS_USECOMPUTER_BIN
    }

    $candidates = @(
        (Join-Path $env:APPDATA "npm\usecomputer.cmd"),
        (Join-Path $env:APPDATA "npm\usecomputer.ps1")
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    $cmd = Get-Command usecomputer -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return $null
}

function Invoke-Usecomputer {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Args
    )

    $uc = Resolve-UsecomputerPath
    if (-not $uc) {
        throw "usecomputer not found on Windows host. Install with: npm install -g usecomputer"
    }

    $stdoutFile = Join-Path $env:TEMP ("hashi-usecomputer-stdout-" + [guid]::NewGuid().ToString() + ".log")
    $stderrFile = Join-Path $env:TEMP ("hashi-usecomputer-stderr-" + [guid]::NewGuid().ToString() + ".log")
    try {
        $proc = Start-Process -FilePath $uc -ArgumentList $Args -WorkingDirectory $env:TEMP -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
        $stdout = if (Test-Path $stdoutFile) { Get-Content -LiteralPath $stdoutFile -Raw -ErrorAction SilentlyContinue } else { "" }
        $stderr = if (Test-Path $stderrFile) { Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue } else { "" }
        $output = (($stdout, $stderr) -join "`n").Trim()
        return @{
            path = $uc
            output = $output
            exit_code = $proc.ExitCode
        }
    } finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

function Resolve-UvInvocation {
    if ($env:HASHI_WINDOWS_UV_BIN -and (Test-Path $env:HASHI_WINDOWS_UV_BIN)) {
        return @{
            command = $env:HASHI_WINDOWS_UV_BIN
            base_args = @()
        }
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        return @{
            command = $uv.Source
            base_args = @()
        }
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        return @{
            command = $py.Source
            base_args = @('-m', 'uv')
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return @{
            command = $python.Source
            base_args = @('-m', 'uv')
        }
    }

    return $null
}

function Invoke-UvPythonScriptJson {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ScriptPath,
        [Parameter(Mandatory = $true)]
        [string]$PayloadBase64,
        [Parameter(Mandatory = $true)]
        [string[]]$WithPackages
    )

    $uv = Resolve-UvInvocation
    if (-not $uv) {
        throw "uv not found on Windows host. Install with: python -m pip install uv"
    }

    $argsList = @()
    $argsList += $uv.base_args
    $argsList += @('run', '--no-project')
    foreach ($pkg in $WithPackages) {
        $argsList += @('--with', $pkg)
    }
    $argsList += @('python', $ScriptPath, $PayloadBase64)

    $stdoutFile = Join-Path $env:TEMP ("hashi-windows-use-stdout-" + [guid]::NewGuid().ToString() + ".log")
    $stderrFile = Join-Path $env:TEMP ("hashi-windows-use-stderr-" + [guid]::NewGuid().ToString() + ".log")
    try {
        $proc = Start-Process -FilePath $uv.command -ArgumentList $argsList -NoNewWindow -Wait -PassThru `
            -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
        $stdout = if (Test-Path $stdoutFile) { Get-Content -LiteralPath $stdoutFile -Raw -ErrorAction SilentlyContinue } else { "" }
        $stderr = if (Test-Path $stderrFile) { Get-Content -LiteralPath $stderrFile -Raw -ErrorAction SilentlyContinue } else { "" }
        $output = (($stdout, $stderr) -join "`n").Trim()
        $jsonLine = ($output -split "`r?`n" | Where-Object { $_ -like 'HASHI_JSON:*' } | Select-Object -Last 1)
        if (-not $jsonLine) {
            throw ("no HASHI_JSON line returned. Raw output: " + $output)
        }
        return @{
            raw = $output
            json = $jsonLine.Substring(11)
            exit_code = $proc.ExitCode
        }
    } finally {
        Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

if (-not ("HashiWin" -as [type])) {
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;

public static class HashiWin {
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);

    [DllImport("user32.dll")]
    public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool BringWindowToTop(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool PostMessage(IntPtr hWnd, uint Msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);

    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll")]
    public static extern IntPtr GetKeyboardLayout(uint idThread);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern bool GetKeyboardLayoutName(StringBuilder pwszKLID);
}
"@
}

function Get-HashiWindowList {
    $items = New-Object System.Collections.Generic.List[object]
    $callback = [HashiWin+EnumWindowsProc]{
        param([IntPtr]$hWnd, [IntPtr]$lParam)
        if (-not [HashiWin]::IsWindowVisible($hWnd)) {
            return $true
        }
        $length = [HashiWin]::GetWindowTextLength($hWnd)
        if ($length -le 0) {
            return $true
        }
        $sb = New-Object System.Text.StringBuilder ($length + 1)
        [void][HashiWin]::GetWindowText($hWnd, $sb, $sb.Capacity)
        $title = $sb.ToString()
        if ([string]::IsNullOrWhiteSpace($title)) {
            return $true
        }
        [uint32]$windowPid = 0
        [void][HashiWin]::GetWindowThreadProcessId($hWnd, [ref]$windowPid)
        $procName = $null
        try {
            $procName = (Get-Process -Id $windowPid -ErrorAction Stop).ProcessName
        } catch {}
        $items.Add([pscustomobject]@{
            id = [int64]$hWnd
            pid = [int]$windowPid
            ownerName = $procName
            title = $title
        }) | Out-Null
        return $true
    }
    [void][HashiWin]::EnumWindows($callback, [IntPtr]::Zero)
    return $items
}

function Resolve-HashiWindow {
    param(
        [Int64]$WindowId = 0,
        [int]$TargetPid = 0,
        [string]$TitleContains = "",
        [string]$ExactTitle = ""
    )

    $windows = Get-HashiWindowList
    if ($WindowId) {
        return $windows | Where-Object { $_.id -eq $WindowId } | Select-Object -First 1
    }
    if ($TargetPid) {
        return $windows | Where-Object { $_.pid -eq $TargetPid } | Select-Object -First 1
    }
    if ($ExactTitle) {
        return $windows | Where-Object { $_.title -eq $ExactTitle } | Select-Object -First 1
    }
    if ($TitleContains) {
        return $windows | Where-Object { $_.title -like ("*" + $TitleContains + "*") } | Select-Object -First 1
    }
    return $null
}

function Reset-HashiInputState {
    $keyUp = 0x0002
    foreach ($vk in @(0x10, 0x11, 0x12, 0x5B, 0x5C)) {
        [HashiWin]::keybd_event([byte]$vk, 0, $keyUp, [UIntPtr]::Zero)
    }
    foreach ($flag in @(0x0004, 0x0010, 0x0040)) {
        [HashiWin]::mouse_event([uint32]$flag, 0, 0, 0, [UIntPtr]::Zero)
    }
    [pscustomobject]@{
        ok = $true
        released_keys = @("SHIFT", "CTRL", "ALT", "LWIN", "RWIN")
        released_mouse = @("left", "right", "middle")
    }
}

function Get-HashiInputState {
    $foreground = [HashiWin]::GetForegroundWindow()
    [uint32]$foregroundPid = 0
    [void][HashiWin]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
    $foregroundThread = [HashiWin]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
    $layoutHandle = [HashiWin]::GetKeyboardLayout($foregroundThread)
    $layoutHex = ("0x{0:X8}" -f ([int64]$layoutHandle -band 0xFFFFFFFF))
    $layoutSb = New-Object System.Text.StringBuilder 9
    $layoutNameOk = [HashiWin]::GetKeyboardLayoutName($layoutSb)
    $window = $null
    if ($foreground -ne [IntPtr]::Zero) {
        $window = Resolve-HashiWindow -WindowId ([int64]$foreground)
    }
    [pscustomobject]@{
        foreground_window = $window
        keyboard_layout = [pscustomobject]@{
            hkl = $layoutHex
            klid = $(if ($layoutNameOk) { $layoutSb.ToString() } else { $null })
        }
    }
}
"""


async def _run_powershell(script: str, timeout: int = 30) -> tuple[int, str, str]:
    pwsh = _powershell_exe()
    if not pwsh:
        return 1, "", "powershell.exe unavailable; windows_use requires Windows or WSL"

    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    cmd = [
        pwsh,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]
    logger.debug("windows_use cmd via %s", pwsh)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode or 0
        out = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        if rc != 0:
            logger.warning("windows_use PowerShell exit %d stderr=%s", rc, err or out)
        return rc, out, err
    except asyncio.TimeoutError:
        return 1, "", f"command timed out after {timeout}s"
    except FileNotFoundError as exc:
        return 1, "", str(exc)


async def _run_powershell_json(body: str, timeout: int = 30) -> tuple[dict | None, str | None]:
    rc, out, err = await _run_powershell(_build_prelude() + body, timeout=timeout)
    if rc != 0:
        return None, err or out or f"PowerShell exited with {rc}"
    try:
        return json.loads(out), None
    except json.JSONDecodeError as exc:
        logger.error("windows_use JSON decode failed: %s | raw=%r", exc, out[:500])
        return None, f"invalid JSON response from Windows host: {exc}"


async def _helper_post(action: str, args: dict, timeout: int = 30) -> tuple[str | None, str | None]:
    payload = json.dumps({"action": action, "args": args}, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        _windows_helper_base_url() + "/action",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    def _do_request():
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(_do_request)
        return str(data.get("output", "")), None
    except HTTPError as exc:
        return None, f"helper HTTP error: {exc.code}"
    except URLError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, str(exc)


async def _helper_healthcheck(timeout: int = 3) -> bool:
    req = urllib_request.Request(_windows_helper_base_url() + "/health", method="GET")

    def _do_request():
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(_do_request)
        return bool(data.get("ok"))
    except Exception:
        return False


async def _ensure_windows_helper_started() -> bool:
    if await _helper_healthcheck():
        return True

    repo_root_path, repo_root_error = _resolve_windows_save_path(str(Path(__file__).resolve().parent.parent))
    if repo_root_error:
        logger.warning("windows helper repo root resolution failed: %s", repo_root_error)
        return False

    body = f"""
$repoRoot = {_ps_quote(repo_root_path)}
$logDir = Join-Path $env:LOCALAPPDATA "HASHI\\windows_helper\\logs"
$helperWorkingDir = Join-Path $env:LOCALAPPDATA "HASHI\\windows_helper"
New-Item -ItemType Directory -Force -Path $helperWorkingDir | Out-Null
$uv = Resolve-UvInvocation
if (-not $uv) {{
    throw "uv not found on Windows host. Install with: python -m pip install uv"
}}
$argsList = @()
$argsList += $uv.base_args
$argsList += @('run', '--no-project', '--with', 'fastapi', '--with', 'uvicorn', '--with', 'fastmcp', '--with', 'windows-mcp', '--with', 'pillow', 'python', '-m', 'tools.windows_helper.server', '--host', '127.0.0.1', '--port', {_ps_quote(str(_WINDOWS_HELPER_PORT))}, '--log-dir', $logDir)
$env:PYTHONPATH = $(if ($env:PYTHONPATH) {{ $repoRoot + ';' + $env:PYTHONPATH }} else {{ $repoRoot }})
Start-Process -FilePath $uv.command -ArgumentList $argsList -WorkingDirectory $helperWorkingDir | Out-Null
@{{ ok = $true }} | ConvertTo-Json -Compress
"""
    data, error = await _run_powershell_json(body, timeout=20)
    if error or not data or not data.get("ok"):
        logger.warning("windows helper start failed: %s", error or data)
        return False

    for _ in range(20):
        await asyncio.sleep(0.25)
        if await _helper_healthcheck():
            return True
    logger.warning("windows helper did not become healthy in time")
    return False


async def _maybe_execute_windows_helper(action: str, args: dict) -> str | None:
    if not _windows_helper_enabled():
        return None
    output, error = await _helper_post(action, args, timeout=60)
    if output is not None:
        return output
    if error and "HTTP error: 500" in error:
        await _best_effort_reset_windows_input_state()
        output, retry_error = await _helper_post(action, args, timeout=60)
        if output is not None:
            return output
        error = retry_error or error
    if not await _ensure_windows_helper_started():
        logger.info("windows helper unavailable for %s, falling back to local path (%s)", action, error)
        return None
    output, error = await _helper_post(action, args, timeout=60)
    if output is not None:
        return output
    logger.warning("windows helper call failed for %s, falling back to local path: %s", action, error)
    return None


async def _run_usecomputer_json(body: str, timeout: int = 30) -> tuple[dict | None, str | None]:
    return await _run_powershell_json(body, timeout=timeout)


def _resolve_provider(provider: str | None, action: str) -> str:
    normalized = _normalize_provider(provider)
    if normalized != "auto":
        return normalized
    preferred = {
        "screenshot": "usecomputer",
        "mouse_move": "usecomputer",
        "click": "usecomputer",
        "drag": "usecomputer",
        "scroll": "usecomputer",
        "type": "usecomputer",
        "key": "usecomputer",
        "window_list": "usecomputer",
        "window_focus": "usecomputer",
        "window_close": "usecomputer",
        "info": "usecomputer",
    }
    return preferred.get(action, "usecomputer")


def _provider_error(provider: str | None) -> str | None:
    normalized = _normalize_provider(provider)
    if normalized == "invalid":
        return "Error: provider must be one of: auto, usecomputer, windows-mcp"
    return None


def _is_auto_provider(provider: str | None) -> bool:
    return _normalize_provider(provider) == "auto"


def _window_selector_args(args: dict) -> tuple[int, int, str, str]:
    return (
        int(args.get("window_id", 0) or 0),
        int(args.get("pid", 0) or 0),
        str(args.get("title_contains", "") or ""),
        str(args.get("exact_title", "") or ""),
    )


def _focus_window_snippet(window_id: int, pid: int, title_contains: str, exact_title: str) -> str:
    return f"""
$target = Resolve-HashiWindow -WindowId {window_id} -TargetPid {pid} -TitleContains {_ps_quote(title_contains)} -ExactTitle {_ps_quote(exact_title)}
if (-not $target) {{
    throw "target window not found"
}}
$handle = [IntPtr]$target.id
if ([HashiWin]::IsIconic($handle)) {{
    [void][HashiWin]::ShowWindowAsync($handle, 9)
}} else {{
    [void][HashiWin]::ShowWindowAsync($handle, 5)
}}
Start-Sleep -Milliseconds 120
[void][HashiWin]::BringWindowToTop($handle)
[void][HashiWin]::SetForegroundWindow($handle)
"""


def _normalize_ps_value(value):
    if isinstance(value, dict):
        if set(value.keys()) == {"value", "Count"} and isinstance(value.get("value"), list):
            return [_normalize_ps_value(item) for item in value["value"]]
        return {key: _normalize_ps_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_normalize_ps_value(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return _normalize_ps_value(json.loads(stripped))
            except json.JSONDecodeError:
                return value
    return value


def _windows_mcp_helper_path() -> tuple[str | None, str | None]:
    helper = Path(__file__).with_name("windows_use_mcp_client.py")
    return _resolve_windows_save_path(str(helper))


async def _run_windows_mcp_json(payload: dict, timeout: int = 90) -> tuple[dict | None, str | None]:
    helper_path, helper_error = _windows_mcp_helper_path()
    if helper_error:
        return None, helper_error
    payload_b64 = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    body = f"""
$result = Invoke-UvPythonScriptJson -ScriptPath {_ps_quote(helper_path)} -PayloadBase64 {_ps_quote(payload_b64)} -WithPackages @('fastmcp', 'windows-mcp')
$obj = $result.json | ConvertFrom-Json
$obj | ConvertTo-Json -Compress -Depth 8
"""
    data, error = await _run_powershell_json(body, timeout=timeout)
    if error:
        return None, error
    if not data:
        return None, "empty response from windows-mcp helper"
    data = _normalize_ps_value(data)
    if data.get("ok") is False:
        return None, data.get("error") or "windows-mcp helper returned failure"
    return data, None


async def _best_effort_reset_windows_input_state() -> None:
    data, error = await _run_powershell_json(
        """
$result = Reset-HashiInputState
$result | ConvertTo-Json -Compress -Depth 4
""",
        timeout=10,
    )
    if error:
        logger.warning("windows_use input-state reset failed: %s", error)
        return
    logger.debug("windows_use input-state reset: %s", data)


async def _get_windows_input_state() -> tuple[dict | None, str | None]:
    data, error = await _run_powershell_json(
        """
$result = Get-HashiInputState
$result | ConvertTo-Json -Compress -Depth 6
""",
        timeout=10,
    )
    if error:
        return None, error
    return _normalize_ps_value(data), None


def _extract_mcp_text(content: list[dict] | None) -> str:
    parts = []
    for item in content or []:
        if item.get("type") == "text":
            text = str(item.get("text", "")).strip()
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _extract_mcp_image(content: list[dict] | None) -> tuple[str | None, str | None]:
    for item in content or []:
        if item.get("type") == "image" and item.get("data"):
            return item.get("data"), item.get("mimeType", "image/png")
    return None, None


async def execute_windows_screenshot(args: dict) -> str:
    save_path, save_path_error = _resolve_windows_save_path(args.get("save_path"))
    if save_path_error:
        return f"Error: {save_path_error}"

    helper_args = dict(args)
    if save_path:
        helper_args["save_path"] = save_path

    helper_result = await _maybe_execute_windows_helper("screenshot", helper_args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "screenshot")

    annotate = bool(args.get("annotate", False))
    display = args.get("display")
    window = args.get("window")

    if provider == "windows-mcp":
        mcp_args: dict = {}
        if annotate:
            mcp_args["use_annotation"] = True
        if display is not None:
            mcp_args["display"] = [int(display)]
        if window is not None:
            if not _is_auto_provider(requested_provider):
                return "Error: windows-mcp screenshot provider does not yet support window-targeted capture in windows_use"
            provider = "usecomputer"
        else:
            data, error = await _run_windows_mcp_json({"tool": "Screenshot", "arguments": mcp_args}, timeout=120)
            if error:
                if not _is_auto_provider(requested_provider):
                    return f"Error: screenshot failed: {error}"
                provider = "usecomputer"
            else:
                content = data.get("content") or []
                image_b64, mime_type = _extract_mcp_image(content)
                text = _extract_mcp_text(content)
                if not image_b64:
                    if not _is_auto_provider(requested_provider):
                        return f"Error: screenshot failed: no image returned from windows-mcp. Text: {text}"
                    provider = "usecomputer"
                else:
                    raw_bytes = base64.b64decode(image_b64)
                    if save_path:
                        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                        Path(save_path).write_bytes(raw_bytes)
                    size_kb = len(raw_bytes) // 1024
                    saved_note = f"\nSaved to: {save_path}" if save_path else ""
                    return (
                        f"Windows screenshot OK — provider=windows-mcp, {size_kb}KB\n"
                        f"Details: {text}{saved_note}\n"
                        f"data:{mime_type or 'image/png'};base64,{image_b64}"
                    )

    extra = []
    if annotate:
        extra.append("$argsList += @('--annotate')")
    if display is not None:
        extra.append(f"$argsList += @('--display', {_ps_quote(str(display))})")
    if window is not None:
        extra.append(f"$argsList += @('--window', {_ps_quote(str(window))})")
    save_snippet = ""
    if save_path:
        save_snippet = f"$dest = {_ps_quote(save_path)}; Copy-Item -LiteralPath $tmp -Destination $dest -Force | Out-Null"

    body = f"""
$tmp = Join-Path $env:TEMP ("hashi-windows-use-" + [guid]::NewGuid().ToString() + ".png")
try {{
    $argsList = @('screenshot', $tmp, '--json')
    {'; '.join(extra) if extra else ''}
    $result = Invoke-Usecomputer -Args $argsList
    if ($result.exit_code -ne 0) {{
        throw ($result.output)
    }}
    if (-not (Test-Path $tmp)) {{
        throw "screenshot file was not created"
    }}
    $bytes = [System.IO.File]::ReadAllBytes($tmp)
    {save_snippet}
    $meta = $null
    try {{
        $meta = $result.output | ConvertFrom-Json
    }} catch {{
        $meta = $result.output
    }}
    @{{
        ok = $true
        provider = 'usecomputer'
        usecomputer_path = $result.path
        saved_to = {(_ps_quote(save_path) if save_path else '$null')}
        file_size = $bytes.Length
        metadata = $meta
        base64 = [Convert]::ToBase64String($bytes)
    }} | ConvertTo-Json -Compress -Depth 8
}} finally {{
    if (Test-Path $tmp) {{
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }}
}}
"""
    data, error = await _run_usecomputer_json(body, timeout=45)
    if error:
        return f"Error: screenshot failed: {error}"
    if not data:
        return "Error: screenshot failed: empty response"

    data = _normalize_ps_value(data)
    size_kb = int(data.get("file_size", 0)) // 1024
    metadata = data.get("metadata")
    saved = data.get("saved_to")
    saved_note = f"\nSaved to: {saved}" if saved else ""
    return (
        f"Windows screenshot OK — provider=usecomputer, {size_kb}KB\n"
        f"Metadata: {json.dumps(metadata, ensure_ascii=False)}{saved_note}\n"
        f"data:image/png;base64,{data.get('base64', '')}"
    )


async def execute_windows_mouse_move(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("mouse_move", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "mouse_move")

    x = args.get("x")
    y = args.get("y")
    if x is None or y is None:
        return "Error: x and y are required"

    if provider == "windows-mcp":
        data, error = await _run_windows_mcp_json({"tool": "Move", "arguments": {"loc": [int(x), int(y)]}})
        await _best_effort_reset_windows_input_state()
        if error:
            if not _is_auto_provider(requested_provider):
                return f"Error: mouse move failed: {error}"
            provider = "usecomputer"
        else:
            text = _extract_mcp_text(data.get("content"))
            return text or f"Mouse moved to ({x}, {y}) on Windows host via windows-mcp"

    body = f"""
    $result = Invoke-Usecomputer -Args @('hover', '-x', {_ps_quote(str(x))}, '-y', {_ps_quote(str(y))})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
    usecomputer_path = $result.path
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body)
    await _best_effort_reset_windows_input_state()
    if error:
        return f"Error: mouse move failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: mouse move failed: {(data or {}).get('output', 'unknown error')}"
    return f"Mouse moved to ({x}, {y}) on Windows host via usecomputer"


async def execute_windows_click(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("click", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "click")

    x = args.get("x")
    y = args.get("y")
    button = args.get("button", "left")
    count = int(args.get("count", 1))
    if x is None or y is None:
        return "Error: x and y are required"
    if button not in {"left", "right", "middle"}:
        return "Error: button must be one of: left, right, middle"

    if provider == "windows-mcp":
        data, error = await _run_windows_mcp_json(
            {
                "tool": "Click",
                "arguments": {
                    "loc": [int(x), int(y)],
                    "button": button,
                    "clicks": count,
                },
            }
        )
        await _best_effort_reset_windows_input_state()
        if error:
            if not _is_auto_provider(requested_provider):
                return f"Error: click failed: {error}"
            provider = "usecomputer"
        else:
            text = _extract_mcp_text(data.get("content"))
            return text or f"Clicked ({x}, {y}) button={button} count={count} on Windows host via windows-mcp"

    body = f"""
$result = Invoke-Usecomputer -Args @('click', '-x', {_ps_quote(str(x))}, '-y', {_ps_quote(str(y))}, '--button', {_ps_quote(button)}, '--count', {_ps_quote(str(count))})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body)
    await _best_effort_reset_windows_input_state()
    if error:
        return f"Error: click failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: click failed: {(data or {}).get('output', 'unknown error')}"
    return f"Clicked ({x}, {y}) button={button} count={count} on Windows host"


async def execute_windows_drag(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("drag", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "drag")

    from_x = args.get("from_x")
    from_y = args.get("from_y")
    to_x = args.get("to_x")
    to_y = args.get("to_y")
    curve_x = args.get("curve_x")
    curve_y = args.get("curve_y")
    button = str(args.get("button", "left"))
    if None in {from_x, from_y, to_x, to_y}:
        return "Error: from_x, from_y, to_x, and to_y are required"
    if button not in {"left", "right", "middle"}:
        return "Error: button must be one of: left, right, middle"
    if (curve_x is None) != (curve_y is None):
        return "Error: curve_x and curve_y must be provided together"
    if provider == "windows-mcp":
        if not _is_auto_provider(requested_provider):
            return "Error: windows-mcp drag is not yet supported for windows_use"
        provider = "usecomputer"

    drag_parts = [
        "'drag'",
        _ps_quote(f"{int(from_x)},{int(from_y)}"),
        _ps_quote(f"{int(to_x)},{int(to_y)}"),
    ]
    if curve_x is not None and curve_y is not None:
        drag_parts.append(_ps_quote(f"{int(curve_x)},{int(curve_y)}"))
    drag_parts.extend(["'--button'", _ps_quote(button)])
    body = f"""
$result = Invoke-Usecomputer -Args @({', '.join(drag_parts)})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body, timeout=45)
    await _best_effort_reset_windows_input_state()
    if error:
        return f"Error: drag failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: drag failed: {(data or {}).get('output', 'unknown error')}"
    return f"Dragged from ({int(from_x)}, {int(from_y)}) to ({int(to_x)}, {int(to_y)}) button={button} on Windows host"


async def execute_windows_type(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("type", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "type")

    text = args.get("text", "")
    if not text:
        return "Error: text is required"
    x = args.get("x")
    y = args.get("y")
    focus_first = bool(args.get("focus_first", True))
    window_id, pid, title_contains, exact_title = _window_selector_args(args)
    has_selector = any([window_id, pid, title_contains, exact_title])
    focus_prefix = _focus_window_snippet(window_id, pid, title_contains, exact_title) if focus_first and has_selector else ""

    if provider == "windows-mcp":
        if x is None or y is None:
            return "Error: windows-mcp typing requires x and y. Click or move to the target first and pass the same coordinates."
        data, error = await _run_windows_mcp_json(
            {
                "tool": "Type",
                "arguments": {
                    "loc": [int(x), int(y)],
                    "text": text,
                },
            },
            timeout=120,
        )
        await _best_effort_reset_windows_input_state()
        if error:
            return f"Error: type failed: {error}"
        text_result = _extract_mcp_text(data.get("content"))
        return text_result or f"Typed {len(text)} chars on Windows host via windows-mcp"

    click_prefix = ""
    if x is not None and y is not None:
        click_prefix = f"$null = Invoke-Usecomputer -Args @('click', '-x', {_ps_quote(str(x))}, '-y', {_ps_quote(str(y))})\n"
    body = f"""
{focus_prefix}
{click_prefix}
$result = Invoke-Usecomputer -Args @('type', {_ps_quote(text)})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body, timeout=45)
    await _best_effort_reset_windows_input_state()
    if error:
        return f"Error: type failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: type failed: {(data or {}).get('output', 'unknown error')}"
    return f"Typed {len(text)} chars on Windows host via usecomputer"


async def execute_windows_key(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("key", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "key")

    key = args.get("key", "")
    if not key:
        return "Error: key is required (e.g. 'ctrl+s', 'alt+f4', 'Return')"
    focus_first = bool(args.get("focus_first", True))
    window_id, pid, title_contains, exact_title = _window_selector_args(args)
    has_selector = any([window_id, pid, title_contains, exact_title])
    focus_prefix = _focus_window_snippet(window_id, pid, title_contains, exact_title) if focus_first and has_selector else ""

    if provider == "windows-mcp":
        data, error = await _run_windows_mcp_json(
            {
                "tool": "Shortcut",
                "arguments": {
                    "shortcut": key,
                },
            }
        )
        await _best_effort_reset_windows_input_state()
        if error:
            return f"Error: key press failed: {error}"
        text = _extract_mcp_text(data.get("content"))
        return text or f"Pressed '{key}' on Windows host via windows-mcp"

    body = f"""
{focus_prefix}
$result = Invoke-Usecomputer -Args @('press', {_ps_quote(key)})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body)
    await _best_effort_reset_windows_input_state()
    if error:
        return f"Error: key press failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: key press failed: {(data or {}).get('output', 'unknown error')}"
    return f"Pressed '{key}' on Windows host via usecomputer"


async def execute_windows_scroll(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("scroll", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "scroll")

    direction = args.get("direction", "down")
    amount = int(args.get("amount", 3))
    x = args.get("x")
    y = args.get("y")
    if direction not in {"up", "down", "left", "right"}:
        return "Error: direction must be one of: up, down, left, right"

    if provider == "windows-mcp":
        mcp_args = {
            "direction": direction,
            "wheel_times": amount,
            "type": "vertical" if direction in {"up", "down"} else "horizontal",
        }
        if x is not None and y is not None:
            mcp_args["loc"] = [int(x), int(y)]
        data, error = await _run_windows_mcp_json({"tool": "Scroll", "arguments": mcp_args})
        await _best_effort_reset_windows_input_state()
        if error:
            if not _is_auto_provider(requested_provider):
                return f"Error: scroll failed: {error}"
            provider = "usecomputer"
        else:
            text = _extract_mcp_text(data.get("content"))
            return text or f"Scrolled {direction} x{amount} on Windows host via windows-mcp"

    arg_parts = [
        "'scroll'",
        _ps_quote(direction),
        _ps_quote(str(amount)),
    ]
    if x is not None and y is not None:
        arg_parts.extend(["'--at'", _ps_quote(f"{x},{y}")])

    body = f"""
$result = Invoke-Usecomputer -Args @({', '.join(arg_parts)})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body)
    await _best_effort_reset_windows_input_state()
    if error:
        return f"Error: scroll failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: scroll failed: {(data or {}).get('output', 'unknown error')}"
    return f"Scrolled {direction} x{amount} on Windows host"


async def execute_windows_reset_input_state(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("reset_input_state", args)
    if helper_result is not None:
        return helper_result
    await _best_effort_reset_windows_input_state()
    state, error = await _get_windows_input_state()
    if error:
        return "Windows input state reset requested, but current state could not be read back."
    return json.dumps(
        {
            "ok": True,
            "message": "Windows input state reset completed",
            "state": state,
        },
        ensure_ascii=False,
        indent=2,
    )


async def execute_windows_helper_warmup(args: dict) -> str:
    if not _windows_helper_enabled():
        return json.dumps(
            {
                "ok": False,
                "enabled": False,
                "message": "Windows helper warmup skipped because HASHI_WINDOWS_HELPER is disabled",
            },
            ensure_ascii=False,
            indent=2,
        )

    started = await _ensure_windows_helper_started()
    healthy = await _helper_healthcheck()
    return json.dumps(
        {
            "ok": bool(started and healthy),
            "enabled": True,
            "started": bool(started),
            "healthy": bool(healthy),
            "message": (
                "Windows helper is warm and ready"
                if started and healthy
                else "Windows helper warmup failed"
            ),
            "base_url": _windows_helper_base_url(),
        },
        ensure_ascii=False,
        indent=2,
    )


async def execute_windows_window_list(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("window_list", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "window_list")

    title_contains = args.get("title_contains", "")
    pid = int(args.get("pid", 0) or 0)
    body = f"""
$windows = Get-HashiWindowList
if ({_ps_bool(bool(title_contains))}) {{
    $windows = $windows | Where-Object {{ $_.title -like ("*" + {_ps_quote(str(title_contains))} + "*") }}
}}
if ({_ps_bool(pid > 0)}) {{
    $windows = $windows | Where-Object {{ $_.pid -eq {pid} }}
}}
@($windows) | ConvertTo-Json -Compress -Depth 6
"""
    data, error = await _run_powershell_json(body)
    if error:
        return f"Error: window list failed: {error}"
    data = _normalize_ps_value(data)
    return json.dumps(data, ensure_ascii=False, indent=2)


async def execute_windows_window_focus(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("window_focus", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "window_focus")

    window_id = int(args.get("window_id", 0) or 0)
    pid = int(args.get("pid", 0) or 0)
    title_contains = str(args.get("title_contains", "") or "")
    exact_title = str(args.get("exact_title", "") or "")
    if not any([window_id, pid, title_contains, exact_title]):
        return "Error: provide one of: window_id, pid, title_contains, exact_title"

    body = f"""
$target = Resolve-HashiWindow -WindowId {window_id} -TargetPid {pid} -TitleContains {_ps_quote(title_contains)} -ExactTitle {_ps_quote(exact_title)}
if (-not $target) {{
    throw "target window not found"
}}
$handle = [IntPtr]$target.id
if ([HashiWin]::IsIconic($handle)) {{
    [void][HashiWin]::ShowWindowAsync($handle, 9)
}} else {{
    [void][HashiWin]::ShowWindowAsync($handle, 5)
}}
Start-Sleep -Milliseconds 120
[void][HashiWin]::BringWindowToTop($handle)
[void][HashiWin]::SetForegroundWindow($handle)
@{{
    ok = $true
    window = $target
}} | ConvertTo-Json -Compress -Depth 6
"""
    data, error = await _run_powershell_json(body)
    if error:
        return f"Error: window focus failed: {error}"
    data = _normalize_ps_value(data)
    target = (data or {}).get("window") or {}
    return f"Focused window id={target.get('id')} title={target.get('title', '')}"


async def execute_windows_window_close(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("window_close", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "window_close")

    window_id = int(args.get("window_id", 0) or 0)
    pid = int(args.get("pid", 0) or 0)
    title_contains = str(args.get("title_contains", "") or "")
    exact_title = str(args.get("exact_title", "") or "")
    dismiss_unsaved = bool(args.get("dismiss_unsaved", False))
    force = bool(args.get("force", False))
    wait_ms = max(0, min(int(args.get("wait_ms", 1200) or 1200), 10000))
    if not any([window_id, pid, title_contains, exact_title]):
        return "Error: provide one of: window_id, pid, title_contains, exact_title"

    body = f"""
$target = Resolve-HashiWindow -WindowId {window_id} -TargetPid {pid} -TitleContains {_ps_quote(title_contains)} -ExactTitle {_ps_quote(exact_title)}
if (-not $target) {{
    throw "target window not found"
}}
$handle = [IntPtr]$target.id
[void][HashiWin]::ShowWindowAsync($handle, 5)
[void][HashiWin]::BringWindowToTop($handle)
[void][HashiWin]::SetForegroundWindow($handle)
[System.Threading.Thread]::Sleep(150)
[void][HashiWin]::PostMessage($handle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
Start-Sleep -Milliseconds {wait_ms}
$remaining = Resolve-HashiWindow -WindowId $target.id
$dismissAttempted = $false
$forced = $false
if ($remaining -and {_ps_bool(dismiss_unsaved)}) {{
    $dismissAttempted = $true
    try {{
        $null = Invoke-Usecomputer -Args @('press', 'n')
    }} catch {{}}
    Start-Sleep -Milliseconds 500
    $remaining = Resolve-HashiWindow -WindowId $target.id
}}
if ($remaining -and {_ps_bool(force)}) {{
    Stop-Process -Id $target.pid -Force -ErrorAction Stop
    $forced = $true
    Start-Sleep -Milliseconds 250
    $remaining = Resolve-HashiWindow -WindowId $target.id
}}
@{{
    ok = $true
    window = $target
    dismiss_attempted = $dismissAttempted
    forced = $forced
    closed = (-not $remaining)
}} | ConvertTo-Json -Compress -Depth 6
"""
    data, error = await _run_powershell_json(body)
    if error:
        return f"Error: window close failed: {error}"
    data = _normalize_ps_value(data)
    target = (data or {}).get("window") or {}
    if data.get("closed"):
        if data.get("forced"):
            return f"Closed window id={target.get('id')} title={target.get('title', '')} with force=true"
        if data.get("dismiss_attempted"):
            return f"Closed window id={target.get('id')} title={target.get('title', '')} after dismissing unsaved prompt"
        return f"Closed window id={target.get('id')} title={target.get('title', '')}"
    note = " (dismiss_unsaved attempted)" if data.get("dismiss_attempted") else ""
    note += " (force not requested)" if not data.get("forced") else ""
    return f"Sent close request to window id={target.get('id')} title={target.get('title', '')}, but it is still open{note}"


async def execute_windows_info(args: dict) -> str:
    helper_result = await _maybe_execute_windows_helper("info", args)
    if helper_result is not None:
        return helper_result
    requested_provider = args.get("provider")
    if error := _provider_error(requested_provider):
        return error
    provider = _resolve_provider(requested_provider, "info")

    include_windows = bool(args.get("include_windows", True))
    include_displays = bool(args.get("include_displays", True))

    windows_block = """
$windows = $null
if ($includeWindows) {
    $windowsResult = Invoke-Usecomputer -Args @('window', 'list', '--json')
    if ($windowsResult.exit_code -eq 0) {
        try { $windows = $windowsResult.output | ConvertFrom-Json } catch { $windows = $windowsResult.output }
    } else {
        $windows = "error: " + $windowsResult.output
    }
}
"""
    displays_block = """
$displays = $null
if ($includeDisplays) {
    $displayResult = Invoke-Usecomputer -Args @('display', 'list', '--json')
    if ($displayResult.exit_code -eq 0) {
        try { $displays = $displayResult.output | ConvertFrom-Json } catch { $displays = $displayResult.output }
    } else {
        $displays = "error: " + $displayResult.output
    }
}
"""

    body = f"""
$includeWindows = {_ps_bool(include_windows)}
$includeDisplays = {_ps_bool(include_displays)}
$uc = Resolve-UsecomputerPath
$inputState = Get-HashiInputState
$mouse = $null
if ($uc) {{
    $mouseResult = Invoke-Usecomputer -Args @('mouse', 'position', '--json')
    if ($mouseResult.exit_code -eq 0) {{
        try {{ $mouse = $mouseResult.output | ConvertFrom-Json }} catch {{ $mouse = $mouseResult.output }}
    }} else {{
        $mouse = "error: " + $mouseResult.output
    }}
}}
{displays_block if include_displays else "$displays = $null"}
{windows_block if include_windows else "$windows = $null"}
@{{
    platform = [System.Environment]::OSVersion.VersionString
    provider = 'usecomputer'
    host_python = { _ps_quote(platform.python_version()) }
    powershell = $PSVersionTable.PSVersion.ToString()
    is_wsl_caller = {_ps_bool(_is_wsl())}
    usecomputer_path = $uc
    input_state = $inputState
    mouse_position = $mouse
    displays = $displays
    windows = $windows
}} | ConvertTo-Json -Compress -Depth 8
"""
    data, error = await _run_usecomputer_json(body)
    if error:
        return f"Error: windows info failed: {error}"
    if not data:
        return "Error: windows info failed: empty response"
    data = _normalize_ps_value(data)
    return json.dumps(data, ensure_ascii=False, indent=2)
