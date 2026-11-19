import base64
import io
import wave
import pytest
from orchestrator.frontend_call.contract import (
    MAX_SPEECH_CHARS,
    MAX_SPEECH_SEGMENTS,
    CallError,
    decode_wav,
    speech_segments,
)


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
    for value in ["not audio", base64.b64encode(b"fake").decode()]:
        with pytest.raises(CallError):
            decode_wav(value)
    assert decode_wav(wav(61))[:4] == b"RIFF"


def test_wav_stereo_is_resampled_and_selected_duration_is_enforced(monkeypatch):
    out = io.BytesIO()
    with wave.open(out, "wb") as source:
        source.setnchannels(2)
        source.setsampwidth(2)
        source.setframerate(48000)
        source.writeframes(bytes(4) * 48000)
    normalized = decode_wav(base64.b64encode(out.getvalue()).decode())
    with wave.open(io.BytesIO(normalized), "rb") as result:
        assert (
            result.getnchannels(),
            result.getsampwidth(),
            result.getframerate(),
        ) == (1, 2, 16000)
        assert abs(result.getnframes() - 16000) <= 1
    monkeypatch.setenv("HASHI_CALL_MAX_AUDIO_SECONDS", "1")
    with pytest.raises(CallError, match="call_audio_duration_invalid"):
        decode_wav(wav(2))
    monkeypatch.setenv("HASHI_CALL_MAX_AUDIO_SECONDS", "180")
    monkeypatch.setenv("HASHI_CALL_MAX_AUDIO_DECODED_BYTES", "1000")
    with pytest.raises(CallError, match="call_media_limit"):
        decode_wav(base64.b64encode(out.getvalue()).decode())
    monkeypatch.setenv("HASHI_CALL_MAX_AUDIO_DECODED_BYTES", str(64 * 1024 * 1024))
    bad = io.BytesIO()
    with wave.open(bad, "wb") as source:
        source.setnchannels(1)
        source.setsampwidth(1)
        source.setframerate(16000)
        source.writeframes(bytes(1600))
    with pytest.raises(CallError, match="call_wav_format_required"):
        decode_wav(base64.b64encode(bad.getvalue()).decode())


def test_speech_is_bounded_and_never_reads_code_blocks():
    parts, truncated = speech_segments(
        "Hello.\n```python\nSECRET_CODE\n```\n" + ("sentence. " * 3000)
    )
    assert parts and len(parts) <= MAX_SPEECH_SEGMENTS and truncated
    assert all(len(p) <= MAX_SPEECH_CHARS for p in parts)
    assert "SECRET_CODE" not in "".join(parts)


def test_selected_smaller_wire_capacity_derives_default_media_limits(monkeypatch):
    from orchestrator.frontend_call.contract import call_media_capacity

    monkeypatch.setenv("HASHI_CALL_MAX_BODY_BYTES", str(1024 * 1024))
    monkeypatch.delenv("HASHI_CALL_MAX_AUDIO_BYTES", raising=False)
    monkeypatch.delenv("HASHI_CALL_MAX_IMAGE_BYTES", raising=False)
    capacity = call_media_capacity()
    budget = ((1024 * 1024 - 4096) // 4) * 3
    assert capacity["max_audio_bytes"] == budget
    assert capacity["max_image_bytes"] == budget
    monkeypatch.setenv("HASHI_CALL_MAX_AUDIO_BYTES", str(budget + 1))
    with pytest.raises(CallError, match="call_media_capacity_invalid"):
        call_media_capacity()
    monkeypatch.delenv("HASHI_CALL_MAX_AUDIO_BYTES")
    monkeypatch.setenv("HASHI_CALL_MAX_BODY_BYTES", "4096")
    with pytest.raises(CallError, match="call_media_capacity_invalid"):
        call_media_capacity()


def test_structured_speech_preserves_non_table_operators_and_link_destination():
    from orchestrator.frontend_call.contract import (
        structured_speech_text,
        video_speech_segments,
    )

    fence = chr(96) * 3
    result = (
        fence
        + "js\nleft || right\n"
        + fence
        + "\n[Docs](https://example.test/path)\n"
        + "| key | value |\n| --- | --- |\n| answer | 42 |"
    )
    spoken = structured_speech_text(result)
    assert "left || right" in spoken
    assert "https://example.test/path" in spoken
    assert "answer, 42" in spoken
    assert "---" not in spoken
    assert structured_speech_text("left || right") == "left || right"
    parts, truncated = video_speech_segments(
        spoken, already_clean=True, max_segments=None
    )
    assert not truncated
    assert "https://example.test/path" in "".join(parts)
    assert "left || right" in "".join(parts)


def test_call_queue_capacities_are_device_configurable_and_validated(monkeypatch):
    from orchestrator.frontend_call.contract import call_queue_capacity

    for key in (
        "HASHI_CALL_MAX_ACTIVE_CALLS",
        "HASHI_CALL_CONCURRENT_TURNS",
        "HASHI_CALL_MAX_PENDING_TURNS",
    ):
        monkeypatch.delenv(key, raising=False)
    assert call_queue_capacity() == {
        "max_active_calls": 16,
        "concurrent_turns": 4,
        "max_pending_turns": 8,
    }
    monkeypatch.setenv("HASHI_CALL_MAX_ACTIVE_CALLS", "24")
    monkeypatch.setenv("HASHI_CALL_CONCURRENT_TURNS", "2")
    monkeypatch.setenv("HASHI_CALL_MAX_PENDING_TURNS", "6")
    assert call_queue_capacity() == {
        "max_active_calls": 24,
        "concurrent_turns": 2,
        "max_pending_turns": 6,
    }
    monkeypatch.setenv("HASHI_CALL_MAX_PENDING_TURNS", "1")
    with pytest.raises(CallError, match="call_queue_capacity_invalid"):
        call_queue_capacity()
    monkeypatch.setenv("HASHI_CALL_MAX_PENDING_TURNS", "0")
    with pytest.raises(CallError, match="call_queue_capacity_invalid"):
        call_queue_capacity()


def test_call_lease_policy_is_selected_and_can_disable_total_duration(monkeypatch):
    from orchestrator.frontend_call.contract import call_lease_policy

    monkeypatch.delenv("HASHI_CALL_LEASE_SECONDS", raising=False)
    monkeypatch.delenv("HASHI_CALL_MAX_DURATION_SECONDS", raising=False)
    assert call_lease_policy() == {"lease_seconds": 180, "max_duration_seconds": None}
    monkeypatch.setenv("HASHI_CALL_LEASE_SECONDS", "120")
    monkeypatch.setenv("HASHI_CALL_MAX_DURATION_SECONDS", "0")
    assert call_lease_policy() == {"lease_seconds": 120, "max_duration_seconds": None}
    for lease, duration in [
        ("0", "0"),
        ("nan", "10"),
        ("20", "-1"),
        ("20", "2147483648"),
    ]:
        monkeypatch.setenv("HASHI_CALL_LEASE_SECONDS", lease)
        monkeypatch.setenv("HASHI_CALL_MAX_DURATION_SECONDS", duration)
        with pytest.raises(CallError, match="call_lease_policy_invalid"):
            call_lease_policy()
