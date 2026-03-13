from __future__ import annotations
from pathlib import Path

from orchestrator.voice_synthesizer import VoiceAsset, prepare_spoken_text


class BaseTTSProvider:
    provider_name = "base"

    def __init__(self, ffmpeg_cmd: str = "ffmpeg"):
        self.ffmpeg_cmd = ffmpeg_cmd

    def prepare_spoken_text(self, text: str, max_chars: int = 1200) -> str:
        return prepare_spoken_text(text, max_chars=max_chars)

    def list_voices(self) -> list[str]:
        return []

    async def synthesize(
        self,
        text: str,
        output_dir: Path,
        stem: str,
        voice_name: str | None = None,
        rate: int = 0,
        max_chars: int = 1200,
        provider_options: dict | None = None,
    ) -> VoiceAsset:
        raise NotImplementedError
