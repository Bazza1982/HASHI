from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


LANGUAGE_DOMINANCE_THRESHOLD = 0.90
_JAPANESE_KANA_CONTEXT_THRESHOLD = 0.30
_ENGLISH = "en"
_SUPPORTED_LANGUAGES = ("en", "zh", "ja")
_AUTO_LANGUAGE_PROVIDERS = frozenset({"windows", "macos"})
_STRICT_LOCALE_VOICE_PROVIDERS = frozenset(
    {"edge", "windows", "macos", "piper", "kokoro"}
)


@dataclass(frozen=True)
class LanguageAssessment:
    language: str | None
    confidence: float
    scores: dict[str, float]
    character_count: int
    threshold: float
    source_language: str | None
    preferred_language: str | None
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "confidence": self.confidence,
            "scores": dict(self.scores),
            "character_count": self.character_count,
            "threshold": self.threshold,
            "source_language": self.source_language,
            "preferred_language": self.preferred_language,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class VoiceLanguageProfile:
    provider: str
    voice_name: str | None
    native_languages: tuple[str, ...]
    english_compatible: bool
    other_languages_unsupported: bool
    style_support: str
    source: str

    def supports(self, language: str | None) -> bool:
        if not language:
            return True
        if language == _ENGLISH and self.english_compatible:
            return True
        return language in self.native_languages


@dataclass(frozen=True)
class TTSRoute:
    provider: str
    voice_name: str | None
    provider_options: dict[str, Any]
    requested_style: str | None
    style_applied: bool | None
    style_reason: str | None
    language: LanguageAssessment
    voice_language: str | None
    adapted_from_voice: str | None
    adaptation_reason: str | None
    unsupported_language: bool
    capability_source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "voice_name": self.voice_name,
            "provider_options": dict(self.provider_options),
            "requested_style": self.requested_style,
            "style_applied": self.style_applied,
            "style_reason": self.style_reason,
            "language": self.language.as_dict(),
            "voice_language": self.voice_language,
            "adapted_from_voice": self.adapted_from_voice,
            "adaptation_reason": self.adaptation_reason,
            "unsupported_language": self.unsupported_language,
            "capability_source": self.capability_source,
        }


_PROVIDER_PROFILES: dict[str, dict[str, Any]] = {
    # These future provider entries make the downgrade policy explicit before
    # those paid engines are registered as ordinary HASHI TTS providers.
    "openai": {
        "cost": "paid",
        "locality": "network",
        "style_support": "supported",
        "fallbacks": ("edge", "windows", "macos", "piper", "kokoro"),
    },
    "gemini": {
        "cost": "paid",
        "locality": "network",
        "style_support": "supported",
        "fallbacks": ("edge", "windows", "macos", "piper", "kokoro"),
    },
    "elevenlabs": {
        "cost": "paid",
        "locality": "network",
        "style_support": "supported",
        "fallbacks": ("edge", "windows", "macos", "piper", "kokoro"),
    },
    "edge": {
        "cost": "free",
        "locality": "network",
        "style_support": "unsupported",
        "fallbacks": ("windows", "macos", "piper", "kokoro"),
    },
    "windows": {
        "cost": "free",
        "locality": "local",
        "style_support": "unsupported",
        "fallbacks": (),
    },
    "macos": {
        "cost": "free",
        "locality": "local",
        "style_support": "unsupported",
        "fallbacks": (),
    },
    "piper": {
        "cost": "free",
        "locality": "local",
        "style_support": "unsupported",
        "fallbacks": (),
    },
    "kokoro": {
        "cost": "free",
        "locality": "local",
        "style_support": "unsupported",
        "fallbacks": (),
    },
    "coqui": {
        "cost": "free",
        "locality": "local",
        "style_support": "unknown",
        "fallbacks": (),
    },
}


def normalize_language(value: object) -> str | None:
    raw = str(value or "").strip().casefold().replace("_", "-")
    if not raw:
        return None
    aliases = {
        "english": "en",
        "chinese": "zh",
        "chinese-simplified": "zh",
        "chinese-traditional": "zh",
        "simplified chinese": "zh",
        "traditional chinese": "zh",
        "japanese": "ja",
        "中文": "zh",
        "简体中文": "zh",
        "繁體中文": "zh",
        "日本語": "ja",
    }
    if raw in aliases:
        return aliases[raw]
    prefix = raw.split("-", 1)[0]
    return prefix if prefix in _SUPPORTED_LANGUAGES else None


def _language_from_voice_name(voice_name: object) -> str | None:
    raw = str(voice_name or "").strip().replace("\\", "/")
    match = re.search(r"(?:^|[/])([a-zA-Z]{2})[-_]([a-zA-Z]{2})(?:[-_]|$)", raw)
    if match:
        return normalize_language(match.group(1))
    match = re.match(r"^([a-zA-Z]{2})[-_]", raw)
    return normalize_language(match.group(1)) if match else None


