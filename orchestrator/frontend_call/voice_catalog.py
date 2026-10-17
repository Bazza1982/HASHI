"""Gemini voice identities; presentation strings live in the UI catalogs.

Gender source: https://docs.cloud.google.com/text-to-speech/docs/gemini-tts
Style source: https://ai.google.dev/gemini-api/docs/speech-generation
"""

from orchestrator import ui_language
from orchestrator.gemini_tts_catalog import GEMINI_VOICES
from .config import OPENROUTER_GEMINI_TTS_MODELS

def voice_label(target, voice):
    metadata = (
        GEMINI_VOICES.get(voice)
        if target.get("model") in OPENROUTER_GEMINI_TTS_MODELS
        else None
    )
    style = target.get("voice_styles", {}).get(voice, metadata[1] if metadata else "")
    if not metadata:
        return voice + (" · " + style if style else "")
    gender = ui_language.tr("call.voice.gender." + metadata[0])
    if style in {value[1] for value in GEMINI_VOICES.values()}:
        style = ui_language.tr("call.voice.style." + style.lower().replace("-", "_"))
    return " · ".join(part for part in (gender, voice, style) if part)
