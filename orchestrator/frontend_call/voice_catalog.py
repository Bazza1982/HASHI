"""Gemini voice identities; presentation strings live in the UI catalogs.

Gender source: https://docs.cloud.google.com/text-to-speech/docs/gemini-tts
Style source: https://ai.google.dev/gemini-api/docs/speech-generation
"""

from orchestrator import ui_language
from orchestrator.voice_catalog import VoiceCatalog
from .config import OPENROUTER_GEMINI_TTS_MODELS

# Compatibility view for existing preview manifests. The shared TTS catalogue
# remains the only voice metadata owner.
_SHIPPED_CATALOG = VoiceCatalog.load(include_disabled=True)
GEMINI_VOICES = {
    str(preset["voice_name"]): (
        str(preset.get("gender") or "unknown"),
        str(preset.get("style") or ""),
    )
    for preset in _SHIPPED_CATALOG.presets.values()
    if preset.get("provider") == "openrouter-gemini-tts"
}


def voice_label(target, voice):
    declared = (target.get("voice_metadata") or {}).get(voice)
    if isinstance(declared, dict):
        locale = ui_language.current_locale()
        display = str(
            (declared.get("display_names") or {}).get(locale)
            or declared.get("label")
            or declared.get("voice_name")
            or voice
        )
        gender = str(declared.get("gender") or "unknown")
        style = str(declared.get("style") or "")
        if gender in {"female", "male"}:
            gender = ui_language.tr("call.voice.gender." + gender)
        else:
            gender = ""
        if style in {value[1] for value in GEMINI_VOICES.values()}:
            style = ui_language.tr(
                "call.voice.style." + style.lower().replace("-", "_")
            )
        return " · ".join(part for part in (gender, display, style) if part)
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
