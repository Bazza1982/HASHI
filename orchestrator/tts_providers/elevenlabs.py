from __future__ import annotations
import asyncio
import os
from pathlib import Path

from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_audio_to_ogg

# Default voice: Rachel — natural, clear, English
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
_DEFAULT_MODEL = "eleven_multilingual_v2"
_API_BASE = "https://api.elevenlabs.io/v1"


class ElevenLabsProvider(BaseTTSProvider):
    """ElevenLabs TTS provider — high-quality, near-human voices.

    Configuration via secrets.json or provider_options:
      elevenlabs_api_key  — required
      elevenlabs_voice_id — optional, defaults to Rachel
      elevenlabs_model    — optional, defaults to eleven_multilingual_v2

    Supports multilingual text including Chinese/Japanese/English.

    Provider options (per-request overrides):
      voice_id   — ElevenLabs voice ID
      model      — model ID (eleven_multilingual_v2 / eleven_turbo_v2_5 etc.)
      stability         — 0.0–1.0, default 0.5
      similarity_boost  — 0.0–1.0, default 0.75
      style             — 0.0–1.0, default 0.0 (expressiveness)
      speed             — 0.7–1.2, default 1.0
    """

    provider_name = "elevenlabs"

    def __init__(self, ffmpeg_cmd: str = "ffmpeg", api_key: str | None = None):
        super().__init__(ffmpeg_cmd=ffmpeg_cmd)
        self._api_key = api_key

    def _get_api_key(self, provider_options: dict | None = None) -> str:
        opts = provider_options or {}
        key = (
            opts.get("api_key")
            or self._api_key
            or os.environ.get("ELEVENLABS_API_KEY", "")
        )
        if not key:
            raise RuntimeError(
                "ElevenLabs API key not configured. "
                "Set elevenlabs_api_key in secrets.json or ELEVENLABS_API_KEY env var."
            )
        return key

    def list_voices(self) -> list[str]:
        # Common preset voices — user can override with any ElevenLabs voice ID
        return [
            "Rachel (21m00Tcm4TlvDq8ikWAM)",
            "Drew (29vD33N1CtxCmqQRPOHJ)",
            "Clyde (2EiwWnXFnvU5JabPnv8n)",
            "Paul (5Q0t7uMcjvnagumLfvZi)",
            "Domi (AZnzlk1XvdvUeBnXmlld)",
            "Dave (CYw3kZ02Hs0563khs1Fj)",
            "Fin (D38z5RcWu1voky8WS1ja)",
            "Sarah (EXAVITQu4vr4xnSDxMaL)",
            "Antoni (ErXwobaYiN019PkySvjV)",
            "Thomas (GBv7mTt0atIp3Br8iCZE)",
            "Charlie (IKne3meq5aSn9XLyUdCD)",
            "Emily (LcfcDJNUP1GQjkzn1xUU)",
            "Elli (MF3mGyEYCl7XYWbV9V6O)",
            "Callum (N2lVS1w4EtoT3dr4eOWO)",
            "Patrick (ODq5zmih8GrVes37Dy9N)",
            "Harry (SOYHLrjzK2X1ezoPC6cr)",
            "Liam (TX3LPaxmHKxFdv7VOQHJ)",
            "Dorothy (ThT5KcBeYPX3keUQqHPh)",
            "Josh (TxGEqnHWrfWFTfGW9XjX)",
            "Arnold (VR6AewLTigWG4xSOukaG)",
            "Adam (pNInz6obpgDQGcFmaJgB)",
            "Bella (EXAVITQu4vr4xnSDxMaL)",
            "Freya (jsCqWAovK2LkecY7zXl4)",
            "Gigi (jBpfuIE2acCO8z3wKNLl)",
            "Giovanni (zcAOhNBS3c14rBihAFp1)",
            "Glinda (z9fAnlkpzviPz146aGWa)",
            "Grace (oWAxZDx7w5VEj9dCyTzz)",
            "Daniel (onwK4e9ZLuTAKqWW03F9)",
        ]

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
        import httpx

        spoken_text = self.prepare_spoken_text(text, max_chars=max_chars)
        if not spoken_text:
            raise RuntimeError("No spoken text available for synthesis.")

        opts = provider_options or {}
        api_key = self._get_api_key(opts)

        # Voice ID: provider_options > voice_name (if looks like an ID) > env > default
        voice_id = opts.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID", "")
        if not voice_id:
            if voice_name and len(voice_name) == 20 and voice_name.isalnum():
                # Looks like a raw ElevenLabs ID
                voice_id = voice_name
            elif voice_name:
                # Try to match a display name from list_voices
                voice_id = _resolve_voice_name(voice_name)
        if not voice_id:
            voice_id = _DEFAULT_VOICE_ID

        model = opts.get("model") or os.environ.get("ELEVENLABS_MODEL", _DEFAULT_MODEL)

        # Voice settings
        voice_settings: dict = {
            "stability": float(opts.get("stability", 0.5)),
            "similarity_boost": float(opts.get("similarity_boost", 0.75)),
            "style": float(opts.get("style", 0.0)),
            "use_speaker_boost": True,
        }

        # Speed (supported in newer models)
        speed = float(opts.get("speed", 1.0))
        # Convert bridge `rate` (-5 to +5) to speed multiplier if speed not explicit
        if "speed" not in opts and rate != 0:
            speed = max(0.7, min(1.2, 1.0 + rate * 0.04))

        payload: dict = {
            "text": spoken_text,
            "model_id": model,
            "voice_settings": voice_settings,
        }
        if speed != 1.0:
            payload["speed"] = speed

        output_dir.mkdir(parents=True, exist_ok=True)
        mp3_path = output_dir / f"{stem}.mp3"
        ogg_path = output_dir / f"{stem}.ogg"

        url = f"{_API_BASE}/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code != 200:
                raise RuntimeError(
                    f"ElevenLabs API error {response.status_code}: {response.text[:200]}"
                )
            mp3_path.write_bytes(response.content)

        await convert_audio_to_ogg(self.ffmpeg_cmd, mp3_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=None,
            ogg_path=ogg_path,
        )


def _resolve_voice_name(name: str) -> str:
    """Try to extract voice ID from display names like 'Rachel (21m00Tcm4TlvDq8ikWAM)'."""
    import re
    m = re.search(r"\(([A-Za-z0-9]{20})\)", name)
    if m:
        return m.group(1)
    # Partial name match against known voices
    _known = {
        "rachel": "21m00Tcm4TlvDq8ikWAM",
        "sarah": "EXAVITQu4vr4xnSDxMaL",
        "emily": "LcfcDJNUP1GQjkzn1xUU",
        "elli": "MF3mGyEYCl7XYWbV9V6O",
        "dorothy": "ThT5KcBeYPX3keUQqHPh",
        "adam": "pNInz6obpgDQGcFmaJgB",
        "josh": "TxGEqnHWrfWFTfGW9XjX",
        "drew": "29vD33N1CtxCmqQRPOHJ",
        "daniel": "onwK4e9ZLuTAKqWW03F9",
        "freya": "jsCqWAovK2LkecY7zXl4",
        "grace": "oWAxZDx7w5VEj9dCyTzz",
    }
    return _known.get(name.lower().strip(), "")
