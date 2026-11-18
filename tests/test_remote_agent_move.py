from __future__ import annotations

import base64
import hashlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from orchestrator.agent_move.package import create_agent_move_package
from orchestrator.agent_move.remote_client import (
    AgentMoveRemoteClient,
    AgentMoveRemoteError,
    _candidate_base_urls,
    _request_json,
)
from orchestrator.agent_move.transport_crypto import (
    ENVELOPE_SCHEME,
    encrypt_package_transport,
)
from orchestrator.pcm import render_pcm_document
from orchestrator.session_store import SessionStore
from remote.api import server as remote_server
from remote.api.server import create_app
from remote.security.pairing import PairingManager
from remote.security.shared_token import (
    HEADER_NONCE,
    build_auth_headers,
    verify_response_auth,
)
from remote.terminal.executor import TerminalExecutor

TOKEN = "shared-agent-move-secret"


@pytest.mark.parametrize("schema_min,schema_max,compatible", [
    (4, 5, True), (5, 9, True), (1, 1, True),
    (6, 7, False), (5, 4, False), (0, 5, False),
])
def test_receiver_negotiates_overlapping_schemas_not_mandatory_schema_one(
    monkeypatch, schema_min, schema_max, compatible,
):
    from orchestrator.agent_move import remote_client as owner

    def request(url, **kwargs):
        if url.endswith("/health"):
            return {"instance": {"instance_id": "HASHI2"}}
        return {
            "capability": "agent_move_receive_v1", "instance_id": "HASHI2",
            "authenticated_instance": "HASHI1", "authenticated_response_proof": True,
            "package_encryption": [ENVELOPE_SCHEME], "schema_min": schema_min,
            "schema_max": schema_max, "max_package_bytes": 1_000_000_000,
        }

    monkeypatch.setattr(owner, "_request_json", request)
    args = ({"HASHI2": {"host": "127.0.0.1", "remote_port": 18800}}, "HASHI2")
    if compatible:
        client = owner.connect_agent_move_receiver(*args, source_instance="HASHI1", shared_token=TOKEN)
        assert client.capabilities["schema_min"] == schema_min
    else:
        with pytest.raises(AgentMoveRemoteError):
            owner.connect_agent_move_receiver(*args, source_instance="HASHI1", shared_token=TOKEN)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _root(tmp_path: Path, name: str, instance: str, *, with_agent: bool) -> Path:
    root = tmp_path / name
    root.mkdir()
    agents = []
    if with_agent:
        workspace = root / "workspaces" / "zelda"
        workspace.mkdir(parents=True)
        (workspace / "agent.md").write_text(
            render_pcm_document(
                persona="Zelda", system="Follow policy", memory="Memory"
            ),
            encoding="utf-8",
        )
        (workspace / "memory.md").write_text("remember", encoding="utf-8")
        agents.append(
            {
                "name": "zelda",
                "type": "flex",
                "workspace_dir": "workspaces/zelda",
                "active_backend": "codex-cli",
                "allowed_backends": [{"engine": "codex-cli", "model": "gpt-5.5"}],
                "is_active": True,
            }
        )
    _write_json(
        root / "agents.json", {"global": {"instance_id": instance}, "agents": agents}
    )
    _write_json(
        root / "tasks.json", {"version": 1, "heartbeats": [], "crons": [], "nudges": []}
    )
    secrets = {"hashi_remote_shared_token": TOKEN}
    if with_agent:
        secrets["zelda"] = "telegram-token"
    _write_json(root / "secrets.json", secrets)
    return root


def _client(target: Path) -> TestClient:
    app = create_app(
        {"instance_id": "HASHI2", "display_name": "Target", "remote_port": 8767},
        PairingManager(storage_dir=target / "pairing", lan_mode=True),
        TerminalExecutor(),
        hashi_root=str(target),
    )
    return TestClient(app)


def _headers(method: str, path: str, body: bytes = b"") -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    headers.update(
        build_auth_headers(
            shared_token=TOKEN,
            method=method,
            path=path,
            from_instance="HASHI1",
            body_bytes=body,
        )
    )
    return headers


def _post(client: TestClient, path: str, payload: dict):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return client.post(path, content=body, headers=_headers("POST", path, body))


def test_remote_agent_move_requires_hmac_even_in_legacy_lan_mode(tmp_path):
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    client = _client(target)

    response = client.get("/agent-move/v1/capabilities")

    assert response.status_code == 401
    assert response.json()["ok"] is False


def test_remote_receiver_uses_live_identity_with_legacy_list_config(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    _write_json(target / "agents.json", [])
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
    )
    client = _client(target)

    capabilities = client.get(
        "/agent-move/v1/capabilities",
        headers=_headers("GET", "/agent-move/v1/capabilities"),
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["instance_id"] == "HASHI2"

    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw,
        shared_token=TOKEN,
        source_instance="HASHI1",
        target_instance="HASHI2",
        package_sha256=digest,
    )
    staged = _post(
        client,
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": ENVELOPE_SCHEME,
            "package_b64": base64.b64encode(envelope).decode("ascii"),
            "sha256": digest,
        },
    )
    assert staged.status_code == 200
    assert staged.json()["target_instance"] == "HASHI2"


