import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from orchestrator.media_runtime import configured_media_executable


@dataclass
class VoiceAsset:
    provider: str
    text: str
    spoken_text: str
    wav_path: Path | None
    ogg_path: Path
    mime_type: str = "audio/ogg"
    fallback_from: str | None = None
    fallback_reason_code: str | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True)
class VoiceProviderFailure:
    provider: str
    code: str
    reason: str
    retryable: bool = False

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "provider": self.provider,
            "code": self.code,
            "reason": self.reason,
            "retryable": self.retryable,
        }


class VoiceSynthesisError(RuntimeError):
    """A user-safe speech failure with stable machine-readable evidence."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failures: Iterable[VoiceProviderFailure] = (),
    ):
        super().__init__(message)
        self.code = str(code)
        self.failures = tuple(failures)


def safe_voice_error_detail(value: object, *, max_chars: int = 500) -> str:
    """Keep a useful reason while removing service URLs and common secret pairs."""

    text = re.sub(
        r"\b(?:https?|wss?)://[^\s'\"<>]+",
        "[service URL omitted]",
        str(value or ""),
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?i)\b(token|key|secret|signature|sig|authorization|sec-ms-gec)\s*[=:]\s*[^\s,;'\" ]+",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return (text or "Speech provider failed.")[: max(1, int(max_chars))]


def _strip_invalid_unicode(text: str) -> str:
    # Some model replies can contain lone surrogate code points from upstream
    # decode paths or malformed emoji fragments. TTS engines reject them.
    return "".join(ch for ch in text if not 0xD800 <= ord(ch) <= 0xDFFF)


def spoken_text_language(text: str) -> str:
    """Shared EN/Chinese/Japanese default voice hint; explicit voices win."""
    if any("\u3040" <= character <= "\u30ff" for character in text):
        return "ja"
    if any("\u3400" <= character <= "\u9fff" for character in text):
        return "zh"
    return "en"


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
    converter = configured_media_executable("ffmpeg") or ffmpeg_cmd
    proc = await asyncio.create_subprocess_exec(
        converter,
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
