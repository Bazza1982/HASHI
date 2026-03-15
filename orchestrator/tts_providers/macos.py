# orchestrator/tts_providers/macos.py
from __future__ import annotations
import asyncio
from pathlib import Path
from .base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_wav_to_ogg


# The macOS `say` command writes AIFF by default.
# Pass -o with a .wav extension and it writes WAVE (PCM).
# Rate: `say -r N` where N is words-per-minute. Default ~175 wpm.
# We map HASHI rate (-10 to +10) → wpm range (100–250).

_RATE_MIN = 100    # wpm at rate=-10
_RATE_MAX = 250    # wpm at rate=+10
_RATE_DEFAULT = 175


def _hashi_rate_to_wpm(rate: int) -> int:
    """Convert HASHI rate (-10..+10) to macOS say words-per-minute."""
    clamped = max(-10, min(10, rate))
    return int(_RATE_DEFAULT + clamped * (_RATE_MAX - _RATE_MIN) / 20)


class MacOSTTSProvider(BaseTTSProvider):
    provider_name = "macos"

    # Built-in macOS English voices as of Ventura/Sonoma.
    # Full list: `say -v ?`
    VOICES = {
        "Ava":      "en-US female (enhanced)",
        "Samantha": "en-US female",
        "Alex":     "en-US male",
        "Moira":    "en-IE female",
        "Karen":    "en-AU female",
        "Daniel":   "en-GB male",
        "Siri":     "Siri voice (if enabled in System Settings)",
    }

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
            raise RuntimeError("No spoken text after preparation.")

        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = output_dir / f"{stem}.wav"
        ogg_path = output_dir / f"{stem}.ogg"

        cmd = ["say"]
        if voice_name:
            cmd += ["-v", voice_name]
        if rate != 0:
            cmd += ["-r", str(_hashi_rate_to_wpm(rate))]
        cmd += ["-o", str(wav_path)]
        # Pass text as final argument (say does NOT read from stdin by default)
        cmd.append(spoken_text)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(
                f"macOS `say` failed (exit {proc.returncode}): {err or '(no stderr)'}"
            )
        if not wav_path.exists():
            raise RuntimeError(
                f"macOS `say` exited 0 but wav not created at {wav_path}"
            )

        await convert_wav_to_ogg(self.ffmpeg_cmd, wav_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
        )