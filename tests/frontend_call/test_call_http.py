import base64
import io
import json
from types import SimpleNamespace
import pytest
from aiohttp import web, ClientSession
from PIL import Image
from orchestrator.frontend_call.adapters import MediaAdapters
from orchestrator.frontend_call.contract import CallError, decode_jpeg
from orchestrator.frontend_call.routes import register_call_api
from orchestrator.enterprise.secret_refs import ConnectorSecretResolver
from test_call_contract import wav


async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def test_pcm_direct_and_proxy_http_share_declared_codec_and_reject_mismatch():
    import wave
    from orchestrator.frontend_call.config import validate_target

    raw = b"\x01\x02\x03\x04" * 3200
    seen = []
    app = web.Application()
    async def speech(request):
        seen.append(request.path)
        body = await request.json()
        assert body["response_format"] == "pcm"
        rate = "24000" if request.path.startswith("/bad") else "16000"
        return web.Response(body=raw, headers={
            "Content-Type": f"audio/pcm; rate={rate}; channels=2; bits=16; encoding=s16le",
        })
    app.router.add_post("/{route}/v1/audio/speech", speech)
    runner, root = await serve(app)
    adapter = MediaAdapters()
    try:
        results = []
        for route in ("direct", "proxy"):
            target = validate_target({
                "id": route, "kind": "tts", "adapter": "openai_compatible",
                "location": "local", "base_url": root + f"/{route}/v1",
                "model": "fixture/model", "voices": ["warm"], "audio_format": "pcm",
                "pcm_sample_rate_hz": 16000, "pcm_sample_width_bytes": 2,
                "pcm_channels": 2, "pcm_encoding": "s16le",
            })
            result = await adapter.synthesize(target, {"voice_id": "warm", "options": {}}, "Hello")
            results.append(base64.b64decode(result["content_b64"]))
        assert results[0] == results[1]
        with wave.open(io.BytesIO(results[0]), "rb") as audio:
            assert (audio.getframerate(), audio.getsampwidth(), audio.getnchannels()) == (16000, 2, 2)
            assert audio.readframes(audio.getnframes()) == raw
        target["base_url"] = root + "/bad/v1"
        with pytest.raises(CallError, match="call_speech_format_invalid"):
            await adapter.synthesize(target, {"voice_id": "warm", "options": {}}, "Hello")
        assert seen == ["/direct/v1/audio/speech", "/proxy/v1/audio/speech", "/bad/v1/audio/speech"]
    finally:
        await runner.cleanup()


async def test_adapter_exercises_real_multipart_json_audio_and_no_redirect(monkeypatch):
    app = web.Application()
    seen = []

    async def stt(request):
        data = await request.post()
        assert data["model"] == "fixture"
        assert data["file"].file.read()[:4] == b"RIFF"
        seen.append("stt")
        assert request.headers["Authorization"] == "Bearer secret-fixture"
        return web.json_response({"text": "你好，HASHI implementation。"})

    async def tts(request):
        body = await request.json()
        assert body["input"] == "Hello" and body["voice"] == "warm"
        seen.append("tts")
        return web.Response(body=base64.b64decode(wav()), content_type="audio/wav")

    async def vision(request):
        body = await request.json()
        assert body["messages"][1]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"
        )
        return web.json_response(
            {"choices": [{"message": {"content": "A book is visible."}}]}
        )

    app.router.add_post("/v1/audio/transcriptions", stt)
    app.router.add_post("/v1/audio/speech", tts)
    app.router.add_post("/v1/chat/completions", vision)
    runner, url = await serve(app)
    try:
        monkeypatch.setenv("CALL_TEST_KEY", "secret-fixture")
        target = {
            "base_url": url + "/v1",
            "model": "fixture",
            "credential_env": "CALL_TEST_KEY",
            "audio_format": "wav",
        }
        adapters = MediaAdapters()
        assert (
            await adapters.transcribe(target, {"options": {}}, base64.b64decode(wav()))
        )["text"].startswith("你好")
        assert (
            await adapters.synthesize(
                target, {"voice_id": "warm", "options": {}}, "Hello"
            )
        )["media_type"] == "audio/wav"
        assert (
            await adapters.observe(
                target, {"options": {}}, b"fixture-image", "what is it?"
            )
            == "A book is visible."
        )
        assert seen == ["stt", "tts"]
    finally:
        await runner.cleanup()


