from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.config import ConfigManager
from orchestrator.workbench_api import WorkbenchApiServer
from orchestrator.private_authorization import authorization_content_sha256
from orchestrator.voice_synthesizer import VoiceProviderFailure, VoiceSynthesisError


class _Request:
    def __init__(
        self,
        payload=None,
        *,
        query=None,
        match_info=None,
        headers=None,
        body=b"",
    ):
        self._payload = payload or {}
        if isinstance(self._payload, dict) and "message" in self._payload and "originated_at" not in self._payload:
            from datetime import datetime, timezone
            self._payload.setdefault("originated_at", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
        self.query = query or {}
        self.match_info = match_info or {}
        self.headers = headers or {}
        self._body = bytes(body)

    async def json(self):
        return self._payload

    async def read(self):
        return self._body


class _MultipartPart:
    def __init__(
        self,
        name: str,
        value: str = "",
        *,
        filename: str | None = None,
        payload: bytes = b"",
        content_type: str = "",
    ):
        self.name = name
        self._value = value
        self.filename = filename
        self.headers = {"Content-Type": content_type} if content_type else {}
        self._payload = bytes(payload)
        self._read = False

    async def text(self):
        return self._value

    async def read_chunk(self, size=1024 * 1024):
        if self._read:
            return b""
        self._read = True
        return self._payload


class _MultipartReader:
    def __init__(self, fields: dict[str, str], uploads=None):
        parts = [_MultipartPart(key, value) for key, value in fields.items()]
        parts.extend(uploads or [])
        self._parts = iter(parts)

    async def next(self):
        return next(self._parts, None)


class _MultipartRequest(_Request):
    content_type = "multipart/form-data"

    def __init__(self, fields: dict[str, str], uploads=None):
        super().__init__()
        self._fields = fields
        self._uploads = list(uploads or [])

    async def multipart(self):
        return _MultipartReader(self._fields, self._uploads)


class _Runtime:
    name = "lily"
    backend_ready = True

    def __init__(self):
        self.server = None
        self.last_request_metadata = None
        self.last_request_content = None
        self.last_source = None
        self.enqueue_request_calls = 0
        self.api_request_metadata = []
        self.api_request_content = []
        self.api_delivery_flags = []
        self.media_dir = None
        self.api_media_calls = []
        self._safevoice_enabled = False
        self._native_voice_transcripts = {}
        self.voice_manager = SimpleNamespace(
            native_audio_enabled=lambda: True,
            native_policy={"mode": "auto"},
        )
        self.backend_manager = SimpleNamespace(
            current_backend=SimpleNamespace(
                capabilities=SimpleNamespace(
                    input_modalities=frozenset({"text", "audio"}),
                    output_modalities=frozenset({"text", "audio"}),
                    output_formats={"audio": ("wav",)},
                    output_streaming="sse",
                    api_surface="chat_completions",
                )
            )
        )

    def get_display_name(self):
        return "Lily"

    async def cancel_session_run(self, *, owner_id, session_id, run_id, request_id, reason):
        run = self.server.session_store.get_run(run_id, owner_id=owner_id)
        assert (run["session_id"], run["request_id"]) == (session_id, request_id)
        stopped = self.server.session_store.cancel_run(run_id, owner_id=owner_id, reason=reason)
        return {"ok": True, "session_id": session_id, "run_id": run_id,
                "request_id": request_id, "status": stopped["state"], "terminal": True}

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
        self.enqueue_request_calls += 1
        self.last_source = source
        self.last_request_metadata = dict(request_metadata)
        self.last_request_content = _kwargs.get("request_content")
        request_id = "req-api"
        accepted = self.server.session_store.accept_run(
            session_id=request_metadata["session_id"],
            owner_id=request_metadata["owner_id"],
            agent_id=self.name,
            request_id=request_id,
            text=str(request_metadata.get("session_message_text", prompt)),
            source=source,
            idempotency_key=idempotency_key,
            display_text=request_metadata.get("session_message_display_text"),
            content=request_metadata.get("session_message_content"),
            response_preferences=request_metadata.get("response_preferences"),
            agent_workspace=getattr(self, "workspace_dir", None),
        )
        if getattr(self, "workspace_dir", None) is not None and self.last_request_content:
            self.last_request_content = self.server.session_store.rebind_request_content_attachments(
                session_id=accepted.session_id,
                owner_id=request_metadata["owner_id"],
                request_content=self.last_request_content,
            )
        return accepted.request_id

    async def enqueue_api_text(
        self,
        _text,
        source="api",
        *,
        deliver_to_telegram=True,
        request_metadata,
        request_content=None,
        idempotency_key=None,
    ):
        del source
        del idempotency_key
        self.api_delivery_flags.append(bool(deliver_to_telegram))
        self.api_request_metadata.append(dict(request_metadata))
        self.api_request_content.append(request_content)
        return f"req-api-{len(self.api_request_metadata)}"

    async def enqueue_api_media(self, **kwargs):
        self.api_media_calls.append(dict(kwargs))
        return f"req-media-{len(self.api_media_calls)}"

    def tui_voice_state(self):
        return {
            "profile": "warm_female",
            "profiles": [{"id": "warm_female", "label": "Warm"}],
        }

    def set_tui_voice_profile(self, profile):
        return {
            "profile": profile,
            "profiles": [{"id": profile, "label": "Selected"}],
        }

    async def synthesize_tui_speech(self, text, request_id, source_language=None):
        content = b"OggS-test-audio"
        return {
            "content_b64": base64.b64encode(content).decode("ascii"),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "media_type": "audio/ogg",
            "request_id": request_id,
            "spoken": text,
            "source_language": source_language,
            "provider": "windows",
            "fallback_from": "edge",
            "fallback_reason_code": "tts_protocol_rejected",
            "fallback_reason": "Edge TTS rejected the speech request (HTTP 403).",
        }

def _server(
    tmp_path: Path,
    *,
    reconcile_session_runs: bool = True,
) -> tuple[WorkbenchApiServer, _Runtime]:
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
        reconcile_session_runs=reconcile_session_runs,
    )
    runtime.server = server
    return server, runtime


def _command_server(tmp_path):
    server, runtime = _server(tmp_path)
    server.admin_token = "session-command-test-token"
    return server, runtime


def _command_request(*args, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers["X-Workbench-Token"] = "session-command-test-token"
    return _Request(*args, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_shared_chat_projection_reads_start_notice_without_fenced_worker(tmp_path):
    server, runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    notice = server.session_store.append_presentation_message(
        session_id=session["session_id"], owner_id=owner, agent_id="lily",
        role="assistant", text="Starting hot restart", source="telegram.runtime_notice",
        idempotency_key="restart-start", history_eligible=False,
    )
    # A fenced worker must not be consulted to read already-durable messages.
    def worker_unavailable(*_args, **_kwargs):
        raise AssertionError("chat projection crossed the fenced Worker")
    runtime.command = worker_unavailable
    response = await server.handle_chat_projection(_Request(match_info={"name": "lily"}))
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    projection = json.loads(response.text)["projection"]
    assert any(row["message_id"] == notice["message_id"] and row["text"] == "Starting hot restart"
               for row in projection["messages"])
    assert runtime.enqueue_request_calls == 0
    other = server.session_store.create_session(owner_id="user:8", agent_id="lily")
    denied = await server.handle_chat_projection(_Request(
        match_info={"name": "lily"}, query={"session_id": other["session_id"]}))
    assert denied.status == 404
    malformed = await server.handle_chat_projection(_Request(match_info={"name": "lily"}, query={"offset": "-1"}))
    assert malformed.status == 400


@pytest.mark.asyncio
async def test_attachment_discard_is_owner_scoped_atomic_and_refuses_message_bound_assets(tmp_path):
    server, runtime = _server(tmp_path)
    store = server.session_store
    session = store.create_session(owner_id='user:7', agent_id='lily')
    session_id = session['session_id']
    ids = []
    for name in ('first.txt','second.txt'):
        staged = store.stage_attachment(session_id=session_id,owner_id='user:7',filename=name,
            media_type='text/plain',size_bytes=3,sha256=hashlib.sha256(b'one').hexdigest(),upload_required=True)
        ids.append(staged['attachment_id'])
        store.upload_attachment_bytes(session_id=session_id,owner_id='user:7',attachment_id=ids[-1],payload=b'one')
        store.commit_attachment(session_id=session_id,owner_id='user:7',attachment_id=ids[-1])
    admitted = await server.handle_v1_session_runs_create(_Request(
        {'idempotency_key':'bound-intake','message':{'content':[{'type':'attachment','attachment_id':ids[0]}]}},
        match_info={'session_id':session_id}))
    assert admitted.status == 202, admitted.text
    rejected = await server.handle_v1_attachment_discard(_Request(
        {'attachment_ids':ids},match_info={'session_id':session_id}))
    assert rejected.status == 409
    for attachment_id in ids:
        assert store.attachment_bytes(session_id=session_id,owner_id='user:7',attachment_id=attachment_id)[1] == b'one'
    removed = await server.handle_v1_attachment_discard(_Request(
        {'attachment_ids':[ids[1]]},match_info={'session_id':session_id}))
    assert removed.status == 200
    assert json.loads(removed.text)['attachment_ids'] == [ids[1]]
    repeated = await server.handle_v1_attachment_discard(_Request(
        {'attachment_ids':[ids[1]]},match_info={'session_id':session_id}))
    assert json.loads(repeated.text)['attachment_ids'] == []
    other = store.create_session(owner_id='user:8',agent_id='lily')
    denied = await server.handle_v1_attachment_discard(_Request(
        {'attachment_ids':ids},match_info={'session_id':other['session_id']}))
    assert denied.status == 404
    invalid = await server.handle_v1_attachment_discard(_Request(
        {'attachment_ids':[]},match_info={'session_id':session_id}))
    assert invalid.status == 400


@pytest.mark.asyncio
async def test_reboot_discovery_includes_telegram_and_recent_completion_only_for_owner(tmp_path):
    from orchestrator.reboot_manager import RebootManager
    import time

    server, _runtime = _server(tmp_path)
    manager = RebootManager(SimpleNamespace(paths=SimpleNamespace(bridge_home=tmp_path)), None)
    server.orchestrator = SimpleNamespace(reboot_manager=manager)
    def create(owner, surface):
        return manager.receipts.create(source="lily", targets=["lily"], display_names={}, mode="max",
            origin={"owner_id": owner, "actor_id": owner.split(":")[-1], "chat_id": 123, "surface": surface})
    visible = create("user:7", "telegram")
    create("user:8", "workbench")
    old = create("user:7", "telegram")
    manager.receipts.update(old["id"], status="succeeded", progress=[{
        "sequence": 1, "phase": "online", "status": "succeeded", "created_at": time.time()-120}])
    response = await server.handle_reboot_operations(_Request())
    assert response.status == 200
    assert [item["operation_id"] for item in json.loads(response.text)["operations"]] == [visible["id"]]
    manager.receipts.update(visible["id"], status="succeeded")
    recovered = await server.handle_reboot_operations(_Request())
    assert json.loads(recovered.text)["operations"][0]["status"] == "completed"


def test_live_phone_resolver_uses_authoritative_pcm_and_same_session_history(tmp_path):
    from orchestrator.bridge_memory import BridgeContextAssembler, BridgeMemoryStore
    from orchestrator.hcc import set_hcc_enabled
    from orchestrator.memory_plus_mode import (
        append_memory_plus_manual_note,
        set_memory_plus_enabled,
    )
    from orchestrator.pcm import render_pcm_document
    from orchestrator.phone_manager import PhoneManager

    server, runtime = _server(tmp_path)
    workspace = tmp_path / "phone-agent"
    workspace.mkdir()
    (workspace / "agent.md").write_text(
        render_pcm_document(
            persona="PERSONA_PHONE_SENTINEL",
            system="PERMANENT_SYS_PHONE_SENTINEL",
            memory="LONG_MEMORY_PHONE_SENTINEL",
            hcc="FULL_HCC_PHONE_SENTINEL",
        ),
        encoding="utf-8",
    )
    set_hcc_enabled(workspace, True)
    set_memory_plus_enabled(workspace, True)
    runtime.workspace_dir = workspace
    runtime.phone_manager = PhoneManager(workspace)
    runtime.context_assembler = BridgeContextAssembler(
        BridgeMemoryStore(workspace),
        workspace / "agent.md",
        sys_prompt_manager=SimpleNamespace(
            get_active_texts=lambda: ["LOCAL_SYS_PHONE_SENTINEL"]
        ),
        global_sys_prompt_manager=SimpleNamespace(
            get_active_entries=lambda: [
                {"slot": 1, "text": "GLOBAL_SYS_PHONE_SENTINEL"}
            ]
        ),
    )

    owner_id = "owner:phone"
    session = server.session_store.create_session(
        owner_id=owner_id,
        agent_id="lily",
        title="Phone continuity",
    )
    server.session_store.bind_primary_session(
        owner_id=owner_id, agent_id="lily", session_id=session["session_id"]
    )
    accepted = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id=owner_id,
        agent_id="lily",
        request_id="phone-history-request",
        text="RECENT_USER_PHONE_SENTINEL",
        source="session-api",
        idempotency_key="phone-history-run",
    )
    server.session_store.mark_request_running(
        accepted.request_id,
        worker_id="test-worker",
    )
    server.session_store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="RECENT_ASSISTANT_PHONE_SENTINEL",
        assistant_source="test-backend",
    )
    activity = server.session_store.ensure_agent_activity_session(
        owner_id=owner_id,
        agent_id="lily",
    )
    activity_run = server.session_store.accept_run(
        session_id=activity["session_id"],
        owner_id=owner_id,
        agent_id="lily",
        request_id="phone-activity-request",
        text="SCHEDULER_PROMPT_MUST_NOT_ENTER_PHONE",
        source="scheduler",
        idempotency_key="phone-activity-run",
    )
    server.session_store.mark_request_running(
        activity_run.request_id,
        worker_id="scheduler-worker",
    )
    server.session_store.finish_request(
        activity_run.request_id,
        success=True,
        assistant_text="TODAY_COMPLETED_BACKGROUND_RESULT_SENTINEL",
        assistant_source="test-backend",
    )
    server.session_store.append_presentation_message(
        session_id=activity["session_id"],
        owner_id=owner_id,
        agent_id="lily",
        role="assistant",
        text="METER_AND_PROGRESS_NOISE_MUST_NOT_ENTER_PHONE",
        source="meter-cost",
        idempotency_key="phone-meter-noise",
        presentation_channel="meter",
    )
    session_workspace = server.session_store.session_workspace(
        session["session_id"],
        int(session["context_generation"]),
    )
    append_memory_plus_manual_note(
        session_workspace,
        "MEMORY_PLUS_PHONE_SENTINEL",
    )

    resolved = server._resolve_live_voice_phone_session(
        "lily",
        owner_id=owner_id,
        session_id=session["session_id"],
        context_generation=int(session["context_generation"]),
    )
    assert "PERMANENT_SYS_PHONE_SENTINEL" in resolved["instructions"]
    assert "GLOBAL_SYS_PHONE_SENTINEL" in resolved["instructions"]
    assert "LOCAL_SYS_PHONE_SENTINEL" in resolved["instructions"]
    assert "PERSONA_PHONE_SENTINEL" in resolved["instructions"]
    startup = json.dumps(resolved["input"], ensure_ascii=False)
    assert "FULL_HCC_PHONE_SENTINEL" in startup
    assert "LONG_MEMORY_PHONE_SENTINEL" in startup
    assert "MEMORY_PLUS_PHONE_SENTINEL" in startup
    assert "RECENT_USER_PHONE_SENTINEL" in startup
    assert "RECENT_ASSISTANT_PHONE_SENTINEL" in startup
    assert "TODAY_COMPLETED_BACKGROUND_RESULT_SENTINEL" in startup
    assert "SCHEDULER_PROMPT_MUST_NOT_ENTER_PHONE" not in startup
    assert "METER_AND_PROGRESS_NOISE_MUST_NOT_ENTER_PHONE" not in startup

    # Recovery does not consult next-call configuration before PCM is assembled.
    from unittest.mock import patch
    runtime.phone_manager.set_language("en")
    runtime.phone_manager.set_provider("hy-simulmt")
    with patch.object(runtime.phone_manager, "_read", side_effect=AssertionError("next-call settings were read")):
        recovered = server._resolve_live_voice_phone_session(
            "lily", owner_id=owner_id, session_id=session["session_id"],
            context_generation=int(session["context_generation"]),
            frozen_selection=resolved["selection"],
        )
    assert recovered["public"]["language"] == resolved["public"]["language"]
    assert recovered["public"]["provider"] == resolved["public"]["provider"]
    assert "GLOBAL_SYS_PHONE_SENTINEL" in recovered["instructions"]
    assert "MEMORY_PLUS_PHONE_SENTINEL" in json.dumps(recovered["input"], ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("absolute,large", [(False, False), (True, False), (True, True)])
async def test_session_workzone_reference_stages_committed_managed_bytes(tmp_path, monkeypatch, absolute, large):
    from orchestrator.frontend_delivery import tui_run_delivery_policy

    server, runtime = _server(tmp_path)
    zone = tmp_path / "zone"
    zone.mkdir()
    content = b"x" * (26 * 1024 * 1024) if large else b"inside the workzone"
    (zone / "report.txt").write_bytes(content)
    runtime._workzone_state = {
        "revision": 1,
        "slots": [
            {"slot_id": "main", "path": str(zone), "enabled": True,
             "available": True}
        ],
    }
    owner = server._v1_owner_id(_Request())
    session = server.session_store.resolve_primary_session(
        owner_id=owner, agent_id="lily", establish=True
    )
    monkeypatch.setattr(
        server.session_store, "upload_attachment_bytes",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("ordinary file must stream")),
    )
    monkeypatch.setattr(
        server, "_resolve_workzone_attachment",
        lambda *_args: (_ for _ in ()).throw(AssertionError("canonical staging must not buffer")),
    )
    response = await server.handle_v1_workzone_attachment_stage(
        _Request(
            {"reference": str(zone / "report.txt") if absolute else "report.txt"},
            match_info={"session_id": session["session_id"]},
        )
    )

    assert response.status == 201
    attachment = json.loads(response.text)["attachment"]
    assert attachment["state"] == "committed"
    stored, data = server.session_store.attachment_bytes(
        session_id=session["session_id"], owner_id=owner,
        attachment_id=attachment["attachment_id"],
    )
    assert data == content
    assert stored["sha256"] == hashlib.sha256(content).hexdigest()
    assert runtime.enqueue_request_calls == 0

    run_response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "tui-workzone-one-turn",
                "surface": "tui",
                "client_id": "tui-window-1",
                "delivery_policy": tui_run_delivery_policy(
                    telegram_mirror=False, client_id="tui-window-1"
                ),
                "message": {"content": [
                    {"type": "text", "text": "read this"},
                    {"type": "attachment", "attachment_id": attachment["attachment_id"]},
                ]},
            },
            match_info={"session_id": session["session_id"]},
        )
    )
    assert run_response.status == 202, json.loads(run_response.text)
    assert runtime.enqueue_request_calls == 1
    assert [
        part.get("attachment_id")
        for part in runtime.last_request_content["parts"]
        if part["type"] == "media"
    ] == [attachment["attachment_id"]]


