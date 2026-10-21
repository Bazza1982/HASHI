"""Shared OpenAI-compatible speech request and response contract."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Any, Mapping


_PROTOCOL_FIELDS = frozenset({"model", "input", "voice", "response_format"})


class SpeechProtocolError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message)


@dataclass(frozen=True)
class SpeechAudio:
    data: bytes
    media_type: str
    file_format: str
    duration_seconds: float | None


def build_speech_request(
    *,
    model: str,
    text: str,
    voice: str,
    response_format: str,
    options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected_options = dict(options or {})
    if set(selected_options) & _PROTOCOL_FIELDS:
        raise SpeechProtocolError(
            "tts_options_invalid",
            "TTS options cannot replace protocol-owned request fields.",
        )
    return {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": response_format,
        **selected_options,
    }


def validate_speech_audio(
    data: bytes,
    media_type: str,
    response_format: str,
    *,
    pcm_sample_rate_hz: int = 24000,
) -> SpeechAudio:
    if not data:
        raise SpeechProtocolError(
            "tts_response_empty",
            "The TTS service returned no audio.",
            retryable=True,
        )
    selected_format = str(response_format or "").strip().casefold()
    selected_media_type = str(media_type or "").split(";", 1)[0].strip().casefold()
    duration: float | None = None

    if selected_format == "pcm":
        if (
            selected_media_type not in {"audio/pcm", "application/octet-stream"}
            or len(data) < max(2, int(pcm_sample_rate_hz) // 10 * 2)
            or len(data) % 2
        ):
            raise SpeechProtocolError(
                "tts_audio_format_invalid",
                "The TTS service returned invalid PCM audio.",
                retryable=True,
            )
        duration = len(data) / (int(pcm_sample_rate_hz) * 2)
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(int(pcm_sample_rate_hz))
            audio.writeframes(data)
        return SpeechAudio(output.getvalue(), "audio/wav", "wav", duration)

    if selected_format == "wav":
        if not (data.startswith(b"RIFF") and data[8:12] == b"WAVE"):
            raise SpeechProtocolError(
                "tts_audio_format_invalid",
                "The TTS service returned malformed WAV audio.",
                retryable=True,
            )
        try:
            with wave.open(io.BytesIO(data), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
        except (wave.Error, EOFError, OSError, ZeroDivisionError):
            duration = None
        return SpeechAudio(data, "audio/wav", "wav", duration)

    if selected_format == "mp3":
        if not (
            data.startswith(b"ID3")
            or (len(data) > 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0)
        ):
            raise SpeechProtocolError(
                "tts_audio_format_invalid",
                "The TTS service returned malformed MP3 audio.",
                retryable=True,
            )
        return SpeechAudio(data, "audio/mpeg", "mp3", None)

    raise SpeechProtocolError(
        "tts_audio_format_unsupported",
        f"Unsupported TTS audio format: {selected_format or 'missing'}.",
    )


__all__ = [
    "SpeechAudio",
    "SpeechProtocolError",
    "build_speech_request",
    "validate_speech_audio",
]
