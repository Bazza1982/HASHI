from __future__ import annotations
from orchestrator.tts_providers.coqui import CoquiProvider
from orchestrator.tts_providers.edge import EdgeTTSProvider
from orchestrator.tts_providers.kokoro import KokoroProvider
from orchestrator.tts_providers.piper import PiperProvider
from orchestrator.tts_providers.windows import WindowsSapiProvider


def build_provider(provider_name: str, ffmpeg_cmd: str = "ffmpeg"):
    name = (provider_name or "windows").strip().lower()
    mapping = {
        "windows": WindowsSapiProvider,
        "edge": EdgeTTSProvider,
        "piper": PiperProvider,
        "kokoro": KokoroProvider,
        "coqui": CoquiProvider,
    }
    provider_cls = mapping.get(name)
    if provider_cls is None:
        raise RuntimeError(f"Unknown TTS provider: {provider_name}")
    return provider_cls(ffmpeg_cmd=ffmpeg_cmd)


def list_provider_names() -> list[str]:
    return ["windows", "edge", "piper", "kokoro", "coqui"]
