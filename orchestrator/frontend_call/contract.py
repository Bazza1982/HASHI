"""Small, provider-neutral /call wire contract and bounded media validation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import wave

PROTOCOL = 1
MAX_BODY = 4 * 1024 * 1024
MAX_AUDIO = 16000 * 2 * 60 + 4096
MAX_IMAGE = 512 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
MAX_SPEECH_SEGMENTS = 32
MAX_SPEECH_CHARS = 240
_SENTENCE_END = re.compile(
    r'[。！？!?]+[”"\x27’』」)]*|\.(?:[”"\x27’)]*)?(?=\s|$)|\n+'
)
_FILLERS = {
    "en": ("Hmm…", "Let me think for a moment…", "I’m still working on that. Please give me a moment."),
    "zh-CN": ("嗯……", "让我想一想……", "我还在处理，请稍等一下。"),
    "zh-TW": ("嗯……", "讓我想一想……", "我還在處理，請稍等一下。"),
    "ja": ("うーん……", "少し考えさせてください……", "まだ確認しています。もう少しお待ちください。"),
}
_LOCALE_ALIASES = {"zh": "zh-CN", "zh-Hans": "zh-CN", "zh-Hant": "zh-TW"}
LEASE_SECONDS = 45
MAX_CALL_SECONDS = 8 * 3600
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
COMMON = {"operation", "client_id", "agent_id", "session_id", "context_generation"}
OPERATIONS = {
    "route": set(),
    "context": set(),
    "save_profile": {"revision", "profile"},
    "start": {"generation", "revision", "call_id", "allow_cloud", "locale"},
    "turn": {
        "generation",
        "call_id",
        "turn_id",
        "sequence",
        "audio_b64",
        "image_b64",
        "captured_at",
    },
    "snapshot": {"generation", "call_id"},
    "camera": {"generation", "call_id", "enabled"},
    "observe": {"generation", "call_id", "frame_sequence", "image_b64", "captured_at"},
    "speech": {"generation", "call_id", "turn_id", "segment", "retry"},
    "filler": {"generation", "call_id", "slot"},
    "end": {"generation", "call_id"},
}


class CallError(ValueError):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code, self.status = code, status


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CallError("call_invalid_identifier")
    return value


def integer(value, low=1, high=2**31 - 1):
    if type(value) is not int or not low <= value <= high:
        raise CallError("call_invalid_number")
    return value


def call_locale(value):
    if value is None:
        return "en"
    if not isinstance(value, str) or len(value) > 16:
        raise CallError("call_locale_invalid")
    normalized = _LOCALE_ALIASES.get(value, value)
    return normalized if normalized in _FILLERS else "en"


def filler_phrases(value):
    return _FILLERS[call_locale(value)]


def validate_body(body):
    if not isinstance(body, dict) or body.get("operation") not in OPERATIONS:
        raise CallError("call_invalid_operation")
    if set(body) - COMMON - OPERATIONS[body["operation"]]:
        raise CallError("call_unknown_field")
    for name in ("client_id", "agent_id", "session_id"):
        identifier(body.get(name))
    integer(body.get("context_generation"))
    if body["operation"] == "start":
        call_locale(body.get("locale"))
    return body


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def decode_base64(value, maximum):
    if not isinstance(value, str) or not value or len(value) > (maximum + 2) // 3 * 4:
        raise CallError("call_media_limit", 413)
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise CallError("call_invalid_media") from exc
    if len(data) > maximum:
        raise CallError("call_media_limit", 413)
    return data


def decode_wav(value):
    data = decode_base64(value, MAX_AUDIO)
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if (
                audio.getnchannels(),
                audio.getsampwidth(),
                audio.getframerate(),
                audio.getcomptype(),
            ) != (1, 2, 16000, "NONE"):
                raise CallError("call_wav_format_required")
            frames = audio.getnframes()
            if (
                not 1600 <= frames <= 16000 * 60
                or len(audio.readframes(frames)) != frames * 2
            ):
                raise CallError("call_audio_duration_invalid")
    except (wave.Error, EOFError, OSError) as exc:
        raise CallError("call_wav_format_required") from exc
    return data


def decode_jpeg(value):
    """Header and dimension guard without introducing native model dependencies."""
    data = decode_base64(value, MAX_IMAGE)
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise CallError("call_jpeg_required")
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            break
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        size = int.from_bytes(data[offset : offset + 2], "big")
        if size < 2 or offset + size > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2}:
            if size < 8:
                break
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if not (1 <= width <= 2048 and 1 <= height <= 2048):
                raise CallError("call_image_dimensions_invalid")
            return data
        if marker == 0xDA:
            break
        offset += size
    raise CallError("call_invalid_jpeg")


def _clean_speech_text(text: str) -> str:
    """Remove non-spoken rendering without interpreting or rewriting an answer."""
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*#`_]", "", text)
    return "\n".join(
        line for line in text.splitlines() if line.count("|") < 2
    ).strip()


def _bounded_sentence(text: str) -> list[str]:
    """Keep normal sentences whole; bound only pathological provider output."""
    parts = []
    value = text.strip()
    while value:
        cut = min(MAX_SPEECH_CHARS, len(value))
        if cut < len(value):
            space = value.rfind(" ", 0, cut + 1)
            clause = max(
                value.rfind(mark, 0, cut + 1)
                for mark in ("，", ",", "；", ";", "：", ":")
            )
            boundary = max(space, clause + 1)
            if boundary >= 24:
                cut = boundary
        part, value = value[:cut].strip(), value[cut:].strip()
        if part:
            parts.append(part)
    return parts


def _sentence_parts(text: str, *, include_tail: bool) -> tuple[list[str], str]:
    clean = _clean_speech_text(str(text or ""))
    parts: list[str] = []
    offset = 0
    for match in _SENTENCE_END.finditer(clean):
        sentence = clean[offset : match.end()].strip()
        offset = match.end()
        if sentence:
            parts.extend(_bounded_sentence(sentence))
    remainder = clean[offset:].strip()
    if include_tail and remainder:
        parts.extend(_bounded_sentence(remainder))
        remainder = ""
    return parts, remainder


def complete_speech_segments(text: str):
    """Return stable complete sentences and the still-changing answer tail."""
    parts, remainder = _sentence_parts(text, include_tail=False)
    if len(parts) > MAX_SPEECH_SEGMENTS:
        return parts[:MAX_SPEECH_SEGMENTS], "".join(parts[MAX_SPEECH_SEGMENTS:]) + remainder
    return parts, remainder


def speech_segments(text: str):
    """Queue final answer TTS by complete sentence, in original order."""
    parts, _ = _sentence_parts(text, include_tail=True)
    return parts[:MAX_SPEECH_SEGMENTS], len(parts) > MAX_SPEECH_SEGMENTS