async def test_provider_errors_do_not_disclose_body_or_key(monkeypatch):
    app = web.Application()

    async def reject(request):
        return web.Response(status=401, text="private account TOKEN=secret")

    app.router.add_post("/audio/transcriptions", reject)
    runner, url = await serve(app)
    try:
        with pytest.raises(CallError) as caught:
            await MediaAdapters().transcribe(
                {"base_url": url, "model": "m"},
                {"options": {}},
                base64.b64decode(wav()),
            )
        assert str(caught.value) == "call_provider_rejected"
    finally:
        await runner.cleanup()


async def test_openrouter_media_returns_without_secondary_provider_queries(monkeypatch):
    app = web.Application()
    lookups = []

    async def stt(request):
        assert request.headers["Authorization"] == "Bearer test-secret"
        form = await request.post()
        assert form["model"] == "openai/whisper-large-v3"
        assert form["file"].file.read()[:4] == b"RIFF"
        return web.json_response(
            {"text": "你好，测试成功。"},
            headers={"X-Generation-Id": "gen-stt-1"},
        )

    async def tts(request):
        body = await request.json()
        assert body["model"] == "google/gemini-3.8-flash-lite-tts"
        assert body["input"] == "你好，测试成功。"
        assert body["voice"] == "Sulafat"
        assert body["response_format"] == "pcm"
        assert body["provider"] == {
            "options": {
                "google-ai-studio": {"speech_metadata": {"style": "warm and clear"}}
            }
        }
        assert "style" not in body
        return web.Response(
            body=b"\0\x01" * 4800,
            content_type="audio/pcm",
            headers={"X-Generation-Id": "gen-tts-1"},
        )

    async def generation(request):
        generation_id = request.query["id"]
        lookups.append(generation_id)
        provider = {"gen-stt-1": "Groq", "gen-tts-1": "Google AI Studio"}[generation_id]
        return web.json_response(
            {"data": {"id": generation_id, "provider_name": provider}}
        )

    app.router.add_post("/v1/audio/transcriptions", stt)
    app.router.add_post("/v1/audio/speech", tts)
    app.router.add_get("/v1/generation", generation)
    runner, url = await serve(app)
    try:
        monkeypatch.setattr(
            "orchestrator.frontend_call.adapters.OPENROUTER_API_BASE",
            url + "/v1",
        )
        adapters = MediaAdapters(
            secret_resolver=ConnectorSecretResolver(
                secrets={"openrouter-api_key": "test-secret"}
            )
        )
        base = {
            "base_url": url + "/v1",
            "credential_ref": "secrets://openrouter-api_key",
        }
        transcript = await adapters.transcribe(
            {**base, "model": "openai/whisper-large-v3"},
            {"options": {}},
            base64.b64decode(wav()),
        )
        assert transcript["text"] == "你好，测试成功。"
        assert transcript["provider_receipt"] is None
        speech = await adapters.synthesize(
            {
                **base,
                "kind": "tts",
                "model": "google/gemini-3.8-flash-lite-tts",
                "audio_format": "pcm",
                "pcm_sample_rate_hz": 24000,
                "pcm_sample_width_bytes": 2,
                "pcm_channels": 1,
                "pcm_encoding": "s16le",
            },
            {"voice_id": "Sulafat", "options": {"style": "warm and clear"}},
            transcript["text"],
        )
        assert speech["provider_receipt"] is None
        assert speech["media_type"] == "audio/wav"
        assert base64.b64decode(speech["content_b64"]).startswith(b"RIFF")
        assert lookups == []
    finally:
        await runner.cleanup()


def test_jpeg_dimensions_checked_without_ocr(monkeypatch):
    out = io.BytesIO()
    Image.new("RGB", (640, 480)).save(out, format="JPEG")
    assert decode_jpeg(base64.b64encode(out.getvalue()).decode()) == out.getvalue()
    out = io.BytesIO()
    Image.new("RGB", (3000, 1000)).save(out, format="JPEG")
    assert decode_jpeg(base64.b64encode(out.getvalue()).decode()) == out.getvalue()
    monkeypatch.setenv("HASHI_CALL_MAX_IMAGE_DIMENSION", "2048")
    with pytest.raises(CallError, match="dimensions"):
        decode_jpeg(base64.b64encode(out.getvalue()).decode())
    with pytest.raises(CallError):
        decode_jpeg(base64.b64encode(b"\xff\xd8fake\xff\xd9").decode())


