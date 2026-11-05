from types import SimpleNamespace
import pytest

from orchestrator import agent_presence_notify, agent_presence_settings, runtime_session
from orchestrator.agent_presence_captions import caption_content
from orchestrator.frontend_agent_presence import deliver_agentpresence_audio_event
from orchestrator.frontend_projection import project_frontend_event
from orchestrator.connector_delivery_preferences import set_connector_preference
from orchestrator.session_store import SessionStore


def fixture(tmp_path):
    runtime = SimpleNamespace(name="agent-a", session_store=SessionStore(tmp_path / "sessions.sqlite3"),
        global_config=SimpleNamespace(bridge_home=tmp_path, authorized_id=1, instance_id="HASHI6"))
    owner = runtime_session.owner_id(runtime)
    set_connector_preference(tmp_path, owner, "agentpresence", "mirror", True)
    agent_presence_settings.set_default_agent(tmp_path, "agent-a", expected_revision=0)
    target = runtime_session.publish_frontend_message(runtime, role="assistant", text="完整字幕 🌸" * 5000,
        source="test", publication_id="caption-1", surface="workbench", channel_key="default",
        presentation_channel="final")
    message = runtime.session_store.get_message(target["message_id"],
        session_id=target["session_id"], owner_id=owner)
    raw = next(x for x in runtime.session_store.events(target["session_id"], owner_id=owner)
               if x["event_id"] == target["delivery_event_id"])
    event = project_frontend_event(raw, message_map={message["message_id"]: message})
    return runtime, owner, target, event


def test_caption_uses_public_message_and_current_primary_owners(tmp_path):
    runtime, owner, target, event = fixture(tmp_path)
    value = caption_content(runtime, owner, event)
    assert value["text"] == "完整字幕 🌸" * 5000
    assert value["target"]["session_id"] == target["session_id"]
    assert value["target"]["route_revision"] == 1
    assert caption_content(runtime, owner, {**event, "presentation_channel": "reasoning"}) is None
    assert caption_content(runtime, owner, {**event, "visibility": "internal"}) is None
    assert caption_content(runtime, owner, {**event, "semantic_kind": "command"}) is None
    agent_presence_settings.set_default_agent(tmp_path, "other-agent", expected_revision=1)
    assert caption_content(runtime, owner, event) is None


@pytest.mark.asyncio
async def test_caption_is_claimed_once_through_existing_outbox_without_audio(tmp_path, monkeypatch):
    runtime, owner, target, event = fixture(tmp_path)
    monkeypatch.setattr(agent_presence_notify, "_token", lambda: "test-local-token")
    posts = []
    monkeypatch.setattr(agent_presence_notify, "_post_json", lambda path, body: posts.append((path, body)) or True)
    async def deliver():
        return await deliver_agentpresence_audio_event(runtime, target={**target, "event_id": target["delivery_event_id"]}, owner_id=owner,
                                                       source_event_id=target["delivery_event_id"])
    result = await deliver()
    assert result is not None
    assert len(posts) == 1
    path, body = posts[0]
    assert path == "/v1/captions/project"
    assert body["producer_id"] == "hashi:HASHI6:agent-a"
    assert body["caption"]["phase"] == "final"
    assert body["caption"]["text"] == "完整字幕 🌸" * 5000
    await deliver()
    assert len(posts) == 1


def test_empty_or_private_media_cannot_create_a_caption(tmp_path):
    runtime, owner, target, event = fixture(tmp_path)
    assert caption_content(runtime, owner, {**event, "content_blocks": []}) is None
    assert caption_content(runtime, owner, {**event, "message_id": None}) is None
# HASHI-Fingerprint: hfp_5567f23319834687955cef86b64d97ff

@pytest.mark.asyncio
async def test_terminal_run_uses_its_frozen_caption_task(tmp_path, monkeypatch):
    from orchestrator.frontend_delivery import freeze_run_delivery_route
    from orchestrator.agent_presence_captions import deliver_run_caption
    runtime, owner, _, _ = fixture(tmp_path)
    store = runtime.session_store
    session = store.resolve_primary_session(owner_id=owner, agent_id=runtime.name, establish=False)
    route = freeze_run_delivery_route(message_source_id='workbench', session_surface='workbench',
        session_channel_key='default', chat_id='default', telegram_requested=False,
        additional_mirrors=[{'surface':'agentpresence','channel_key':owner}])
    store.accept_run(session_id=session['session_id'], owner_id=owner, agent_id=runtime.name,
        request_id='caption-run', text='question', source='workbench', idempotency_key='caption-run',
        delivery_route=route)
    store.mark_request_running('caption-run', worker_id='test-worker')
    run = store.finish_request('caption-run', success=True, assistant_text='完成的真实结果')
    posts=[]
    monkeypatch.setattr(agent_presence_notify, '_token', lambda:'local-test')
    monkeypatch.setattr(agent_presence_notify, '_post_json', lambda path,body:posts.append((path,body)) or True)
    await deliver_run_caption(runtime, run, owner)
    assert len(posts)==1
    assert posts[0][1]['caption']['text']=='完成的真实结果'
    assert posts[0][1]['caption']['caption_id']=='assistant:caption-run'
    await deliver_run_caption(runtime, run, owner)
    assert len(posts)==1
