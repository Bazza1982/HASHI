from __future__ import annotations
import asyncio
from pathlib import Path

from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_wav_to_ogg


class PiperProvider(BaseTTSProvider):
    provider_name = "piper"

    def _resolve_model(self, voice_name: str | None, provider_options: dict | None) -> str:
        model = (provider_options or {}).get("model") or voice_name
        if not model:
            raise RuntimeError("Piper provider requires voice_name or provider_options.model (for example en_US-lessac-medium.onnx).")
        return str(model)

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
        model = self._resolve_model(voice_name, provider_options)
        provider_options = provider_options or {}
        exe = provider_options.get("exe") or "piper"
        python_exe = provider_options.get("python_exe") or "python"
        if provider_options.get("module_mode"):
            argv = [
                python_exe,
                "-m",
                "piper",
                "--model",
                model,
                "--output_file",
                str(wav_path),
            ]
        else:
            argv = [
                exe,
                "--model",
                model,
                "--output_file",
                str(wav_path),
            ]

        import os
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate(spoken_text.encode("utf-8"))
        if proc.returncode != 0 or not wav_path.exists():
            err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Piper synthesis failed: {err or f'exit_code={proc.returncode}'}")

        await convert_wav_to_ogg(self.ffmpeg_cmd, wav_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
        )
