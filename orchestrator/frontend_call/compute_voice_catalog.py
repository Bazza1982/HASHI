"""Compatibility imports for the shared Hashi Compute speech catalogue."""

from orchestrator.compute_voice_catalog import (
    ComputeVoice,
    ComputeVoiceDirectory,
    DownloadedVoicePreview,
    SpeechCatalogue,
    VoiceAudition,
    discover_compute_voice_directory,
    parse_speech_catalogue,
)

__all__ = [
    "ComputeVoice",
    "ComputeVoiceDirectory",
    "DownloadedVoicePreview",
    "SpeechCatalogue",
    "VoiceAudition",
    "discover_compute_voice_directory",
    "parse_speech_catalogue",
]