@pytest.mark.asyncio
async def test_session_workzone_reference_rejects_traversal_before_staging(tmp_path):
    server, runtime = _server(tmp_path)
    zone = tmp_path / "zone"
    zone.mkdir()
    (tmp_path / "outside.txt").write_text("not allowed", encoding="utf-8")
    runtime._workzone_state = {
        "revision": 1,
        "slots": [
            {"slot_id": "main", "path": str(zone), "enabled": True,
             "available": True}
        ],
    }
    owner = server._v1_owner_id(_Request())
    session = server.session_store.resolve_primary_session(
        owner_id=owner, agent_id="lily", establish=True
    )
    response = await server.handle_v1_workzone_attachment_stage(
        _Request(
            {"reference": "../outside.txt"},
            match_info={"session_id": session["session_id"]},
        )
    )
    assert response.status == 400
    assert json.loads(response.text)["ok"] is False


@pytest.mark.asyncio
async def test_session_workzone_reference_requires_session_owner_before_file_read(
    tmp_path, monkeypatch
):
    server, _runtime = _server(tmp_path)
    monkeypatch.setattr(server, "_v1_owner_id", lambda request: None)
    monkeypatch.setattr(
        server, "_open_workzone_attachment",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("unowned Workzone must not be read")
        ),
    )
    response = await server.handle_v1_workzone_attachment_stage(
        _Request(
            {"reference": "report.txt"},
            match_info={"session_id": "ses-unowned"},
        )
    )
    assert response.status == 401
    assert json.loads(response.text)["ok"] is False


@pytest.mark.asyncio
async def test_tui_speech_generates_asset_without_enqueue_or_connector_send(tmp_path):
    server, runtime = _server(tmp_path)
    response = await server.handle_tui_speech(
        _Request({
            "agent": "lily",
            "text": "read this",
            "request_id": "say-1",
            "source_language": "ja-JP",
        })
    )
    payload = json.loads(response.text)

    assert response.status == 200, response.text
    assert payload["ok"] is True
    assert base64.b64decode(payload["content_b64"]).startswith(b"OggS")
    assert payload["spoken"] == "read this"
    assert payload["source_language"] == "ja-JP"
    assert payload["provider"] == "windows"
    assert payload["fallback_from"] == "edge"
    assert payload["fallback_reason_code"] == "tts_protocol_rejected"
    assert runtime.api_delivery_flags == []
    assert runtime.api_media_calls == []


@pytest.mark.asyncio
async def test_tui_speech_failure_returns_safe_provider_reasons(tmp_path):
    server, runtime = _server(tmp_path)

    async def fail_speech(_text, _request_id, _source_language=None):
        raise VoiceSynthesisError(
            "tts_all_providers_failed",
            (
                "Edge TTS rejected the speech request (HTTP 403). "
                "Automatic Windows TTS fallback also failed: "
                "Windows TTS has no installed voice for language: ja."
            ),
            failures=(
                VoiceProviderFailure(
                    "edge",
                    "tts_protocol_rejected",
                    "Edge TTS rejected the speech request (HTTP 403).",
                ),
                VoiceProviderFailure(
                    "windows",
                    "tts_voice_unavailable",
                    "Windows TTS has no installed voice for language: ja.",
                ),
            ),
        )

    runtime.synthesize_tui_speech = fail_speech
    response = await server.handle_tui_speech(
        _Request(
            {
                "agent": "lily",
                "text": "read this",
                "request_id": "say-failed",
            }
        )
    )
    payload = json.loads(response.text)

    assert response.status == 503
    assert payload["code"] == "tts_all_providers_failed"
    assert "HTTP 403" in payload["error"]
    assert "language: ja" in payload["error"]
    assert [failure["code"] for failure in payload["provider_failures"]] == [
        "tts_protocol_rejected",
        "tts_voice_unavailable",
    ]


@pytest.mark.asyncio
async def test_tui_voice_profile_uses_existing_agent_owner(tmp_path):
    server, _runtime = _server(tmp_path)
    response = await server.handle_tui_voice(
        _Request({"agent": "lily", "profile": "calm_male"})
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["profile"] == "calm_male"
    assert payload["profiles"] == [{"id": "calm_male", "label": "Selected"}]


@pytest.mark.asyncio
async def test_unqualified_session_api_is_not_advertised(tmp_path):
    server, _runtime = _server(tmp_path)

    capabilities = json.loads((await server.handle_v1_capabilities(_Request())).text)
    unavailable = await server.handle_v1_session_not_ready(_Request())

    assert "session_api_version" not in capabilities
    assert capabilities["message_source"]["external_declarations_supported"] is True
    assert capabilities["private_authorization"]["proof_type"] == (
        "hashi.private-authorization-proof"
    )
    assert capabilities["private_authorization"]["raw_secret_on_wire"] is False
    assert unavailable.status == 503
    assert json.loads(unavailable.text)["code"] == "session_api_not_ready"
    assert "disabled in the instance settings" in json.loads(unavailable.text)["error"]


@pytest.mark.asyncio
async def test_personal_instance_enables_standard_frontend_attachments_by_default(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HASHI_ATTACHMENT_STORAGE_WAIT_MS", "2400")
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps({"global": {}, "agents": []}),
        encoding="utf-8",
    )
    secrets_path.write_text("{}", encoding="utf-8")
    global_config, _agents, _secrets = ConfigManager(
        config_path,
        secrets_path,
        bridge_home=tmp_path,
        code_root=tmp_path,
    ).load()
    server, _runtime = _server(tmp_path)
    server.global_config = global_config

    capabilities = json.loads(
        (await server.handle_v1_capabilities(_Request())).text
    )

    assert global_config.persistent_session_v1 is True
    assert capabilities["session_api_version"] == "1.0"
    assert capabilities["agent_history_search"]["max_query_characters"] == 16384
    assert capabilities["agent_history_search"]["max_request_bytes"] >= 16384 * 12
    assert capabilities["frontend_connector"]["multi_attachment"] is True
    assert capabilities["frontend_connector"]["atomic_run_admission"] is True
    assert capabilities["frontend_connector"]["attachment_stage_idempotency"] is True
    assert capabilities["frontend_connector"]["attachment_storage_wait_ms"] == 2400
    assert capabilities["frontend_connector"]["feed"] == {
        "version": "2.0",
        "transport": "cursor-polling",
        "durable": True,
        "ephemeral": True,
        "answer_preview": True,
    }
    assert capabilities["frontend_contract_versions"] == {
        "ingress": 2,
        "delivery_intent": 2,
        "delivery_receipt": 1,
        "media_group": 1,
        "command_invocation": 2,
        "relay_envelope": 1,
        "tool_interaction": 1,
    }
    assert capabilities["frontend_connector"]["event_source"] == "persistent_session_events"
    connector_ids = {
        connector["id"]
        for connector in capabilities["frontend_connector_registry"]["connectors"]
    }
    assert {"telegram", "tui", "backend_api", "session_api", "hchat", "remote", "exchange"} <= connector_ids


@pytest.mark.asyncio
async def test_v2_frontend_capability_endpoint_is_discoverable_and_keeps_v1_compatibility(
    tmp_path,
):
    server, _runtime = _server(tmp_path)
    server.global_config.persistent_session_v1 = True

    response = await server.handle_v2_frontend_capabilities(_Request())
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["frontend_contract_protocol"] == {
        "type": "hashi.frontend-contracts",
        "version": 2,
        "event_source": "persistent_session_events",
    }
    assert payload["frontend_connector_registry"]["version"] == 3
    legacy = json.loads((await server.handle_v1_capabilities(_Request())).text)
    assert "frontend_contract_protocol" not in legacy
    assert legacy["session_api_version"] == "1.0"


def test_personal_instance_can_explicitly_opt_out_of_persistent_session(tmp_path):
    config_path = tmp_path / "agents.json"
    secrets_path = tmp_path / "secrets.json"
    config_path.write_text(
        json.dumps(
            {"global": {"persistent_session_v1": False}, "agents": []}
        ),
        encoding="utf-8",
    )
    secrets_path.write_text("{}", encoding="utf-8")

    global_config, _agents, _secrets = ConfigManager(
        config_path,
        secrets_path,
        bridge_home=tmp_path,
        code_root=tmp_path,
    ).load()

    assert global_config.persistent_session_v1 is False


@pytest.mark.asyncio
async def test_qualified_capability_is_client_neutral_and_limit_driven(
    tmp_path, monkeypatch
):
    from orchestrator import workbench_api

    server, _runtime = _server(tmp_path)
    monkeypatch.setattr(workbench_api, "PERSISTENT_SESSION_V1_QUALIFIED", True)
    server.global_config.persistent_session_v1 = True

    capabilities = json.loads((await server.handle_v1_capabilities(_Request())).text)

    assert capabilities["compatibility_policy"] == "capabilities-and-advertised-limits"
    assert capabilities["limits"] == {
        "max_message_chars": 200000,
        "max_attachments_per_message": 16,
        "max_attachment_bytes": 64 * 1024 * 1024,
        "max_total_attachment_bytes_per_message": 64 * 1024 * 1024,
        "max_sessions_page_size": 100,
        "max_messages_page_size": 200,
        "max_events_page_size": 2000,
        "agent_history": {
            "version": "1.0",
            "scope": "owner_agent",
            "includes_archived_sessions": True,
            "opaque_cursor": True,
            "max_page_size": 200,
        },
    }
    assert not any(
        "client" in str(value).lower() and "specific" in str(value).lower()
        for value in capabilities.values()
    )


@pytest.mark.asyncio
async def test_frontend_connector_admits_ordered_multi_attachment_as_one_run(
    tmp_path, monkeypatch
):
    from orchestrator import workbench_api

    server, runtime = _server(tmp_path)
    monkeypatch.setattr(workbench_api, "PERSISTENT_SESSION_V1_QUALIFIED", True)
    server.global_config.persistent_session_v1 = True
    server.global_config.native_audio_chat_v1 = False

    capabilities = json.loads((await server.handle_v1_capabilities(_Request())).text)
    connector = capabilities["frontend_connector"]
    assert connector["version"] == "1.2"
    assert connector["message_display_projection"] == {
        "version": "1.0",
        "field": "message.display_text",
        "canonical_input": "message.content",
        "binding": "server-message-id",
        "fallback": "canonical-text",
    }
    assert connector["multi_attachment"] is True
    assert connector["assistant_multi_attachment"] is True
    assert connector["assistant_attachment_delivery"] == "terminal-message-projection"
    assert connector["atomic_run_admission"] is True
    assert connector["content_types"] == ["text", "attachment", "audio"]
    assert connector["attachment_modalities"] == [
        "image",
        "audio",
        "video",
        "document",
    ]

    created = json.loads(
        (
            await server.handle_v1_sessions_create(
                _Request({"agent_id": "lily", "title": "Multi attachment"})
            )
        ).text
    )
    session_id = created["session"]["session_id"]
    fixtures = [
        ("first.png", "image/png", b"\x89PNG\r\n\x1a\nfirst"),
        ("notes.txt", "text/plain", b"second"),
        ("clip.webm", "video/webm", bytes.fromhex("1a45dfa3") + b"third"),
    ]
    attachment_ids = []
    for filename, media_type, body in fixtures:
        staged_response = await server.handle_v1_attachment_stage(
            _Request(
                {
                    "filename": filename,
                    "media_type": media_type,
                    "size_bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                },
                match_info={"session_id": session_id},
            )
        )
        assert staged_response.status == 201
        staged = json.loads(staged_response.text)["attachment"]
        assert staged["upload_required"] is True
        attachment_id = staged["attachment_id"]
        attachment_ids.append(attachment_id)

        uploaded_response = await server.handle_v1_attachment_upload(
            _Request(
                match_info={
                    "session_id": session_id,
                    "attachment_id": attachment_id,
                },
                headers={"Content-Type": media_type},
                body=body,
            )
        )
        assert uploaded_response.status == 200
        committed_response = await server.handle_v1_attachment_commit(
            _Request(
                match_info={
                    "session_id": session_id,
                    "attachment_id": attachment_id,
                }
            )
        )
        assert committed_response.status == 200

    run_response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "multi-attachment-one-turn",
                "surface": "generic-frontend-test",
                "message": {
                    "content": [
                        {"type": "text", "text": "inspect these in order"},
                        *[
                            {"type": "attachment", "attachment_id": attachment_id}
                            for attachment_id in attachment_ids
                        ],
                    ]
                },
            },
            match_info={"session_id": session_id},
            headers={"X-Client-Id": "connector-contract-test"},
        )
    )
    payload = json.loads(run_response.text)

    assert run_response.status == 202, payload
    assert payload["request_id"] == "req-api"
    assert runtime.enqueue_request_calls == 1
    assert [
        part.get("attachment_id")
        for part in runtime.last_request_content["parts"]
        if part["type"] == "media"
    ] == attachment_ids
    messages = server.session_store.messages(session_id, owner_id="user:7")
    assert len(messages) == 1
    assert [part["type"] for part in messages[0]["content"]] == [
        "text",
        "attachment",
        "attachment",
        "attachment",
    ]


