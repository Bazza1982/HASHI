from __future__ import annotations
import sys

from orchestrator.tts_providers.coqui import CoquiProvider
from orchestrator.tts_providers.edge import EdgeTTSProvider
from orchestrator.tts_providers.kokoro import KokoroProvider
from orchestrator.tts_providers.hashi_compute import HashiComputeTTSProvider
from orchestrator.tts_providers.openrouter import OpenRouterTTSProvider
from orchestrator.tts_providers.piper import PiperProvider

PROVIDER_REGISTRY: dict[str, type] = {
    "edge": EdgeTTSProvider,
    "piper": PiperProvider,
    "kokoro": KokoroProvider,
    "coqui": CoquiProvider,
    "hashi_compute": HashiComputeTTSProvider,
    "openai_compatible": OpenRouterTTSProvider,
}

if sys.platform == "win32":
    from orchestrator.tts_providers.windows import WindowsSapiProvider

    PROVIDER_REGISTRY["windows"] = WindowsSapiProvider

if sys.platform == "darwin":
    from orchestrator.tts_providers.macos import MacOSTTSProvider

    PROVIDER_REGISTRY["macos"] = MacOSTTSProvider


def build_provider(
    provider_name: str,
    ffmpeg_cmd: str = "ffmpeg",
    *,
    project_root=None,
    secrets: dict | None = None,
):
    name = (provider_name or "windows").strip().lower()
    provider_cls = PROVIDER_REGISTRY.get(name)
    if provider_cls is None:
        raise RuntimeError(f"Unknown TTS provider: {provider_name}")
    if name == "hashi_compute":
        return provider_cls(
            ffmpeg_cmd=ffmpeg_cmd,
            project_root=project_root,
            secrets=secrets,
        )
    if name == "openai_compatible":
        return provider_cls(ffmpeg_cmd=ffmpeg_cmd, secrets=secrets)
    return provider_cls(ffmpeg_cmd=ffmpeg_cmd)


def list_provider_names() -> list[str]:
    return list(PROVIDER_REGISTRY.keys())
