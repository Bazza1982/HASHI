"""Management failure replies preserve safe causes and uncertain write facts."""

from __future__ import annotations

import json
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.agent_overview import _system_prompt_overview
from orchestrator.parked_topics import ParkedTopicStore, parked_topic_deletion_revision
from orchestrator.session_store import SessionStore
from tests.test_workbench_workzone_management import api as api, headers
from tests.test_workbench_guarded_deletion import environment as environment, Request


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["file", "missing", "home"])
async def test_workzone_path_rejection_keeps_its_actual_finite_reason(
    api, tmp_path, kind
):
    server, runtime, directories = api
    path = {
        "file": directories[0] / "keep.txt",
        "missing": tmp_path / "not-present",
        "home": runtime.workspace_dir,
    }[kind]
    async with TestClient(TestServer(server.app)) as client:
        response = await client.put(
            "/api/agents/momo/workzones/1",
            headers=headers(),
            json={"expected_revision": 0, "mode": "add", "path": str(path)},
        )
        data = await response.json()
    reason = {
        "file": "workzone_path_is_file",
        "missing": "workzone_path_not_directory",
        "home": "workzone_path_is_agent_home",
    }[kind]
    assert response.status == 400 and data["error_code"] == "workzone_path_invalid"
    assert data["failure"]["reason"] == reason and data["effect_state"] == "not_started"
    assert data["diagnostic_ref"] in data["message"] and str(tmp_path) not in str(data)


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "nt", reason="native Windows path validation")
async def test_native_windows_path_mismatch_keeps_the_platform_reason(api):
    server, _, _ = api
    async with TestClient(TestServer(server.app)) as client:
        response = await client.put(
            "/api/agents/momo/workzones/1",
            headers=headers(),
            json={"expected_revision": 0, "mode": "add", "path": "/tmp/posix-workzone"},
        )
        data = await response.json()
    assert response.status == 400
    assert data["failure"]["reason"] == "workzone_windows_path_required"
    assert data["effect_state"] == "not_started"


@pytest.mark.asyncio
async def test_workzone_duplicate_does_not_collapse_into_an_unexplained_invalid_path(
    api,
):
    server, _, directories = api
    async with TestClient(TestServer(server.app)) as client:
        first = await client.put(
            "/api/agents/momo/workzones/1",
            headers=headers(),
            json={"expected_revision": 0, "mode": "add", "path": str(directories[0])},
        )
        assert first.status == 200
        response = await client.put(
            "/api/agents/momo/workzones/2",
            headers=headers(),
            json={"expected_revision": 1, "mode": "add", "path": str(directories[0])},
        )
        data = await response.json()
    assert (
        response.status == 400
        and data["failure"]["reason"] == "workzone_path_duplicate"
    )
    assert data["accepted"] is False


@pytest.mark.asyncio
async def test_workzone_read_failure_precedes_any_save(api, monkeypatch):
    server, _, directories = api
    writes = []

    def inaccessible(**kwargs):
        raise PermissionError("private-profile token=private-secret")

    monkeypatch.setattr(server.session_store, "get_agent_workzone_set", inaccessible)
    monkeypatch.setattr(
        server.session_store,
        "set_agent_workzone_slot",
        lambda **kwargs: writes.append(kwargs),
    )
    async with TestClient(TestServer(server.app)) as client:
        response = await client.put(
            "/api/agents/momo/workzones/1",
            headers=headers(),
            json={"expected_revision": 0, "mode": "add", "path": str(directories[0])},
        )
        data = await response.json()
    assert response.status == 503 and data["effect_state"] == "not_started"
    assert data["cause_code"] == "workzone_read_access_denied"
    assert writes == [] and "private-" not in str(data)