@pytest.mark.asyncio
async def test_long_canvas_attachment_is_readable_in_full_from_target_workspace(tmp_path):
    from tools.builtins import BuiltinExecutionResult, execute_file_read

    server, runtime = _server(tmp_path)
    runtime.workspace_dir = tmp_path / "target-workspace"
    runtime.workspace_dir.mkdir()
    created = json.loads((await server.handle_v1_sessions_create(
        _Request({"agent_id": "lily", "title": "Long Canvas"})
    )).text)
    session_id = created["session"]["session_id"]
    text = "Canvas full selection: " + ("选🎨 and literal paths /tmp/example\n" * 9000) + "END"
    payload = text.encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    stage = await server.handle_v1_attachment_stage(_Request({
        "filename": "canvas-context.txt", "media_type": "text/plain",
        "size_bytes": len(payload), "sha256": digest, "idempotency_key": "canvas-stage-one",
    }, match_info={"session_id": session_id}))
    assert stage.status == 201
    attachment_id = json.loads(stage.text)["attachment"]["attachment_id"]
    match = {"session_id": session_id, "attachment_id": attachment_id}
    upload = await server.handle_v1_attachment_upload(_Request(
        match_info=match, headers={"Content-Type": "text/plain"}, body=payload))
    assert upload.status == 200
    assert (await server.handle_v1_attachment_commit(_Request(match_info=match))).status == 200
    result = await server.handle_v1_session_runs_create(_Request({
        "idempotency_key": "canvas-run-one", "surface": "workbench",
        "message": {"content": [
            {"type": "text", "text": "Read the full Canvas attachment."},
            {"type": "attachment", "attachment_id": attachment_id},
        ]},
    }, match_info={"session_id": session_id}))
    assert result.status == 202, result.text
    assert runtime.enqueue_request_calls == 1
    part = next(item for item in runtime.last_request_content["parts"]
                if item.get("attachment_id") == attachment_id)
    attachment = Path(part["local_ref"])
    assert attachment.parents[2].name == ".attachments"
    workspace = attachment.parents[3]
    assert workspace.is_relative_to(tmp_path)
    assert hashlib.sha256(attachment.read_bytes()).hexdigest() == digest

    chunks = []
    cursor = None
    while True:
        args = {"path": str(attachment)}
        if cursor:
            args["cursor"] = cursor
        page = await execute_file_read(args, workspace, workspace)
        assert isinstance(page, BuiltinExecutionResult)
        content = page.output.partition("\n")[2]
        cursor = page.details["file_read_continuation"]
        if cursor:
            marker = f"\n[truncated by HASHI; next_cursor={cursor}]"
            assert content.endswith(marker)
            content = content[:-len(marker)]
        chunks.append(content)
        if not cursor:
            break
    assert len(chunks) > 1
    assert "".join(chunks) == text


@pytest.mark.asyncio
async def test_frontend_connector_rejects_multi_attachment_atomically(tmp_path):
    server, runtime = _server(tmp_path)
    created = json.loads(
        (
            await server.handle_v1_sessions_create(_Request({"agent_id": "lily"}))
        ).text
    )
    session_id = created["session"]["session_id"]

    response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "invalid-multi-attachment",
                "message": {
                    "content": [
                        {"type": "text", "text": "do not partially admit"},
                        {"type": "attachment", "attachment_id": "att_missing_one"},
                        {"type": "attachment", "attachment_id": "att_missing_two"},
                    ]
                },
            },
            match_info={"session_id": session_id},
        )
    )

    assert response.status == 404
    assert runtime.enqueue_request_calls == 0
    assert server.session_store.messages(session_id, owner_id="user:7") == []


@pytest.mark.asyncio
async def test_primary_session_endpoint_resolves_shared_tui_conversation(tmp_path):
    server, runtime = _server(tmp_path)
    shared = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    response = await server.handle_v1_agent_primary_session(
        _Request(match_info={"agent_id": "lily"})
    )
    payload = json.loads(response.text)
    assert response.status == 200
    assert payload["session"]["session_id"] == shared["session_id"]
    assert runtime.enqueue_request_calls == 0


@pytest.mark.asyncio
async def test_tui_session_ingress_capability_is_explicit_when_ready(tmp_path):
    server, _runtime = _server(tmp_path)
    server.global_config.persistent_session_v1 = True

    response = await server.handle_v1_capabilities(_Request())
    payload = json.loads(response.text)

    assert payload["tui_session_ingress"] == {
        "version": 1,
        "primary_session": True,
        "text_runs": True,
        "attachment_runs": True,
        "workzone_attachment_runs": True,
        "command_invocations": True,
    }


@pytest.mark.asyncio
async def test_session_api_command_invocation_dispatches_with_bound_session_metadata(
    tmp_path, monkeypatch
):
    from orchestrator import command_interaction_bridge, slash_command_audit

    server, _runtime = _command_server(tmp_path)
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    seen = {}
    dispatch_calls = []

    monkeypatch.setattr(
        slash_command_audit,
        "is_supported_slash_command",
        lambda _runtime, command: command == "model",
    )

    async def dispatch(_runtime, payload, metadata):
        dispatch_calls.append(1)
        seen["payload"] = payload
        seen["metadata"] = metadata
        return {
            "ok": True,
            "http_status": 200,
            "result": {
                "type": "hashi.steer-receipt",
                "version": 2,
                "old_run": {"run_id": "run-old"},
                "new_run": {"run_id": "run-new", "parent_run_id": "run-old"},
                "continuation": {"evidence_ref": "herv3:turn-old"},
            },
        }

    monkeypatch.setattr(
        command_interaction_bridge, "dispatch_command_interaction", dispatch
    )
    request_payload = {
        "command": "model",
        "arguments": ["balanced"],
        "client_id": "session-api-client-7",
        "request_id": "command-request-123456",
        "ui_locale": "zh-CN",
        "context_generation": session["context_generation"],
    }
    response = await server.handle_v1_session_command_invocation(
        _command_request(
            request_payload,
            match_info={"session_id": session["session_id"]},
        )
    )

    result = json.loads(response.text)
    assert response.status == 200, response.text
    assert result["slash_command"] is True
    assert result["session_id"] == session["session_id"]
    assert seen["payload"]["op"] == "open"
    assert seen["payload"]["command"] == "/model balanced"
    assert seen["metadata"]["connector_id"] == "session_api"
    assert seen["metadata"]["actor_id"] == "user:7"
    assert seen["metadata"]["_hashi_owner_id"] == "user:7"
    assert seen["metadata"]["session_surface"] == "session-api"
    assert seen["metadata"]["ingress_transport"] == "session-api-command"
    assert seen["metadata"]["source_channel"] == "session_api_command"
    assert seen["metadata"]["fc_compatibility_adapter_id"] == "session_api.command"
    assert seen["metadata"]["context_generation"] == session["context_generation"]
    assert result["command_invocation"]["type"] == "hashi.frontend-command"
    assert result["command_invocation"]["connector_id"] == "session_api"
    assert result["result"]["type"] == "hashi.steer-receipt"
    assert result["result"]["old_run"]["run_id"] == "run-old"
    assert result["result"]["new_run"]["parent_run_id"] == "run-old"
    assert result["result"]["continuation"]["evidence_ref"] == "herv3:turn-old"
    assert result["replayed"] is False
    events = server.session_store.events(
        session["session_id"], owner_id="user:7"
    )
    command_events = [event for event in events if event["kind"] == "frontend.command_result"]
    assert len(command_events) == 1
    assert command_events[0]["event_id"] == result["command_event_id"]
    assert command_events[0]["detail"]["command_invocation"] == result["command_invocation"]

    from orchestrator.session_store import SessionStore

    server.session_store = SessionStore(
        server.session_store.db_path, instance_id="HASHI1"
    )
    replay = await server.handle_v1_session_command_invocation(
        _command_request(
            request_payload,
            match_info={"session_id": session["session_id"]},
        )
    )
    replayed = json.loads(replay.text)
    assert replay.status == 200
    assert replayed["replayed"] is True
    assert replayed["command_event_id"] == result["command_event_id"]
    assert dispatch_calls == [1]


@pytest.mark.asyncio
async def test_session_command_uses_authenticated_owner_without_telegram(tmp_path, monkeypatch):
    from orchestrator import command_interaction_bridge, slash_command_audit

    server, _runtime = _command_server(tmp_path)
    server.global_config.authorized_id = 0
    server.global_config.owner_scope_id = "local:alice"
    session = server.session_store.resolve_primary_session(
        owner_id="local:alice", agent_id="lily", establish=True
    )
    seen = {}
    monkeypatch.setattr(slash_command_audit, "is_supported_slash_command", lambda *_: True)

    async def dispatch(_runtime, _payload, metadata):
        seen.update(metadata)
        return {"ok": True, "http_status": 200}

    monkeypatch.setattr(command_interaction_bridge, "dispatch_command_interaction", dispatch)
    request = {
        "command": "model", "arguments": [], "client_id": "session-client-123456",
        "request_id": "request-without-telegram-123456",
        "context_generation": session["context_generation"],
    }
    response = await server.handle_v1_session_command_invocation(
        _command_request(request, match_info={"session_id": session["session_id"]})
    )
    assert response.status == 200, response.text
    assert seen["actor_id"] == "local:alice"
    assert seen["_hashi_owner_id"] == "local:alice"

    foreign_session = server.session_store.create_session(owner_id="local:bob", agent_id="lily")
    rejected = await server.handle_v1_session_command_invocation(
        _command_request({**request, "request_id": "foreign-request-123456"},
                 match_info={"session_id": foreign_session["session_id"]})
    )
    assert rejected.status in {403, 404}


@pytest.mark.asyncio
async def test_session_api_sys_accepts_shared_limit_and_preserves_exact_prompt(
    tmp_path, monkeypatch
):
    from orchestrator import command_interaction_bridge, slash_command_audit
    from orchestrator.sys_command_contract import MAX_SYS_PROMPT_CHARS

    server, _runtime = _command_server(tmp_path)
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    monkeypatch.setattr(
        slash_command_audit,
        "is_supported_slash_command",
        lambda _runtime, command: command == "sys",
    )
    seen = {}

    async def dispatch(_runtime, payload, metadata):
        seen["payload"] = payload
        seen["metadata"] = metadata
        return {"ok": True, "http_status": 200}

    monkeypatch.setattr(
        command_interaction_bridge, "dispatch_command_interaction", dispatch
    )
    prefix = '  indented first line\n\n"quoted" C:\\Prompt\\rule\n'
    suffix = "  "
    prompt = (
        prefix
        + "🙂" * (MAX_SYS_PROMPT_CHARS - len(prefix) - len(suffix))
        + suffix
    )
    arguments = ["3", "save", prompt]
    response = await server.handle_v1_session_command_invocation(
        _command_request(
            {
                "command": "sys",
                "arguments": arguments,
                "client_id": "session-api-client-7",
                "request_id": "sys-command-request-123456",
                "ui_locale": "zh-CN",
                "context_generation": session["context_generation"],
            },
            match_info={"session_id": session["session_id"]},
        )
    )

    result = json.loads(response.text)
    assert response.status == 200, response.text
    assert len(prompt) == MAX_SYS_PROMPT_CHARS
    assert seen["payload"]["command"] == f"/sys 3 save {prompt}"
    assert seen["metadata"]["frontend_operation"]["arguments"] == arguments
    assert result["command_invocation"]["arguments"] == ["[redacted]"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["x"] * 65, id="argument-count"),
        pytest.param(["x" * 4097], id="single-argument-length"),
    ],
)
async def test_session_api_non_sys_commands_keep_existing_argument_limits(
    tmp_path, arguments
):
    server, _runtime = _command_server(tmp_path)
    response = await server.handle_v1_session_command_invocation(
        _command_request(
            {
                "command": "model",
                "arguments": arguments,
                "client_id": "session-api-client-7",
                "request_id": "model-command-request-123456",
                "ui_locale": "en",
                "context_generation": 1,
            },
            match_info={"session_id": "not-reached"},
        )
    )

    result = json.loads(response.text)
    assert response.status == 400
    assert result["error"] == "command arguments are invalid"


@pytest.mark.asyncio
async def test_session_api_sys_oversize_uses_shared_typed_error(tmp_path):
    from orchestrator.sys_command_contract import MAX_SYS_PROMPT_CHARS

    server, _runtime = _command_server(tmp_path)
    response = await server.handle_v1_session_command_invocation(
        _command_request(
            {
                "command": "sys",
                "arguments": ["3", "save", "🙂" * (MAX_SYS_PROMPT_CHARS + 1)],
                "client_id": "session-api-client-7",
                "request_id": "sys-command-request-oversize",
                "ui_locale": "en",
                "context_generation": 1,
            },
            match_info={"session_id": "not-reached"},
        )
    )

    result = json.loads(response.text)
    assert response.status == 413
    assert result["code"] == "sys_prompt_too_large"
    assert str(MAX_SYS_PROMPT_CHARS) in result["error"]