@pytest.mark.parametrize("frontend_owner", [None, "owner:frontend"])
@pytest.mark.parametrize("history_mode", ["copy", "inherit_read_only", "none"])
def test_remote_clone_maps_history_owners_through_shared_token(tmp_path, frontend_owner, history_mode):
    from orchestrator.frontend_command_context import bind_frontend_command_principal

    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    for root, owner in ((source, "owner:source"), (target, "owner:target")):
        config = json.loads((root / "agents.json").read_text(encoding="utf-8"))
        config["global"]["owner_scope_id"] = owner
        _write_json(root / "agents.json", config)
    # The transfer is independent of any Telegram credential.
    _write_json(source / "secrets.json", {"hashi_remote_shared_token": TOKEN})
    store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    source_owner = frontend_owner or "owner:source"
    session = store.ensure_default_session(owner_id=source_owner, agent_id="zelda")
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id=source_owner, agent_id="zelda",
        request_id="remote-history", text="source question", source="workbench",
        idempotency_key="remote-history",
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(
        accepted.request_id, success=True, assistant_text="source answer",
        assistant_source="fixture",
    )
    source_messages = store.messages(session["session_id"], owner_id=source_owner)
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    unrelated = target_store.ensure_default_session(owner_id="owner:target", agent_id="another-agent")
    existing = target_store.accept_run(
        session_id=unrelated["session_id"], owner_id="owner:target", agent_id="another-agent",
        request_id="unrelated-history", text="unrelated question", source="workbench",
        idempotency_key="unrelated-history",
    )
    target_store.mark_request_running(existing.request_id, worker_id="fixture")
    target_store.finish_request(existing.request_id, success=True, assistant_text="unrelated answer",
                                assistant_source="fixture")
    package_path = tmp_path / "cross-owner.hashi-agent"
    runtime = SimpleNamespace(global_config=SimpleNamespace(
        project_root=source, instance_id="HASHI1", deployment_profile="personal"))
    scope = bind_frontend_command_principal(runtime, frontend_owner) if frontend_owner else nullcontext()
    with scope:
        package = create_agent_move_package(
            source, "zelda", package_path, source_instance="HASHI1",
            operation="clone", transfer_mode="workspace", history_mode=history_mode,
            include_telegram_secret=False,
        )
    if history_mode == "none":
        assert package.conversation_continuity is None
    else:
        assert package.conversation_continuity["owner_id"] == source_owner
    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw, shared_token=TOKEN, source_instance="HASHI1", target_instance="HASHI2",
        package_sha256=digest,
    )
    client = _client(target)
    payload = {
        "from_instance": "HASHI1", "encryption": ENVELOPE_SCHEME,
        "package_b64": base64.b64encode(envelope).decode("ascii"),
        "sha256": digest, "operation": "clone", "target_agent_id": "zelda-copy",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = _headers("POST", "/agent-move/v1/stage", body)
    headers["X-Hashi-Digest"] = "0" * 64
    assert client.post("/agent-move/v1/stage", content=body, headers=headers).status_code == 401
    staged = _post(client, "/agent-move/v1/stage", payload)
    assert staged.status_code == 200, staged.text
    action = {"from_instance": "HASHI1", "package_id": package.package_id}
    committed = _post(client, "/agent-move/v1/commit", action)
    assert committed.status_code == 200, committed.text
    assert _post(client, "/agent-move/v1/commit", action).status_code == 200
    sessions = target_store.list_sessions(
        owner_id="owner:target", agent_id="zelda-copy", include_archived=True
    )
    if history_mode == "none":
        assert sessions == []
    else:
        assert len(sessions) == 1
        imported = target_store.messages(sessions[0]["session_id"], owner_id="owner:target")
        assert [item["text"] for item in imported] == ["source question", "source answer"]
        provenance = imported[0]["message_context"]["conversation_continuity"]
        assert provenance["source_instance"] == "HASHI1"
        assert provenance["source_session_id"] == session["session_id"]
        assert provenance["source_message_id"] == source_messages[0]["message_id"]
        assert provenance["transfer_id"] == package.package_id
        # Read-only inherited sessions are archived; later work has its own active session.
        later_session = target_store.ensure_default_session(
            owner_id="owner:target", agent_id="zelda-copy",
        )
        later = target_store.accept_run(
            session_id=later_session["session_id"], owner_id="owner:target",
            agent_id="zelda-copy", request_id="later-target", text="later question",
            source="workbench", idempotency_key="later-target",
        )
        target_store.mark_request_running(later.request_id, worker_id="fixture")
        target_store.finish_request(
            later.request_id, success=True, assistant_text="later answer", assistant_source="fixture",
        )
    assert target_store.list_sessions(
        owner_id=source_owner, agent_id="zelda-copy", include_archived=True
    ) == []
    rolled_back = _post(client, "/agent-move/v1/rollback", action)
    assert rolled_back.status_code == 200, rolled_back.text
    surviving = target_store.list_sessions(
        owner_id="owner:target", agent_id="zelda-copy", include_archived=True
    )
    if history_mode == "none":
        assert surviving == []
    else:
        assert len(surviving) == 1
        assert [item["text"] for item in target_store.messages(
            surviving[0]["session_id"], owner_id="owner:target"
        )] == ["later question", "later answer"]
    assert [item["text"] for item in target_store.messages(
        unrelated["session_id"], owner_id="owner:target"
    )] == ["unrelated question", "unrelated answer"]
    assert store.messages(session["session_id"], owner_id=source_owner) == source_messages


def test_personal_remote_clone_does_not_depend_on_target_telegram_identity(tmp_path):
    from orchestrator.agent_move.package import configured_conversation_owner_id

    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    config = json.loads((source / "agents.json").read_text(encoding="utf-8"))
    config["global"]["owner_scope_id"] = "owner:source"
    _write_json(source / "agents.json", config)
    config = json.loads((target / "agents.json").read_text(encoding="utf-8"))
    config["global"]["telegram_owner_id"] = "invalid"
    _write_json(target / "agents.json", config)

    store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id="owner:source", agent_id="zelda")
    request = store.accept_run(
        session_id=session["session_id"], owner_id="owner:source", agent_id="zelda",
        request_id="no-telegram-target", text="question", source="workbench",
        idempotency_key="no-telegram-target",
    )
    store.mark_request_running(request.request_id, worker_id="fixture")
    store.finish_request(request.request_id, success=True, assistant_text="answer", assistant_source="fixture")
    package_path = tmp_path / "no-telegram-target.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="workspace", history_mode="copy",
        include_telegram_secret=False,
    )
    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw, shared_token=TOKEN, source_instance="HASHI1", target_instance="HASHI2",
        package_sha256=digest,
    )
    client = _client(target)
    payload = {
        "from_instance": "HASHI1", "encryption": ENVELOPE_SCHEME,
        "package_b64": base64.b64encode(envelope).decode("ascii"),
        "sha256": digest, "operation": "clone", "target_agent_id": "zelda-copy",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = _headers("POST", "/agent-move/v1/stage", body)
    headers["X-Hashi-Digest"] = "0" * 64
    assert client.post("/agent-move/v1/stage", content=body, headers=headers).status_code == 401
    assert not (target / "state" / "personal_data_owner.json").exists()
    wrong_target = encrypt_package_transport(
        raw, shared_token=TOKEN, source_instance="HASHI1", target_instance="HASHI3",
        package_sha256=digest,
    )
    bad_payload = {**payload, "package_b64": base64.b64encode(wrong_target).decode("ascii")}
    assert _post(client, "/agent-move/v1/stage", bad_payload).status_code == 400
    assert not (target / "state" / "personal_data_owner.json").exists()

    staged = _post(client, "/agent-move/v1/stage", payload)
    assert staged.status_code == 200, staged.text
    config = json.loads((target / "agents.json").read_text(encoding="utf-8"))
    config["global"]["telegram_owner_id"] = 99
    _write_json(target / "agents.json", config)
    action = {"from_instance": "HASHI1", "package_id": package.package_id}
    committed = _post(client, "/agent-move/v1/commit", action)
    assert committed.status_code == 200, committed.text
    assert configured_conversation_owner_id(target, instance_role="target") == "user:0"
    target_config = json.loads((target / "agents.json").read_text(encoding="utf-8"))
    assert target_config["global"]["owner_scope_id"] == "user:0"

    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    sessions = target_store.list_sessions(owner_id="user:0", agent_id="zelda-copy")
    assert len(sessions) == 1
    assert [row["text"] for row in target_store.messages(
        sessions[0]["session_id"], owner_id="user:0"
    )] == ["question", "answer"]
    assert target_store.list_sessions(owner_id="user:99", agent_id="zelda-copy") == []

    later = target_store.accept_run(
        session_id=sessions[0]["session_id"], owner_id="user:0", agent_id="zelda-copy",
        request_id="after-import", text="later", source="workbench", idempotency_key="after-import",
    )
    target_store.mark_request_running(later.request_id, worker_id="fixture")
    target_store.finish_request(later.request_id, success=True, assistant_text="later answer", assistant_source="fixture")
    rolled_back = _post(client, "/agent-move/v1/rollback", action)
    assert rolled_back.status_code == 200, rolled_back.text
    surviving = target_store.list_sessions(owner_id="user:0", agent_id="zelda-copy")
    assert len(surviving) == 1
    assert [row["text"] for row in target_store.messages(
        surviving[0]["session_id"], owner_id="user:0"
    )] == ["later", "later answer"]
    assert json.loads((target / "agents.json").read_text(encoding="utf-8"))["global"]["owner_scope_id"] == "user:0"


def test_remote_move_maps_distinct_owners_with_authenticated_transport_and_safe_rollback(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    for root, owner in ((source, "owner:source"), (target, "owner:target")):
        config = json.loads((root / "agents.json").read_text(encoding="utf-8"))
        config["global"]["owner_scope_id"] = owner
        _write_json(root / "agents.json", config)
    # No Telegram bot credential or primary identity is needed for Workbench history.
    _write_json(source / "secrets.json", {"hashi_remote_shared_token": TOKEN})
    store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id="owner:source", agent_id="zelda")
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id="owner:source", agent_id="zelda",
        request_id="move-history", text="original question", source="workbench",
        idempotency_key="move-history",
    )
    store.mark_request_running(accepted.request_id, worker_id="fixture")
    store.finish_request(accepted.request_id, success=True, assistant_text="original answer",
                         assistant_source="fixture")
    path = tmp_path / "authenticated-move.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", path, source_instance="HASHI1", operation="move",
        transfer_mode="identity_memory", include_agent_secrets=True, secret_passphrase=TOKEN,
    )
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw, shared_token=TOKEN, source_instance="HASHI1", target_instance="HASHI2",
        package_sha256=digest,
    )
    payload = {
        "from_instance": "HASHI1", "encryption": ENVELOPE_SCHEME,
        "package_b64": base64.b64encode(envelope).decode("ascii"),
        "sha256": digest, "operation": "move", "target_agent_id": "zelda",
    }
    client = _client(target)
    denied = client.post("/agent-move/v1/stage", json=payload)
    assert denied.status_code == 401
    staged = _post(client, "/agent-move/v1/stage", payload)
    assert staged.status_code == 200, staged.text
    action = {"from_instance": "HASHI1", "package_id": package.package_id}
    assert client.post("/agent-move/v1/commit", json=action).status_code == 401
    committed = _post(client, "/agent-move/v1/commit", action)
    assert committed.status_code == 200, committed.text
    replay = _post(client, "/agent-move/v1/commit", action)
    assert replay.status_code == 200
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    sessions = target_store.list_sessions(owner_id="owner:target", agent_id="zelda", include_archived=True)
    assert len(sessions) == 1
    assert [item["text"] for item in target_store.messages(
        sessions[0]["session_id"], owner_id="owner:target",
    )] == ["original question", "original answer"]
    assert target_store.list_sessions(owner_id="owner:source", agent_id="zelda", include_archived=True) == []
    assert _post(client, "/agent-move/v1/rollback", action).status_code == 200
    assert target_store.list_sessions(owner_id="owner:target", agent_id="zelda", include_archived=True) == []
    assert [item["text"] for item in store.messages(session["session_id"], owner_id="owner:source")] == [
        "original question", "original answer",
    ]