async def test_route_auth_json_and_disable_fail_closed(tmp_path):
    api = SimpleNamespace(
        app=web.Application(),
        config_path=tmp_path / "agents.json",
        admin_token="test-token",
        live_voice_manager=SimpleNamespace(),
        _is_governed_profile=lambda: False,
        _check_admin_auth=lambda request: (
            request.headers.get("X-Workbench-Token") == "test-token"
        ),
        _v1_owner_id=lambda request: "owner",
    )
    register_call_api(api)
    runner, url = await serve(api.app)
    try:
        async with ClientSession() as session:
            async with session.post(
                url + "/api/v1/call/operation", json={"operation": "context"}
            ) as response:
                assert response.status == 403
            (tmp_path / "call_profiles.json").write_text(
                json.dumps({"version": 1, "enabled": False})
            )
            async with session.post(
                url + "/api/v1/call/operation",
                json={},
                headers={"X-Workbench-Token": "test-token"},
            ) as response:
                assert (await response.json())["error_code"] == "call_disabled"
    finally:
        await runner.cleanup()


async def test_http_queue_reports_selected_capacity_and_preserves_retry_identity(tmp_path, monkeypatch):
    import asyncio
    from test_call_service import setup

    monkeypatch.setenv("HASHI_CALL_CONCURRENT_TURNS", "1")
    monkeypatch.setenv("HASHI_CALL_MAX_PENDING_TURNS", "2")
    monkeypatch.setenv("HASHI_CALL_LEASE_SECONDS", "120")
    monkeypatch.setenv("HASHI_CALL_MAX_DURATION_SECONDS", "0")
    api = SimpleNamespace(
        app=web.Application(), config_path=tmp_path / "agents.json",
        admin_token="test-token", live_voice_manager=SimpleNamespace(),
        _is_governed_profile=lambda: False,
        _check_admin_auth=lambda request: request.headers.get("X-Workbench-Token") == "test-token",
        _v1_owner_id=lambda request: "owner",
    )
    registered = register_call_api(api)
    fixture, ports, adapters, base, _ = setup(tmp_path)
    registered.config, registered.ports, registered.adapters = fixture.config, ports, adapters
    adapters.wait = asyncio.Event()
    runner, url = await serve(api.app)
    headers = {"X-Workbench-Token": "test-token"}

    async def send(session, payload):
        async with session.post(url + "/api/v1/call/operation", json=payload, headers=headers) as response:
            return response.status, await response.json()

    try:
        async with ClientSession() as session:
            status, context = await send(session, {**base, "operation": "context"})
            assert status == 200 and context["turn_capacity"]["max_pending_turns"] == 2
            assert context["lease_policy"] == {"lease_seconds": 120, "max_duration_seconds": None}
            start = {**base, "operation": "start", "call_id": "http-queue",
                     "generation": context["generation"], "revision": context["revision"]}
            status, started = await send(session, start)
            assert status == 200 and started["lease_policy"] == context["lease_policy"]
            binding = {**base, "call_id": "http-queue", "generation": context["generation"]}
            bodies = [{**binding, "operation": "turn", "turn_id": f"http-{i}",
                       "sequence": i, "audio_b64": wav()} for i in range(1, 4)]
            assert (await send(session, bodies[0]))[0] == 200
            status, queued = await send(session, bodies[1])
            assert status == 200 and queued["turn"]["phase"] == "queued"
            assert queued["turn_queue"]["pending"] == 2
            assert (await send(session, bodies[1]))[0] == 200
            status, full = await send(session, bodies[2])
            assert status == 429 and full == {
                "ok": False, "error_code": "call_turn_capacity",
                "turn_queue": {"pending": 2, "limit": 2, "retryable": True},
            }
            tasks = list(registered.calls["http-queue"].turn_tasks.values())
            adapters.wait.set()
            await asyncio.wait_for(asyncio.gather(*tasks), 3)
            assert [row[0] for row in ports.accepted] == ["http-1", "http-2"]
            assert (await send(session, bodies[2]))[0] == 200
            await registered.calls["http-queue"].task
            assert [row[0] for row in ports.accepted] == ["http-1", "http-2", "http-3"]
    finally:
        adapters.wait.set()
        await registered.close()
        await runner.cleanup()