@pytest.mark.asyncio
async def test_signed_tui_sys_command_relay_preserves_text_and_connector_identity(
    tmp_path, monkeypatch
):
    from orchestrator import command_interaction_bridge, slash_command_audit
    from orchestrator.message_context import seal_connector_evidence

    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "test-network-secret"}),
        encoding="utf-8",
    )
    server, _runtime = _command_server(tmp_path)
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    monkeypatch.setattr(
        slash_command_audit,
        "is_supported_slash_command",
        lambda _runtime, command: command == "sys",
    )
    seen = {}

    async def dispatch(_runtime, payload, metadata):
        seen["payload"] = payload
        seen["metadata"] = metadata
        return {"ok": True, "http_status": 200}

    monkeypatch.setattr(
        command_interaction_bridge, "dispatch_command_interaction", dispatch
    )
    prompt = '  first line\n\n"quoted" C:\\Prompt\\rule\t  '
    arguments = ["4", "save", prompt]
    prompt_binding = json.dumps(
        {"command": "sys", "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence = seal_connector_evidence(
        tmp_path,
        claims={
            "_message_source_reserved": "tui",
            "_origin_instance_evidence": {
                "id": "HASHI2",
                "assurance": "shared_network_hmac",
            },
        },
        prompt=prompt_binding,
    )

    response = await server.handle_v1_session_command_invocation(
        _command_request(
            {
                "command": "sys",
                "arguments": arguments,
                "client_id": "tui-window-client-7",
                "request_id": "tui-command-request-123456",
                "ui_locale": "en",
                "context_generation": session["context_generation"],
                "request_metadata": {"_connector_evidence": evidence},
            },
            match_info={"session_id": session["session_id"]},
        )
    )

    result = json.loads(response.text)
    assert response.status == 200, response.text
    assert seen["metadata"]["connector_id"] == "tui"
    assert seen["metadata"]["session_surface"] == "tui"
    assert seen["metadata"]["ingress_transport"] == "tui-session-command"
    assert seen["metadata"]["source_channel"] == "tui_session_command"
    assert seen["metadata"]["fc_compatibility_adapter_id"] == "tui.command"
    assert seen["metadata"]["origin_instance"] == "HASHI2"
    assert seen["payload"]["command"] == f"/sys 4 save {prompt}"
    assert result["command_invocation"]["connector_id"] == "tui"
    assert result["command_invocation"]["arguments"] == ["[redacted]"]


@pytest.mark.asyncio
async def test_tui_command_invocation_rejects_stale_session_generation(
    tmp_path, monkeypatch
):
    from orchestrator import command_interaction_bridge, slash_command_audit

    server, _runtime = _command_server(tmp_path)
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    monkeypatch.setattr(
        slash_command_audit, "is_supported_slash_command", lambda *_args: True
    )
    monkeypatch.setattr(
        command_interaction_bridge,
        "dispatch_command_interaction",
        lambda *_args: pytest.fail("stale command must not dispatch"),
    )
    response = await server.handle_v1_session_command_invocation(
        _command_request(
            {
                "command": "model",
                "arguments": [],
                "client_id": "tui-window-client-7",
                "request_id": "command-request-123456",
                "ui_locale": "en",
                "context_generation": session["context_generation"] + 1,
            },
            match_info={"session_id": session["session_id"]},
        )
    )

    assert response.status == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("untrusted", [
    {"message_source": "telegram"},
    {"_hashi_owner_id": "user:other", "owner_id": "user:other"},
])
async def test_tui_command_invocation_rejects_untrusted_request_metadata(tmp_path, untrusted):
    server, _runtime = _command_server(tmp_path)
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    response = await server.handle_v1_session_command_invocation(
        _command_request(
            {
                "command": "model",
                "arguments": [],
                "client_id": "tui-window-7",
                "request_id": "command-request-123456",
                "ui_locale": "en",
                "context_generation": session["context_generation"],
                "request_metadata": untrusted,
            },
            match_info={"session_id": session["session_id"]},
        )
    )

    assert response.status == 400


@pytest.mark.asyncio
async def test_session_run_tui_policy_is_validated_but_cannot_override_central_mirror(tmp_path):
    from orchestrator.frontend_delivery import (
        telegram_delivery_for_admission,
        tui_run_delivery_policy,
    )
    from orchestrator.message_context import resolve_message_source_fact

    server, runtime = _server(tmp_path)
    shared = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    policy = tui_run_delivery_policy(
        telegram_mirror=False, client_id="tui-window-7"
    )
    response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "tui-message-1",
                "surface": "tui",
                "client_id": "tui-window-7",
                "ui_locale": "zh-CN",
                "delivery_policy": policy,
                "message": {"content": [{"type": "text", "text": "hello"}]},
            },
            match_info={"session_id": shared["session_id"]},
        )
    )
    payload = json.loads(response.text)
    assert response.status == 202, payload
    assert runtime.last_request_metadata["frontend_client"] == {
        "kind": "tui", "client_id": "tui-window-7"
    }
    assert "frontend_delivery_policy" not in runtime.last_request_metadata
    assert "frontend_delivery_policy" not in runtime.last_request_metadata["response_preferences"]
    assert runtime.last_request_metadata["ui_locale"] == "zh-CN"
    assert runtime.last_source == "tui"
    assert telegram_delivery_for_admission(
        source=runtime.last_source,
        request_metadata=runtime.last_request_metadata,
        state_root=tmp_path,
    ) is True
    assert resolve_message_source_fact(
        source=runtime.last_source,
        chat_id=123,
        metadata=runtime.last_request_metadata,
    )["id"] == "tui"

    invalid = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "tui-message-2",
                "surface": "tui",
                "client_id": "different-client",
                "delivery_policy": policy,
                "message": {"content": [{"type": "text", "text": "reject"}]},
            },
            match_info={"session_id": shared["session_id"]},
        )
    )
    assert invalid.status == 400
    assert runtime.enqueue_request_calls == 1


@pytest.mark.asyncio
async def test_session_api_external_delivery_policy_is_validated_but_cannot_suppress_mirror(tmp_path):
    from orchestrator.frontend_delivery import (
        frontend_run_delivery_policy,
        telegram_delivery_for_admission,
    )

    server, runtime = _server(tmp_path)
    shared = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    policy = frontend_run_delivery_policy(
        connector_id="session_api",
        client_id="hashi-workbench-v2",
        targets=[
            {"connector_id": "telegram", "role": "mirror", "enabled": False}
        ],
    )
    response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "external-private-1",
                "surface": "workbench",
                "client_id": "hashi-workbench-v2",
                "delivery_policy": policy,
                "message": {"content": [{"type": "text", "text": "private reply"}]},
            },
            match_info={"session_id": shared["session_id"]},
        )
    )
    payload = json.loads(response.text)
    assert response.status == 202, payload
    assert runtime.last_source == "session-api"
    assert runtime.last_request_metadata["frontend_client"] == {
        "kind": "session_api",
        "client_id": "hashi-workbench-v2",
    }
    assert "frontend_delivery_policy" not in runtime.last_request_metadata
    assert telegram_delivery_for_admission(
        source=runtime.last_source,
        request_metadata=runtime.last_request_metadata,
        state_root=tmp_path,
    ) is True

    invalid_policy = frontend_run_delivery_policy(
        connector_id="session_api",
        client_id="hashi-workbench-v2",
        targets=[
            {"connector_id": "hchat", "role": "subscriber", "enabled": True}
        ],
    )
    rejected = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "external-policy-2",
                "surface": "workbench",
                "client_id": "hashi-workbench-v2",
                "delivery_policy": invalid_policy,
                "message": {"content": [{"type": "text", "text": "reject route injection"}]},
            },
            match_info={"session_id": shared["session_id"]},
        )
    )
    assert rejected.status == 400
    assert runtime.enqueue_request_calls == 1


@pytest.mark.asyncio
async def test_tui_session_run_passes_only_signed_remote_origin_evidence(tmp_path):
    from orchestrator.frontend_delivery import tui_run_delivery_policy
    from orchestrator.message_context import seal_connector_evidence

    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "test-network-secret"}),
        encoding="utf-8",
    )
    server, runtime = _server(tmp_path)
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )
    evidence = seal_connector_evidence(
        tmp_path,
        claims={"_origin_instance_evidence": {
            "id": "HASHI2", "assurance": "shared_network_hmac"
        }},
        prompt="remote message",
    )
    response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "remote-tui-message-1",
                "surface": "tui",
                "client_id": "tui-remote-1",
                "delivery_policy": tui_run_delivery_policy(
                    telegram_mirror=True, client_id="tui-remote-1"
                ),
                "message": {
                    "content": [{"type": "text", "text": "remote message"}]
                },
                "request_metadata": {
                    "_connector_evidence": evidence,
                    "_message_source_reserved": "telegram",
                },
            },
            match_info={"session_id": session["session_id"]},
        )
    )

    assert response.status == 202
    assert runtime.last_request_metadata["_connector_evidence"] == evidence
    assert runtime.last_source == "tui"


@pytest.mark.asyncio
async def test_tui_and_workbench_legacy_chat_share_default_conversation_binding(
    tmp_path,
):
    server, runtime = _server(tmp_path)
    tui_request = _Request({"agent": "lily", "text": "from TUI"})
    tui_request.content_type = "application/json"
    workbench_request = _Request(
        {
            "agent": "lily",
            "text": "from Workbench",
            "source": "workbench_ui_chat",
            "client_session_id": "workbench:lily",
            "reply_target": {
                "type": "ui_chat",
                "surface": "workbench",
                "conversation_id": "workbench:lily",
            },
        }
    )
    workbench_request.content_type = "application/json"

    assert (await server.handle_chat(tui_request)).status == 200
    assert (await server.handle_chat(workbench_request)).status == 200

    assert runtime.api_request_metadata == [
        {
            "session_id": None,
            "owner_id": "user:7",
            "session_surface": "workbench",
            "session_channel_key": "default",
        },
        {
            "session_id": None,
            "owner_id": "user:7",
            "session_surface": "workbench",
            "session_channel_key": "default",
        },
    ]


@pytest.mark.asyncio
async def test_legacy_chat_metadata_cannot_choose_command_owner(tmp_path, monkeypatch):
    from orchestrator import workbench_api

    server, _runtime = _command_server(tmp_path)
    observed = []

    async def dispatch(_runtime, _text, *, source_channel, session_metadata):
        observed.append((source_channel, session_metadata))
        return {"ok": True}

    monkeypatch.setattr(workbench_api, "try_execute_slash_command_text", dispatch)
    request = _command_request({
        "agent": "lily", "text": "/model", "source": "workbench_ui_chat",
        "request_metadata": {"owner_id": "user:other", "_hashi_owner_id": "user:other"},
    })
    request.content_type = "application/json"

    response = await server.handle_chat(request)
    assert response.status == 200
    assert observed[0][1]["owner_id"] == "user:7"
    assert observed[0][1]["_hashi_owner_id"] == "user:7"


@pytest.mark.asyncio
async def test_legacy_chat_response_is_queue_ack_without_transport_receipt(tmp_path):
    server, _runtime = _server(tmp_path)
    request = _Request({"agent": "lily", "text": "queue this"})
    request.content_type = "application/json"

    response = await server.handle_chat(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["ok"] is True
    assert payload["request_id"] == "req-api-1"
    assert "delivery_receipt" not in payload
    assert "delivered" not in payload


@pytest.mark.asyncio
async def test_transferred_chat_returns_canonical_409_without_model_run_or_telegram(
    tmp_path,
):
    server, runtime = _server(tmp_path)
    runtime._transfer_state = {
        "status": "accepted",
        "transfer_id": "trf-chat-1",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }
    request = _Request(
        {
            "agent": "lily",
            "text": "must continue on the target",
            "idempotency_key": "chat-after-transfer-1",
        }
    )
    request.content_type = "application/json"

    first = await server.handle_chat(request)
    replay = await server.handle_chat(request)
    conflicting_request = _Request(
        {
            "agent": "lily",
            "text": "different content with the same key",
            "idempotency_key": "chat-after-transfer-1",
        }
    )
    conflicting_request.content_type = "application/json"
    conflict = await server.handle_chat(conflicting_request)
    first_payload = json.loads(first.text)
    replay_payload = json.loads(replay.text)
    conflict_payload = json.loads(conflict.text)

    assert first.status == replay.status == 409
    assert first_payload["ok"] is False
    assert first_payload["accepted"] is False
    assert first_payload["error_code"] == "session_transferred"
    assert first_payload["transfer_id"] == "trf-chat-1"
    assert first_payload["target_agent"] == "akane"
    assert first_payload["target_instance"] == "HASHI2"
    assert first_payload["admission"]["status"] == "rejected"
    assert first_payload["admission"]["reason"] == "session_transferred"
    assert replay_payload["admission"]["replayed"] is True
    assert replay_payload["canonical_result"] == first_payload["canonical_result"]
    assert conflict.status == 409
    assert conflict_payload["error_code"] == "idempotency_conflict"
    assert conflict_payload["admission"]["status"] == "conflict"
    assert runtime.api_request_metadata == []

    session_id = first_payload["session_id"]
    assert server.session_store.recent_session_runs(
        session_id, owner_id="user:7"
    ) == []
    messages = server.session_store.messages(session_id, owner_id="user:7")
    assert len(messages) == 1
    assert messages[0]["source"] == "workbench.transfer-redirect"
    events = server.session_store.events(session_id, owner_id="user:7")
    assert sum(event["kind"] == "frontend.command_result" for event in events) == 1
    assert sum(event["kind"] == "frontend.message.recorded" for event in events) == 1
    claims = server.session_store.claim_delivery_outbox(
        session_id=session_id,
        owner_id="user:7",
        worker_id="test-transfer-redirect",
        event_id=first_payload["canonical_result"]["message_event_id"],
        connector_id="backend_api",
        limit=1,
    )
    assert len(claims) == 1


@pytest.mark.asyncio
async def test_unknown_transfer_outcome_rejects_chat_without_claiming_redirect(tmp_path):
    server, runtime = _server(tmp_path)
    runtime._transfer_state = {
        "status": "pending",
        "outcome_unknown": True,
        "transfer_id": "trf-chat-unknown",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }
    request = _Request(
        {
            "agent": "lily",
            "text": "must not enter a new Run",
            "idempotency_key": "chat-transfer-unknown-1",
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["accepted"] is False
    assert payload["error_code"] == "session_transfer_outcome_unknown"
    assert payload["transfer_id"] == "trf-chat-unknown"
    assert payload["admission"]["reason"] == "session_transfer_outcome_unknown"
    assert "redirect" not in payload
    assert runtime.api_request_metadata == []
    assert server.session_store.recent_session_runs(
        payload["session_id"], owner_id="user:7"
    ) == []


@pytest.mark.asyncio
async def test_session_run_reads_unknown_transfer_fence_before_creating_run(tmp_path):
    server, runtime = _server(tmp_path)
    workspace = tmp_path / "lily"
    workspace.mkdir()
    runtime.workspace_dir = workspace
    (workspace / "active_transfer.json").write_text(
        json.dumps(
            {
                "status": "pending",
                "outcome_unknown": True,
                "transfer_id": "trf-session-unknown",
                "target_agent": "akane",
                "target_instance": "HASHI2",
            }
        ),
        encoding="utf-8",
    )
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )

    response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "session-transfer-unknown-1",
                "surface": "workbench",
                "client_id": "workbench-window-1",
                "message_source": {"id": "scheduler"},
                "request_metadata": {"voice_origin": True},
                "message": {
                    "content": [
                        {"type": "text", "text": "must not create a Run"}
                    ]
                },
            },
            match_info={"session_id": session["session_id"]},
        )
    )
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["accepted"] is False
    assert payload["error_code"] == "session_transfer_outcome_unknown"
    assert payload["transfer_id"] == "trf-session-unknown"
    assert runtime.enqueue_request_calls == 0
    assert server.session_store.recent_session_runs(
        session["session_id"], owner_id="user:7"
    ) == []
    assert not any(
        message["role"] == "user"
        for message in server.session_store.messages(
            session["session_id"], owner_id="user:7"
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redirect", "error_code"),
    [
        (
            {
                "status": "accepted",
                "transfer_id": "trf-worker-race-accepted",
                "target_agent": "akane",
                "target_instance": "HASHI3",
            },
            "session_transferred",
        ),
        (
            {
                "status": "unknown",
                "transfer_id": "trf-worker-race-unknown",
                "target_agent": "akane",
                "target_instance": "HASHI3",
            },
            "session_transfer_outcome_unknown",
        ),
    ],
)
async def test_session_run_maps_worker_transfer_fence_to_canonical_409(
    tmp_path, redirect, error_code
):
    from orchestrator.function_worker_protocol import FunctionWorkerRemoteError
    from orchestrator.runtime_transfer import TransferRedirectRequired

    server, runtime = _server(tmp_path)
    runtime.workspace_dir = tmp_path / "lily"
    runtime.workspace_dir.mkdir()
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )

    async def reject_after_worker_recheck(*_args, **_kwargs):
        typed = TransferRedirectRequired(redirect)
        raise FunctionWorkerRemoteError(
            "runtime.enqueue_request",
            {"type": "TransferRedirectRequired", "message": str(typed)},
        )

    runtime.enqueue_request = reject_after_worker_recheck
    response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": f"worker-race-{redirect['status']}",
                "surface": "workbench",
                "client_id": "workbench-window-race",
                "message": {
                    "content": [{"type": "text", "text": "must stay fenced"}]
                },
            },
            match_info={"session_id": session["session_id"]},
        )
    )
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["accepted"] is False
    assert payload["error_code"] == error_code
    assert payload["admission"]["run_id"] is None
    assert payload["transfer_id"] == redirect["transfer_id"]
    assert server.session_store.recent_session_runs(
        session["session_id"], owner_id="user:7"
    ) == []


