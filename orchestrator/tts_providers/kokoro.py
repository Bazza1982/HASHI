from __future__ import annotations
import asyncio
import os
from pathlib import Path

from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_wav_to_ogg


class KokoroProvider(BaseTTSProvider):
    provider_name = "kokoro"

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

        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = output_dir / f"{stem}.wav"
        ogg_path = output_dir / f"{stem}.ogg"
        provider_options = provider_options or {}
        voice = voice_name or provider_options.get("voice")
        if not voice:
            raise RuntimeError("Kokoro provider requires voice_name or provider_options.voice (for example af_heart).")

        code = (
            "import soundfile as sf\n"
            "from kokoro import KPipeline\n"
            f"text = {spoken_text!r}\n"
            f"voice = {voice!r}\n"
            f"lang = {provider_options.get('lang_code', 'a')!r}\n"
            f"speed = {float(provider_options.get('speed', 1.0))!r}\n"
            f"wav_path = {str(wav_path)!r}\n"
            "pipeline = KPipeline(lang_code=lang)\n"
            "generator = pipeline(text, voice=voice, speed=speed)\n"
            "audio = None\n"
            "for _, _, audio_arr in generator:\n"
            "    audio = audio_arr\n"
            "    break\n"
            "if audio is None:\n"
            "    raise RuntimeError('Kokoro returned no audio')\n"
            "sf.write(wav_path, audio, 24000)\n"
        )
        env = os.environ.copy()
        python_exe = provider_options.get("python_exe") or "python"
        proc = await asyncio.create_subprocess_exec(
            python_exe,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0 or not wav_path.exists():
            err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Kokoro synthesis failed: {err or f'exit_code={proc.returncode}'}")

        await convert_wav_to_ogg(self.ffmpeg_cmd, wav_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
        )
