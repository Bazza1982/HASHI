"""Native audio conversion must expose its failed step without retrying synthesis."""
from unittest.mock import AsyncMock

import pytest

from orchestrator import voice_synthesizer
from orchestrator.voice_manager import _provider_failure


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    FileNotFoundError(2, "No such file or directory"),
    PermissionError(13, "Permission denied; authorization=private_probe_credential"),
])
async def test_converter_launch_failure_retains_conversion_phase_and_is_not_retryable(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(voice_synthesizer, "configured_media_executable", lambda _: None)
    start = AsyncMock(side_effect=failure)
    monkeypatch.setattr(voice_synthesizer.asyncio, "create_subprocess_exec", start)
    with pytest.raises(voice_synthesizer.VoiceSynthesisError) as caught:
        await voice_synthesizer.convert_audio_to_ogg(
            "ffmpeg", tmp_path / "generated.wav", tmp_path / "playable.ogg")
    projected = _provider_failure("windows", caught.value, configured_label="Windows TTS")
    assert projected.code == "tts_audio_conversion_failed"
    assert projected.retryable is False
    assert "FFmpeg" in projected.reason and "conversion" in projected.reason.casefold()
    assert "private_probe_credential" not in projected.reason
    assert start.await_count == 1
    assert not (tmp_path / "playable.ogg").exists()
    if isinstance(failure, FileNotFoundError):
        assert "configure" in projected.reason.casefold()


@pytest.mark.asyncio
async def test_converter_uses_configured_instance_executable_without_path_fallback(tmp_path, monkeypatch):
    converter = str(tmp_path / "private tools" / "ffmpeg.exe")
    output = tmp_path / "playable.ogg"

    async def communicate():
        output.write_bytes(b"OggSconversion-probe")
        return b"", b""

    from types import SimpleNamespace
    process = SimpleNamespace(returncode=0, communicate=communicate)
    start = AsyncMock(return_value=process)
    monkeypatch.setattr(voice_synthesizer, "configured_media_executable", lambda _: converter)
    monkeypatch.setattr(voice_synthesizer.asyncio, "create_subprocess_exec", start)
    await voice_synthesizer.convert_wav_to_ogg("ffmpeg", tmp_path / "generated.wav", output)
    assert start.await_args.args[0] == converter
    assert start.await_args.args[-1] == str(output)
    assert start.await_count == 1
    assert not (tmp_path / "state").exists()

@pytest.mark.asyncio
async def test_native_speech_provider_failures_survive_real_json_rpc_and_http(tmp_path):
    import json
    import multiprocessing
    from types import SimpleNamespace
    from orchestrator.function_worker_host import FunctionWorkerHost
    from orchestrator.function_worker_protocol import JsonConnectionPeer
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle
    from orchestrator.voice_synthesizer import VoiceProviderFailure, VoiceSynthesisError
    from tests.test_session_api import _Request, _server

    error = VoiceSynthesisError(
        "tts_audio_conversion_failed",
        "FFmpeg converter unavailable; configure the instance. token=do_not_export",
        failures=(VoiceProviderFailure(
            "windows", "tts_audio_conversion_failed",
            "FFmpeg missing; authorization=do_not_export https://private.example/speech", False),))
    runtime = SimpleNamespace(synthesize_tui_speech=AsyncMock(side_effect=error))
    host = object.__new__(FunctionWorkerHost)
    host.phase, host.accepting = "ACTIVE", True
    host._require_runtime = lambda: runtime
    host.emit_metadata = AsyncMock()
    left_connection, right_connection = multiprocessing.Pipe(duplex=True)
    left = JsonConnectionPeer(left_connection, label="speech-caller")
    right = JsonConnectionPeer(right_connection, label="speech-worker", request_handler=host.handle_request)
    left.start()
    right.start()
    handle = object.__new__(AgentRuntimeHandle)
    handle.name = "lily"
    handle._route = left.request
    server, original = _server(tmp_path)
    server._runtime_map = lambda: {"lily": handle}
    try:
        response = await server.handle_tui_speech(_Request({
            "agent": "lily", "text": "synthetic speech", "request_id": "conversion-wire",
        }))
        payload = json.loads(response.text)
        assert response.status == 503
        assert payload["code"] == "tts_audio_conversion_failed"
        assert "FFmpeg" in payload["error"]
        assert payload["provider_failures"][0]["code"] == "tts_audio_conversion_failed"
        assert payload["provider_failures"][0]["retryable"] is False
        assert "do_not_export" not in response.text and "private.example" not in response.text
        assert runtime.synthesize_tui_speech.await_count == 1
        assert original.api_delivery_flags == [] and original.api_media_calls == []
        host.emit_metadata.assert_not_awaited()
    finally:
        import asyncio
        await asyncio.gather(left.close(), right.close())


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"type": "hashi.voice-synthesis-failure", "version": True, "code": "tts_audio_conversion_failed",
     "reason": "FFmpeg unavailable", "failures": []},
    {"type": "hashi.voice-synthesis-failure", "version": 2, "code": "tts_audio_conversion_failed",
     "reason": "FFmpeg unavailable", "failures": []},
    {"type": "hashi.voice-synthesis-failure", "version": 1, "code": "tts_audio_conversion_failed",
     "reason": "FFmpeg unavailable", "failures": [{"provider": "windows", "code": "tts_audio_conversion_failed",
        "reason": "missing", "retryable": "false"}]},
])
async def test_malformed_speech_failure_is_not_an_asset_or_automatic_replay(payload):
    from orchestrator.function_worker_supervisor import AgentRuntimeHandle, FunctionWorkerError

    handle = object.__new__(AgentRuntimeHandle)
    handle._route = AsyncMock(return_value=payload)
    with pytest.raises(FunctionWorkerError, match="Invalid speech"):
        await handle.synthesize_tui_speech("synthetic", "original-attempt")
    assert handle._route.await_count == 1