@pytest.mark.asyncio
async def test_session_attachment_stage_rejects_transfer_before_staging(tmp_path):
    server, runtime = _server(tmp_path)
    workspace = tmp_path / "lily"
    workspace.mkdir()
    runtime.workspace_dir = workspace
    runtime._transfer_state = {
        "status": "accepted",
        "transfer_id": "trf-session-attachment",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }
    session = server.session_store.resolve_primary_session(
        owner_id="user:7", agent_id="lily", establish=True
    )

    response = await server.handle_v1_attachment_stage(
        _Request(
            {
                "filename": "never-staged.txt",
                "media_type": "text/plain",
                "size_bytes": 8,
                "sha256": hashlib.sha256(b"fixture").hexdigest(),
                "idempotency_key": "attachment-transfer-1",
            },
            match_info={"session_id": session["session_id"]},
        )
    )
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["error_code"] == "session_transferred"
    assert runtime.enqueue_request_calls == 0
    with server.session_store._connection() as connection:
        attachment_count = connection.execute(
            "SELECT COUNT(*) FROM session_attachments WHERE session_id=?",
            (session["session_id"],),
        ).fetchone()[0]
    assert attachment_count == 0


@pytest.mark.asyncio
async def test_transferred_nonvoice_media_is_rejected_before_upload_is_written(
    tmp_path,
):
    from orchestrator.frontend_delivery import tui_run_delivery_policy

    server, runtime = _server(tmp_path)
    runtime._transfer_state = {
        "status": "accepted",
        "transfer_id": "trf-media-1",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }
    content = b"\x89PNG\r\n\x1a\nnot-written"
    request = _Request(
        {
            "agent": "lily",
            "text": "inspect",
            "source": "tui",
            "client_id": "tui-transfer",
            "delivery_policy": tui_run_delivery_policy(
                telegram_mirror=False,
                client_id="tui-transfer",
            ),
            "attachment": {
                "filename": "image.png",
                "media_type": "image/png",
                "content_b64": base64.b64encode(content).decode("ascii"),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
            "idempotency_key": "media-after-transfer-1",
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["error_code"] == "session_transferred"
    assert runtime.api_media_calls == []
    assert [path for path in runtime.media_dir.rglob("*") if path.is_file()] == []


@pytest.mark.asyncio
async def test_transferred_multipart_media_is_rejected_before_upload_is_written(
    tmp_path,
):
    server, runtime = _server(tmp_path)
    runtime._transfer_state = {
        "status": "accepted",
        "transfer_id": "trf-multipart-1",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }
    request = _MultipartRequest(
        {
            "agent": "lily",
            "text": "inspect multipart",
            "idempotency_key": "multipart-after-transfer-1",
        },
        uploads=[
            _MultipartPart(
                "file",
                filename="image.png",
                payload=b"\x89PNG\r\n\x1a\nnot-written",
                content_type="image/png",
            )
        ],
    )

    response = await server.handle_chat(request)
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["error_code"] == "session_transferred"
    assert runtime.api_media_calls == []
    assert [path for path in runtime.media_dir.rglob("*") if path.is_file()] == []


@pytest.mark.asyncio
async def test_protocol_hchat_attachments_enter_one_canonical_session_request(tmp_path):
    from orchestrator.hchat_attachment_contract import (
        HCHAT_ATTACHMENT_CLAIM_KEY,
        canonical_hchat_attachment_manifest,
    )
    from orchestrator.message_context import seal_connector_evidence

    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "test-network-secret"}),
        encoding="utf-8",
    )
    server, runtime = _server(tmp_path)
    content = b"PK\x03\x04small spreadsheet fixture"
    received = (
        tmp_path
        / "state"
        / "remote_attachments"
        / "hashi1"
        / "messages"
        / "wire-attachment-1"
        / "report.xlsx"
    )
    received.parent.mkdir(parents=True)
    received.write_bytes(content)
    attachments = [
        {
            "attachment_id": "att-1",
            "filename": "report.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "caption": "quarterly extract",
            "stored_path": str(received),
        }
    ]
    text = "System exchange message from testing@HASHI2:\ninspect this"
    claims = {
        "_message_source_reserved": "hchat",
        "_hchat_context": {
            "from_agent": "testing",
            "from_instance": "HASHI2",
            "to_agent": "lily",
            "to_instance": "HASHI1",
            "authenticated_peer": "HASHI2",
            "network_authentication": "shared_network_hmac",
            "sender_assurance": "shared_network_member_declared",
            "relay_chain": [],
            "origin_instance": {
                "id": "HASHI2",
                "assurance": "shared_network_hmac",
            },
        },
        HCHAT_ATTACHMENT_CLAIM_KEY: canonical_hchat_attachment_manifest(attachments),
    }
    request = _Request(
        {
            "agent": "lily",
            "text": text,
            "source": "protocol:message",
            "request_metadata": {
                "session_surface": "remote",
                "session_channel_key": "HASHI2:conversation-1",
                "_connector_evidence": seal_connector_evidence(
                    tmp_path, claims=claims, prompt=text
                ),
            },
            "remote_attachments": attachments,
            "message_id": "wire-attachment-1",
            "originated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "message_type": "agent_message",
            "idempotency_key": "hchat:wire-attachment-1",
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)

    assert response.status == 200
    canonical = runtime.api_request_content[-1]
    media = [part for part in canonical["parts"] if part["type"] == "media"]
    assert len(media) == 1
    assert media[0]["filename"] == "report.xlsx"
    assert media[0]["mime_type"].endswith("spreadsheetml.sheet")
    assert Path(media[0]["local_ref"]).read_bytes() == content
    assert Path(media[0]["local_ref"]).is_relative_to(
        server.session_store.attachment_files_root
    )


@pytest.mark.asyncio
async def test_transferred_protocol_hchat_document_is_rejected_before_staging(tmp_path):
    from orchestrator.hchat_attachment_contract import (
        HCHAT_ATTACHMENT_CLAIM_KEY,
        canonical_hchat_attachment_manifest,
    )
    from orchestrator.message_context import seal_connector_evidence

    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "test-network-secret"}),
        encoding="utf-8",
    )
    server, runtime = _server(tmp_path)
    runtime._transfer_state = {
        "status": "accepted",
        "transfer_id": "trf-hchat-document-1",
        "target_agent": "akane",
        "target_instance": "HASHI2",
    }
    content = b"%PDF-1.7 remote document"
    received = (
        tmp_path
        / "state"
        / "remote_attachments"
        / "hashi1"
        / "messages"
        / "wire-transfer-document"
        / "report.pdf"
    )
    received.parent.mkdir(parents=True)
    received.write_bytes(content)
    attachments = [
        {
            "attachment_id": "att-transfer-document",
            "filename": "report.pdf",
            "mime_type": "application/pdf",
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "caption": "read after moving",
            "stored_path": str(received),
        }
    ]
    text = "System exchange message from testing@HASHI2:\ninspect this document"
    claims = {
        "_message_source_reserved": "hchat",
        "_hchat_context": {
            "from_agent": "testing",
            "from_instance": "HASHI2",
            "to_agent": "lily",
            "to_instance": "HASHI1",
            "authenticated_peer": "HASHI2",
            "network_authentication": "shared_network_hmac",
            "sender_assurance": "shared_network_member_declared",
            "relay_chain": [],
            "origin_instance": {
                "id": "HASHI2",
                "assurance": "shared_network_hmac",
            },
        },
        HCHAT_ATTACHMENT_CLAIM_KEY: canonical_hchat_attachment_manifest(attachments),
    }
    request = _Request(
        {
            "agent": "lily",
            "text": text,
            "source": "protocol:message",
            "request_metadata": {
                "session_surface": "remote",
                "session_channel_key": "HASHI2:conversation-transfer-document",
                "_connector_evidence": seal_connector_evidence(
                    tmp_path, claims=claims, prompt=text
                ),
            },
            "remote_attachments": attachments,
            "message_id": "wire-transfer-document",
            "originated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "message_type": "agent_message",
            "idempotency_key": "hchat:wire-transfer-document",
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)
    payload = json.loads(response.text)

    assert response.status == 409
    assert payload["error_code"] == "session_transferred"
    assert payload["transfer_id"] == "trf-hchat-document-1"
    assert runtime.api_request_content == []
    assert list(server.session_store.attachment_files_root.iterdir()) == []


@pytest.mark.asyncio
async def test_protocol_hchat_attachments_fail_closed_before_partial_admission(tmp_path):
    server, runtime = _server(tmp_path)
    received = (
        tmp_path
        / "state"
        / "remote_attachments"
        / "hashi1"
        / "messages"
        / "wire-attachment-2"
        / "script.ps1"
    )
    received.parent.mkdir(parents=True)
    received.write_bytes(b"Write-Host safe")
    request = _Request(
        {
            "agent": "lily",
            "text": "unsigned attachment must not enter admission",
            "source": "protocol:message",
            "remote_attachments": [
                {
                    "attachment_id": "att-script",
                    "filename": "script.ps1",
                    "mime_type": "application/octet-stream",
                    "size_bytes": received.stat().st_size,
                    "sha256": hashlib.sha256(received.read_bytes()).hexdigest(),
                    "stored_path": str(received),
                }
            ],
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)

    assert response.status == 401
    assert runtime.api_request_metadata == []
    assert list(server.session_store.attachment_files_root.iterdir()) == []


@pytest.mark.asyncio
async def test_protocol_hchat_attachment_batch_rolls_back_if_one_file_changes(tmp_path):
    from orchestrator.hchat_attachment_contract import (
        HCHAT_ATTACHMENT_CLAIM_KEY,
        canonical_hchat_attachment_manifest,
    )
    from orchestrator.message_context import seal_connector_evidence

    (tmp_path / "secrets.json").write_text(
        json.dumps({"hashi_remote_shared_token": "test-network-secret"}),
        encoding="utf-8",
    )
    server, runtime = _server(tmp_path)
    inbox = (
        tmp_path
        / "state"
        / "remote_attachments"
        / "hashi1"
        / "messages"
        / "wire-attachment-rollback"
    )
    inbox.mkdir(parents=True)
    good = inbox / "good.ps1"
    changed = inbox / "changed.7z"
    good.write_bytes(b"Write-Host safe")
    changed.write_bytes(b"changed")
    attachments = [
        {
            "attachment_id": "att-good",
            "filename": good.name,
            "mime_type": "application/octet-stream",
            "size_bytes": good.stat().st_size,
            "sha256": hashlib.sha256(good.read_bytes()).hexdigest(),
            "stored_path": str(good),
        },
        {
            "attachment_id": "att-changed",
            "filename": changed.name,
            "mime_type": "application/x-7z-compressed",
            "size_bytes": changed.stat().st_size,
            "sha256": hashlib.sha256(b"initial").hexdigest(),
            "stored_path": str(changed),
        },
    ]
    text = "System exchange message from testing@HASHI2:\ninspect atomically"
    claims = {
        "_message_source_reserved": "hchat",
        "_hchat_context": {
            "network_authentication": "shared_network_hmac",
        },
        HCHAT_ATTACHMENT_CLAIM_KEY: canonical_hchat_attachment_manifest(attachments),
    }
    request = _Request(
        {
            "agent": "lily",
            "text": text,
            "source": "protocol:message",
            "request_metadata": {
                "session_surface": "remote",
                "session_channel_key": "HASHI2:conversation-rollback",
                "_connector_evidence": seal_connector_evidence(
                    tmp_path, claims=claims, prompt=text
                ),
            },
            "remote_attachments": attachments,
            "message_id": "wire-attachment-rollback",
            "originated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "message_type": "agent_message",
            "idempotency_key": "hchat:wire-attachment-rollback",
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)

    assert response.status == 400
    assert runtime.api_request_metadata == []
    assert list(server.session_store.attachment_files_root.iterdir()) == []


@pytest.mark.asyncio
async def test_tui_attachment_bytes_and_caption_enter_one_media_request(tmp_path):
    import base64
    import hashlib

    from orchestrator.frontend_delivery import tui_run_delivery_policy

    server, runtime = _server(tmp_path)
    content = b"\x89PNG\r\n\x1a\nactual-image"
    request = _Request(
        {
            "agent": "lily",
            "text": "describe this",
            "source": "tui",
            "client_id": "tui-7",
            "delivery_policy": tui_run_delivery_policy(
                telegram_mirror=False, client_id="tui-7"
            ),
            "attachment": {
                "filename": "image.png",
                "media_type": "image/png",
                "content_b64": base64.b64encode(content).decode("ascii"),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload == {"ok": True, "request_id": "req-media-1"}
    assert len(runtime.api_media_calls) == 1
    call = runtime.api_media_calls[0]
    assert call["caption"] == "describe this"
    assert call["deliver_to_telegram"] is True
    assert call["local_path"].read_bytes() == content
    assert call["filename"] == "image.png"


@pytest.mark.asyncio
async def test_tui_chat_ignores_legacy_mirror_policy_without_forking_conversation(
    tmp_path,
):
    from orchestrator.frontend_delivery import tui_run_delivery_policy

    server, runtime = _server(tmp_path)
    policy = tui_run_delivery_policy(
        telegram_mirror=False,
        client_id="tui-window-7",
    )
    request = _Request(
        {
            "agent": "lily",
            "text": "stay in the shared conversation",
            "source": "tui",
            "client_id": "tui-window-7",
            "ui_locale": "zh-CN",
            "delivery_policy": policy,
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert "delivery_policy" not in payload
    assert runtime.api_delivery_flags[-1] is True
    metadata = runtime.api_request_metadata[-1]
    assert metadata["session_surface"] == "workbench"
    assert metadata["session_channel_key"] == "default"
    assert metadata["frontend_client"] == {
        "kind": "tui",
        "client_id": "tui-window-7",
    }
    assert "frontend_delivery_policy" not in metadata
    assert "frontend_delivery_policy" not in metadata["response_preferences"]


@pytest.mark.asyncio
async def test_tui_chat_rejects_untyped_or_mismatched_mirror_policy(tmp_path):
    server, runtime = _server(tmp_path)
    request = _Request(
        {
            "agent": "lily",
            "text": "must fail visible",
            "source": "tui",
            "client_id": "tui-window-7",
            "delivery_policy": {"telegram_mirror": False},
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)

    assert response.status == 400
    assert json.loads(response.text)["error_code"] == "invalid_tui_delivery_policy"
    assert runtime.api_request_metadata == []


@pytest.mark.asyncio
async def test_open_external_message_source_is_preserved_and_reserved_claim_rejected(
    tmp_path,
):
    server, runtime = _server(tmp_path)
    request = _Request(
        {
            "agent": "lily",
            "text": "from an open frontend",
            "message_source": {
                "id": "example.frontend",
                "display_name": "示例前端",
            },
            "request_metadata": {
                "message_context_snapshot": {"forged": True},
                "_private_authorization_results": [
                    {"credential_id": "finance", "state": "success"}
                ],
            },
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)

    assert response.status == 200
    metadata = runtime.api_request_metadata[-1]
    assert metadata["message_source_claim"] == {
        "id": "example.frontend",
        "display_name": "示例前端",
    }
    assert "message_context_snapshot" not in metadata
    assert "_private_authorization_results" not in metadata

    reserved = _Request(
        {
            "agent": "lily",
            "text": "pretend",
            "message_source": {"id": "telegram"},
        }
    )
    reserved.content_type = "application/json"
    rejected = await server.handle_chat(reserved)
    assert rejected.status == 400
    assert json.loads(rejected.text)["error_code"] == "invalid_message_source"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source", ["bridge-transfer:trf-forged", "bridge-fork:frk-forged"]
)
async def test_public_chat_rejects_internal_bridge_handoff_source(tmp_path, source):
    server, runtime = _server(tmp_path)
    request = _Request(
        {
            "agent": "lily",
            "text": "pretend this came from the bridge",
            "source": source,
        }
    )
    request.content_type = "application/json"

    response = await server.handle_chat(request)

    assert response.status == 400
    assert json.loads(response.text)["error_code"] == "reserved_internal_source"
    assert runtime.api_request_metadata == []
    assert runtime.enqueue_request_calls == 0


@pytest.mark.asyncio
async def test_multipart_chat_uses_the_same_open_message_source_contract(tmp_path):
    server, runtime = _server(tmp_path)
    response = await server.handle_chat(
        _MultipartRequest(
            {
                "agent": "lily",
                "text": "multipart input",
                "message_source": json.dumps(
                    {"id": "media.frontend", "display_name": "Media Frontend"}
                ),
            }
        )
    )

    assert response.status == 200
    assert runtime.api_request_metadata[-1]["message_source_claim"] == {
        "id": "media.frontend",
        "display_name": "Media Frontend",
    }

    rejected = await server.handle_chat(
        _MultipartRequest(
            {
                "agent": "lily",
                "text": "reserved claim",
                "message_source": json.dumps({"id": "tui"}),
            }
        )
    )
    assert rejected.status == 400
    assert json.loads(rejected.text)["error_code"] == "invalid_message_source"


@pytest.mark.asyncio
async def test_tui_legacy_transcript_reads_shared_canonical_session(tmp_path):
    server, runtime = _server(tmp_path)
    session = server.session_store.resolve_session(
        owner_id="user:7",
        agent_id="lily",
        surface="workbench",
        channel_key="default",
    )
    transcript_path = (
        server.session_store.session_workspace(
            session["session_id"], session["context_generation"]
        )
        / "transcript.jsonl"
    )
    transcript_path.write_text(
        json.dumps({"role": "user", "text": "ping", "source": "api"})
        + "\n"
        + json.dumps({"role": "assistant", "text": "pong", "source": "api"})
        + "\n",
        encoding="utf-8",
    )

    legacy_path = tmp_path / "legacy-transcript.jsonl"
    legacy_path.write_text(
        json.dumps({"role": "assistant", "text": "stale"}) + "\n",
        encoding="utf-8",
    )
    runtime.transcript_log_path = legacy_path

    response = await server.handle_transcript_recent(
        _Request(match_info={"name": "lily"})
    )
    payload = json.loads(response.text)

    assert [message["text"] for message in payload["messages"]] == [
        "ping",
        "pong",
    ]


@pytest.mark.asyncio
async def test_workbench_deferred_startup_maintenance_reconciles_lost_session_runs(
    tmp_path,
):
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
    assert run["state"] == "running"

    await restarted._run_startup_maintenance()

    run = restarted.session_store.get_run(accepted.run_id, owner_id="user:7")
    assert run["state"] == "interrupted"
    assert run["error_code"] == "runtime_restart_interrupted"
    assert [row["run_id"] for row in restarted.reconciled_session_runs] == [
        accepted.run_id
    ]


def test_workbench_service_refresh_preserves_runs_owned_by_live_workers(tmp_path):
    server, _runtime = _server(tmp_path)
    session = server.session_store.ensure_default_session(
        owner_id="user:7", agent_id="lily"
    )
    accepted = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="lily",
        request_id="req-live-during-service-refresh",
        text="still executing in a live Function Worker",
        source="test",
        idempotency_key="live-during-service-refresh",
    )
    server.session_store.mark_request_running(
        accepted.request_id,
        worker_id="function-worker-live",
    )

    refreshed, _runtime = _server(tmp_path, reconcile_session_runs=False)

    run = refreshed.session_store.get_run(accepted.run_id, owner_id="user:7")
    assert run["state"] == "running"
    assert refreshed.reconciled_session_runs == []


@pytest.mark.asyncio
async def test_session_api_keeps_agent_text_and_persists_identity_bound_display_text(
    tmp_path,
):
    from orchestrator.chat_transcript_projection import build_chat_projection
    from orchestrator.session_store import SessionStore

    server, runtime = _server(tmp_path)
    created_response = await server.handle_v1_sessions_create(
        _Request({"agent_id": "lily", "title": "Display projection"})
    )
    session_id = json.loads(created_response.text)["session"]["session_id"]
    canonical_text = (
        "[WORKBENCH_LOCAL_PATH_DIRECTIONS]\n"
        '{"schema_version":1,"path":"C:\\\\Users\\\\example.txt"}\n'
        "[/WORKBENCH_LOCAL_PATH_DIRECTIONS]\n"
        "Please inspect the selected file."
    )
    display_text = "Please inspect the selected file."

    response = await server.handle_v1_session_runs_create(
        _Request(
            {
                "idempotency_key": "display-projection-one",
                "surface": "workbench",
                "message": {
                    "content": [{"type": "text", "text": canonical_text}],
                    "display_text": display_text,
                },
            },
            match_info={"session_id": session_id},
            headers={"X-Client-Id": "workbench-window"},
        )
    )
    accepted = json.loads(response.text)

    assert response.status == 202
    assert runtime.last_request_metadata["session_message_text"] == canonical_text
    assert runtime.last_request_metadata["session_message_display_text"] == display_text
    assert runtime.last_request_content["parts"][0]["text"] == canonical_text

    messages_response = await server.handle_v1_session_messages(
        _Request(match_info={"session_id": session_id})
    )
    messages = json.loads(messages_response.text)["messages"]
    stored = next(
        item for item in messages if item["message_id"] == accepted["message_id"]
    )
    assert stored["text"] == canonical_text
    assert stored["display_text"] == display_text

    reopened = SessionStore(server.session_store.db_path, instance_id="HASHI1")
    session = reopened.get_session(session_id, owner_id="user:7")
    projection = build_chat_projection(
        reopened,
        session=session,
        owner_id="user:7",
    )
    projected = next(
        item
        for item in projection["messages"]
        if item["message_id"] == accepted["message_id"]
    )
    assert projected["text"] == display_text


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
                "message_source": {
                    "id": "desktop.client",
                    "display_name": "Desktop Client",
                },
                "private_authorization_proofs": [
                    {
                        "type": "hashi.private-authorization-proof",
                        "credential_id": "synthetic",
                    }
                ],
                "private_authorization_binding": {
                    "message_id": "client-message-1",
                    "from_instance": "HASHI1",
                    "from_agent": "client",
                    "to_instance": "HASHI1",
                    "to_agent": "lily",
                    "content_sha256": authorization_content_sha256(
                        "hello Session"
                    ),
                    "resources": [],
                },
                "message": {"content": [{"type": "text", "text": "hello Session"}]},
            },
            match_info={"session_id": session_id},
            headers={"X-Client-Id": "frontend-test"},
        )
    )
    run_payload = json.loads(run_response.text)
    assert run_response.status == 202
    assert run_payload["session_id"] == session_id
    assert run_payload["message_id"].startswith("msg_")
    assert _runtime.last_request_metadata["session_surface"] == "desktop-client"
    assert _runtime.last_request_metadata["message_source_claim"] == {
        "id": "desktop.client",
        "display_name": "Desktop Client",
    }
    assert _runtime.last_request_metadata["_private_authorization_proofs"][0][
        "credential_id"
    ] == "synthetic"

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
    run_events = json.loads(
        (
            await server.handle_v1_session_events(
                _Request(
                    query={"run_id": run_payload["run_id"], "after_sequence": "0"},
                    match_info={"session_id": session_id},
                )
            )
        ).text
    )
    assert run_events["events"]
    assert all(event["run_id"] == run_payload["run_id"] for event in run_events["events"])

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
async def test_frontend_feed_projects_final_message_and_accepts_exact_endpoint(tmp_path):
    from adapters.stream_events import (
        DELIVERY_ANSWER_PREVIEW,
        DELIVERY_FINAL,
        DELIVERY_USER_COMMENTARY,
    )
    from orchestrator.frontend_delivery import freeze_run_delivery_route
    from orchestrator.request_activity import RequestActivityStore

    server, runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.ensure_default_session(
        owner_id=owner, agent_id="lily"
    )
    route = freeze_run_delivery_route(
        message_source_id="api",
        session_surface="backend-api",
        session_channel_key="client-a",
        chat_id=7,
        telegram_requested=False,
    )
    accepted = server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="req-feed",
        text="question",
        source="api",
        idempotency_key="feed-key",
        delivery_route=route,
    )
    server.session_store.mark_request_running(
        accepted.request_id, worker_id="feed-test"
    )
    finished = server.session_store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="canonical final answer",
    )
    runtime.request_activity = RequestActivityStore(epoch=17)
    runtime.request_activity.bind_presentation_settings(
        accepted.request_id,
        lambda: {"commentary": True, "answer_preview": True},
    )
    runtime.request_activity.start(accepted.request_id)
    runtime.request_activity.publish_stream(
        accepted.request_id,
        SimpleNamespace(
            kind="commentary",
            summary="safe commentary",
            event_id="commentary-1",
            delivery_class=DELIVERY_USER_COMMENTARY,
        ),
    )
    runtime.request_activity.publish_stream(
        accepted.request_id,
        SimpleNamespace(
            kind="text_delta",
            summary="raw provider delta must stay internal",
            event_id="raw-delta-1",
        ),
    )
    runtime.request_activity.publish_stream(
        accepted.request_id,
        SimpleNamespace(
            kind="answer_preview",
            summary="safe answer preview",
            event_id="preview-1",
            delivery_class=DELIVERY_ANSWER_PREVIEW,
        ),
    )
    runtime.request_activity.publish_stream(
        accepted.request_id,
        SimpleNamespace(
            kind="final",
            summary="activity final must not replace durable final",
            event_id="activity-final-1",
            delivery_class=DELIVERY_FINAL,
        ),
    )

    request = _Request(
        query={
            "surface": "backend-api",
            "client_id": "client-a",
            "request_id": accepted.request_id,
        },
        match_info={"session_id": session["session_id"]},
    )
    response = await server.handle_v2_frontend_feed(request)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["connector_id"] == "backend_api"
    terminal = next(
        event
        for event in payload["durable_events"]
        if event["run_id"] == accepted.run_id
        and event["semantic_kind"] == "final"
    )
    assert terminal["semantic_kind"] == "final"
    assert terminal["message_id"] == finished["final_message_id"]
    assert terminal["request_id"] == accepted.request_id
    assert terminal["run_id"] == accepted.run_id
    assert terminal["content_blocks"][0]["text"] == "canonical final answer"
    assert payload["ephemeral_epoch"] == 17
    assert [
        event["content_blocks"][0]["text"]
        for event in payload["ephemeral_events"]
    ] == ["safe commentary", "safe answer preview"]
    assert [
        event["semantic_kind"] for event in payload["ephemeral_events"]
    ] == ["commentary", "answer_preview"]
    assert all(
        event["request_id"] == accepted.request_id
        and event["run_id"] == accepted.run_id
        for event in payload["ephemeral_events"]
    )
    answer_preview = payload["ephemeral_events"][1]
    assert answer_preview["presentation_channel"] == "answer"
    assert answer_preview["content_blocks"][0]["format"] == "markdown"
    assert payload["ephemeral_watermark"] == 5
    assert terminal["event_id"] in payload["accepted_event_ids"]
    receipts = server.session_store.frontend_delivery_receipts(
        session_id=session["session_id"],
        owner_id=owner,
        event_id=terminal["event_id"],
    )
    assert [(row["endpoint_id"], row["status"]) for row in receipts] == [
        (payload["endpoint_id"], "accepted")
    ]

    replay = json.loads((await server.handle_v2_frontend_feed(request)).text)
    assert replay["accepted_event_ids"] == []

    reset_request = _Request(
        query={
            "surface": "backend-api",
            "client_id": "client-a",
            "request_id": accepted.request_id,
            "after_durable_sequence": str(payload["durable_watermark"]),
            "after_ephemeral_sequence": str(payload["ephemeral_watermark"]),
            "ephemeral_epoch": "16",
        },
        match_info={"session_id": session["session_id"]},
    )
    reset = json.loads((await server.handle_v2_frontend_feed(reset_request)).text)
    assert reset["durable_events"] == []
    assert reset["ephemeral_reset"] is True
    assert len(reset["ephemeral_events"]) == 2


@pytest.mark.asyncio
async def test_session_cancel_reaches_selected_worker_before_marking_run_stopped(tmp_path):
    server, runtime = _server(tmp_path)
    session = server.session_store.resolve_primary_session(owner_id=server._v1_owner_id(_Request()), agent_id="lily")
    accepted = server.session_store.accept_run(
        session_id=session["session_id"], owner_id=session["owner_id"], agent_id="lily",
        request_id="req-cancel-target", text="work", source="workbench",
        idempotency_key="cancel-target",
    )
    server.session_store.mark_request_running(accepted.request_id, worker_id="worker")
    calls = []

    async def cancel_session_run(**scope):
        calls.append(scope)
        return {"ok": True, "status": "cancellation_requested", "terminal": False,
                "request_id": accepted.request_id}

    runtime.cancel_session_run = cancel_session_run
    response = await server.handle_v1_session_run_cancel(_Request(
        {"reason": "cancelled_by_user"},
        match_info={"session_id": session["session_id"], "run_id": accepted.run_id},
    ))
    assert response.status == 202
    assert calls == [{"owner_id": session["owner_id"], "session_id": session["session_id"],
                      "run_id": accepted.run_id, "request_id": accepted.request_id,
                      "reason": "cancelled_by_user"}]
    assert server.session_store.get_run(accepted.run_id)["state"] == "running"


@pytest.mark.asyncio
async def test_request_cancel_fences_session_and_run_before_worker_call(tmp_path):
    server, runtime = _server(tmp_path)
    session = server.session_store.resolve_primary_session(
        owner_id=server._v1_owner_id(_Request()), agent_id="lily",
    )
    accepted = server.session_store.accept_run(
        session_id=session["session_id"], owner_id=session["owner_id"], agent_id="lily",
        request_id="req-scoped", text="work", source="workbench",
        idempotency_key="scoped-cancel",
    )
    calls = []

    async def cancel_session_run(**scope):
        calls.append(scope)
        return {"ok": True, "request_id": accepted.request_id,
                "status": "cancellation_requested", "terminal": False}

    runtime.cancel_session_run = cancel_session_run
    target = {"name": "lily", "request_id": accepted.request_id}
    missing_scope = await server.handle_request_cancel(_Request({}, match_info=target))
    assert missing_scope.status == 400
    assert calls == []
    wrong = await server.handle_request_cancel(_Request(
        {"session_id": session["session_id"], "run_id": "run-wrong"}, match_info=target,
    ))
    assert wrong.status == 404
    assert calls == []
    accepted_response = await server.handle_request_cancel(_Request(
        {"session_id": session["session_id"], "run_id": accepted.run_id}, match_info=target,
    ))
    assert accepted_response.status == 202
    assert len(calls) == 1
    assert calls[0]["request_id"] == accepted.request_id
    controls = [event for event in server.session_store.events(session["session_id"])
                if event["kind"] == "run.control"]
    assert [event["status"] for event in controls] == ["requested", "cancellation_requested"]
    assert controls[-1]["detail"] == {
        "action": "cancel", "actor_id": session["owner_id"], "source": "backend_api.request_cancel",
        "agent_id": "lily", "session_id": session["session_id"], "run_id": accepted.run_id,
        "request_id": accepted.request_id, "reason": "cancelled_by_user", "error_code": "",
    }


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

    body = b"proof"
    staged = json.loads(
        (
            await server.handle_v1_attachment_stage(
                _Request(
                    {
                        "filename": "proof.txt",
                        "media_type": "text/plain",
                        "size_bytes": len(body),
                        "sha256": hashlib.sha256(body).hexdigest(),
                    },
                    match_info={"session_id": session_id},
                )
            )
        ).text
    )["attachment"]
    uploaded = await server.handle_v1_attachment_upload(
        _Request(
            match_info={
                "session_id": session_id,
                "attachment_id": staged["attachment_id"],
            },
            headers={"Content-Type": "text/plain"},
            body=body,
        )
    )
    assert uploaded.status == 200
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
    downloaded = await server.handle_v1_attachment_get(
        _Request(
            match_info={
                "session_id": session_id,
                "attachment_id": staged["attachment_id"],
            }
        )
    )
    assert downloaded.status == 200
    assert downloaded.body == body
    assert downloaded.headers["X-Content-SHA256"] == hashlib.sha256(body).hexdigest()
    assert downloaded.headers["Content-Disposition"].startswith("attachment;")


@pytest.mark.asyncio
async def test_agent_history_api_reads_archived_session_before_new_session(tmp_path):
    server, _runtime = _server(tmp_path)
    owner = "user:7"
    earlier = server.session_store.create_session(owner_id=owner, agent_id="lily")
    earlier_message = server.session_store.append_presentation_message(
        session_id=earlier["session_id"],
        owner_id=owner,
        agent_id="lily",
        role="assistant",
        text="A message from the earlier session",
        source="test",
        idempotency_key="agent-history-earlier",
    )
    later_earlier_message = server.session_store.append_presentation_message(
        session_id=earlier["session_id"],
        owner_id=owner,
        agent_id="lily",
        role="assistant",
        text="A later message from the earlier session",
        source="test",
        idempotency_key="agent-history-later-earlier",
    )
    server.session_store.archive_session(earlier["session_id"])

    current = server.session_store.create_session(owner_id=owner, agent_id="lily")
    current_message = server.session_store.append_presentation_message(
        session_id=current["session_id"],
        owner_id=owner,
        agent_id="lily",
        role="assistant",
        text="A message from the new session",
        source="test",
        idempotency_key="agent-history-current",
    )

    response = await server.handle_v1_agent_history(
        _Request(
            query={
                "before_session_id": current["session_id"],
                "before_ordinal": str(current_message["ordinal"]),
                "limit": "1",
            },
            match_info={"agent_id": "lily"},
        )
    )
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["history_complete"] is False
    assert isinstance(payload["next_cursor"], str)
    assert [message["text"] for message in payload["messages"]] == [
        "A later message from the earlier session"
    ]
    assert payload["messages"][0]["session_id"] == earlier["session_id"]
    assert payload["messages"][0]["message_id"] == later_earlier_message["message_id"]

    older_response = await server.handle_v1_agent_history(
        _Request(
            query={"cursor": payload["next_cursor"], "limit": "1"},
            match_info={"agent_id": "lily"},
        )
    )
    older_payload = json.loads(older_response.text)

    assert older_response.status == 200
    assert older_payload["history_complete"] is True
    assert older_payload["next_cursor"] is None
    assert [message["text"] for message in older_payload["messages"]] == [
        "A message from the earlier session"
    ]
    assert older_payload["messages"][0]["message_id"] == earlier_message["message_id"]
    with pytest.raises(ValueError):
        server._decode_agent_history_cursor(
            payload["next_cursor"],
            owner_id="user:8",
            agent_id="lily",
        )

@pytest.mark.asyncio
async def test_session_patch_archives_and_restores_without_deleting_records(tmp_path):
    server, _runtime = _server(tmp_path)
    owner = "user:7"
    session = server.session_store.create_session(
        owner_id=owner,
        agent_id="lily",
        title="Keep this Session",
    )
    server.session_store.accept_run(
        session_id=session["session_id"],
        owner_id=owner,
        agent_id="lily",
        request_id="archive-api",
        text="Persistent message",
        source="workbench",
        idempotency_key="archive-api",
    )
    current = server.session_store.get_session(session["session_id"], owner_id=owner)

    archived_response = await server.handle_v1_session_patch(
        _Request(
            {"status": "archived", "expected_revision": current["revision"]},
            match_info={"session_id": session["session_id"]},
        )
    )
    archived = json.loads(archived_response.text)["session"]
    assert archived_response.status == 200
    assert archived["status"] == "archived"
    assert archived_response.headers["ETag"] == f'"{archived["revision"]}"'

    restored_response = await server.handle_v1_session_patch(
        _Request(
            {"status": "active", "expected_revision": archived["revision"]},
            match_info={"session_id": session["session_id"]},
        )
    )
    restored = json.loads(restored_response.text)["session"]
    assert restored_response.status == 200
    assert restored["status"] == "active"
    assert [
        message["text"]
        for message in server.session_store.messages(session["session_id"], owner_id=owner)
    ] == ["Persistent message"]
# HASHI-Fingerprint: hfp_e8fcaf14d72b4d6bba89c5a6777a3203



@pytest.mark.asyncio
@pytest.mark.parametrize("multipart,audio", [(False, False), (True, False), (False, True)])
async def test_real_http_large_attachment_stream_respects_selected_capacity(tmp_path, monkeypatch, multipart, audio):
    import aiohttp
    import io
    import struct
    from aiohttp import web
    from orchestrator import audio_assets, session_store, workbench_api

    maximum = 70 * 1024 * 1024
    monkeypatch.setattr(session_store, "MAX_SESSION_ATTACHMENT_BYTES", maximum)
    monkeypatch.setattr(session_store, "MAX_SESSION_ATTACHMENT_TOTAL_BYTES", maximum)
    monkeypatch.setattr(workbench_api, "MAX_SESSION_ATTACHMENT_BYTES", maximum)
    monkeypatch.setattr(audio_assets, "MAX_AUDIO_ASSET_BYTES", maximum)
    server, _runtime = _server(tmp_path)
    owner = server._v1_owner_id(_Request())
    monkeypatch.setattr(server, "_v1_owner_id", lambda request: owner)
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    size = 65 * 1024 * 1024
    if audio:
        header = (b"RIFF" + struct.pack("<I", 36 + size) + b"WAVEfmt " +
                  struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
                  b"data" + struct.pack("<I", size))
        data, filename, media_type = header + bytes(size), "large.wav", "audio/wav"
    else:
        data, filename, media_type = b"x" * size, "large.txt", "text/plain"
    staged = server.session_store.stage_attachment(
        session_id=session["session_id"], owner_id=owner, filename=filename,
        media_type=media_type, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
    )
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_put("/sessions/{session_id}/attachments/{attachment_id}/content", server.handle_v1_attachment_upload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = "http://127.0.0.1:{}/sessions/{}/attachments/{}/content".format(
        port, session["session_id"], staged["attachment_id"],
    )
    if multipart:
        body = aiohttp.FormData()
        body.add_field("file", io.BytesIO(data), filename="large.txt", content_type="text/plain")
    else:
        body = data
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as client:
            async with client.put(url, data=body) as response:
                result = await response.json()
                assert response.status == 200, result
        server.session_store.commit_attachment(
            session_id=session["session_id"], owner_id=owner, attachment_id=staged["attachment_id"],
        )
        row = server.session_store.attachment_upload_metadata(
            session_id=session["session_id"], owner_id=owner, attachment_id=staged["attachment_id"],
        )
        if audio:
            info, path = server.session_store.audio_assets.authorized_path(
                staged["attachment_id"], owner_id=owner, session_id=session["session_id"],
            )
            assert info["duration_ms"] == round(size * 1000 / 32000)
        else:
            path = server.session_store._validated_attachment_file(row)
        assert path.stat().st_size == len(data)
        with path.open("rb") as stored:
            assert hashlib.file_digest(stored, "sha256").hexdigest() == hashlib.sha256(data).hexdigest()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_attachment_upload_rejects_wrong_owner_digest_and_actual_capacity(tmp_path, monkeypatch):
    from orchestrator import session_store, workbench_api

    monkeypatch.setattr(session_store, "MAX_SESSION_ATTACHMENT_BYTES", 10)
    monkeypatch.setattr(workbench_api, "MAX_SESSION_ATTACHMENT_BYTES", 4)
    server, _runtime = _server(tmp_path)
    owner = server._v1_owner_id(_Request())
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    staged = server.session_store.stage_attachment(
        session_id=session["session_id"], owner_id=owner, filename="data.txt",
        media_type="text/plain", size_bytes=5, sha256=hashlib.sha256(b"12345").hexdigest(),
    )
    match = {"session_id": session["session_id"], "attachment_id": staged["attachment_id"]}
    response = await server.handle_v1_attachment_upload(
        _Request(match_info=match, headers={"Content-Type": "text/plain"}, body=b"12345")
    )
    assert response.status == 413
    assert json.loads(response.text)["maximum_bytes"] == 4
    monkeypatch.setattr(workbench_api, "MAX_SESSION_ATTACHMENT_BYTES", 10)
    response = await server.handle_v1_attachment_upload(
        _Request(match_info=match, headers={"Content-Type": "text/plain"}, body=b"other")
    )
    assert response.status == 409
    assert "digest" in json.loads(response.text)["error"]
    monkeypatch.setattr(server, "_v1_owner_id", lambda request: "user:other")
    response = await server.handle_v1_attachment_upload(
        _Request(match_info=match, headers={"Content-Type": "text/plain"}, body=b"12345")
    )
    assert response.status == 404



@pytest.mark.asyncio
async def test_workzone_audio_is_streamed_and_retains_format_and_digest(tmp_path, monkeypatch):
    import io
    import wave
    server, runtime = _server(tmp_path)
    zone = tmp_path / "audio-zone"
    zone.mkdir()
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(3200))
    payload = stream.getvalue()
    (zone / "voice.wav").write_bytes(payload)
    runtime._workzone_state = {"revision": 1, "slots": [{
        "slot_id": "main", "path": str(zone), "enabled": True, "available": True,
    }]}
    owner = server._v1_owner_id(_Request())
    session = server.session_store.resolve_primary_session(owner_id=owner, agent_id="lily", establish=True)
    monkeypatch.setattr(
        server.session_store, "upload_attachment_bytes",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("audio must stream")),
    )
    response = await server.handle_v1_workzone_attachment_stage(_Request(
        {"reference": str(zone / "voice.wav")}, match_info={"session_id": session["session_id"]},
    ))
    assert response.status == 201, response.text
    attachment = json.loads(response.text)["attachment"]
    metadata, actual = server.session_store.attachment_bytes(
        session_id=session["session_id"], owner_id=owner, attachment_id=attachment["attachment_id"],
    )
    assert actual == payload and metadata["sha256"] == hashlib.sha256(payload).hexdigest()
    assert metadata["duration_ms"] == 100


@pytest.mark.asyncio
async def test_attachment_sqlite_contention_keeps_status_responsive_and_returns_busy(tmp_path, monkeypatch):
    import asyncio
    import sqlite3
    import time

    monkeypatch.setenv("HASHI_ATTACHMENT_STORAGE_WAIT_MS", "600")
    server, _runtime = _server(tmp_path)
    store = server.session_store
    session = store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    session_id = session["session_id"]
    content = b"fixture"
    payload = {"filename": "fixture.txt", "media_type": "text/plain", "size_bytes": len(content),
               "sha256": hashlib.sha256(content).hexdigest(), "idempotency_key": "contention-stage"}
    request = _Request(payload, match_info={"session_id": session_id})

    async def check_while_locked(handler):
        connection = sqlite3.connect(store.db_path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            operation = asyncio.create_task(handler())
            await asyncio.sleep(0.05)
            assert not operation.done()
            response = await server.handle_v1_capabilities(_Request())
            assert response.status == 200
            assert time.monotonic() - started < 0.4
            busy = await asyncio.wait_for(operation, 3)
            assert busy.status == 503 and json.loads(busy.text)["code"] == "session_storage_busy"
        finally:
            connection.rollback()
            connection.close()

    await check_while_locked(lambda: server.handle_v1_attachment_stage(request))
    first = await server.handle_v1_attachment_stage(request)
    assert first.status == 201
    attachment_id = json.loads(first.text)["attachment"]["attachment_id"]
    repeated = await server.handle_v1_attachment_stage(request)
    assert json.loads(repeated.text)["attachment"]["attachment_id"] == attachment_id
    match = {"session_id": session_id, "attachment_id": attachment_id}
    uploaded = await server.handle_v1_attachment_upload(_Request(match_info=match,
        headers={"Content-Type": "text/plain"}, body=content))
    assert uploaded.status == 200
    commit_request = _Request(match_info=match)
    await check_while_locked(lambda: server.handle_v1_attachment_commit(commit_request))
    committed = await server.handle_v1_attachment_commit(commit_request)
    assert committed.status == 200
    record = json.loads(committed.text)["attachment"]
    assert record["state"] == "committed"
    replay = await server.handle_v1_attachment_commit(commit_request)
    assert json.loads(replay.text)["attachment"]["committed_at"] == record["committed_at"]
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM session_attachments WHERE session_id=?", (session_id,)).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_cancelled_attachment_stage_settles_once_and_can_reconcile_by_key(tmp_path, monkeypatch):
    import asyncio
    import sqlite3
    import threading

    server, _runtime = _server(tmp_path)
    store = server.session_store
    session_id = store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)["session_id"]
    payload = {"filename": "fixture.txt", "media_type": "text/plain", "size_bytes": 1,
               "sha256": hashlib.sha256(b"x").hexdigest(), "idempotency_key": "cancelled-stage"}
    request = _Request(payload, match_info={"session_id": session_id})
    started, release = threading.Event(), threading.Event()
    original = server._v1_stage_attachment
    calls = []
    def delayed(**kwargs):
        calls.append("submitted")
        started.set()
        assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(server, "_v1_stage_attachment", delayed)
    task = asyncio.create_task(server.handle_v1_attachment_stage(request))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    monkeypatch.setattr(server, "_v1_stage_attachment", original)
    recovered = await server.handle_v1_attachment_stage(request)
    assert recovered.status == 201 and calls == ["submitted"]
    attachment_id = json.loads(recovered.text)["attachment"]["attachment_id"]
    with sqlite3.connect(store.db_path) as connection:
        rows = connection.execute("SELECT attachment_id FROM session_attachments WHERE session_id=?", (session_id,)).fetchall()
    assert rows == [(attachment_id,)]


@pytest.mark.asyncio
async def test_invalid_search_or_attachment_budget_does_not_hide_other_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv("HASHI_HISTORY_SEARCH_MAX_QUERY_CHARS", "bad")
    monkeypatch.setenv("HASHI_ATTACHMENT_STORAGE_WAIT_MS", "bad")
    server, _runtime = _server(tmp_path)
    server.global_config.persistent_session_v1 = True
    response = await server.handle_v1_capabilities(_Request())
    assert response.status == 200
    data = json.loads(response.text)
    assert data["session_api_version"] == "1.0"
    assert data["agent_history_search"]["available"] is False
    assert data["agent_history_search"]["configuration_error"] == "invalid_history_search_budget"
    assert data["frontend_connector"]["attachment_storage_wait_ms"] is None
    assert data["frontend_connector"]["attachment_storage_wait_configured"] is False


@pytest.mark.asyncio
async def test_attachment_inprocess_lock_timeout_is_reported_without_blocking_loop(tmp_path, monkeypatch):
    import asyncio
    import threading
    import time

    monkeypatch.setenv("HASHI_ATTACHMENT_STORAGE_WAIT_MS", "300")
    server, _runtime = _server(tmp_path)
    store = server.session_store
    session_id = store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)["session_id"]
    held, release = threading.Event(), threading.Event()
    def lock_store():
        with store._lock:
            held.set()
            release.wait(3)
    thread = threading.Thread(target=lock_store)
    thread.start()
    try:
        assert await asyncio.to_thread(held.wait, 2)
        payload = {"filename": "x.txt", "media_type": "text/plain", "size_bytes": 1,
                   "sha256": hashlib.sha256(b"x").hexdigest(), "idempotency_key": "inprocess-wait"}
        start = time.monotonic()
        operation = asyncio.create_task(server.handle_v1_attachment_stage(
            _Request(payload, match_info={"session_id": session_id})))
        await asyncio.sleep(0.04)
        assert time.monotonic() - start < 0.2
        response = await asyncio.wait_for(operation, 2)
        assert response.status == 503 and json.loads(response.text)["code"] == "session_storage_busy"
    finally:
        release.set()
        await asyncio.to_thread(thread.join, 2)
    response = await server.handle_v1_attachment_stage(_Request(payload, match_info={"session_id": session_id}))
    assert response.status == 201


@pytest.mark.asyncio
async def test_session_update_and_snapshot_do_not_block_status_during_sqlite_writer(tmp_path):
    import asyncio
    import sqlite3
    import time

    server, _runtime = _server(tmp_path)
    store = server.session_store
    session = store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    session_id = session["session_id"]
    connection = sqlite3.connect(store.db_path, isolation_level=None)
    update = snapshot = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        update = asyncio.create_task(server.handle_v1_session_patch(_Request(
            {"expected_revision": session["revision"], "title": "new title"},
            match_info={"session_id": session_id})))
        await asyncio.sleep(0.05)
        assert not update.done()
        snapshot = asyncio.create_task(server.handle_v1_session_snapshot(
            _Request(match_info={"session_id": session_id})))
        await asyncio.sleep(0.05)
        assert not snapshot.done()
        response = await server.handle_v1_capabilities(_Request())
        assert response.status == 200
        assert time.monotonic() - start < 0.5
    finally:
        connection.rollback()
        connection.close()
    updated = await asyncio.wait_for(update, 3)
    observed = await asyncio.wait_for(snapshot, 3)
    assert updated.status == 200 and observed.status == 200
    assert json.loads(updated.text)["session"]["title"] == "new title"

@pytest.mark.asyncio
async def test_command_reservation_keeps_status_responsive_and_dispatches_once(tmp_path, monkeypatch):
    import asyncio
    import sqlite3
    import threading
    import time

    from orchestrator import command_interaction_bridge, slash_command_audit

    server, _runtime = _command_server(tmp_path)
    session = server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    started = threading.Event()
    original = server.session_store.reserve_frontend_command_invocation
    calls = []

    def observed_reservation(**kwargs):
        started.set()
        return original(**kwargs)

    async def dispatch(_runtime, _payload, _metadata):
        calls.append(1)
        return {"ok": True, "http_status": 200}

    monkeypatch.setattr(server.session_store, "reserve_frontend_command_invocation", observed_reservation)
    monkeypatch.setattr(slash_command_audit, "is_supported_slash_command", lambda _runtime, _command: True)
    monkeypatch.setattr(command_interaction_bridge, "dispatch_command_interaction", dispatch)
    payload = {
        "command": "model", "arguments": ["balanced"], "client_id": "session-api-client-7",
        "request_id": "command-request-123456", "ui_locale": "en",
        "context_generation": session["context_generation"],
    }

    def request():
        return _command_request(payload, match_info={"session_id": session["session_id"]})

    connection = sqlite3.connect(server.session_store.db_path, isolation_level=None)
    task = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(server.handle_v1_session_command_invocation(request()))
        assert await asyncio.to_thread(started.wait, 2)
        assert not task.done()
        before = time.monotonic()
        status = await server.handle_v1_capabilities(_Request())
        assert status.status == 200
        assert time.monotonic() - before < 1
        connection.rollback()
        result = await asyncio.wait_for(task, 3)
        assert result.status == 200, result.text
        replay = await server.handle_v1_session_command_invocation(request())
        assert replay.status == 200
        assert json.loads(replay.text)["replayed"] is True
        assert calls == [1]
    finally:
        connection.rollback()
        connection.close()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_promotion_configuration_keeps_status_responsive_during_database_writer(tmp_path, monkeypatch):
    import asyncio
    import sqlite3
    import threading
    import time

    server, _runtime = _server(tmp_path)
    started = threading.Event()
    original = server.session_store.set_promotion_schedule

    def observed_schedule(**kwargs):
        started.set()
        return original(**kwargs)

    monkeypatch.setattr(server.session_store, "set_promotion_schedule", observed_schedule)
    connection = sqlite3.connect(server.session_store.db_path, isolation_level=None)
    task = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        task = asyncio.create_task(server.handle_v1_promotion_post(_Request(
            {"action": "configure", "enabled": False, "local_time": "04:30"},
            match_info={"agent_id": "lily"},
        )))
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.05)
        assert not task.done()
        before = time.monotonic()
        status = await server.handle_v1_capabilities(_Request())
        assert status.status == 200
        assert time.monotonic() - before < 1
        connection.rollback()
        result = await asyncio.wait_for(task, 3)
        assert result.status == 200, result.text
        assert json.loads(result.text)["schedule"]["local_time"] == "04:30"
        readback = await server.handle_v1_promotion_get(_Request(match_info={"agent_id": "lily"}))
        assert json.loads(readback.text)["promotion"]["schedule"]["enabled"] == 0
    finally:
        connection.rollback()
        connection.close()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)

