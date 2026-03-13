from __future__ import annotations
import asyncio
import os
from pathlib import Path

from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_wav_to_ogg


class CoquiProvider(BaseTTSProvider):
    provider_name = "coqui"

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
        model_name = provider_options.get("model") or "tts_models/en/vctk/vits"
        speaker = provider_options.get("speaker") or voice_name
        language = provider_options.get("language")
        python_exe = provider_options.get("python_exe") or "python"

        code = (
            "from TTS.api import TTS\n"
            f"text = {spoken_text!r}\n"
            f"wav_path = {str(wav_path)!r}\n"
            f"model_name = {model_name!r}\n"
            f"speaker = {speaker!r}\n"
            f"language = {language!r}\n"
            "tts = TTS(model_name=model_name)\n"
            "kwargs = {}\n"
            "if speaker:\n"
            "    kwargs['speaker'] = speaker\n"
            "if language:\n"
            "    kwargs['language'] = language\n"
            "tts.tts_to_file(text=text, file_path=wav_path, **kwargs)\n"
        )
        env = os.environ.copy()
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
            raise RuntimeError(f"Coqui synthesis failed: {err or f'exit_code={proc.returncode}'}")

        await convert_wav_to_ogg(self.ffmpeg_cmd, wav_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
        )
