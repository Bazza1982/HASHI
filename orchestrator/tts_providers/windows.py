from __future__ import annotations
import asyncio
from pathlib import Path

from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_wav_to_ogg


class WindowsSapiProvider(BaseTTSProvider):
    provider_name = "windows"

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

        ps_script = self._powershell_script(str(wav_path), spoken_text, voice_name, rate)
        proc = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0 or not wav_path.exists():
            err = stderr.decode("utf-8", errors="replace").strip() or stdout.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Windows TTS synthesis failed: {err or f'exit_code={proc.returncode}'}")

        await convert_wav_to_ogg(self.ffmpeg_cmd, wav_path, ogg_path)
        return VoiceAsset(
            provider=self.provider_name,
            text=text,
            spoken_text=spoken_text,
            wav_path=wav_path,
            ogg_path=ogg_path,
        )

    def _powershell_script(self, wav_path: str, spoken_text: str, voice_name: str | None, rate: int) -> str:
        safe_wav = wav_path.replace("'", "''")
        safe_text = spoken_text.replace("'", "''")
        voice_line = ""
        if voice_name:
            safe_voice = voice_name.replace("'", "''")
            voice_line = (
                "$voice = $synth.GetInstalledVoices() | "
                f"Where-Object {{ $_.VoiceInfo.Name -eq '{safe_voice}' }} | Select-Object -First 1; "
                "if ($voice) { $synth.SelectVoice($voice.VoiceInfo.Name) }\n"
            )
        return (
            "Add-Type -AssemblyName System.Speech\n"
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            f"$synth.Rate = {int(rate)}\n"
            f"{voice_line}"
            f"$synth.SetOutputToWaveFile('{safe_wav}')\n"
            f"$synth.Speak('{safe_text}')\n"
            "$synth.Dispose()\n"
        )
