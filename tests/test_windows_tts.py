"""Real SAPI language selection and file output; no speaker playback or network."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import wave

import pytest

from orchestrator.tts_providers.windows import WindowsSapiProvider

pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"), reason="Windows SAPI required"),
]


@pytest.fixture(scope="module")
def installed_voices():
    result = subprocess.run([
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
        "Add-Type -AssemblyName System.Speech; "
        "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try { @($synth.GetInstalledVoices() | Where-Object Enabled | ForEach-Object { "
        "@{voice=$_.VoiceInfo.Name; language=$_.VoiceInfo.Culture.TwoLetterISOLanguageName} "
        "}) | ConvertTo-Json -Compress } finally { $synth.Dispose() }",
    ], capture_output=True, timeout=25, check=True)
    value = json.loads(result.stdout.decode("utf-8-sig"))
    return value if isinstance(value, list) else [value]


@pytest.mark.parametrize("text,explicit,language", [
    ("中文语音测试，二加五等于七。", False, "zh"),
    ("Two plus five equals seven.", False, "en"),
    ("Explicit voice. 中文内容仍尊重指定的音色。", True, "en"),
])
def test_sapi_selects_content_language_unless_voice_is_explicit(tmp_path, installed_voices, text, explicit, language):
    matching = [v for v in installed_voices if v["language"] == language]
    if not matching:
        pytest.skip(f"No installed SAPI voice for {language}")
    voice = matching[0]["voice"] if explicit else None
    output = tmp_path / "speech.wav"
    script = WindowsSapiProvider()._powershell_script(str(output), text, voice, 0)
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=25)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    selected = json.loads(result.stdout.decode("utf-8-sig"))
    assert selected["language"] == language
    if voice:
        assert selected["voice"] == voice
    with wave.open(str(output)) as audio:
        assert audio.getnframes() / audio.getframerate() > 0.2
        assert any(audio.readframes(audio.getnframes()))


def test_missing_explicit_sapi_voice_fails_without_wrong_voice_audio(tmp_path):
    output = tmp_path / "speech.wav"
    script = WindowsSapiProvider()._powershell_script(str(output), "测试", "HASHI nonexistent voice", 0)
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=25)
    assert result.returncode != 0
    assert not output.exists()
