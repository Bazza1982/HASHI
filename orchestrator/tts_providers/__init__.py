from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path


def build_provider(
    provider_name: str,
    ffmpeg_cmd: str = "ffmpeg",
    *,
    provider_config: Mapping[str, object] | None = None,
    secrets: Mapping[str, object] | None = None,
    bridge_home: Path | None = None,
):
    """Build one protocol adapter for a configured TTS service."""

    name = str(provider_name or "").strip().casefold()
    config = dict(provider_config or {})
    adapter = str(config.get("adapter") or name).strip().casefold()
    if adapter == "edge":
        from orchestrator.tts_providers.edge import EdgeTTSProvider

        return EdgeTTSProvider(ffmpeg_cmd=ffmpeg_cmd)
    if adapter == "piper":
        from orchestrator.tts_providers.piper import PiperProvider

        return PiperProvider(ffmpeg_cmd=ffmpeg_cmd)
    if adapter == "kokoro":
        from orchestrator.tts_providers.kokoro import KokoroProvider

        return KokoroProvider(ffmpeg_cmd=ffmpeg_cmd)
    if adapter == "coqui":
        from orchestrator.tts_providers.coqui import CoquiProvider

        return CoquiProvider(ffmpeg_cmd=ffmpeg_cmd)
    if adapter == "windows" and sys.platform == "win32":
        from orchestrator.tts_providers.windows import WindowsSapiProvider

        return WindowsSapiProvider(ffmpeg_cmd=ffmpeg_cmd)
    if adapter == "macos" and sys.platform == "darwin":
        from orchestrator.tts_providers.macos import MacOSTTSProvider

        return MacOSTTSProvider(ffmpeg_cmd=ffmpeg_cmd)
    if adapter == "openai_compatible" and config:
        from orchestrator.tts_providers.openai_compatible import (
            OpenAICompatibleTTSProvider,
        )

        return OpenAICompatibleTTSProvider(
            ffmpeg_cmd=ffmpeg_cmd,
            service_id=name,
            service=config,
            secrets=secrets,
            bridge_home=bridge_home,
        )
    raise RuntimeError(
        f"Unknown or unavailable TTS adapter for service {provider_name}: {adapter}"
    )


def list_provider_names() -> list[str]:
    """Compatibility view derived from the shipped service catalogue."""

    from orchestrator.voice_catalog import VoiceCatalog

    return list(VoiceCatalog.load().service_ids)


__all__ = ["build_provider", "list_provider_names"]
