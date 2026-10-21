from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

from orchestrator import (
    agent_presence_notify,
    frontend_optional_egress,
    runtime_session,
)
from orchestrator.connector_delivery_preferences import set_connector_preference
from orchestrator.frontend_connector_registry import endpoint_id_for
from orchestrator.frontend_agent_presence import deliver_agentpresence_audio_event
from orchestrator.frontend_optional_egress import _call_event
from orchestrator.session_store import SessionStore


class _Response:
    def __init__(self, status=204, payload=b"{}"):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._payload


def test_owner_enabled_notifications_use_token_authenticated_local_api(
    tmp_path, monkeypatch
):
    appdata = tmp_path / "appdata"
    token_dir = appdata / "AgentPresence"
    token_dir.mkdir(parents=True)
    (token_dir / "ap-token").write_text("local-token\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("AGENT_PRESENCE_URL", "http://127.0.0.1:7740/")

    bridge_home = tmp_path / "bridge"
    set_connector_preference(bridge_home, "owner-a", "agentpresence", "mirror", True)
    requests = []

    def _urlopen(request, *, timeout):
        requests.append((request, timeout))
        return _Response()

    monkeypatch.setattr(agent_presence_notify.urllib.request, "urlopen", _urlopen)

    assert agent_presence_notify.call_started(bridge_home, "owner-a") is True
    assert agent_presence_notify.call_ended(bridge_home, "owner-a") is True
    assert (
        agent_presence_notify.voice_bytes(
            bridge_home, "owner-a", b"ogg-bytes", "audio/ogg"
        )
        is True
    )
    assert agent_presence_notify.call_started(bridge_home, "owner-b") is False

    assert [request.full_url for request, _timeout in requests] == [
        "http://127.0.0.1:7740/v1/call/incoming",
        "http://127.0.0.1:7740/v1/call/ended",
        "http://127.0.0.1:7740/v1/voice/speak",
    ]
    assert all(
        timeout == agent_presence_notify.TIMEOUT_SECONDS
        for _request, timeout in requests
    )
    headers = {key.casefold(): value for key, value in requests[0][0].header_items()}
    assert headers["x-ap-token"] == "local-token"
    assert json.loads(requests[0][0].data) == {}
    assert requests[2][0].data == b"ogg-bytes"
    assert dict(requests[2][0].header_items())["Content-type"] == "audio/ogg"


def test_missing_companion_or_token_is_fail_open(tmp_path, monkeypatch):
    bridge_home = tmp_path / "bridge"
    set_connector_preference(bridge_home, "owner-a", "agentpresence", "mirror", True)
    monkeypatch.delenv("APPDATA", raising=False)
    assert agent_presence_notify.call_started(bridge_home, "owner-a") is False

    appdata = tmp_path / "appdata"
    token_dir = appdata / "AgentPresence"
    token_dir.mkdir(parents=True)
    (token_dir / "ap-token").write_text("local-token", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))

    def _unreachable(*_args, **_kwargs):
        raise OSError("companion unavailable")

    monkeypatch.setattr(agent_presence_notify.urllib.request, "urlopen", _unreachable)
    assert agent_presence_notify.call_ended(bridge_home, "owner-a") is False
    assert (
        agent_presence_notify.voice_bytes(bridge_home, "owner-a", b"voice", "audio/ogg")
        is False
    )
    assert (
        agent_presence_notify.voice_bytes(bridge_home, "owner-a", b"", "audio/ogg")
        is False
    )


