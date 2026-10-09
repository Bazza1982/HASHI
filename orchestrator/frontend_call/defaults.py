"""Qualified default media targets; credentials stay in instance configuration."""


def default_call_configuration():
    common = {
        "adapter": "openai_compatible", "location": "cloud",
        "base_url": "https://api.openai.com/v1",
        "credential_ref": "secrets://openai_api_key", "options": {},
    }
    return {
        "version": 1, "enabled": True,
        "targets": [
            {**common, "id": "openai-stt", "kind": "stt",
             "label": "OpenAI STT", "model": "gpt-transcribe"},
            {**common, "id": "openai-tts", "kind": "tts",
             "label": "OpenAI TTS", "model": "gpt-4o-mini-tts",
             "audio_format": "wav", "voices": ["coral"],
             "options": {"instructions": {"type": "string", "max_length": 300},
                         "speed": {"type": "number", "min": 0.5, "max": 2.0}}},
            {**common, "id": "openai-vision", "kind": "vision",
             "label": "OpenAI Vision", "model": "gpt-4.1-mini"},
        ],
        "default_profile": {
            "stt": {"target_id": "openai-stt", "options": {}},
            "tts": {"target_id": "openai-tts", "voice_id": "coral", "options": {}},
            "vision": {"target_id": "openai-vision", "options": {}},
        },
        "profiles": {},
    }
