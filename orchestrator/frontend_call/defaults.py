"""Approved OpenRouter Call defaults; credentials stay in instance configuration."""

from .config import OPENROUTER_API_BASE


from .voice_catalog import GEMINI_VOICES

_VOICE_STYLES = {name: style for name, (_gender, style) in GEMINI_VOICES.items()}


def default_call_configuration(*, voice_catalog=None):
    common = {
        "adapter": "openai_compatible",
        "location": "cloud",
        "base_url": OPENROUTER_API_BASE,
        "credential_ref": "secrets://openrouter-api_key",
        "options": {},
    }
    targets = [
        {
            **common,
            "id": "openrouter-whisper",
            "kind": "stt",
            "label": "Whisper Large V3 (OpenRouter)",
            "model": "openai/whisper-large-v3",
        },
        {
            **common,
            "id": "openrouter-gemini-vision",
            "kind": "vision",
            "label": "Gemini 3.8 Flash visual snapshot (OpenRouter)",
            "model": "google/gemini-3.8-flash",
        },
    ]
    if voice_catalog is None:
        targets.insert(
            1,
            {
                **common,
                "id": "openrouter-gemini-tts",
                "kind": "tts",
                "label": "Gemini 3.8 Flash-Lite TTS (OpenRouter)",
                "model": "google/gemini-3.8-flash-lite-tts",
                "audio_format": "pcm",
                "voices": list(_VOICE_STYLES),
                "voice_styles": dict(_VOICE_STYLES),
                "options": {"style": {"type": "string", "max_length": 160}},
            },
        )
        tts_profile = {
            "target_id": "openrouter-gemini-tts",
            "voice_id": "Achernar",
            "options": {},
        }
    else:
        candidates = [
            service_id
            for service_id in voice_catalog.service_ids
            if voice_catalog.voice_ids(service_id)
        ]
        if not candidates:
            raise ValueError("the shared TTS catalogue has no selectable voices")
        selected = (
            "openrouter-gemini-tts"
            if "openrouter-gemini-tts" in candidates
            else candidates[0]
        )
        tts_profile = {
            "target_id": selected,
            "voice_id": voice_catalog.voice_ids(selected)[0],
            "options": {},
        }
    return {
        "version": 1,
        "enabled": True,
        "targets": targets,
        "default_profile": {
            "stt": {"target_id": "openrouter-whisper", "options": {}},
            "tts": tts_profile,
            "vision": {"target_id": "openrouter-gemini-vision", "options": {}},
        },
        "profiles": {},
    }
