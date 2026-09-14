from __future__ import annotations
import asyncio
from pathlib import Path

from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesis_runtime import (
    resolve_tts_python,
    synthesize_edge_to_mp3,
)
from orchestrator.voice_synthesizer import VoiceAsset, convert_audio_to_ogg


async def _synthesize_in_process(
    text: str,
    *,
    voice: str,
    rate: str,
    output_path: Path,
) -> None:
    """Compatibility path for installations that keep Edge TTS in Functions."""

    try:
        import edge_tts
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Edge TTS is unavailable: configure an isolated TTS runtime"
        ) from exc
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(str(output_path))


class EdgeTTSProvider(BaseTTSProvider):
    provider_name = "edge"

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
        spoken_text = self.prepare_spoken_text(text, max_chars=max_chars)
        if not spoken_text:
            raise RuntimeError("No spoken text available for synthesis.")

        provider_options = provider_options or {}
        voice = voice_name or provider_options.get("voice") or "en-US-EmmaNeural"
        output_dir.mkdir(parents=True, exist_ok=True)
        mp3_path = output_dir / f"{stem}.mp3"
        ogg_path = output_dir / f"{stem}.ogg"

        rate_percent = int(provider_options.get("rate_percent", int(rate) * 10))
        rate_sign = "+" if rate_percent >= 0 else ""
        rate_value = f"{rate_sign}{rate_percent}%"
        if resolve_tts_python() is not None:
            await asyncio.to_thread(
                synthesize_edge_to_mp3,
                spoken_text,
                output_path=mp3_path,
                voice=voice,
                rate=rate_value,
            )
        else:
            await _synthesize_in_process(
                spoken_text,
                voice=voice,
                rate=rate_value,
                output_path=mp3_path,
            )
        if not mp3_path.exists():
            raise RuntimeError("Edge TTS did not produce an audio file.")

        await convert_audio_to_ogg(self.ffmpeg_cmd, mp3_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=None,
            ogg_path=ogg_path,
        )