def test_remote_agent_move_stage_commit_activate_protocol(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "zelda.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        include_agent_secrets=True,
        secret_passphrase=TOKEN,
    )
    client = _client(target)

    capability_headers = _headers("GET", "/agent-move/v1/capabilities")
    capabilities = client.get(
        "/agent-move/v1/capabilities",
        headers=capability_headers,
    )
    assert capabilities.status_code == 200
    capability_payload = capabilities.json()
    response_auth = capability_payload.pop("response_auth")
    assert capability_payload["capability"] == "agent_move_receive_v1"
    assert capability_payload["authenticated_response_proof"] is True
    assert ENVELOPE_SCHEME in capability_payload["package_encryption"]
    assert verify_response_auth(
        shared_token=TOKEN,
        request_nonce=capability_headers[HEADER_NONCE],
        payload=capability_payload,
        response_auth=response_auth,
    )

    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw,
        shared_token=TOKEN,
        source_instance="HASHI1",
        target_instance="HASHI2",
        package_sha256=digest,
    )
    staged = _post(
        client,
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": ENVELOPE_SCHEME,
            "package_b64": base64.b64encode(envelope).decode("ascii"),
            "sha256": digest,
        },
    )
    assert staged.status_code == 200
    assert staged.json()["status"] == "staged"

    committed = _post(
        client,
        "/agent-move/v1/commit",
        {"from_instance": "HASHI1", "package_id": package.package_id},
    )
    assert committed.status_code == 200
    assert committed.json()["status"] == "committed_inactive"

    activated = _post(
        client,
        "/agent-move/v1/activate",
        {"from_instance": "HASHI1", "package_id": package.package_id},
    )
    assert activated.status_code == 200
    assert activated.json()["status"] == "activated_pending_reboot"
    assert (
        json.loads((target / "agents.json").read_text())["agents"][0]["is_active"]
        is True
    )