@pytest.mark.asyncio
async def test_capabilities_describe_long_draft_defaults_without_claiming_worker_configuration(tmp_path):
    server, _runtime = _server(tmp_path)
    response = await server.handle_v1_capabilities(_Request())
    capability = json.loads(response.text)["telegram_long_draft"]
    assert capability == {
        "version": 1,
        "idle_actions": ["remind", "submit"],
        "default_idle_action": "remind",
        "default_idle_seconds": 300,
        "configurable_idle_seconds": True,
        "explicit_submit_excludes_unconfirmed_voice": True,
        "draft_persistence": "process_memory",
    }

@pytest.mark.asyncio
async def test_chat_projection_waits_off_loop_for_store_lock(tmp_path):
    import asyncio
    import threading
    import time

    server, _runtime = _server(tmp_path)
    session = server.session_store.resolve_primary_session(owner_id="user:7", agent_id="lily", establish=True)
    locked, release = threading.Event(), threading.Event()

    def hold_lock():
        with server.session_store._lock:
            locked.set()
            release.wait(3)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    task = None
    try:
        assert await asyncio.to_thread(locked.wait, 2)
        task = asyncio.create_task(server.handle_chat_projection(_Request(
            match_info={"name": "lily"}, query={"session_id": session["session_id"]},
        )))
        await asyncio.sleep(0.05)
        assert not task.done()
        before = time.monotonic()
        assert (await server.handle_v1_capabilities(_Request())).status == 200
        assert time.monotonic() - before < 1
    finally:
        release.set()
        await asyncio.to_thread(thread.join, 2)
    response = await asyncio.wait_for(task, 3)
    assert response.status == 200, response.text
    assert json.loads(response.text)["projection"]["session_id"] == session["session_id"]
