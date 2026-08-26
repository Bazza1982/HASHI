from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _Request:
    def __init__(
        self,
        payload=None,
        *,
        query=None,
        match_info=None,
        headers=None,
    ):
        self._payload = payload or {}
        self.query = query or {}
        self.match_info = match_info or {}
        self.headers = headers or {}

    async def json(self):
        return self._payload


class _Runtime:
    name = "lily"
    backend_ready = True

    def __init__(self):
        self.server = None
        self.last_request_metadata = None

    def get_display_name(self):
        return "Lily"

    def _primary_chat_id(self):
        return 123

    async def enqueue_request(
        self,
        _chat_id,
        prompt,
        source,
        _summary,
        *,
        idempotency_key,
        request_metadata,
        **_kwargs,
    ):
        self.last_request_metadata = dict(request_metadata)
        request_id = "req-api"
        accepted = self.server.session_store.accept_run(
            session_id=request_metadata["session_id"],
            owner_id=request_metadata["owner_id"],
            agent_id=self.name,
            request_id=request_id,
            text=prompt,
            source=source,
            idempotency_key=idempotency_key,
        )
        return accepted.request_id


def _server(tmp_path: Path) -> tuple[WorkbenchApiServer, _Runtime]:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps({"global": {}, "agents": [{"name": "lily"}]}),
        encoding="utf-8",
    )
    runtime = _Runtime()
    server = WorkbenchApiServer(
        config_path=config_path,
        global_config=SimpleNamespace(
            bridge_home=tmp_path,
            project_root=tmp_path,
            instance_id="HASHI1",
            authorized_id=7,
            workbench_port=18800,
            api_gateway_port=18801,
            deployment_profile="personal",
        ),
        runtimes=[runtime],
    )
    runtime.server = server
    return server, runtime


@pytest.mark.asyncio
async def test_unqualified_session_api_is_not_advertised(tmp_path):
    server, _runtime = _server(tmp_path)

    capabilities = json.loads((await server.handle_v1_capabilities(_Request())).text)
    unavailable = await server.handle_v1_session_not_ready(_Request())

    assert "session_api_version" not in capabilities
    assert unavailable.status == 503
    assert json.loads(unavailable.text)["code"] == "session_api_not_ready"


def test_workbench_startup_reconciles_lost_session_runs(tmp_path):
    server, _runtime = _server(tmp_path)
    session = server.session_store.ensure_default_session(
        owner_id="user:7", agent_id="lily"
    )
    accepted = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="lily",
        request_id="req-before-workbench-restart",
        text="work in progress",
        source="test",
        idempotency_key="workbench-restart",
    )
    server.session_store.mark_request_running(
        accepted.request_id, worker_id="old-workbench"
    )

    restarted, _runtime = _server(tmp_path)

    run = restarted.session_store.get_run(accepted.run_id, owner_id="user:7")
    assert run["state"] == "interrupted"
    assert run["error_code"] == "runtime_restart_interrupted"
    assert [row["run_id"] for row in restarted.reconciled_session_runs] == [
        accepted.run_id
    ]


@pytest.mark.asyncio
async def test_session_api_run_event_ack_and_fresh_contract(tmp_path):
    server, _runtime = _server(tmp_path)
    created_response = await server.handle_v1_sessions_create(
        _Request({"agent_id": "lily", "title": "API Session"})
    )
    created = json.loads(created_response.text)
    assert created_response.status == 201
    session_id = created["session"]["session_id"]

    run_response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "api-key",
                "surface": "desktop-client",
                "message": {"content": [{"type": "text", "text": "hello Session"}]},
            },
            match_info={"session_id": session_id},
            headers={"X-Client-Id": "aptenra-test"},
        )
    )
    run_payload = json.loads(run_response.text)
    assert run_response.status == 202
    assert run_payload["session_id"] == session_id
    assert run_payload["message_id"].startswith("msg_")
    assert _runtime.last_request_metadata["session_surface"] == "desktop-client"

    server.session_store.mark_request_running(
        run_payload["request_id"], worker_id="test"
    )
    server.session_store.finish_request(
        run_payload["request_id"],
        success=True,
        assistant_text="hello back",
        assistant_source="test",
    )

    consumer_response = await server.handle_v1_event_consumer_create(
        _Request({}, match_info={"session_id": session_id})
    )
    consumer = json.loads(consumer_response.text)["consumer"]
    poll_response = await server.handle_v1_session_events(
        _Request(
            query={"consumer_id": consumer["consumer_id"]},
            match_info={"session_id": session_id},
        )
    )
    poll = json.loads(poll_response.text)
    assert [event["kind"] for event in poll["events"]] == [
        "session.created",
        "run.accepted",
        "run.started",
        "run.completed",
    ]

    ack_response = await server.handle_v1_event_consumer_ack(
        _Request(
            {"sequence": poll["issued_through_sequence"]},
            match_info={
                "session_id": session_id,
                "consumer_id": consumer["consumer_id"],
            },
        )
    )
    assert (
        json.loads(ack_response.text)["consumer"]["acknowledged_sequence"]
        == poll["issued_through_sequence"]
    )
    replay = json.loads(
        (
            await server.handle_v1_session_events(
                _Request(
                    query={"consumer_id": consumer["consumer_id"]},
                    match_info={"session_id": session_id},
                )
            )
        ).text
    )
    assert replay["events"] == []

    fresh_response = await server.handle_v1_session_fresh(
        _Request({}, match_info={"session_id": session_id})
    )
    assert json.loads(fresh_response.text)["session"]["context_generation"] == 2
    assert [row["text"] for row in server.session_store.messages(session_id)] == [
        "hello Session",
        "hello back",
    ]


@pytest.mark.asyncio
async def test_session_api_cancel_and_attachment_controls(tmp_path):
    server, _runtime = _server(tmp_path)
    created = json.loads(
        (
            await server.handle_v1_sessions_create(
                _Request({"agent_id": "lily", "title": "Controls"})
            )
        ).text
    )
    session_id = created["session"]["session_id"]
    run = json.loads(
        (
            await server.handle_v1_session_runs_create(
                _Request(
                    {
                        "idempotency_key": "controls-key",
                        "message": {"content": [{"type": "text", "text": "wait"}]},
                    },
                    match_info={"session_id": session_id},
                )
            )
        ).text
    )
    server.session_store.mark_request_running(run["request_id"], worker_id="worker")
    cancelled = json.loads(
        (
            await server.handle_v1_session_run_cancel(
                _Request(
                    {"reason": "user stop"},
                    match_info={"session_id": session_id, "run_id": run["run_id"]},
                )
            )
        ).text
    )
    assert cancelled["run"]["state"] == "stopped"

    staged = json.loads(
        (
            await server.handle_v1_attachment_stage(
                _Request(
                    {
                        "filename": "proof.txt",
                        "media_type": "text/plain",
                        "size_bytes": 5,
                        "sha256": "e" * 64,
                    },
                    match_info={"session_id": session_id},
                )
            )
        ).text
    )["attachment"]
    committed = json.loads(
        (
            await server.handle_v1_attachment_commit(
                _Request(
                    match_info={
                        "session_id": session_id,
                        "attachment_id": staged["attachment_id"],
                    }
                )
            )
        ).text
    )
    assert committed["attachment"]["state"] == "committed"
