import io
import sys
import wave
from pathlib import Path

import pytest
from PIL import Image

from orchestrator.frontend_call import fallback_av
from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_call.fallback_av import (
    mux_audio_to_static_mp4,
    silence_wav,
)


def _png():
    output = io.BytesIO()
    Image.new("RGB", (64, 64), (20, 30, 40)).save(output, format="PNG")
    return output.getvalue()


def test_captions_only_silence_is_valid_bounded_pcm():
    audio = silence_wav()
    with wave.open(io.BytesIO(audio), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getframerate() == 16000
        assert 0 < stream.getnframes() <= 16000


def test_packager_can_use_the_checkout_dependency_from_an_isolated_runtime(
    monkeypatch,
):
    monkeypatch.setattr(fallback_av.shutil, "which", lambda _name: None)
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    assert Path(fallback_av._ffmpeg_executable()).is_file()


@pytest.mark.integration
async def test_bundled_ffmpeg_packages_fallback_audio_as_complete_mp4():
    media = await mux_audio_to_static_mp4(_png(), silence_wav(), "audio/wav")
    assert b"ftyp" in media[:32]
    assert len(media) < 64 * 1024 * 1024


async def test_fallback_packager_rejects_unknown_audio_type():
    with pytest.raises(CallError, match="call_audio_fallback_media_type_invalid"):
        await mux_audio_to_static_mp4(_png(), silence_wav(), "audio/unknown")