def test_hashi_binding_roundtrip_and_ingress_token_check(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    token_dir = appdata / "AgentPresence"
    token_dir.mkdir(parents=True)
    (token_dir / "ap-token").write_text("local-token", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("AGENT_PRESENCE_URL", "http://127.0.0.1:7740/")
    requests = []

    def _urlopen(request, *, timeout):
        requests.append((request, timeout))
        payload = {
            "configured": True,
            "instance_id": "HASHI3",
            "base_url": "http://127.0.0.1:18804",
            "default_agent": "agent1",
            "target": "agent1@HASHI3",
        }
        return _Response(200, json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(agent_presence_notify.urllib.request, "urlopen", _urlopen)

    assert agent_presence_notify.token_matches("local-token") is True
    assert agent_presence_notify.token_matches("wrong-token") is False
    assert agent_presence_notify.get_hashi_binding()["target"] == "agent1@HASHI3"
    changed = agent_presence_notify.set_hashi_binding(
        instance_id="HASHI3",
        base_url="http://127.0.0.1:18804",
        default_agent="agent1",
    )
    assert changed["configured"] is True
    assert [request.method for request, _timeout in requests] == ["GET", "PUT"]
    assert json.loads(requests[1][0].data) == {
        "instance_id": "HASHI3",
        "base_url": "http://127.0.0.1:18804",
        "default_agent": "agent1",
    }
    assert all(timeout == agent_presence_notify.TIMEOUT_SECONDS for _, timeout in requests)


@pytest.mark.asyncio
async def test_standard_adapter_keeps_call_audio_with_call_frontend(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(agent_presence_notify, "_token", lambda: "local-token")
    posted = []

    def _post_json(path, payload):
        posted.append((path, payload, "application/json"))
        return True

    def _post(path, *, data=None, content_type=""):
        posted.append((path, data, content_type))
        return True

    monkeypatch.setattr(agent_presence_notify, "_post_json", _post_json)
    monkeypatch.setattr(agent_presence_notify, "_post", _post)

    binding = {"call_id": "call-1", "session_id": "ses-1"}
    started, _ = _call_event(binding, "call.started", {})
    ended, _ = _call_event(binding, "call.ended", {"reason": "hangup"})
    speech, resolved_media = _call_event(
        binding,
        "call.speech",
        {
            "segment": 1,
            "asset": {
                "content_b64": base64.b64encode(b"wav-bytes").decode("ascii"),
                "media_type": "audio/wav",
            },
        },
    )
    endpoint_id = endpoint_id_for(
        "agentpresence",
        ingress_transport="local",
        channel_key="owner-a",
    )

    def resolve_media(_event, media_ref):
        return resolved_media[media_ref["attachment_id"]]

    adapter = agent_presence_notify.AgentPresenceConnectorAdapter(
        tmp_path,
        "owner-a",
        enabled=True,
        media_resolver=resolve_media,
    )
    assert (await adapter.dispatch(started, endpoint_id=endpoint_id))[
        "status"
    ] == "accepted"
    assert (await adapter.dispatch(speech, endpoint_id=endpoint_id))["status"] == "skipped"
    assert (await adapter.dispatch(ended, endpoint_id=endpoint_id))["status"] == "accepted"
    assert posted == [
        ("/v1/call/incoming", {}, "application/json"),
        ("/v1/call/ended", {}, "application/json"),
    ]

    disabled = agent_presence_notify.AgentPresenceConnectorAdapter(
        tmp_path,
        "owner-a",
        enabled=False,
        media_resolver=resolve_media,
    )
    assert (await disabled.dispatch(started, endpoint_id=endpoint_id))[
        "status"
    ] == "skipped"


@pytest.mark.asyncio
async def test_workbench_voice_publication_has_independent_agentpresence_outbox(
    tmp_path, monkeypatch
):
    owner_id = "owner-a"
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3")
    runtime = SimpleNamespace(
        name="agent-a",
        session_store=store,
        global_config=SimpleNamespace(bridge_home=tmp_path, authorized_id=1),
    )
    set_connector_preference(tmp_path, owner_id, "agentpresence", "mirror", True)
    scheduled = []

    def schedule(_runtime, **kwargs):
        scheduled.append(kwargs)
        return ()

    monkeypatch.setattr(
        frontend_optional_egress,
        "schedule_persisted_frontend_media_event",
        schedule,
    )
    payload = b"OggSworkbench-voice"
    publication = runtime_session.publish_frontend_media_notification(
        runtime,
        filename="reply.ogg",
        media_type="audio/ogg",
        payload=payload,
        sha256=hashlib.sha256(payload).hexdigest(),
        caption="",
        publication_id="voice-1",
        surface="workbench",
        channel_key="default",
        semantic_role="voice_message",
        explicit_owner_id=owner_id,
    )

    assert publication["surface"] == "workbench"
    assert scheduled == [
        {
            "target": {
                "session_id": publication["session_id"],
                "event_id": publication["delivery_event_id"],
            },
            "owner_id": owner_id,
            "source_event_id": publication["delivery_event_id"],
        }
    ]
    posted = []
    monkeypatch.setattr(agent_presence_notify, "_token", lambda: "local-token")

    def _post(path, *, data=None, content_type=""):
        posted.append((path, data, content_type))
        return True

    monkeypatch.setattr(agent_presence_notify, "_post", _post)
    result = await deliver_agentpresence_audio_event(
        runtime,
        target={
            "session_id": publication["session_id"],
            "event_id": publication["delivery_event_id"],
        },
        owner_id=owner_id,
        source_event_id=publication["delivery_event_id"],
    )
    assert result["status"] == "completed"
    assert posted == [("/v1/voice/speak", payload, "audio/ogg")]
    endpoint_id = endpoint_id_for(
        "agentpresence",
        ingress_transport="agentpresence",
        channel_key=owner_id,
    )
    receipts = store.frontend_delivery_receipts(
        session_id=publication["session_id"],
        owner_id=owner_id,
        event_id=publication["delivery_event_id"],
    )
    assert [receipt["endpoint_id"] for receipt in receipts] == [endpoint_id]
    assert [receipt["status"] for receipt in receipts] == ["accepted"]