@pytest.mark.asyncio
async def test_workzone_commit_then_missing_ack_preserves_saved_profile_and_unknown_result(
    api, monkeypatch
):
    server, _, directories = api
    original = server.session_store.set_agent_workzone_slot
    calls = []

    def committed(**kwargs):
        calls.append(kwargs)
        original(**kwargs)
        raise PermissionError("private-after-commit")

    monkeypatch.setattr(server.session_store, "set_agent_workzone_slot", committed)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.put(
            "/api/agents/momo/workzones/1",
            headers=headers(),
            json={"expected_revision": 0, "mode": "add", "path": str(directories[0])},
        )
        data = await response.json()
    assert response.status == 503 and data["error_code"] == "outcome_unknown"
    assert data["accepted"] is None and data["retry_safe"] is False
    assert data["failure"]["phase"] == "persist" and "private-" not in str(data)
    saved = server.session_store.get_agent_workzone_set(
        owner_id=SessionStore.owner_id_for(server.global_config), agent_id="momo"
    )
    assert saved["revision"] == 1 and len(saved["slots"]) == 1 and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("after_commit", [False, True])
async def test_sys_prompt_failure_retains_stage_without_touching_global_or_replaying(
    environment, monkeypatch, after_commit
):
    server, _, prompts, global_prompts = environment
    prompts.save("1", "private-prompt")
    global_prompts.save("1", "private-global")
    revision = _system_prompt_overview(prompts, agent_name="momo")["slots"][0][
        "deletion_revision"
    ]
    calls = []
    original_delete = prompts.delete
    original_read = prompts.get_slot
    if after_commit:

        def unavailable(slot, **kwargs):
            calls.append(slot)
            original_delete(slot, **kwargs)
            raise PermissionError("private-after-commit")

        monkeypatch.setattr(prompts, "delete", unavailable)
    else:

        def unavailable(slot):
            raise PermissionError("private-read")

        monkeypatch.setattr(prompts, "get_slot", unavailable)
        monkeypatch.setattr(
            prompts, "delete", lambda *args, **kwargs: calls.append(args)
        )
    response = await server.handle_agent_sys_prompt_conditional_delete(
        Request(
            slot="1",
            payload={
                "authorization": "explicit_user_authorization",
                "expected_revision": revision,
            },
        )
    )
    data = json.loads(response.text)
    assert response.status == 503
    assert data["effect_state"] == ("unconfirmed" if after_commit else "not_started")
    assert data["failure"]["phase"] == ("persist" if after_commit else "read")
    assert data["diagnostic_ref"] in data["message"] and "private-" not in response.text
    assert original_read("1")["text"] == ("" if after_commit else "private-prompt")
    assert (
        len(calls) == (1 if after_commit else 0)
        and global_prompts.get_slot("1")["text"] == "private-global"
    )


@pytest.mark.asyncio
async def test_parked_topic_missing_delete_ack_has_safe_reason_and_keeps_actual_deleted_state(
    api, monkeypatch
):
    server, runtime, _ = api
    store = ParkedTopicStore(runtime.workspace_dir)
    topic = store.create_topic(
        title="private-title",
        summary_short="",
        summary_long="",
        recent_context="",
        last_user_text="",
        last_assistant_text="",
        last_exchange_text="",
        source_session="",
    )
    runtime.parked_topics = store
    revision = parked_topic_deletion_revision(topic, agent_name="momo")
    original = store.delete_if_unchanged
    calls = []

    def committed(*args, **kwargs):
        calls.append(args)
        original(*args, **kwargs)
        raise PermissionError("private-after-delete")

    monkeypatch.setattr(store, "delete_if_unchanged", committed)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            "/api/agents/momo/parked-topics/1/delete-if-unchanged",
            headers=headers(),
            json={
                "authorization": "explicit_user_authorization",
                "expected_revision": revision,
            },
        )
        data = await response.json()
    assert response.status == 503 and data["effect_state"] == "unconfirmed"
    assert data["cause_code"] == "parked_delete_persist_access_denied"
    assert "private-" not in str(data) and store.list_topics() == [] and len(calls) == 1
