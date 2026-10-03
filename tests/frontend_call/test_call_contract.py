import base64
import io
import wave
import pytest
from orchestrator.frontend_call.contract import CallError, decode_wav, speech_segments


def wav(seconds=0.2):
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * int(16000 * seconds))
    return base64.b64encode(out.getvalue()).decode()


def test_wav_is_validated_not_trusted_by_filename():
    assert decode_wav(wav())[:4] == b"RIFF"
    for value in ["not audio", base64.b64encode(b"fake").decode(), wav(61)]:
        with pytest.raises(CallError):
            decode_wav(value)


def test_speech_is_bounded_and_never_reads_code_blocks():
    parts, truncated = speech_segments(
        "Hello.\n```python\nSECRET_CODE\n```\n" + ("sentence. " * 3000)
    )
    assert parts and len(parts) <= 12 and truncated
    assert all(len(p) <= 600 for p in parts)
    assert "SECRET_CODE" not in "".join(parts)
