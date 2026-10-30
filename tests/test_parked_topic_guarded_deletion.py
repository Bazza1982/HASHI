from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.agent_overview import _parked_topics_overview
from orchestrator.config_json import ConfigConflictError, ConfigDurabilityError
from orchestrator.parked_topics import ParkedTopicStore
from orchestrator.workbench_api import WorkbenchApiServer


def create_topic(store, title):
    return store.create_topic(
        title=title, summary_short="short", summary_long="private long",
        recent_context="private context", last_user_text="private user",
        last_assistant_text="private assistant", last_exchange_text="private exchange",
        source_session="session-one",
    )


def revision(store, agent="momo"):
    return _parked_topics_overview(store, agent_name=agent)["topics"][0]["deletion_revision"]


@pytest.fixture
def api(tmp_path):
    workspace = tmp_path / "workspaces" / "momo"
    workspace.mkdir(parents=True)
    store = ParkedTopicStore(workspace)
    create_topic(store, "First topic")
    transcript = workspace / "transcript-sentinel.txt"
    transcript.write_text("conversation must survive", encoding="utf-8")
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"agents":[{"name":"momo","project_id":"p1"}]}), encoding="utf-8")
    runtime = SimpleNamespace(name="momo", workspace_dir=workspace, parked_topics=store)
    settings = SimpleNamespace(bridge_home=tmp_path, project_root=tmp_path,
                               deployment_profile="personal", authorized_id=1)
    server = WorkbenchApiServer(config_path=config, global_config=settings,
        orchestrator=SimpleNamespace(runtimes=[runtime], scheduler=SimpleNamespace(state={})),
        secrets={"workbench_admin_token":"test-owner-token"}, reconcile_session_runs=False)
    server._metadata_for_agent = lambda _row, _runtime: {
        "id":"momo", "name":"momo", "workspace_dir":str(workspace), "online":True,
        "parked_topic_guarded_writes":bool(getattr(_runtime, "guarded_supported", False)),
    }
    return server, store, transcript


def headers(token="test-owner-token"):
    return {"Authorization":f"Bearer {token}"}


@pytest.mark.asyncio
async def test_http_guard_rejects_unauthorized_and_reused_slot_then_preserves_conversation(api):
    server, store, transcript = api
    async with TestClient(TestServer(server.app)) as client:
        anonymous = await client.get("/api/agents/momo/overview")
        if anonymous.status == 200:
            topic = (await anonymous.json())["overview"]["parked_topics"]["topics"][0]
            assert "deletion_revision" not in topic
        else:
            assert anonymous.status == 401
        overview = await client.get("/api/agents/momo/overview", headers=headers())
        assert overview.status == 200
        snapshot = (await overview.json())["overview"]["parked_topics"]["topics"][0]
        old_revision = snapshot["deletion_revision"]
        assert len(old_revision) == 64
        assert old_revision != revision(store, "other-agent")
        body = {"expected_revision":old_revision, "authorization":"explicit_user_authorization"}
        url = "/api/agents/momo/parked-topics/1/delete-if-unchanged"
        assert (await client.post(url, json=body)).status == 401
        assert (await client.post(url, headers=headers(), json={"expected_revision":old_revision})).status == 403

        store.delete_topic(1)
        assert create_topic(store, "Replacement topic")["slot_id"] == 1
        stale = await client.post(url, headers=headers(), json=body)
        assert stale.status == 409
        assert (await stale.json())["error_code"] == "snapshot_changed"
        assert store.get_topic(1)["title"] == "Replacement topic"
        body["expected_revision"] = revision(store)
        deleted = await client.post(url, headers=headers(), json=body)
        assert deleted.status == 200
        assert store.list_topics() == []
        assert transcript.read_text(encoding="utf-8") == "conversation must survive"


def test_guarded_delete_rejects_a_concurrent_writer_without_losing_either_record(api, monkeypatch):
    _server, store, _transcript = api
    expected = revision(store)
    other = ParkedTopicStore(store.path.parent)
    original_save = store._save

    def concurrent_save(snapshot):
        create_topic(other, "Concurrent topic")
        return original_save(snapshot)

    monkeypatch.setattr(store, "_save", concurrent_save)
    with pytest.raises(ConfigConflictError):
        store.delete_if_unchanged(1, expected_revision=expected, agent_name="momo")
    assert [item["title"] for item in other.list_topics()] == ["First topic", "Concurrent topic"]



def test_existing_followup_load_and_delete_behaviour_is_preserved(api):
    _server, store, _transcript = api
    first = store.get_topic(1)
    created = datetime.fromisoformat(first["created_at"])
    assert [item["slot_id"] for item in store.due_topics(created + timedelta(hours=3))] == [1]
    sent = store.record_followup_sent(1, sent_at=created + timedelta(hours=3))
    assert sent["followup"]["attempts"] == 1
    assert sent["followup"]["status"] == "scheduled"
    loaded = store.mark_loaded(1)
    assert loaded["followup"]["status"] == "loaded"
    assert store.due_topics(created + timedelta(days=10)) == []
    assert store.delete_topic(1)["title"] == "First topic"
    assert store.get_topic(1) is None