def test_inactive_move_remote_requires_actual_offline_health_before_finalizing(tmp_path, monkeypatch):
    from orchestrator.agent_move.package import INACTIVE_MOVE_CAPABILITY

    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    config = json.loads((source / "agents.json").read_text())
    config["agents"][0]["is_active"] = False
    _write_json(source / "agents.json", config)
    path = tmp_path / "inactive.hashi-agent"
    package = create_agent_move_package(source, "zelda", path, source_instance="HASHI1",
        operation="move", include_agent_secrets=True, secret_passphrase=TOKEN)
    required = package.manifest["required_receiver_capabilities"]
    outdated = AgentMoveRemoteClient(base_url="http://fixture.invalid", target_instance="HASHI2",
        source_instance="HASHI1", shared_token=TOKEN,
        capabilities={"schema_min":1,"schema_max":5,
                      "capabilities":[item for item in required if item != INACTIVE_MOVE_CAPABILITY]})
    with pytest.raises(AgentMoveRemoteError, match=INACTIVE_MOVE_CAPABILITY):
        outdated.ensure_package_compatible(package)
    client = _client(target)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(raw, shared_token=TOKEN, source_instance="HASHI1",
        target_instance="HASHI2", package_sha256=digest)
    staged = _post(client, "/agent-move/v1/stage", {"from_instance":"HASHI1", "operation":"move",
        "encryption":ENVELOPE_SCHEME, "package_b64":base64.b64encode(envelope).decode("ascii"), "sha256":digest})
    assert staged.status_code == 200 and staged.json()["desired_target_active"] is False
    payload = {"from_instance":"HASHI1", "package_id":package.package_id}
    assert _post(client, "/agent-move/v1/commit", payload).status_code == 200
    assert _post(client, "/agent-move/v1/start", payload).status_code == 409
    monkeypatch.setattr(remote_server, "_fetch_workbench_health", lambda timeout=1.0: None)
    assert _post(client, "/agent-move/v1/finalize", payload).status_code == 409
    monkeypatch.setattr(remote_server, "_fetch_workbench_health", lambda timeout=1.0: {"ok":True,"agents":[]})
    assert _post(client, "/agent-move/v1/finalize", payload).status_code == 409
    monkeypatch.setattr(remote_server, "_fetch_workbench_health", lambda timeout=1.0: {"ok":True,"agents":[],"agent_runtime_presence":["zelda"]})
    assert _post(client, "/agent-move/v1/finalize", payload).status_code == 409
    monkeypatch.setattr(remote_server, "_fetch_workbench_health", lambda timeout=1.0: {"ok":True,"agents":[],"agent_runtime_presence":[]})
    completed = _post(client, "/agent-move/v1/finalize", payload)
    assert completed.status_code == 200
    assert completed.json()["target_verification"] == {"runtime_online":False,"runtime_present":False,
        "presence_checked":True,"desired_active":False,"config_inactive":True}
    assert json.loads((target / "agents.json").read_text())["agents"][0]["is_active"] is False


