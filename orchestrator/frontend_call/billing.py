"""Voice-media billing projection for Call; backend text and vision stay excluded."""

from __future__ import annotations

import io
import math
import wave
from dataclasses import dataclass, field
from typing import Any, Mapping


OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"

# Qualified provider catalogue rates as of 2026-10-11. Provider-returned cost
# always wins; these rates are the live estimate while a final receipt is absent.
_PRICING = {
    (OPENROUTER_API_BASE, "openai/whisper-large-v3"): {
        "kind": "stt",
        "audio_second_usd": 0.000008,
    },
    (OPENROUTER_API_BASE, "x-ai/grok-stt-1.0"): {
        "kind": "stt",
        "audio_second_usd": 0.000028,
    },
    (OPENROUTER_API_BASE, "google/gemini-3.8-flash-lite-tts"): {
        "kind": "tts",
        "text_token_usd": 0.50 / 1_000_000,
        "audio_second_usd": 6.00 * 25 / 1_000_000,
    },
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def wav_seconds(data: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            rate = audio.getframerate()
            return audio.getnframes() / rate if rate > 0 else None
    except (wave.Error, EOFError, OSError):
        return None


def estimate_text_tokens(text: str) -> int:
    # CJK is commonly near one token per character; Latin prose is nearer four
    # characters per token. This is deliberately labelled as an estimate.
    total = sum(1.0 if ord(character) > 127 else 0.25 for character in text)
    return max(1, math.ceil(total)) if text else 0


def _pricing(target: Mapping[str, Any]) -> Mapping[str, Any] | None:
    key = (
        str(target.get("base_url") or "").rstrip("/"),
        str(target.get("model") or ""),
    )
    return _PRICING.get(key)


@dataclass
class _Meter:
    model: str = ""
    requests: int = 0
    audio_seconds: float = 0.0
    text_tokens_estimated: int = 0
    exact_cost_usd: float = 0.0
    estimated_cost_usd: float = 0.0
    unpriced_requests: int = 0


@dataclass
class MediaBilling:
    meters: dict[str, _Meter] = field(
        default_factory=lambda: {
            "stt": _Meter(),
            "tts": _Meter(),
        }
    )

    def record_stt(
        self,
        target: Mapping[str, Any],
        audio_seconds: float | None,
        provider_usage: Mapping[str, Any] | None = None,
    ) -> None:
        self._record(
            "stt", target, audio_seconds=audio_seconds, provider_usage=provider_usage
        )

    def record_tts(
        self,
        target: Mapping[str, Any],
        text: str,
        provider_usage: Mapping[str, Any] | None = None,
    ) -> None:
        usage = provider_usage if isinstance(provider_usage, Mapping) else {}
        self._record(
            "tts",
            target,
            audio_seconds=_number(usage.get("audio_seconds")),
            text_tokens=estimate_text_tokens(text),
            provider_usage=usage,
        )

    def _record(
        self,
        kind: str,
        target: Mapping[str, Any],
        *,
        audio_seconds: float | None,
        text_tokens: int = 0,
        provider_usage: Mapping[str, Any] | None = None,
    ) -> None:
        usage = provider_usage if isinstance(provider_usage, Mapping) else {}
        provider_seconds = _number(usage.get("seconds"))
        seconds = (
            provider_seconds if provider_seconds is not None else _number(audio_seconds)
        )
        meter = self.meters[kind]
        meter.model = str(target.get("model") or meter.model)
        meter.requests += 1
        meter.audio_seconds += seconds or 0.0
        meter.text_tokens_estimated += max(0, int(text_tokens))
        exact_cost = _number(usage.get("cost"))
        if exact_cost is not None:
            meter.exact_cost_usd += exact_cost
            return
        pricing = _pricing(target)
        if not pricing or pricing.get("kind") != kind:
            meter.unpriced_requests += 1
            return
        meter.estimated_cost_usd += (seconds or 0.0) * float(
            pricing.get("audio_second_usd", 0)
        )
        meter.estimated_cost_usd += text_tokens * float(
            pricing.get("text_token_usd", 0)
        )

    def view(self, targets: Mapping[str, Mapping[str, Any] | None]) -> dict[str, Any]:
        breakdown = {}
        exact = estimated = 0.0
        incomplete = False
        for kind in ("stt", "tts"):
            meter = self.meters[kind]
            target = targets.get(kind) or {}
            model = meter.model or str(target.get("model") or "")
            row_cost = meter.exact_cost_usd + meter.estimated_cost_usd
            exact += meter.exact_cost_usd
            estimated += meter.estimated_cost_usd
            incomplete = incomplete or meter.unpriced_requests > 0
            breakdown[kind] = {
                "model": model,
                "requests": meter.requests,
                "audio_seconds": round(meter.audio_seconds, 3),
                "cost_usd": round(row_cost, 8),
                "estimated": meter.estimated_cost_usd > 0
                or (meter.requests == 0 and _pricing(target) is not None),
            }
            if kind == "tts":
                breakdown[kind]["text_tokens_estimated"] = meter.text_tokens_estimated
        return {
            "scope": "voice_media",
            "currency": "USD",
            "cost_usd": round(exact + estimated, 8),
            "estimated": estimated > 0
            or incomplete
            or not any(meter.requests for meter in self.meters.values()),
            "incomplete": incomplete,
            "breakdown": breakdown,
            "excludes": ["backend_text_model", "vision"],
        }
