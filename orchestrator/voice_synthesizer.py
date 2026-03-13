import asyncio
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VoiceAsset:
    provider: str
    text: str
    spoken_text: str
    wav_path: Path | None
    ogg_path: Path
    mime_type: str = "audio/ogg"


def _strip_invalid_unicode(text: str) -> str:
    # Some model replies can contain lone surrogate code points from upstream
    # decode paths or malformed emoji fragments. TTS engines reject them.
    return "".join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF)


def prepare_spoken_text(text: str, max_chars: int = 1200) -> str:
    raw = _strip_invalid_unicode((text or "")).strip()
    if not raw:
        return ""
    raw = re.sub(r"```[\s\S]*?```", " ", raw)
    raw = re.sub(r"`([^`]+)`", r"\1", raw)
    raw = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw)
    raw = re.sub(r"[*_#>-]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if len(raw) <= max_chars:
        return raw
    clipped = raw[:max_chars].rsplit(" ", 1)[0].strip()
    return f"{clipped}."


async def convert_audio_to_ogg(ffmpeg_cmd: str, input_path: Path, ogg_path: Path):
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_cmd,
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "libopus",
        str(ogg_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0 or not ogg_path.exists():
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"ffmpeg voice conversion failed: {err or f'exit_code={proc.returncode}'}")


async def convert_wav_to_ogg(ffmpeg_cmd: str, wav_path: Path, ogg_path: Path):
    await convert_audio_to_ogg(ffmpeg_cmd, wav_path, ogg_path)
