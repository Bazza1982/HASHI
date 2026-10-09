"""Approved OpenRouter Call defaults; credentials stay in instance configuration."""

from .config import OPENROUTER_API_BASE


_VOICE_STYLES = {
    "Achernar": "Soft", "Achird": "Friendly", "Algenib": "Gravelly",
    "Algieba": "Smooth", "Alnilam": "Firm", "Aoede": "Breezy",
    "Autonoe": "Bright", "Callirrhoe": "Easy-going", "Charon": "Informative",
    "Despina": "Smooth", "Enceladus": "Breathy", "Erinome": "Clear",
    "Fenrir": "Excitable", "Gacrux": "Mature", "Iapetus": "Clear",
    "Kore": "Firm", "Laomedeia": "Upbeat", "Leda": "Youthful", "Orus": "Firm",
    "Pulcherrima": "Forward", "Puck": "Upbeat", "Rasalgethi": "Informative",
    "Sadachbia": "Lively", "Sadaltager": "Knowledgeable", "Schedar": "Even",
    "Sulafat": "Warm", "Umbriel": "Easy-going", "Vindemiatrix": "Gentle",
    "Zephyr": "Bright", "Zubenelgenubi": "Casual",
}


def default_call_configuration():
    common = {
        "adapter": "openai_compatible", "location": "cloud",
        "base_url": OPENROUTER_API_BASE,
        "credential_ref": "secrets://openrouter-api_key", "options": {},
    }
    return {
        "version": 1, "enabled": True,
        "targets": [
            {**common, "id": "openrouter-whisper", "kind": "stt",
             "label": "Whisper Large V3 (OpenRouter)", "model": "openai/whisper-large-v3"},
            {**common, "id": "openrouter-gemini-tts", "kind": "tts",
             "label": "Gemini 3.8 Flash-Lite TTS (OpenRouter)",
             "model": "google/gemini-3.8-flash-lite-tts",
             "audio_format": "pcm", "voices": list(_VOICE_STYLES),
             "voice_styles": dict(_VOICE_STYLES),
             "options": {"style": {"type": "string", "max_length": 160}}},
            {**common, "id": "openrouter-gemini-vision", "kind": "vision",
             "label": "Gemini 3.8 Flash visual snapshot (OpenRouter)",
             "model": "google/gemini-3.8-flash"},
        ],
        "default_profile": {
            "stt": {"target_id": "openrouter-whisper", "options": {}},
            "tts": {"target_id": "openrouter-gemini-tts", "voice_id": "Achernar", "options": {}},
            "vision": {"target_id": "openrouter-gemini-vision", "options": {}},
        },
        "profiles": {},
    }
