"""The slash-command distinction is enforced at the call output boundary."""
import pytest
from test_call_video_service import arranged
from orchestrator.frontend_call.contract import CallError
from orchestrator.frontend_optional_egress import _call_event
from orchestrator.agent_presence_notify import AgentPresenceConnectorAdapter


async def test_connected_call_cannot_silently_use_audio_when_video_is_disabled(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    service.config.video_tts_selection = lambda revision: None
    with pytest.raises(CallError, match="call_video_not_configured"):
        await service.invoke("owner", body)
    assert output.starts == 0 and adapters.tts == 0
    assert not service.busy("owner")
    await service.close()


async def test_connected_readiness_explains_video_requirement(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path)
    service.config.video_tts_selection = lambda revision: None
    result = await service.invoke("owner", {**base, "operation": "context"})
    assert result["call_ready"] is False
    assert result["output_mode"] == "external_av"
    assert result["output_unavailable_reason"] == "call_video_not_configured"
    assert result["fallback_route"] == "phone"
    await service.close()


async def test_no_companion_retains_audio_with_disabled_video(tmp_path):
    service, ports, adapters, base, gen, output, body = await arranged(tmp_path, connected=False)
    service.config.video_tts_selection = lambda revision: None
    result = await service.invoke("owner", body)
    assert result.get("output", {}).get("mode", "window_audio") == "window_audio"
    assert output.starts == 0 and gen.references == []
    await service.close()


async def test_only_explicit_phone_lifecycle_drives_scoped_departure(tmp_path, monkeypatch):
    from orchestrator import agent_presence_notify as ap
    monkeypatch.setattr(ap, "_token", lambda: "test-only-token")
    posted = []
    monkeypatch.setattr(ap, "_post_json", lambda path, payload: posted.append((path, payload)) or True)
    adapter = AgentPresenceConnectorAdapter(tmp_path, "owner", enabled=True)
    binding = {"call_id": "call-1", "session_id": "ses-1", "instance_id": "HASHI3"}
    for payload in ({}, {"mode": "call"}):
        event, _ = _call_event(binding, "call.started", payload)
        assert (await adapter.dispatch(event, endpoint_id="test-endpoint"))["status"] == "skipped"
    for kind in ("call.started", "call.ended"):
        event, _ = _call_event(binding, kind, {"mode": "phone", "instance_id": "HASHI3"})
        assert (await adapter.dispatch(event, endpoint_id="test-endpoint"))["status"] == "accepted"
    assert posted == [
        ("/v1/phone/start", {"call_id": "call-1", "instance_id": "HASHI3"}),
        ("/v1/phone/end", {"call_id": "call-1", "instance_id": "HASHI3"}),
    ]
