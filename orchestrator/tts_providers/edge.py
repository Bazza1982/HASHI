from __future__ import annotations
from pathlib import Path

import edge_tts

from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_audio_to_ogg


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
        communicate = edge_tts.Communicate(
            text=spoken_text,
            voice=voice,
            rate=f"{rate_sign}{rate_percent}%",
        )
        await communicate.save(str(mp3_path))
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