@pytest.mark.asyncio
async def test_capabilities_advertise_selected_private_proof_budget_without_secrets(tmp_path):
    server, _runtime = _server(tmp_path)
    (tmp_path / "secrets.json").write_text(json.dumps({
        "hashi_private_shared_credentials": {
            "credentials": {"team": {"secret": "synthetic-secret-long-enough", "scopes": ["read.fixture"]}},
            "policy": {"max_proofs_per_message": 24, "max_resources_per_message": 80,
                       "max_ttl_seconds": 1200, "default_ttl_seconds": 600},
        }
    }), encoding="utf-8")
    for response in (await server.handle_v1_capabilities(_Request()),
                     await server.handle_message_source_capabilities(_Request())):
        payload = json.loads(response.text)
        assert payload["private_authorization"]["max_proofs_per_message"] == 24
        assert payload["private_authorization"]["max_resources_per_message"] == 80
        assert payload["private_authorization"]["max_ttl_seconds"] == 1200
        assert "synthetic-secret" not in response.text


@pytest.mark.asyncio
async def test_quote_source_exact_visible_message_obeys_session_owner(tmp_path):
    server, _ = _server(tmp_path)
    store = server.session_store
    session = store.create_session(owner_id="user:7", agent_id="lily")
    accepted = store.accept_run(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        request_id="quote-source-run", text="PRIVATE_CANONICAL", display_text="visible 🌸" * 1000,
        source="workbench", idempotency_key="quote-source",
        message_context={"private_marker": "PRIVATE_CONTEXT"},
    )
    request = _Request(match_info={"session_id": session["session_id"], "message_id": accepted.message_id})
    response = await server.handle_v1_session_quote_source(request)
    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["message"]["text"] == "visible 🌸" * 1000
    assert payload["message"]["message_id"] == accepted.message_id
    assert "PRIVATE_CANONICAL" not in response.text
    assert "PRIVATE_CONTEXT" not in response.text
    assert "no-store" in response.headers["Cache-Control"]

    wrong_session = store.create_session(owner_id="user:7", agent_id="lily")
    wrong = await server.handle_v1_session_quote_source(_Request(match_info={
        "session_id": wrong_session["session_id"], "message_id": accepted.message_id}))
    assert wrong.status == 404

    other = store.create_session(owner_id="user:8", agent_id="lily")
    other_msg = store.append_presentation_message(
        session_id=other["session_id"], owner_id="user:8", agent_id="lily", role="assistant",
        text="other-owner", source="test", idempotency_key="other", history_eligible=True,
    )
    denied = await server.handle_v1_session_quote_source(_Request(match_info={
        "session_id": other["session_id"], "message_id": other_msg["message_id"]}))
    assert denied.status == 404

    with store._connection() as connection:
        connection.execute("UPDATE messages SET visibility='forgotten' WHERE message_id=?", (accepted.message_id,))
        connection.commit()
    hidden = await server.handle_v1_session_quote_source(request)
    assert hidden.status == 404


@pytest.mark.asyncio
async def test_quote_source_route_reads_a_synthetic_session_over_http(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    monkeypatch.setattr(WorkbenchApiServer, "_persistent_session_v1_ready", lambda self: True)
    server, _ = _server(tmp_path)
    session = server.session_store.create_session(owner_id="user:7", agent_id="lily")
    message = server.session_store.append_presentation_message(
        session_id=session["session_id"], owner_id="user:7", agent_id="lily",
        role="assistant", text="synthetic old source", source="test",
        idempotency_key="quote-http", history_eligible=True,
    )
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get(
            f"/api/v1/sessions/{session['session_id']}/messages/{message['message_id']}/quote-source"
        )
        assert response.status == 200
        assert (await response.json())["message"]["text"] == "synthetic old source"