def test_remote_clone_resolve_lifecycle_and_finalize_without_telegram(
    tmp_path,
    monkeypatch,
):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=True)
    source_secrets = json.loads((source / "secrets.json").read_text())
    source_secrets["zelda_api_key"] = "safe-agent-key"
    _write_json(source / "secrets.json", source_secrets)
    package_path = tmp_path / "zelda-clone.hashi-agent"
    package = create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        operation="clone",
        include_agent_secrets=True,
        include_telegram_secret=False,
        secret_passphrase=TOKEN,
    )
    lifecycle_calls = []
    monkeypatch.setattr(
        remote_server,
        "_request_workbench_agent_lifecycle",
        lambda agent, *, action: lifecycle_calls.append((agent, action))
        or {"ok": True, "agent": agent, "action": action},
    )
    monkeypatch.setattr(
        remote_server,
        "_fetch_workbench_health",
        lambda timeout=1.0: {"ok": True, "agents": ["zelda_1"]},
    )
    client = _client(target)

    resolved = _post(
        client,
        "/agent-move/v1/resolve",
        {
            "from_instance": "HASHI1",
            "source_agent_id": "zelda",
            "operation": "clone",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["target_agent_id"] == "zelda_1"

    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw,
        shared_token=TOKEN,
        source_instance="HASHI1",
        target_instance="HASHI2",
        package_sha256=digest,
    )
    staged = _post(
        client,
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": ENVELOPE_SCHEME,
            "package_b64": base64.b64encode(envelope).decode("ascii"),
            "sha256": digest,
            "operation": "clone",
            "target_agent_id": "zelda_1",
        },
    )
    assert staged.status_code == 200
    assert staged.json()["target_agent_id"] == "zelda_1"
    for action in ("commit", "activate", "start", "finalize"):
        response = _post(
            client,
            f"/agent-move/v1/{action}",
            {"from_instance": "HASHI1", "package_id": package.package_id},
        )
        assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert lifecycle_calls == [("zelda_1", "start")]

    rows = json.loads((target / "agents.json").read_text())["agents"]
    clone = next(row for row in rows if row["name"] == "zelda_1")
    assert clone["is_active"] is True
    secrets = json.loads((target / "secrets.json").read_text())
    assert clone["telegram_token_key"] not in secrets
    assert "zelda_1" not in secrets
    assert secrets["zelda_1_api_key"] == "safe-agent-key"


