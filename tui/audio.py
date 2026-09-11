"""Generation-fenced local audio playback for the HASHI TUI."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TUI_SPEECH_MAX_BYTES = 3 * 1024 * 1024


class TuiAudioError(RuntimeError):
    pass


def decode_tui_audio(payload: dict, *, max_bytes: int = TUI_SPEECH_MAX_BYTES) -> bytes:
    encoded = str(payload.get("content_b64") or "")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise TuiAudioError("the target returned invalid audio encoding") from None
    if not content or len(content) > max_bytes:
        raise TuiAudioError("the target returned an invalid audio size")
    declared_size = payload.get("size_bytes")
    if declared_size is not None and int(declared_size) != len(content):
        raise TuiAudioError("the target audio size did not match")
    declared_sha = str(payload.get("sha256") or "")
    if not declared_sha or declared_sha != hashlib.sha256(content).hexdigest():
        raise TuiAudioError("the target audio integrity check failed")
    if str(payload.get("media_type") or "").casefold() != "audio/ogg":
        raise TuiAudioError("the target returned an unsupported audio format")
    if not content.startswith(b"OggS"):
        raise TuiAudioError("the target returned malformed Ogg audio")
    return content


def _windows_path(path: Path) -> str:
    if sys.platform == "win32":
        return str(path)
    try:
        result = subprocess.run(
            ["wslpath", "-w", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        raise TuiAudioError("the temporary audio path is not visible to Windows") from None
    value = result.stdout.strip()
    if not value:
        raise TuiAudioError("the temporary audio path is not visible to Windows")
    return value


def _player_command(path: Path) -> list[str]:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if sys.platform == "win32" or (os.environ.get("WSL_DISTRO_NAME") and powershell):
        if not powershell:
            raise TuiAudioError("Windows MediaPlayer is unavailable")
        script = (
            "Add-Type -AssemblyName PresentationCore;"
            "$p=New-Object System.Windows.Media.MediaPlayer;"
            "$p.Open([Uri]::new($args[0]));$p.Play();"
            "$end=(Get-Date).AddSeconds(10);"
            "while((-not $p.NaturalDuration.HasTimeSpan)-and((Get-Date)-lt $end))"
            "{Start-Sleep -Milliseconds 50};"
            "if(-not $p.NaturalDuration.HasTimeSpan){$p.Close();exit 3};"
            "$ms=[Math]::Ceiling($p.NaturalDuration.TimeSpan.TotalMilliseconds)+100;"
            "Start-Sleep -Milliseconds $ms;$p.Close()"
        )
        return [powershell, "-NoProfile", "-NonInteractive", "-Command", script, _windows_path(path)]
    if sys.platform == "darwin":
        player = shutil.which("afplay")
        if player:
            return [player, str(path)]
    for name, arguments in (
        ("ffplay", ("-nodisp", "-autoexit", "-loglevel", "quiet")),
        ("paplay", ()),
        ("aplay", ()),
    ):
        player = shutil.which(name)
        if player:
            return [player, *arguments, str(path)]
    raise TuiAudioError("no supported local audio player is available")


async def play_ogg_bytes(content: bytes) -> None:
    """Play one Ogg asset to completion and stop the process if cancelled."""

    payload = bytes(content)
    if not payload.startswith(b"OggS") or len(payload) > TUI_SPEECH_MAX_BYTES:
        raise TuiAudioError("refusing malformed or oversized Ogg audio")
    descriptor, name = tempfile.mkstemp(prefix="hashi-tui-speech-", suffix=".ogg")
    path = Path(name)
    process: asyncio.subprocess.Process | None = None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            path.chmod(0o600)
        command = await asyncio.to_thread(_player_command, path)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        exit_code = await process.wait()
        if exit_code:
            raise TuiAudioError(f"the local audio player exited with code {exit_code}")
    except asyncio.CancelledError:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    finally:
        path.unlink(missing_ok=True)


__all__ = [
    "TUI_SPEECH_MAX_BYTES",
    "TuiAudioError",
    "decode_tui_audio",
    "play_ogg_bytes",
]