def _script_counts(segment: str) -> tuple[int, int, int]:
    han = kana = latin = 0
    for character in segment:
        code = ord(character)
        if 0x3040 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF:
            kana += 1
        elif 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
            han += 1
        elif (
            "a" <= character.casefold() <= "z"
            or 0x00C0 <= code <= 0x024F
        ):
            latin += 1
    return han, kana, latin


def assess_text_language(
    text: str,
    *,
    source_language: object = None,
    preferred_language: object = None,
    threshold: float = LANGUAGE_DOMINANCE_THRESHOLD,
) -> LanguageAssessment:
    """Classify only strongly dominant speech.

    Latin text is evaluated when it is the only script family. English inside
    Chinese or Japanese prose is deliberately neutral because every voice is
    English-compatible by policy. Han-only text follows an explicit preferred
    voice, then the request's source language.
    """

    source = normalize_language(source_language)
    preferred = normalize_language(preferred_language)
    weights = {"en": 0, "zh": 0, "ja": 0}
    segments = [
        segment
        for segment in re.split(r"[.!?。！？;；\n]+", str(text or ""))
        if segment.strip()
    ]
    total_cjk = 0
    total_latin = 0
    for segment in segments:
        han, kana, latin = _script_counts(segment)
        total_latin += latin
        cjk = han + kana
        if not cjk:
            continue
        total_cjk += cjk
        if kana == 0:
            ambiguous = preferred if preferred in {"zh", "ja"} else source
            weights[ambiguous if ambiguous in {"zh", "ja"} else "zh"] += han
        elif kana / cjk >= _JAPANESE_KANA_CONTEXT_THRESHOLD:
            weights["ja"] += cjk
        else:
            weights["zh"] += han
            weights["ja"] += kana

    if total_cjk:
        denominator = weights["zh"] + weights["ja"]
    else:
        weights["en"] = total_latin
        denominator = total_latin

    if denominator <= 0:
        return LanguageAssessment(
            language=None,
            confidence=0.0,
            scores={language: 0.0 for language in _SUPPORTED_LANGUAGES},
            character_count=0,
            threshold=float(threshold),
            source_language=source,
            preferred_language=preferred,
            reason="no_language_characters",
        )

    scores = {
        language: round(weights[language] / denominator, 6)
        for language in _SUPPORTED_LANGUAGES
    }
    dominant = max(scores, key=scores.get)
    confidence = scores[dominant]
    language = dominant if confidence >= float(threshold) else None
    return LanguageAssessment(
        language=language,
        confidence=confidence,
        scores=scores,
        character_count=denominator,
        threshold=float(threshold),
        source_language=source,
        preferred_language=preferred,
        reason="dominant_language" if language else "below_dominance_threshold",
    )


def provider_profile(provider: object) -> dict[str, Any]:
    name = str(provider or "").strip().casefold()
    profile = _PROVIDER_PROFILES.get(name)
    if profile is not None:
        return {"provider": name, **profile}
    return {
        "provider": name,
        "cost": "unknown",
        "locality": "unknown",
        "style_support": "unknown",
        "fallbacks": (),
    }