def test_remote_agent_move_rejects_authenticated_source_mismatch(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(source, "zelda", package_path, source_instance="HASHI1")
    raw = package_path.read_bytes()
    client = _client(target)
    payload = {
        "from_instance": "HASHI9",
        "encryption": ENVELOPE_SCHEME,
        "package_b64": base64.b64encode(raw).decode("ascii"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    response = client.post(
        "/agent-move/v1/stage",
        content=body,
        headers=_headers("POST", "/agent-move/v1/stage", body),
    )

    assert response.status_code == 401
    assert not (target / "workspaces" / "zelda").exists()


def test_remote_agent_move_rejects_plaintext_package_transport(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(source, "zelda", package_path, source_instance="HASHI1")
    raw = package_path.read_bytes()
    client = _client(target)

    response = _post(
        client,
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": "none",
            "package_b64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )

    assert response.status_code == 400
    assert "Encrypted Agent move transport is required" in response.json()["error"]


@pytest.mark.skipif(
    os.name == "nt",
    reason="agent.md and AGENT.md cannot coexist on a case-insensitive Windows tree",
)
def test_schema2_upload_is_refused_clearly_by_schema1_receiver(tmp_path, monkeypatch):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    (source / "workspaces" / "zelda" / "AGENT.md").write_text(
        "retained historical identity",
        encoding="utf-8",
    )
    package_path = tmp_path / "zelda-schema2.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        schema_version=2,
    )
    client = AgentMoveRemoteClient(
        base_url="http://127.0.0.1:8767",
        target_instance="HASHI2",
        source_instance="HASHI1",
        shared_token=TOKEN,
        capabilities={
            "capability": "agent_move_receive_v1",
            "schema_min": 1,
            "schema_max": 1,
            "max_package_bytes": 256 * 1024 * 1024,
            "package_encryption": [ENVELOPE_SCHEME],
        },
    )
    monkeypatch.setattr(
        AgentMoveRemoteClient,
        "_request",
        lambda *args, **kwargs: pytest.fail("incompatible package must not upload"),
    )

    with pytest.raises(AgentMoveRemoteError, match="schema 2.*schema 1"):
        client.stage(package_path)


def test_remote_agent_move_rejects_tampered_encrypted_transport(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "zelda.hashi-agent"
    create_agent_move_package(source, "zelda", package_path, source_instance="HASHI1")
    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = bytearray(
        encrypt_package_transport(
            raw,
            shared_token=TOKEN,
            source_instance="HASHI1",
            target_instance="HASHI2",
            package_sha256=digest,
        )
    )
    envelope[-1] ^= 1

    response = _post(
        _client(target),
        "/agent-move/v1/stage",
        {
            "from_instance": "HASHI1",
            "encryption": ENVELOPE_SCHEME,
            "package_b64": base64.b64encode(envelope).decode("ascii"),
            "sha256": digest,
        },
    )

    assert response.status_code == 400
    assert "authentication failed" in response.json()["error"]
    assert not (target / "workspaces" / "zelda").exists()


def test_remote_client_rejects_unsigned_receiver_success(monkeypatch):
    class _UnsignedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"ok":true,"status":"activated_pending_reboot"}'

    monkeypatch.setattr(
        "orchestrator.agent_move.remote_client.urllib_request.urlopen",
        lambda *_args, **_kwargs: _UnsignedResponse(),
    )

    with pytest.raises(AgentMoveRemoteError, match="response authentication failed"):
        _request_json(
            "http://127.0.0.1:8767/agent-move/v1/activate",
            method="POST",
            payload={"from_instance": "HASHI1", "package_id": "move-id"},
            shared_token=TOKEN,
            from_instance="HASHI1",
        )


def test_agent_move_tls_configuration_does_not_silently_downgrade():
    assert _candidate_base_urls(
        {"host": "example.test", "use_tls": True},
        8767,
    ) == ["https://example.test:8767"]
    assert _candidate_base_urls(
        {
            "host": "example.test",
            "use_tls": True,
            "allow_http_fallback": True,
        },
        8767,
    ) == ["https://example.test:8767", "http://example.test:8767"]


def test_streamed_move_authenticates_before_staging_and_cleans_failed_upload(tmp_path):
    from urllib.parse import urlencode
    from orchestrator.agent_move.transport_crypto import encrypt_package_file
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    package_path = tmp_path / "stream.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        package_path,
        source_instance="HASHI1",
        include_agent_secrets=True,
        secret_passphrase=TOKEN,
    )
    digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
    encrypted = tmp_path / "wire.enc"
    encrypt_package_file(package_path, encrypted, shared_token=TOKEN,
                         source_instance="HASHI1", target_instance="HASHI2", package_sha256=digest)
    wire = encrypted.read_bytes()
    path = "/agent-move/v1/stage-stream?" + urlencode(sorted({
        "from_instance": "HASHI1", "sha256": digest, "operation": "move", "target_agent_id": "zelda"}.items()))
    client = _client(target)
    assert client.post(path, content=wire).status_code == 401
    for broken in (wire[:-1], wire[:-1] + bytes([wire[-1] ^ 1])):
        response = client.post(path, content=broken, headers=_headers("POST", path))
        assert response.status_code == 400
        assert json.loads((target / "agents.json").read_text())["agents"] == []
        assert not list((target / "state" / "agent_moves").glob("incoming/*/state.json"))
    response = client.post(path, content=iter([wire[:31], wire[31:]]), headers=_headers("POST", path))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "staged"
    assert json.loads((target / "agents.json").read_text())["agents"] == []


def test_streaming_client_uses_file_body_and_verifies_receiver_proof(tmp_path, monkeypatch):
    from urllib.parse import urlsplit
    from orchestrator.agent_move.service import receiver_capabilities
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    path = tmp_path / "stream.hashi-agent"
    create_agent_move_package(
        source,
        "zelda",
        path,
        source_instance="HASHI1",
        include_agent_secrets=True,
        secret_passphrase=TOKEN,
    )
    client = _client(target)

    class Response:
        def __init__(self, raw): self.raw = raw
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return self.raw[:limit]

    def send(request, **kwargs):
        assert hasattr(request.data, "read")
        url = urlsplit(request.full_url)
        def chunks():
            while chunk := request.data.read(127):
                yield chunk
        response = client.post(url.path + "?" + url.query, content=chunks(), headers=dict(request.header_items()))
        assert response.status_code == 200, response.text
        return Response(response.content)

    monkeypatch.setattr("orchestrator.agent_move.remote_client.urllib_request.urlopen", send)
    remote = AgentMoveRemoteClient("http://receiver", "HASHI2", "HASHI1", TOKEN, receiver_capabilities(target))
    assert remote.stage(path, operation="move", target_agent_id="zelda")["status"] == "staged"


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("tamper", [False, True])
def test_clone_preflight_rejection_is_typed_only_with_authenticated_proof(tmp_path, monkeypatch, streaming, tamper):
    from io import BytesIO
    from urllib.error import HTTPError
    from urllib.parse import urlsplit
    from orchestrator.agent_move.package import CloneImportPreflightError
    from orchestrator.agent_move.service import receiver_capabilities

    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    data = json.loads((source / "agents.json").read_text())
    data["agents"][0]["active_backend"] = "invalid-backend"
    _write_json(source / "agents.json", data)
    path = tmp_path / "rejected.hashi-agent"
    create_agent_move_package(source, "zelda", path, operation="clone", transfer_mode="workspace")
    receiver = _client(target)

    def send(request, **kwargs):
        url = urlsplit(request.full_url)
        content = request.data.read() if hasattr(request.data, "read") else request.data
        response = receiver.post(url.path + ("?" + url.query if url.query else ""), content=content, headers=dict(request.header_items()))
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == CloneImportPreflightError.code
        if tamper:
            body["error"] = "untrusted replacement"
        raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(json.dumps(body).encode()))

    monkeypatch.setattr("orchestrator.agent_move.remote_client.urllib_request.urlopen", send)
    capabilities = receiver_capabilities(target)
    if not streaming:
        capabilities.pop("streaming_upload", None)
    remote = AgentMoveRemoteClient("http://receiver", "HASHI2", "HASHI1", TOKEN, capabilities)
    with pytest.raises(AgentMoveRemoteError if tamper else CloneImportPreflightError):
        remote.stage(path, operation="clone")
    assert not list((target / "state/agent_moves/incoming").glob("*/state.json"))
    assert json.loads((target / "agents.json").read_text())["agents"] == []
@pytest.mark.parametrize("tamper", [False, True])
@pytest.mark.parametrize("changed_policy", ["workspace", "typo"])
def test_scope_change_and_staged_abort_require_signed_receiver_proof(tmp_path, monkeypatch, tamper, changed_policy):
    from io import BytesIO
    from urllib.error import HTTPError
    from urllib.parse import urlsplit
    from orchestrator.agent_move.package import AccessScopePreviewChanged, package_sha256
    from orchestrator.agent_move.service import receiver_capabilities, stage_agent_move

    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    config = json.loads((target / "agents.json").read_text())
    config["global"]["agent_move"] = {"max_access_scope": "project"}
    _write_json(target / "agents.json", config)
    path = tmp_path / "scope.hashi-agent"
    create_agent_move_package(source, "zelda", path, source_instance="HASHI1",
                              operation="move", transfer_mode="workspace",
                              include_agent_secrets=True, secret_passphrase=TOKEN)
    staged = stage_agent_move(target, path, expected_sha256=package_sha256(path),
                              source_instance="HASHI1", target_instance="HASHI2",
                              operation="move", secret_passphrase=TOKEN)
    config["global"]["agent_move"]["max_access_scope"] = changed_policy
    _write_json(target / "agents.json", config)
    receiver = _client(target)
    package_id = staged["package_id"]
    if changed_policy == "typo":
        caps = receiver.get("/agent-move/v1/capabilities",
                            headers=_headers("GET", "/agent-move/v1/capabilities"))
        assert caps.status_code == 200
        assert caps.json()["max_access_scope"] is None
        assert "invalid" in caps.json()["max_access_scope_error"]
    assert receiver.post("/agent-move/v1/abort-staged",
                         json={"from_instance": "HASHI1", "package_id": package_id}).status_code == 401

    class Response:
        def __init__(self, raw): self.raw = raw
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return self.raw

    def send(request, **kwargs):
        url = urlsplit(request.full_url)
        response = receiver.request(request.method, url.path, content=request.data,
                                    headers=dict(request.header_items()))
        body = response.json()
        if response.status_code >= 400:
            if tamper and url.path.endswith("/commit"):
                body["error"] = "forged scope rejection"
            raise HTTPError(request.full_url, response.status_code, "Rejected", {},
                            BytesIO(json.dumps(body).encode()))
        return Response(response.content)

    monkeypatch.setattr("orchestrator.agent_move.remote_client.urllib_request.urlopen", send)
    remote = AgentMoveRemoteClient("http://receiver", "HASHI2", "HASHI1", TOKEN,
                                   receiver_capabilities(target))
    with pytest.raises(AgentMoveRemoteError if tamper else AccessScopePreviewChanged):
        remote.commit(package_id)
    assert json.loads((target / "agents.json").read_text())["agents"] == []
    if not tamper:
        receipt = remote.abort_staged(package_id)
        assert receipt["status"] == "rolled_back" and receipt["staged_abort"] is True
        assert remote.abort_staged(package_id)["staged_abort"] is True
        with pytest.raises(AgentMoveRemoteError):
            remote.commit(package_id)
    else:
        assert remote.status(package_id)["status"] == "staged"


# HASHI-Fingerprint: hfp_055c2b35cf9c4826b08b34ba5f3c5b41

def test_signed_remote_member_budget_is_negotiated_and_enforced_before_upload(tmp_path, monkeypatch):
    from urllib.parse import urlsplit
    from orchestrator.agent_move import package as owner

    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    for index in range(6):
        (source / "workspaces/zelda" / f"extra-{index}.txt").write_text(str(index))
    monkeypatch.setattr(owner, "MAX_ARCHIVE_MEMBERS", 8)
    monkeypatch.setattr(owner, "MAX_UNPACKED_BYTES", 2048)
    monkeypatch.setattr(owner, "WORKSPACE_LIMIT_BYTES", 1024)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_ARCHIVE_MEMBERS", "64")
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_UNPACKED_BYTES", str(128 * 1024))
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_WORKSPACE_BYTES", str(128 * 1024))
    (source / "workspaces/zelda/big.txt").write_text("synthetic data " * 1024)
    path = tmp_path / "member-budget.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", path, operation="clone", transfer_mode="workspace",
        source_instance="HASHI1", max_archive_members=64, max_unpacked_bytes=128 * 1024,
        max_workspace_bytes=128 * 1024,
    )
    assert len(package.names) > 8
    assert package.unpacked_bytes > 2048
    assert package.workspace_bytes > 1024
    client = _client(target)
    headers = _headers("GET", "/agent-move/v1/capabilities")
    response = client.get("/agent-move/v1/capabilities", headers=headers)
    assert response.status_code == 200
    capabilities = response.json()
    assert capabilities["max_archive_members"] == 64
    assert capabilities["max_unpacked_bytes"] == 128 * 1024
    assert capabilities["max_workspace_bytes"] == 128 * 1024
    assert verify_response_auth(
        shared_token=TOKEN, request_nonce=headers[HEADER_NONCE],
        payload={key: value for key, value in capabilities.items() if key != "response_auth"},
        response_auth=capabilities["response_auth"],
    )

    class Response:
        def __init__(self, raw):
            self.raw = raw
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return self.raw[:limit]

    calls = []
    def send(request, **kwargs):
        calls.append(request.full_url)
        url = urlsplit(request.full_url)
        def chunks():
            while chunk := request.data.read(127):
                yield chunk
        result = client.post(url.path + "?" + url.query, content=chunks(),
                             headers=dict(request.header_items()))
        assert result.status_code == 200, result.text
        return Response(result.content)

    monkeypatch.setattr("orchestrator.agent_move.remote_client.urllib_request.urlopen", send)
    remote = AgentMoveRemoteClient("http://receiver", "HASHI2", "HASHI1", TOKEN, capabilities)
    result = remote.stage(path, operation="clone", target_agent_id="zelda")
    assert result["status"] == "staged"
    assert len(calls) == 1

    limited = dict(capabilities, max_archive_members=len(package.names) - 1)
    limited_remote = AgentMoveRemoteClient("http://receiver", "HASHI2", "HASHI1", TOKEN, limited)
    with pytest.raises(owner.AgentMoveError, match="member"):
        limited_remote.stage(path, operation="clone", target_agent_id="zelda")
    assert len(calls) == 1, "over-budget retry must fail before uploading"

    limited_expanded = dict(capabilities, max_unpacked_bytes=package.unpacked_bytes - 1)
    limited_remote = AgentMoveRemoteClient("http://receiver", "HASHI2", "HASHI1", TOKEN, limited_expanded)
    with pytest.raises(owner.AgentMoveError, match="expanded"):
        limited_remote.stage(path, operation="clone", target_agent_id="zelda")
    assert len(calls) == 1, "over-expanded retry must fail before uploading"

    limited_workspace = dict(capabilities, max_workspace_bytes=package.workspace_bytes - 1)
    limited_remote = AgentMoveRemoteClient("http://receiver", "HASHI2", "HASHI1", TOKEN, limited_workspace)
    with pytest.raises(owner.AgentMoveError, match="workspace.*limit"):
        limited_remote.stage(path, operation="clone", target_agent_id="zelda")
    assert len(calls) == 1, "over-workspace retry must fail before uploading"

def test_stream_receiver_uses_advertised_compressed_budget(tmp_path, monkeypatch):
    from urllib.parse import urlencode
    from orchestrator.agent_move.transport_crypto import encrypt_package_file

    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    archive = tmp_path / "stream-budget.hashi-agent"
    create_agent_move_package(source, "zelda", archive, source_instance="HASHI1")
    size = archive.stat().st_size
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    encrypted = tmp_path / "wire.enc"
    encrypt_package_file(archive, encrypted, shared_token=TOKEN, source_instance="HASHI1",
                         target_instance="HASHI2", package_sha256=digest)
    wire = encrypted.read_bytes()
    path = "/agent-move/v1/stage-stream?" + urlencode(sorted({
        "from_instance": "HASHI1", "sha256": digest, "operation": "move", "target_agent_id": "zelda",
    }.items()))
    client = _client(target)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES", str(size - 1))
    cap = client.get("/agent-move/v1/capabilities", headers=_headers("GET", "/agent-move/v1/capabilities"))
    assert cap.json()["max_package_bytes"] == size - 1
    denied = client.post(path, content=iter([wire[:31], wire[31:]]), headers=_headers("POST", path))
    assert denied.status_code == 400
    assert not list((target / "state/agent_moves").glob("incoming/*/state.json"))

    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES", str(size))
    cap = client.get("/agent-move/v1/capabilities", headers=_headers("GET", "/agent-move/v1/capabilities"))
    assert cap.json()["max_package_bytes"] == size
    accepted = client.post(path, content=iter([wire[:31], wire[31:]]), headers=_headers("POST", path))
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "staged"

def test_legacy_receiver_uses_same_advertised_compressed_budget(tmp_path, monkeypatch):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    archive = tmp_path / "legacy-budget.hashi-agent"
    create_agent_move_package(source, "zelda", archive, source_instance="HASHI1")
    raw = archive.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    wire = encrypt_package_transport(raw, shared_token=TOKEN, source_instance="HASHI1",
                                     target_instance="HASHI2", package_sha256=digest)
    payload = {"from_instance": "HASHI1", "encryption": ENVELOPE_SCHEME,
               "package_b64": base64.b64encode(wire).decode("ascii"), "sha256": digest,
               "operation": "move", "target_agent_id": "zelda"}
    client = _client(target)
    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES", str(len(raw) - 1))
    rejected = _post(client, "/agent-move/v1/stage", payload)
    assert rejected.status_code in (400, 413), rejected.text
    assert not list((target / "state/agent_moves").glob("incoming/*/state.json"))

    monkeypatch.setenv("HASHI_AGENT_MOVE_MAX_PACKAGE_BYTES", str(len(raw)))
    accepted = _post(client, "/agent-move/v1/stage", payload)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "staged"



def test_remote_personal_clone_warns_for_unidentified_source_history(tmp_path):
    source = _root(tmp_path, "source", "HASHI1", with_agent=True)
    target = _root(tmp_path, "target", "HASHI2", with_agent=False)
    target_config = json.loads((target / "agents.json").read_text(encoding="utf-8"))
    target_config["global"]["owner_scope_id"] = "owner:target"
    _write_json(target / "agents.json", target_config)
    _write_json(source / "secrets.json", {"hashi_remote_shared_token": TOKEN})
    source_store = SessionStore(source / "state/sessions.sqlite3", instance_id="HASHI1")
    legacy = source_store.ensure_default_session(owner_id="user:7", agent_id="zelda")
    source_store.append_presentation_message(
        session_id=legacy["session_id"], owner_id="user:7", agent_id="zelda",
        role="assistant", text="unidentified history stays here", source="fixture",
        idempotency_key="legacy-history", history_eligible=True,
    )
    package_path = tmp_path / "empty-selected-history.hashi-agent"
    package = create_agent_move_package(
        source, "zelda", package_path, source_instance="HASHI1",
        operation="clone", transfer_mode="identity_memory", history_mode="copy",
    )
    assert package.conversation_continuity["owner_id"] == "user:0"
    assert package.conversation_continuity["summary"]["eligible_message_count"] == 0
    assert any("other local conversation history" in warning
               for warning in package.manifest["warnings"])
    raw = package_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    envelope = encrypt_package_transport(
        raw, shared_token=TOKEN, source_instance="HASHI1", target_instance="HASHI2",
        package_sha256=digest,
    )
    client = _client(target)
    payload = {
        "from_instance": "HASHI1", "encryption": ENVELOPE_SCHEME,
        "package_b64": base64.b64encode(envelope).decode("ascii"),
        "sha256": digest, "operation": "clone", "target_agent_id": "zelda-copy",
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = _headers("POST", "/agent-move/v1/stage", body)
    headers["X-Hashi-Digest"] = "0" * 64
    assert client.post("/agent-move/v1/stage", content=body, headers=headers).status_code == 401
    staged = _post(client, "/agent-move/v1/stage", payload)
    assert staged.status_code == 200, staged.text
    assert any("other local conversation history" in warning
               for warning in staged.json()["warnings"])
    action = {"from_instance": "HASHI1", "package_id": package.package_id}
    committed = _post(client, "/agent-move/v1/commit", action)
    assert committed.status_code == 200, committed.text
    assert committed.json()["conversation_continuity"]["imported_messages"] == 0
    target_store = SessionStore(target / "state/sessions.sqlite3", instance_id="HASHI2")
    assert target_store.list_sessions(
        owner_id="owner:target", agent_id="zelda-copy", include_archived=True,
    ) == []
    assert target_store.list_sessions(
        owner_id="user:7", agent_id="zelda-copy", include_archived=True,
    ) == []
    assert [row["text"] for row in source_store.messages(
        legacy["session_id"], owner_id="user:7",
    )] == ["unidentified history stays here"]
