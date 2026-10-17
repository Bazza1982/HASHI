from __future__ import annotations

from orchestrator.tts_routing import (
    LANGUAGE_DOMINANCE_THRESHOLD,
    assess_text_language,
    fallback_providers,
    resolve_tts_route,
)


PRESETS = {
    "ecn": {
        "provider": "edge",
        "voice_name": "zh-CN-XiaoxiaoNeural",
        "language": "zh-CN",
    },
    "eja": {
        "provider": "edge",
        "voice_name": "ja-JP-NanamiNeural",
        "language": "ja-JP",
    },
    "eus": {
        "provider": "edge",
        "voice_name": "en-US-EmmaNeural",
        "language": "en-US",
    },
    "pcn": {
        "provider": "piper",
        "voice_name": "zh_CN-huayan-medium.onnx",
        "language": "zh-CN",
    },
}

PROFILES = {
    "warm_female": {
        "tts_voices": {
            "en": "en-US-EmmaNeural",
            "zh": "zh-CN-XiaoxiaoNeural",
            "ja": "ja-JP-NanamiNeural",
        }
    }
}


def test_language_assessment_uses_ninety_percent_and_keeps_small_japanese_quote_chinese():
    assert LANGUAGE_DOMINANCE_THRESHOLD == 0.90
    assessment = assess_text_language(
        "这是一段完整的中文说明，主要讨论中文内容和中文语境，"
        "这里只引用一次日语词かわいい，后面仍然继续使用中文解释整个问题。",
        source_language="zh-CN",
    )

    assert assessment.language == "zh"
    assert assessment.confidence >= LANGUAGE_DOMINANCE_THRESHOLD
    assert assessment.scores["ja"] < 0.10


def test_language_assessment_detects_japanese_and_uses_hint_for_ambiguous_han():
    japanese = assess_text_language(
        "これは日本語の文章です。音声をすぐに再生してください。",
        source_language="zh-CN",
    )
    assert japanese.language == "ja"
    assert japanese.confidence >= LANGUAGE_DOMINANCE_THRESHOLD

    assert assess_text_language("日本語", source_language="ja-JP").language == "ja"
    assert assess_text_language(
        "日本語", source_language="zh-CN", preferred_language="ja"
    ).language == "ja"


def test_english_is_compatible_with_every_known_voice():
    route = resolve_tts_route(
        provider="edge",
        voice_name="zh-CN-XiaoxiaoNeural",
        provider_options={},
        requested_style=None,
        text="Please read this answer in the locally appropriate accent.",
        source_language="en",
        presets=PRESETS,
        profiles=PROFILES,
        voice_profile="warm_female",
    )

    assert route.language.language == "en"
    assert route.voice_name == "zh-CN-XiaoxiaoNeural"
    assert route.adapted_from_voice is None
    assert route.requested_style is None
    assert route.style_applied is None
    assert route.style_reason is None


def test_japanese_temporarily_switches_xiaoxiao_to_same_provider_voice():
    route = resolve_tts_route(
        provider="edge",
        voice_name="zh-CN-XiaoxiaoNeural",
        provider_options={"voice": "zh-CN-XiaoxiaoNeural"},
        requested_style=None,
        text="これは日本語の文章です。すぐに読み上げてください。",
        source_language="zh-CN",
        presets=PRESETS,
        profiles=PROFILES,
        voice_profile="warm_female",
    )

    assert route.provider == "edge"
    assert route.voice_name == "ja-JP-NanamiNeural"
    assert route.provider_options["voice"] == "ja-JP-NanamiNeural"
    assert route.adapted_from_voice == "zh-CN-XiaoxiaoNeural"
    assert route.adaptation_reason == "selected_voice_language_unsupported"
    assert route.language.language == "ja"


def test_mixed_text_below_threshold_does_not_override_selected_voice():
    route = resolve_tts_route(
        provider="edge",
        voice_name="zh-CN-XiaoxiaoNeural",
        provider_options={},
        requested_style=None,
        text="中文说明里提到 かわいい and friendly，之后继续解释中文上下文。",
        source_language="zh-CN",
        presets=PRESETS,
        profiles=PROFILES,
    )

    assert route.language.language is None
    assert route.voice_name == "zh-CN-XiaoxiaoNeural"
    assert route.adapted_from_voice is None


def test_local_voice_without_target_language_has_no_upward_fallback():
    route = resolve_tts_route(
        provider="piper",
        voice_name="zh_CN-huayan-medium.onnx",
        provider_options={},
        requested_style=None,
        text="これは日本語だけの文章です。",
        source_language="ja",
        presets=PRESETS,
        profiles=PROFILES,
    )

    assert route.unsupported_language is True
    assert fallback_providers("piper", {"edge", "windows", "piper"}) == ()


def test_provider_fallback_is_monotonic_and_explicit():
    available = {"openai", "edge", "windows", "piper", "coqui"}
    assert fallback_providers("openai", available) == ("edge", "windows", "piper")
    assert fallback_providers("edge", available) == ("windows", "piper")
    assert fallback_providers("windows", available) == ()
    assert fallback_providers("coqui", available) == ()
