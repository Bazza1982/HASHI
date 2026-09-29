"""Qualified, provider-neutral catalogue for HASHI live phone sessions."""
from __future__ import annotations


OPENAI_LIVE_VOICES = (
    "marin", "quartz", "ripple", "vesper", "willow", "stone", "gleam",
    "meridian", "bossa", "tempo", "beacon", "delta", "cinder",
)

PHONE_PROVIDERS = {
    "openai": {
        "label": "OpenAI",
        "models": {
            "gpt-live-1": {
                "label": "GPT Live 1",
                "voices": OPENAI_LIVE_VOICES,
            },
        },
    },
}

PHONE_LANGUAGES = {
    "auto": {
        "label": "Automatic",
        "instruction": "Reply in the language the user is currently speaking; preserve deliberate language switches.",
    },
    "zh-CN": {
        "label": "简体中文",
        "instruction": "Default to natural Simplified Chinese unless the user clearly switches languages.",
    },
    "zh-TW": {
        "label": "繁體中文",
        "instruction": "Default to natural Traditional Chinese unless the user clearly switches languages.",
    },
    "en": {
        "label": "English",
        "instruction": "Default to natural English unless the user clearly switches languages.",
    },
    "ja": {
        "label": "日本語",
        "instruction": "Default to natural Japanese unless the user clearly switches languages.",
    },
}

PHONE_STYLES = {
    "natural": {
        "label": "Natural",
        "instruction": "Use a natural conversational rhythm, moderate pace, and restrained expressiveness.",
    },
    "warm": {
        "label": "Warm",
        "instruction": "Sound warm, attentive, reassuring, and gently expressive without becoming theatrical.",
    },
    "clear": {
        "label": "Clear",
        "instruction": "Speak crisply and directly, with short sentences, clear pauses, and minimal filler.",
    },
    "calm": {
        "label": "Calm",
        "instruction": "Use a calm, steady, unhurried delivery with low emotional intensity.",
    },
    "lively": {
        "label": "Lively",
        "instruction": "Sound lively and energetic with a brisk rhythm, while remaining easy to understand.",
    },
}
