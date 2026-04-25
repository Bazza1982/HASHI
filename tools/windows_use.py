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

logger = logging.getLogger("Tools.WindowsUse")


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
        return "usecomputer"
    if value == "usecomputer":
        return value
    if value == "windows-mcp":
        return value
    return "invalid"


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

    $output = & $uc @Args 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    return @{
        path = $uc
        output = $output.Trim()
        exit_code = $exitCode
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

    $output = & $uv.command @argsList 2>&1 | Out-String
    $jsonLine = ($output -split "`r?`n" | Where-Object { $_ -like 'HASHI_JSON:*' } | Select-Object -Last 1)
    if (-not $jsonLine) {
        throw ("no HASHI_JSON line returned. Raw output: " + $output.Trim())
    }

    return @{
        raw = $output.Trim()
        json = $jsonLine.Substring(11)
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
        [uint32]$pid = 0
        [void][HashiWin]::GetWindowThreadProcessId($hWnd, [ref]$pid)
        $procName = $null
        try {
            $procName = (Get-Process -Id $pid -ErrorAction Stop).ProcessName
        } catch {}
        $items.Add([pscustomobject]@{
            id = [int64]$hWnd
            pid = [int]$pid
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
        [int]$Pid = 0,
        [string]$TitleContains = "",
        [string]$ExactTitle = ""
    )

    $windows = Get-HashiWindowList
    if ($WindowId) {
        return $windows | Where-Object { $_.id -eq $WindowId } | Select-Object -First 1
    }
    if ($Pid) {
        return $windows | Where-Object { $_.pid -eq $Pid } | Select-Object -First 1
    }
    if ($ExactTitle) {
        return $windows | Where-Object { $_.title -eq $ExactTitle } | Select-Object -First 1
    }
    if ($TitleContains) {
        return $windows | Where-Object { $_.title -like ("*" + $TitleContains + "*") } | Select-Object -First 1
    }
    return $null
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


async def _run_usecomputer_json(body: str, timeout: int = 30) -> tuple[dict | None, str | None]:
    return await _run_powershell_json(body, timeout=timeout)


def _provider_error(provider: str) -> str | None:
    normalized = _normalize_provider(provider)
    if normalized == "invalid":
        return "Error: provider must be one of: auto, usecomputer, windows-mcp"
    return None


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
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

    save_path, save_path_error = _resolve_windows_save_path(args.get("save_path"))
    if save_path_error:
        return f"Error: {save_path_error}"

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
            return "Error: windows-mcp screenshot provider does not yet support window-targeted capture in windows_use"
        data, error = await _run_windows_mcp_json({"tool": "Screenshot", "arguments": mcp_args}, timeout=120)
        if error:
            return f"Error: screenshot failed: {error}"
        content = data.get("content") or []
        image_b64, mime_type = _extract_mcp_image(content)
        text = _extract_mcp_text(content)
        if not image_b64:
            return f"Error: screenshot failed: no image returned from windows-mcp. Text: {text}"
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
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

    x = args.get("x")
    y = args.get("y")
    if x is None or y is None:
        return "Error: x and y are required"

    if provider == "windows-mcp":
        data, error = await _run_windows_mcp_json({"tool": "Move", "arguments": {"loc": [int(x), int(y)]}})
        if error:
            return f"Error: mouse move failed: {error}"
        text = _extract_mcp_text(data.get("content"))
        return text or f"Mouse moved to ({x}, {y}) on Windows host via windows-mcp"

    body = f"""
$result = Invoke-Usecomputer -Args @('mouse', 'move', '-x', {_ps_quote(str(x))}, '-y', {_ps_quote(str(y))})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
    usecomputer_path = $result.path
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body)
    if error:
        return f"Error: mouse move failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: mouse move failed: {(data or {}).get('output', 'unknown error')}"
    return f"Mouse moved to ({x}, {y}) on Windows host via usecomputer"


async def execute_windows_click(args: dict) -> str:
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

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
        if error:
            return f"Error: click failed: {error}"
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
    if error:
        return f"Error: click failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: click failed: {(data or {}).get('output', 'unknown error')}"
    return f"Clicked ({x}, {y}) button={button} count={count} on Windows host"


async def execute_windows_type(args: dict) -> str:
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

    text = args.get("text", "")
    if not text:
        return "Error: text is required"
    x = args.get("x")
    y = args.get("y")

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
        if error:
            return f"Error: type failed: {error}"
        text_result = _extract_mcp_text(data.get("content"))
        return text_result or f"Typed {len(text)} chars on Windows host via windows-mcp"

    click_prefix = ""
    if x is not None and y is not None:
        click_prefix = f"$null = Invoke-Usecomputer -Args @('click', '-x', {_ps_quote(str(x))}, '-y', {_ps_quote(str(y))})\n"
    body = f"""
{click_prefix}
$result = Invoke-Usecomputer -Args @('type', {_ps_quote(text)})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body, timeout=45)
    if error:
        return f"Error: type failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: type failed: {(data or {}).get('output', 'unknown error')}"
    return f"Typed {len(text)} chars on Windows host via usecomputer"


async def execute_windows_key(args: dict) -> str:
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

    key = args.get("key", "")
    if not key:
        return "Error: key is required (e.g. 'ctrl+s', 'alt+f4', 'Return')"

    if provider == "windows-mcp":
        data, error = await _run_windows_mcp_json(
            {
                "tool": "Shortcut",
                "arguments": {
                    "shortcut": key,
                },
            }
        )
        if error:
            return f"Error: key press failed: {error}"
        text = _extract_mcp_text(data.get("content"))
        return text or f"Pressed '{key}' on Windows host via windows-mcp"

    body = f"""
$result = Invoke-Usecomputer -Args @('press', {_ps_quote(key)})
@{{
    ok = ($result.exit_code -eq 0)
    output = $result.output
}} | ConvertTo-Json -Compress
"""
    data, error = await _run_usecomputer_json(body)
    if error:
        return f"Error: key press failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: key press failed: {(data or {}).get('output', 'unknown error')}"
    return f"Pressed '{key}' on Windows host via usecomputer"


async def execute_windows_scroll(args: dict) -> str:
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

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
        if error:
            return f"Error: scroll failed: {error}"
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
    if error:
        return f"Error: scroll failed: {error}"
    if not data or not data.get("ok"):
        return f"Error: scroll failed: {(data or {}).get('output', 'unknown error')}"
    return f"Scrolled {direction} x{amount} on Windows host"


async def execute_windows_window_list(args: dict) -> str:
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

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
$windows | ConvertTo-Json -Compress -Depth 6
"""
    data, error = await _run_powershell_json(body)
    if error:
        return f"Error: window list failed: {error}"
    data = _normalize_ps_value(data)
    return json.dumps(data, ensure_ascii=False, indent=2)


async def execute_windows_window_focus(args: dict) -> str:
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

    window_id = int(args.get("window_id", 0) or 0)
    pid = int(args.get("pid", 0) or 0)
    title_contains = str(args.get("title_contains", "") or "")
    exact_title = str(args.get("exact_title", "") or "")
    if not any([window_id, pid, title_contains, exact_title]):
        return "Error: provide one of: window_id, pid, title_contains, exact_title"

    body = f"""
$target = Resolve-HashiWindow -WindowId {window_id} -Pid {pid} -TitleContains {_ps_quote(title_contains)} -ExactTitle {_ps_quote(exact_title)}
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
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

    window_id = int(args.get("window_id", 0) or 0)
    pid = int(args.get("pid", 0) or 0)
    title_contains = str(args.get("title_contains", "") or "")
    exact_title = str(args.get("exact_title", "") or "")
    if not any([window_id, pid, title_contains, exact_title]):
        return "Error: provide one of: window_id, pid, title_contains, exact_title"

    body = f"""
$target = Resolve-HashiWindow -WindowId {window_id} -Pid {pid} -TitleContains {_ps_quote(title_contains)} -ExactTitle {_ps_quote(exact_title)}
if (-not $target) {{
    throw "target window not found"
}}
$handle = [IntPtr]$target.id
[void][HashiWin]::PostMessage($handle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
@{{
    ok = $true
    window = $target
}} | ConvertTo-Json -Compress -Depth 6
"""
    data, error = await _run_powershell_json(body)
    if error:
        return f"Error: window close failed: {error}"
    data = _normalize_ps_value(data)
    target = (data or {}).get("window") or {}
    return f"Sent WM_CLOSE to window id={target.get('id')} title={target.get('title', '')}"


async def execute_windows_info(args: dict) -> str:
    provider = _normalize_provider(args.get("provider"))
    if error := _provider_error(provider):
        return error

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
