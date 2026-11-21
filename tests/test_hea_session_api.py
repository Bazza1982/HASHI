"""Session API admission-shape, idempotency and HEA reconciliation tests.

These tests pin the canonical HASHI runs-create response so the Workbench
admission parser has a truthful contract, and verify the owner-scoped
by-idempotency read endpoint plus trigger-context passthrough.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.workbench_api import WorkbenchApiServer


class _Request:
    def __init__(self, payload=None, *, query=None, match_info=None, headers=None):
        self._payload = payload or {}
        if isinstance(self._payload, dict) and "message" in self._payload and "originated_at" not in self._payload:
            self._payload.setdefault(
                "originated_at",
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
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
        self.media_dir = None
        self.last_request_metadata = None
        self.last_request_content = None
        self.last_source = None
        self.enqueue_request_calls = 0
        self.voice_manager = SimpleNamespace(
            native_audio_enabled=lambda: True,
            native_policy={"mode": "auto"},
        )

    def _primary_chat_id(self):
        return 123

    def get_display_name(self):
        return "lily"

    def command(self, *_args, **_kwargs):
        raise AssertionError("unexpected worker call")

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
        self.enqueue_request_calls += 1
        self.last_source = source
        self.last_request_metadata = dict(request_metadata)
        self.last_request_content = _kwargs.get("request_content")
        accepted = self.server.session_store.accept_run(
            session_id=request_metadata["session_id"],
            owner_id=request_metadata["owner_id"],
            agent_id=self.name,
            request_id="req-api",
            text=str(request_metadata.get("session_message_text", prompt)),
            source=source,
            idempotency_key=idempotency_key,
            display_text=request_metadata.get("session_message_display_text"),
            content=request_metadata.get("session_message_content"),
            response_preferences=request_metadata.get("response_preferences"),
            parent_run_id=request_metadata.get("parent_run_id"),
            message_context=request_metadata.get("session_message_context"),
        )
        return accepted.request_id


def _server(tmp_path: Path) -> tuple[WorkbenchApiServer, _Runtime]:
    config_path = tmp_path / "agents.json"
    config_path.write_text(
        json.dumps({"global": {}, "agents": [{"name": "lily"}]}),
        encoding="utf-8",
    )
    runtime = _Runtime()
    runtime.media_dir = tmp_path / "media"
    runtime.media_dir.mkdir(exist_ok=True)
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
        reconcile_session_runs=True,
    )
    runtime.server = server
    return server, runtime


def _payload(text, *, key, session_id, originated_at=None):
    payload = {
        "idempotency_key": key,
        "client_id": "hashi-event-automation",
        "surface": "workbench",
        "source_message_id": key,
        "message": {"content": [{"type": "text", "text": text}]},
    }
    if originated_at is not None:
        payload["originated_at"] = originated_at
    return payload


@pytest.mark.asyncio
async def test_fresh_admission_returns_202_canonical_shape(tmp_path):
    server, runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    response = await server.handle_v1_session_runs_create(_Request(
        _payload("hello", key="admit-1", session_id=session["session_id"]),
        match_info={"session_id": session["session_id"]},
    ))
    assert response.status == 202
    body = json.loads(response.text)
    assert body["ok"] is True
    assert body["session_id"] == session["session_id"]
    assert body["run_id"]
    assert body["request_id"] == "req-api"
    assert body["state"] == "queued"
    # The canonical success shape has no "accepted" boolean. A correct
    # admission parser must not require one.
    assert "accepted" not in body
    assert runtime.enqueue_request_calls == 1


@pytest.mark.asyncio
async def test_duplicate_submission_replays_the_same_run(tmp_path):
    server, _runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    payload = _payload("hello", key="admit-replay", session_id=session["session_id"])
    first = await server.handle_v1_session_runs_create(_Request(
        dict(payload), match_info={"session_id": session["session_id"]},
    ))
    second = await server.handle_v1_session_runs_create(_Request(
        dict(payload), match_info={"session_id": session["session_id"]},
    ))
    first_body, second_body = json.loads(first.text), json.loads(second.text)
    assert first.status == 202 and second.status == 202
    assert first_body["run_id"] == second_body["run_id"]
    assert first_body["state"] == "queued"


@pytest.mark.asyncio
async def test_same_key_different_payload_is_rejected_409(tmp_path):
    server, _runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    first = await server.handle_v1_session_runs_create(_Request(
        _payload("hello", key="admit-conflict", session_id=session["session_id"]),
        match_info={"session_id": session["session_id"]},
    ))
    assert first.status == 202
    conflict = await server.handle_v1_session_runs_create(_Request(
        _payload("different text", key="admit-conflict", session_id=session["session_id"]),
        match_info={"session_id": session["session_id"]},
    ))
    assert conflict.status == 409
    body = json.loads(conflict.text)
    assert body["ok"] is False
    assert body["code"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_missing_originated_at_is_explicitly_rejected(tmp_path):
    server, _runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    payload = {
        "idempotency_key": "admit-no-timestamp",
        "surface": "workbench",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    }
    request = _Request(payload, match_info={"session_id": session["session_id"]})
    request._payload.pop("originated_at", None)
    request.remote = "192.168.1.50"
    response = await server.handle_v1_session_runs_create(request)
    assert response.status == 409
    body = json.loads(response.text)
    assert body["ok"] is False
    assert body["accepted"] is False
    assert body["code"] == "frontend_message_protocol_incompatible"


@pytest.mark.asyncio
async def test_stale_timestamp_requires_confirmation_without_accepting(tmp_path):
    server, _runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    response = await server.handle_v1_session_runs_create(_Request(
        _payload("hello", key="admit-stale", session_id=session["session_id"], originated_at=stale),
        match_info={"session_id": session["session_id"]},
    ))
    assert response.status == 202
    body = json.loads(response.text)
    assert body["ok"] is True
    assert body["accepted"] is False
    assert body["confirmation_required"] is True
    assert body["confirmation"]["id"]


@pytest.mark.asyncio
async def test_invalid_body_and_missing_session_errors(tmp_path):
    server, _runtime = _server(tmp_path)
    bad = await server.handle_v1_session_runs_create(_Request(
        {"idempotency_key": "bad-body", "message": {"content": "not-a-list"}},
        match_info={"session_id": "missing"},
    ))
    # Session lookup happens first; missing session is 404.
    assert bad.status == 404
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    invalid = await server.handle_v1_session_runs_create(_Request(
        {"idempotency_key": "bad-body", "message": {"content": "not-a-list"}},
        match_info={"session_id": session["session_id"]},
    ))
    assert invalid.status == 400
    assert json.loads(invalid.text)["ok"] is False


@pytest.mark.asyncio
async def test_by_idempotency_endpoint_is_owner_scoped(tmp_path):
    server, _runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    response = await server.handle_v1_session_runs_create(_Request(
        _payload("hello", key="reconcile-1", session_id=session["session_id"]),
        match_info={"session_id": session["session_id"]},
    ))
    admitted = json.loads(response.text)

    found = await server.handle_v1_session_run_by_idempotency(_Request(
        query={"idempotency_key": "reconcile-1"},
        match_info={"session_id": session["session_id"]},
    ))
    assert found.status == 200
    body = json.loads(found.text)
    assert body["ok"] is True
    assert body["run"]["run_id"] == admitted["run_id"]
    assert body["run"]["session_id"] == session["session_id"]

    missing = await server.handle_v1_session_run_by_idempotency(_Request(
        query={"idempotency_key": "reconcile-unknown"},
        match_info={"session_id": session["session_id"]},
    ))
    assert missing.status == 200
    assert json.loads(missing.text)["run"] is None

    without_key = await server.handle_v1_session_run_by_idempotency(_Request(
        query={}, match_info={"session_id": session["session_id"]},
    ))
    assert without_key.status == 400

    other = server.session_store.create_session(owner_id="user:8", agent_id="lily")
    denied = await server.handle_v1_session_run_by_idempotency(_Request(
        query={"idempotency_key": "reconcile-1"},
        match_info={"session_id": other["session_id"]},
    ))
    assert denied.status == 404


@pytest.mark.asyncio
async def test_trigger_context_is_persisted_via_message_context_and_parent_run(tmp_path):
    server, runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    payload = _payload("fixed prompt", key="hea:job-context", session_id=session["session_id"])
    payload["parent_run_id"] = "run-source-1"
    payload["message_context"] = {
        "hea_trigger": {
            "event_id": "evt-source-1",
            "event_type": "agent.run.completed",
            "source_agent_id": "agent-a",
            "source_session_id": "session-source-1",
            "source_run_id": "run-source-1",
            "source_instance_id": "HASHI1",
            "occurred_at": "2026-11-20T00:00:00Z",
            "rule_id": "hea-rule-1",
            "job_id": "job-1",
        }
    }
    response = await server.handle_v1_session_runs_create(_Request(
        payload, match_info={"session_id": session["session_id"]},
    ))
    assert response.status == 202
    admitted = json.loads(response.text)
    stored = server.session_store.get_run(admitted["run_id"], owner_id=owner)
    assert stored["parent_run_id"] == "run-source-1"
    message_context = stored["message_context"]
    assert message_context["hea_trigger"]["source_run_id"] == "run-source-1"
    assert message_context["hea_trigger"]["rule_id"] == "hea-rule-1"
    assert runtime.last_request_metadata["session_message_context"]["hea_trigger"]["event_id"] == "evt-source-1"


@pytest.mark.asyncio
async def test_agents_catalogue_lists_runtime_agents(tmp_path):
    server, _runtime = _server(tmp_path)
    response = await server.handle_v1_agents(_Request(query={}))
    assert response.status == 200
    body = json.loads(response.text)
    assert body["ok"] is True
    assert [agent["agent_id"] for agent in body["agents"]] == ["lily"]


def test_digest_import_smoke():
    # The file should stay importable as part of the session store surface.
    from orchestrator.session_store import IdempotencyConflict, SessionNotFound  # noqa: F401
    assert IdempotencyConflict.code == "idempotency_conflict"
    assert SessionNotFound.code == "session_not_found"
# HASHI-Fingerprint: hfp_ccf4e40d0d384cff8e2ff9e76a4f4804
