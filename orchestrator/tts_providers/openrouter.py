"""OpenRouter Gemini speech provider for global Agent TTS routes."""

from __future__ import annotations

import asyncio
import io
import wave
from pathlib import Path
from typing import Any

import aiohttp

from orchestrator.gemini_tts_catalog import GEMINI_TTS_MODEL, GEMINI_VOICES
from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_wav_to_ogg


OPENROUTER_SPEECH_ENDPOINT = "https://openrouter.ai/api/v1/audio/speech"
MAX_SPEECH_BYTES = 24 * 1024 * 1024


class OpenRouterTTSProvider(BaseTTSProvider):
    provider_name = "openai_compatible"

    def __init__(
        self,
        ffmpeg_cmd: str = "ffmpeg",
        *,
        secrets: dict | None = None,
        session_factory=aiohttp.ClientSession,
        **_kwargs,
    ) -> None:
        super().__init__(ffmpeg_cmd=ffmpeg_cmd)
        self.secrets = dict(secrets or {})
        self.session_factory = session_factory

    def _credential(self) -> str:
        for key in (
            "openrouter_key",
            "openrouter-api_key",
            "openrouter_api_key",
        ):
            value = str(self.secrets.get(key) or "").strip()
            if value:
                return value
        raise RuntimeError("OpenRouter credential is not configured.")

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
        del rate
        spoken_text = self.prepare_spoken_text(text, max_chars=max_chars)
        if not spoken_text:
            raise RuntimeError("No spoken text available for synthesis.")
        options = dict(provider_options or {})
        options.pop("service", None)
        options.pop("node_id", None)
        options.pop("offering_id", None)
        model = str(options.pop("model", "") or GEMINI_TTS_MODEL).strip()
        voice = str(voice_name or "").strip()
        if model != GEMINI_TTS_MODEL:
            raise RuntimeError("OpenRouter TTS model is not allowed by this service route.")
        if voice not in GEMINI_VOICES:
            raise RuntimeError("OpenRouter TTS voice is not in the provider catalogue.")
        style = str(options.pop("style", "") or "").strip()
        if options:
            raise RuntimeError("OpenRouter TTS route contains unsupported options.")
        body: dict[str, Any] = {
            "model": model,
            "input": spoken_text,
            "voice": voice,
            "response_format": "pcm",
        }
        if style:
            body["provider"] = {
                "options": {
                    "google-ai-studio": {"speech_metadata": {"style": style}}
                }
            }
        try:
            async with self.session_factory(
                timeout=aiohttp.ClientTimeout(total=30), trust_env=False
            ) as session:
                async with session.post(
                    OPENROUTER_SPEECH_ENDPOINT,
                    headers={"Authorization": f"Bearer {self._credential()}"},
                    json=body,
                    allow_redirects=False,
                ) as response:
                    if response.status not in {200, 201}:
                        error = RuntimeError("OpenRouter TTS rejected the speech request.")
                        error.status = response.status
                        error.retryable = response.status >= 500 or response.status == 429
                        raise error
                    media_type = (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .strip()
                        .casefold()
                    )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.content.iter_chunked(16384):
                        total += len(chunk)
                        if total > MAX_SPEECH_BYTES:
                            raise RuntimeError("OpenRouter TTS response exceeded the audio limit.")
                        chunks.append(chunk)
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            error = RuntimeError("OpenRouter TTS is unavailable.")
            error.retryable = True
            raise error from exc
        payload = b"".join(chunks)
        # Match the qualified Call adapter contract: OpenRouter Gemini returns
        # headerless 24 kHz mono signed 16-bit PCM, and a tiny/even error body
        # must not be mistaken for valid audio.
        if media_type != "audio/pcm" or len(payload) < 4800 or len(payload) % 2:
            raise RuntimeError("OpenRouter TTS returned malformed PCM audio.")
        stream = io.BytesIO()
        with wave.open(stream, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(24000)
            output.writeframes(payload)
        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = output_dir / f"{stem}.wav"
        wav_path.write_bytes(stream.getvalue())
        ogg_path = output_dir / f"{stem}.ogg"
        await convert_wav_to_ogg(self.ffmpeg_cmd, wav_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
            voice_name=voice,
        )


__all__ = ["OpenRouterTTSProvider"]