def test_corrupt_parked_state_is_never_replaced_by_a_write(api):
    _server, store, _transcript = api
    store.path.write_text("{broken", encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        create_topic(store, "Must not overwrite")
    assert store.path.read_text(encoding="utf-8") == "{broken"


@pytest.mark.asyncio
async def test_committed_but_unconfirmed_delete_is_reported_unknown_without_retry(api, monkeypatch):
    server, store, _transcript = api
    expected = revision(store)
    original_save = store._save
    calls = 0

    def save_then_fail(snapshot):
        nonlocal calls
        calls += 1
        original_save(snapshot)
        raise ConfigDurabilityError("simulated committed durability failure")

    monkeypatch.setattr(store, "_save", save_then_fail)
    async with TestClient(TestServer(server.app)) as client:
        result = await client.post("/api/agents/momo/parked-topics/1/delete-if-unchanged",
            headers=headers(), json={"expected_revision":expected,
                                    "authorization":"explicit_user_authorization"})
        assert result.status == 503
        assert (await result.json())["error_code"] == "outcome_unknown"
    assert calls == 1
    assert store.list_topics() == []


@pytest.mark.asyncio
async def test_worker_facade_uses_the_same_guarded_store_and_old_data(api):
    server, store, _transcript = api
    old = store.get_topic(1)
    old.pop("topic_id")
    store.path.write_text("\ufeff" + json.dumps({"topics":[old], "preserved":"legacy"}), encoding="utf-8")
    del server.orchestrator.runtimes[0].parked_topics
    server.orchestrator.runtimes[0].guarded_supported = True
    async with TestClient(TestServer(server.app)) as client:
        result = await client.get("/api/agents/momo/overview", headers=headers())
        assert result.status == 200
        expected = (await result.json())["overview"]["parked_topics"]["topics"][0]["deletion_revision"]
        deleted = await client.post("/api/agents/momo/parked-topics/1/delete-if-unchanged",
            headers=headers(), json={"expected_revision":expected,
                                    "authorization":"explicit_user_authorization"})
        assert deleted.status == 200
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert persisted == {"topics":[], "preserved":"legacy"}



@pytest.mark.asyncio
async def test_older_worker_without_guarded_writes_cannot_offer_or_execute_delete(api):
    server, store, _transcript = api
    expected = revision(store)
    del server.orchestrator.runtimes[0].parked_topics
    async with TestClient(TestServer(server.app)) as client:
        overview = await client.get("/api/agents/momo/overview", headers=headers())
        assert overview.status == 200
        topic = (await overview.json())["overview"]["parked_topics"]["topics"][0]
        assert "deletion_revision" not in topic
        result = await client.post("/api/agents/momo/parked-topics/1/delete-if-unchanged",
            headers=headers(), json={"expected_revision":expected,
                                    "authorization":"explicit_user_authorization"})
        assert result.status == 503
    assert store.get_topic(1)["title"] == "First topic"


@pytest.mark.asyncio
async def test_invalid_parked_targets_never_mutate_storage(api):
    server, store, _transcript = api
    current = revision(store)
    body = {"expected_revision":current, "authorization":"explicit_user_authorization"}
    async with TestClient(TestServer(server.app)) as client:
        for slot in ("0", "01", "9007199254740992", "not-a-number", "9" * 5000):
            result = await client.post(
                f"/api/agents/momo/parked-topics/{slot}/delete-if-unchanged", headers=headers(), json=body)
            assert result.status == 400
        for invalid in ({**body, "expected_revision":"bad"}, {**body, "extra":True}):
            result = await client.post("/api/agents/momo/parked-topics/1/delete-if-unchanged",
                                       headers=headers(), json=invalid)
            assert result.status == 400
    assert store.get_topic(1)["title"] == "First topic"



class GovernedIdentity:
    def get_session_user(self, token):
        return SimpleNamespace(id=token) if token in {"admin", "member", "outside"} else None

    def list_project_memberships(self, *, user_id):
        if user_id == "admin":
            return [{"role":"org_admin", "project_id":"p1"}]
        if user_id == "member":
            return [{"role":"member", "project_id":"p1"}]
        return [{"role":"member", "project_id":"p2"}]


@pytest.mark.asyncio
async def test_governed_parked_deletion_requires_agent_management_rights(api):
    server, store, _transcript = api
    server.global_config.deployment_profile = "governed"
    server.identity_service = GovernedIdentity()
    body = {"expected_revision":revision(store), "authorization":"explicit_user_authorization"}
    url = "/api/agents/momo/parked-topics/1/delete-if-unchanged"
    async with TestClient(TestServer(server.app)) as client:
        assert (await client.post(url, headers=headers("member"), json=body)).status == 403
        assert (await client.post(url, headers=headers("outside"), json=body)).status == 404
        assert store.get_topic(1)["title"] == "First topic"
        assert (await client.post(url, headers=headers("admin"), json=body)).status == 200
    assert store.list_topics() == []
