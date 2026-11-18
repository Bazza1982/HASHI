"""Shared OpenAI-compatible speech request and response contract."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from email.message import Message
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
    pcm_channels: int = 1,
    pcm_sample_width_bytes: int = 2,
    pcm_encoding: str = "s16le",
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
        def invalid_pcm():
            return SpeechProtocolError(
                "tts_audio_format_invalid",
                "The TTS service returned invalid or mismatched PCM audio.",
                retryable=True,
            )

        if (
            selected_media_type not in {"audio/pcm", "application/octet-stream"}
            or type(pcm_sample_rate_hz) is not int or not 8000 <= pcm_sample_rate_hz <= 96000
            or type(pcm_channels) is not int or pcm_channels not in (1, 2)
            or type(pcm_sample_width_bytes) is not int or pcm_sample_width_bytes != 2
            or pcm_encoding != "s16le"
        ):
            raise invalid_pcm()
        # Response metadata, when present, must agree with the configured codec.
        message = Message()
        message["content-type"] = str(media_type or "")
        params = message.get_params()[1:]
        seen = set()
        expected = {
            "rate": str(pcm_sample_rate_hz), "sample_rate": str(pcm_sample_rate_hz),
            "sample_rate_hz": str(pcm_sample_rate_hz), "channels": str(pcm_channels),
            "bits": "16", "bit_depth": "16", "sample_width_bytes": "2",
            "encoding": "s16le", "endianness": "little", "signed": "true",
        }
        for key, value in params:
            key = key.casefold()
            if key in seen:
                raise invalid_pcm()
            seen.add(key)
            normalized = str(value or "").strip().casefold()
            if key in expected and normalized != expected[key]:
                raise invalid_pcm()
            if key in {"codec", "format"} and normalized not in {"s16le", "pcm_s16le"}:
                raise invalid_pcm()
        frame_bytes = pcm_channels * pcm_sample_width_bytes
        if len(data) < frame_bytes or len(data) % frame_bytes:
            raise invalid_pcm()
        duration = len(data) / (pcm_sample_rate_hz * frame_bytes)
        output = io.BytesIO()
        with wave.open(output, "wb") as audio:
            audio.setnchannels(pcm_channels)
            audio.setsampwidth(pcm_sample_width_bytes)
            audio.setframerate(pcm_sample_rate_hz)
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
