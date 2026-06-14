from __future__ import annotations
import importlib
import sys

PROVIDER_REGISTRY: dict[str, tuple[str, str]] = {
    "edge": ("orchestrator.tts_providers.edge", "EdgeTTSProvider"),
    "piper": ("orchestrator.tts_providers.piper", "PiperProvider"),
    "kokoro": ("orchestrator.tts_providers.kokoro", "KokoroProvider"),
    "coqui": ("orchestrator.tts_providers.coqui", "CoquiProvider"),
}

if sys.platform == "win32":
    PROVIDER_REGISTRY["windows"] = ("orchestrator.tts_providers.windows", "WindowsSapiProvider")

if sys.platform == "darwin":
    PROVIDER_REGISTRY["macos"] = ("orchestrator.tts_providers.macos", "MacOSTTSProvider")


def build_provider(provider_name: str, ffmpeg_cmd: str = "ffmpeg"):
    name = (provider_name or "windows").strip().lower()
    provider_ref = PROVIDER_REGISTRY.get(name)
    if provider_ref is None:
        raise RuntimeError(f"Unknown TTS provider: {provider_name}")
    module_name, class_name = provider_ref
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"TTS provider '{name}' is not available because optional dependency "
            f"'{exc.name}' is not installed."
        ) from exc
    provider_cls = getattr(module, class_name)
    return provider_cls(ffmpeg_cmd=ffmpeg_cmd)


def list_provider_names() -> list[str]:
    return list(PROVIDER_REGISTRY.keys())
