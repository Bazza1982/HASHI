"""Gemini voice identities; presentation strings live in the UI catalogs.

Gender source: https://docs.cloud.google.com/text-to-speech/docs/gemini-tts
Style source: https://ai.google.dev/gemini-api/docs/speech-generation
"""

from orchestrator import ui_language
from .config import OPENROUTER_GEMINI_TTS_MODELS

# This is the single catalog used by defaults, menus and preview generation.
GEMINI_VOICES = {
    "Achernar": ("female", "Soft"),
    "Achird": ("male", "Friendly"),
    "Algenib": ("male", "Gravelly"),
    "Algieba": ("male", "Smooth"),
    "Alnilam": ("male", "Firm"),
    "Aoede": ("female", "Breezy"),
    "Autonoe": ("female", "Bright"),
    "Callirrhoe": ("female", "Easy-going"),
    "Charon": ("male", "Informative"),
    "Despina": ("female", "Smooth"),
    "Enceladus": ("male", "Breathy"),
    "Erinome": ("female", "Clear"),
    "Fenrir": ("male", "Excitable"),
    "Gacrux": ("female", "Mature"),
    "Iapetus": ("male", "Clear"),
    "Kore": ("female", "Firm"),
    "Laomedeia": ("female", "Upbeat"),
    "Leda": ("female", "Youthful"),
    "Orus": ("male", "Firm"),
    "Pulcherrima": ("female", "Forward"),
    "Puck": ("male", "Upbeat"),
    "Rasalgethi": ("male", "Informative"),
    "Sadachbia": ("male", "Lively"),
    "Sadaltager": ("male", "Knowledgeable"),
    "Schedar": ("male", "Even"),
    "Sulafat": ("female", "Warm"),
    "Umbriel": ("male", "Easy-going"),
    "Vindemiatrix": ("female", "Gentle"),
    "Zephyr": ("female", "Bright"),
    "Zubenelgenubi": ("male", "Casual"),
}


def voice_label(target, voice):
    metadata = GEMINI_VOICES.get(voice) if target.get("model") in OPENROUTER_GEMINI_TTS_MODELS else None
    style = target.get("voice_styles", {}).get(voice, metadata[1] if metadata else "")
    if not metadata:
        return voice + (" · " + style if style else "")
    gender = ui_language.tr("call.voice.gender." + metadata[0])
    if style in {value[1] for value in GEMINI_VOICES.values()}:
        style = ui_language.tr("call.voice.style." + style.lower().replace("-", "_"))
    return " · ".join(part for part in (gender, voice, style) if part)
