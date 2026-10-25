"""Package ordinary Call audio as bounded Agent Presence media."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tempfile
import wave
from pathlib import Path

from .contract import CallError

_MAX_REFERENCE_BYTES = 16 * 1024 * 1024
_MAX_AUDIO_BYTES = 32 * 1024 * 1024
_MAX_OUTPUT_BYTES = 64 * 1024 * 1024
_AUDIO_SUFFIXES = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}


def _checkout_ffmpeg() -> str:
    root = Path(__file__).resolve().parents[2]
    directories = [
        root
        / ".venv"
        / "Lib"
        / "site-packages"
        / "imageio_ffmpeg"
        / "binaries",
    ]
    directories.extend(
        root
        / ".venv"
        / "lib"
        / candidate.name
        / "site-packages"
        / "imageio_ffmpeg"
        / "binaries"
        for candidate in (root / ".venv" / "lib").glob("python*")
        if candidate.is_dir()
    )
    for directory in directories:
        for candidate in sorted(directory.glob("ffmpeg-*")):
            if candidate.is_file():
                return str(candidate)
    return ""


def _ffmpeg_executable() -> str:
    configured = str(os.environ.get("HASHI_FFMPEG_PATH") or "").strip()
    if configured and Path(configured).is_file():
        return configured
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    try:
        import imageio_ffmpeg

        bundled = str(imageio_ffmpeg.get_ffmpeg_exe() or "").strip()
    except (ImportError, OSError, RuntimeError):
        bundled = ""
    if bundled and Path(bundled).is_file():
        return bundled
    checkout = _checkout_ffmpeg()
    if checkout:
        return checkout
    raise CallError("call_audio_fallback_packager_unavailable", 503)


def silence_wav(duration_ms: int = 250) -> bytes:
    """Return a short valid PCM asset for captions-only continuation."""

    frames = max(1, int(16000 * max(50, min(duration_ms, 1000)) / 1000))
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


async def mux_audio_to_static_mp4(
    reference: bytes, audio: bytes, media_type: str
) -> bytes:
    """Create a complete H.264/AAC MP4 without exposing native stderr."""

    if not isinstance(reference, bytes) or not 0 < len(reference) <= _MAX_REFERENCE_BYTES:
        raise CallError("call_audio_fallback_reference_invalid", 502)
    if not isinstance(audio, bytes) or not 0 < len(audio) <= _MAX_AUDIO_BYTES:
        raise CallError("call_audio_fallback_audio_invalid", 502)
    normalized_type = str(media_type or "").split(";", 1)[0].strip().casefold()
    suffix = _AUDIO_SUFFIXES.get(normalized_type)
    if suffix is None:
        raise CallError("call_audio_fallback_media_type_invalid", 502)

    with tempfile.TemporaryDirectory(prefix="hashi-call-audio-fallback-") as folder:
        root = Path(folder)
        image_path = root / "reference.png"
        audio_path = root / f"speech{suffix}"
        output_path = root / "speech.mp4"
        image_path.write_bytes(reference)
        audio_path.write_bytes(audio)
        try:
            process = await asyncio.create_subprocess_exec(
                _ffmpeg_executable(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-loop",
                "1",
                "-framerate",
                "25",
                "-i",
                str(image_path),
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-vf",
                "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-tune",
                "stillimage",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(output_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            raise CallError("call_audio_fallback_packager_unavailable", 503) from exc
        try:
            await asyncio.wait_for(process.communicate(), timeout=45)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise CallError("call_audio_fallback_packaging_timeout", 504) from exc
        if process.returncode != 0 or not output_path.is_file():
            raise CallError("call_audio_fallback_packaging_failed", 502)
        media = output_path.read_bytes()
    if (
        not 12 <= len(media) <= _MAX_OUTPUT_BYTES
        or b"ftyp" not in media[:32]
    ):
        raise CallError("call_audio_fallback_output_invalid", 502)
    return media


__all__ = ["mux_audio_to_static_mp4", "silence_wav"]