def fallback_providers(
    provider: object,
    available_providers: set[str] | frozenset[str] | tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    name = str(provider or "").strip().casefold()
    available = {str(item).strip().casefold() for item in available_providers}
    return tuple(
        candidate
        for candidate in provider_profile(name)["fallbacks"]
        if candidate in available and candidate != name
    )


def _preset_language(preset: Mapping[str, Any]) -> str | None:
    return normalize_language(preset.get("language")) or _language_from_voice_name(
        preset.get("voice_name")
    )


def _matching_preset(
    provider: str,
    voice_name: str | None,
    presets: Mapping[str, Mapping[str, Any]],
) -> tuple[str, Mapping[str, Any]] | None:
    folded_voice = str(voice_name or "").strip().casefold()
    if not folded_voice:
        return None
    for alias, preset in presets.items():
        if (
            str(preset.get("provider") or "").strip().casefold() == provider
            and str(preset.get("voice_name") or "").strip().casefold() == folded_voice
        ):
            return str(alias), preset
    return None


def _profile_language_for_voice(
    voice_name: str | None,
    profiles: Mapping[str, Mapping[str, Any]],
) -> str | None:
    folded = str(voice_name or "").strip().casefold()
    if not folded:
        return None
    for profile in profiles.values():
        for language, candidate in (profile.get("tts_voices") or {}).items():
            if str(candidate or "").strip().casefold() == folded:
                return normalize_language(language)
    return None


def voice_language_profile(
    *,
    provider: object,
    voice_name: object,
    presets: Mapping[str, Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
) -> VoiceLanguageProfile:
    provider_name = str(provider or "").strip().casefold()
    selected_voice = str(voice_name or "").strip() or None
    match = _matching_preset(provider_name, selected_voice, presets)
    if match:
        language = _preset_language(match[1])
        strict = bool(match[1].get("other_languages_unsupported", True))
        source = f"preset:{match[0]}"
    else:
        language = _profile_language_for_voice(selected_voice, profiles)
        if language:
            strict = True
            source = "semantic_profile"
        else:
            language = _language_from_voice_name(selected_voice)
            strict = bool(language and provider_name in _STRICT_LOCALE_VOICE_PROVIDERS)
            source = "voice_name_inference" if language else "unknown"
    return VoiceLanguageProfile(
        provider=provider_name,
        voice_name=selected_voice,
        native_languages=(language,) if language else (),
        english_compatible=True,
        other_languages_unsupported=strict,
        style_support=str(provider_profile(provider_name)["style_support"]),
        source=source,
    )


def _profile_voice(
    provider: str,
    language: str,
    requested_style: str | None,
    profiles: Mapping[str, Mapping[str, Any]],
) -> str | None:
    if provider != "edge" or not requested_style:
        return None
    profile = profiles.get(requested_style) or {}
    candidate = str((profile.get("tts_voices") or {}).get(language) or "").strip()
    return candidate or None


def _provider_voice_for_language(
    provider: str,
    language: str,
    *,
    requested_style: str | None,
    presets: Mapping[str, Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
) -> tuple[str | None, dict[str, Any], str] | None:
    if provider in _AUTO_LANGUAGE_PROVIDERS:
        return None, {"language_hint": language}, "provider_auto_language"

    styled = _profile_voice(provider, language, requested_style, profiles)
    if styled:
        match = _matching_preset(provider, styled, presets)
        options = dict(match[1].get("provider_options") or {}) if match else {}
        return styled, options, "semantic_profile"

    for alias, preset in presets.items():
        if str(preset.get("provider") or "").strip().casefold() != provider:
            continue
        if _preset_language(preset) != language:
            continue
        return (
            str(preset.get("voice_name") or "").strip() or None,
            dict(preset.get("provider_options") or {}),
            f"preset:{alias}",
        )

    return None


def resolve_tts_route(
    *,
    provider: object,
    voice_name: object,
    provider_options: Mapping[str, Any] | None,
    requested_style: object,
    text: str,
    source_language: object,
    presets: Mapping[str, Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
) -> TTSRoute:
    provider_name = str(provider or "").strip().casefold()
    selected_voice = str(voice_name or "").strip() or None
    options = dict(provider_options or {})
    style = str(requested_style or "").strip().casefold() or None
    selected_capability = voice_language_profile(
        provider=provider_name,
        voice_name=selected_voice,
        presets=presets,
        profiles=profiles,
    )
    preferred_language = (
        selected_capability.native_languages[0]
        if selected_capability.native_languages
        else None
    )
    assessment = assess_text_language(
        text,
        source_language=source_language,
        preferred_language=preferred_language,
    )
    target_language = assessment.language or normalize_language(source_language)

    style_support = selected_capability.style_support
    if not style:
        style_applied = None
        style_reason = None
    elif style_support == "supported":
        style_applied = True
        style_reason = "provider_style_supported"
    elif style_support == "unsupported":
        style_applied = False
        style_reason = "provider_style_unsupported"
    else:
        style_applied = False
        style_reason = "provider_style_unknown_ignored"

    adapted_from = None
    adaptation_reason = None
    unsupported = False
    route_source = selected_capability.source

    if selected_voice is None and target_language:
        candidate = _provider_voice_for_language(
            provider_name,
            target_language,
            requested_style=style,
            presets=presets,
            profiles=profiles,
        )
        if candidate:
            selected_voice, candidate_options, route_source = candidate
            options.update(candidate_options)
        elif (
            provider_name in _STRICT_LOCALE_VOICE_PROVIDERS
            and target_language != _ENGLISH
        ):
            unsupported = True
    elif (
        assessment.language
        and selected_capability.other_languages_unsupported
        and not selected_capability.supports(assessment.language)
    ):
        candidate = _provider_voice_for_language(
            provider_name,
            assessment.language,
            requested_style=style,
            presets=presets,
            profiles=profiles,
        )
        if candidate:
            adapted_from = selected_voice
            selected_voice, candidate_options, route_source = candidate
            options.update(candidate_options)
            if "voice" in options:
                if selected_voice:
                    options["voice"] = selected_voice
                else:
                    options.pop("voice", None)
            adaptation_reason = "selected_voice_language_unsupported"
        else:
            unsupported = True

    effective_capability = voice_language_profile(
        provider=provider_name,
        voice_name=selected_voice,
        presets=presets,
        profiles=profiles,
    )
    voice_language = (
        effective_capability.native_languages[0]
        if effective_capability.native_languages
        else target_language if selected_voice is None else None
    )
    return TTSRoute(
        provider=provider_name,
        voice_name=selected_voice,
        provider_options=options,
        requested_style=style,
        style_applied=style_applied,
        style_reason=style_reason,
        language=assessment,
        voice_language=voice_language,
        adapted_from_voice=adapted_from,
        adaptation_reason=adaptation_reason,
        unsupported_language=unsupported,
        capability_source=route_source,
    )
