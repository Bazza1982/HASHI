"""Shared Gemini TTS voice capability catalogue."""

GEMINI_TTS_MODEL = "google/gemini-3.8-flash-lite-tts"

# Provider-published voice identities.  Presentation translations belong to
# each frontend renderer; this module owns only stable capability metadata.
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

__all__ = ["GEMINI_TTS_MODEL", "GEMINI_VOICES"]
