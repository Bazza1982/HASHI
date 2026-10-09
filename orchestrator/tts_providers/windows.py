from __future__ import annotations
import asyncio
import wave
from pathlib import Path

from orchestrator.tts_providers.base import BaseTTSProvider
from orchestrator.voice_synthesizer import VoiceAsset, convert_wav_to_ogg, spoken_text_language


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
            "-NonInteractive",
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

        # SAPI may exit successfully without speaking unsupported characters.
        # Do not turn an empty WAV into a successful 0:00 attachment.
        with wave.open(str(wav_path), "rb") as audio:
            if not audio.getnframes():
                raise RuntimeError("Windows TTS returned empty audio for the selected voice.")

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
        if voice_name:
            safe_voice = voice_name.replace("'", "''")
            voice_line = (
                "$voice = $synth.GetInstalledVoices() | "
                f"Where-Object {{ $_.Enabled -and $_.VoiceInfo.Name -eq '{safe_voice}' }} | Select-Object -First 1\n"
                "if (-not $voice) { throw 'Configured Windows TTS voice is unavailable.' }\n"
                "$synth.SelectVoice($voice.VoiceInfo.Name)\n"
            )
        else:
            language = spoken_text_language(spoken_text)
            voice_line = (
                f"if ($synth.Voice.Culture.TwoLetterISOLanguageName -ne '{language}') {{\n"
                "  $voice = $synth.GetInstalledVoices() | "
                f"Where-Object {{ $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq '{language}' }} | Select-Object -First 1\n"
                f"  if (-not $voice) {{ throw 'No installed Windows TTS voice for language: {language}.' }}\n"
                "  $synth.SelectVoice($voice.VoiceInfo.Name)\n"
                "}\n"
            )
        return (
            "$ErrorActionPreference = 'Stop'\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
            "Add-Type -AssemblyName System.Speech\n"
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
            "try {\n"
            f"$synth.Rate = {int(rate)}\n"
            f"{voice_line}"
            f"$synth.SetOutputToWaveFile('{safe_wav}')\n"
            f"$synth.Speak('{safe_text}')\n"
            "@{ voice = $synth.Voice.Name; language = $synth.Voice.Culture.TwoLetterISOLanguageName } | ConvertTo-Json -Compress\n"
            "} finally { $synth.Dispose() }\n"
        )
