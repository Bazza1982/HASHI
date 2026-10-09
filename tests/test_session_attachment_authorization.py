"""Real Session intake and task-local backend consumption authority."""
import asyncio
import hashlib
import io
import logging
import os
import wave
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse
from adapters.codex_cli import CodexCLIAdapter
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.multimodal_contract import validate_authorized_media_references
from orchestrator.session_store import SessionStore
from tests.mocks.mock_adapters import MockBackend, SimpleGlobalConfig, SimpleTestConfig


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 160)
    return out.getvalue()


class Consumer(MockBackend):
    async def generate_response(self, prompt, request_id, *, request_content=None, **kwargs):
        self.seen_prompt = prompt
        self.seen_content = deepcopy(request_content)
        self.seen_roots = self.authorized_media_roots()
        if getattr(self, "before_read", None):
            await self.before_read()
        validate_authorized_media_references(request_content, authorized_roots=self.authorized_media_roots())
        return BackendResponse(text="consumed", duration_ms=0, is_success=True)


class CodexConsumer(Consumer, CodexCLIAdapter):
    """The actual selected adapter type with a byte/prompt observation endpoint."""


def intake(store, session, *, filename="voice.wav", voice=False):
    payload = wav_bytes()
    row = store.stage_attachment(session_id=session["session_id"], owner_id=session["owner_id"],
        filename=filename, media_type="audio/wav", size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(), semantic_role="voice_message" if voice else "audio_attachment")
    store.upload_attachment_bytes(session_id=session["session_id"], owner_id=session["owner_id"],
        attachment_id=row["attachment_id"], payload=payload)
    store.commit_attachment(session_id=session["session_id"], owner_id=session["owner_id"],
        attachment_id=row["attachment_id"])
    return store.attachment_canonical_part(session_id=session["session_id"], owner_id=session["owner_id"],
        attachment_id=row["attachment_id"], item_index=1)


def fixture(tmp_path, *, agent="qa", owner="user:qa", store=None, codex=False):
    store = store or SessionStore(tmp_path / "sessions.sqlite3", instance_id="HASHI1")
    session = store.ensure_default_session(owner_id=owner, agent_id=agent)
    part = intake(store, session, voice=codex)
    accepted = store.accept_run(session_id=session["session_id"], owner_id=owner, agent_id=agent,
        request_id=f"req-{agent}-{part['attachment_id']}", text="synthetic", source="workbench",
        idempotency_key=part["attachment_id"], content=[{"type":"attachment", "attachment_id":part["attachment_id"]}])
    fence = store.mark_request_running(accepted.request_id, worker_id="qa-worker")
    run = store.get_run_by_request(accepted.request_id)
    cfg = SimpleTestConfig(name=agent, workspace_dir=str(tmp_path / agent))
    cfg.workspace_dir.mkdir(exist_ok=True)
    backend = (CodexConsumer if codex else Consumer)(cfg, SimpleGlobalConfig())
    meta = {"request_id":accepted.request_id, "hashi_session_id":session["session_id"],
        "hashi_run_id":accepted.run_id, "hashi_message_id":run["user_message_id"],
        "hashi_fencing_token":fence, "owner_id":owner, "context_generation":session["context_generation"]}
    runtime = SimpleNamespace(name=agent, session_store=store, _request_meta_by_id={accepted.request_id:meta})
    mgr = object.__new__(FlexibleBackendManager)
    mgr.current_backend = backend
    mgr.runtime = runtime
    mgr.config = cfg
    mgr.logger = logging.getLogger("session-media-test")
    content = {"type":"hashi.request-content", "version":1, "parts":[part]}
    return mgr, backend, store, session, run, meta, content


@pytest.mark.asyncio
async def test_committed_current_run_audio_is_consumed_without_directory_grants(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    original = backend.authorized_media_roots()
    assert Path(content["parts"][0]["local_ref"]) not in original
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert response.is_success, response.error
    exact = Path(content["parts"][0]["local_ref"])
    assert exact in backend.seen_roots
    assert exact.parent not in backend.seen_roots
    assert backend.authorized_media_roots() == original
    assert backend.effective_add_dirs == (backend.config.workspace_dir,)


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("request_id","forged"), ("hashi_session_id","other"),
    ("hashi_run_id","other"), ("hashi_message_id","other"), ("hashi_fencing_token",77),
    ("owner_id","user:other"), ("context_generation",99)])
async def test_forged_request_scope_is_rejected(tmp_path, key, value):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    meta[key] = value
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success and response.error_code == "MEDIA_PATH_NOT_AUTHORIZED"
    assert not hasattr(backend, "seen_roots")


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("local_ref","/tmp/foreign-audio.wav"), ("mime_type","audio/ogg"),
    ("sha256","0" * 64), ("size_bytes",555)])
async def test_forged_media_canonical_is_rejected(tmp_path, key, value):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    content["parts"][0][key] = value
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success
    assert not hasattr(backend, "seen_roots")


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["same_session_unused", "other_session", "other_owner", "other_agent"])
async def test_only_original_run_attachment_is_authorized(tmp_path, origin):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    if origin == "same_session_unused":
        foreign = intake(store, session)
    else:
        other = store.create_session(owner_id="user:other" if origin == "other_owner" else session["owner_id"],
            agent_id="other" if origin == "other_agent" else "qa", title="synthetic")
        foreign = intake(store, other)
    content["parts"][0] = foreign
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success and response.error_code == "MEDIA_PATH_NOT_AUTHORIZED"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["symlink", "replacement", "content", "parent_symlink", "parent_replacement"])
async def test_file_fence_rechecked_at_backend_byte_read(tmp_path, change):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    path = Path(content["parts"][0]["local_ref"])
    original_roots = backend.authorized_media_roots()
    async def mutate():
        if change in {"parent_symlink", "parent_replacement"}:
            renamed = path.parent.with_name("old-files")
            path.parent.rename(renamed)
            if change == "parent_symlink":
                path.parent.symlink_to(renamed, target_is_directory=True)
            else:
                path.parent.mkdir()
                path.write_bytes((renamed / path.name).read_bytes())
        elif change == "content":
            payload = bytearray(path.read_bytes())
            payload[-1] ^= 1
            path.write_bytes(payload)
        else:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            if change == "symlink":
                path.unlink()
                path.symlink_to(replacement)
            else:
                replacement.replace(path)
    backend.before_read = mutate
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success
    assert response.error_code in {"MEDIA_PATH_NOT_AUTHORIZED", "MEDIA_INTEGRITY_CHANGED"}
    assert backend.authorized_media_roots() == original_roots


@pytest.mark.asyncio
async def test_asset_metadata_cannot_forge_committed_identity(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    with store._connection() as conn:
        asset = conn.execute("SELECT asset_id FROM session_attachments WHERE attachment_id=?", (content["parts"][0]["attachment_id"],)).fetchone()[0]
    metadata = store.audio_assets._read(asset)
    metadata["sha256"] = "0" * 64
    store.audio_assets._write(metadata)
    content["parts"][0]["sha256"] = "0" * 64
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success and response.error_code == "MEDIA_INTEGRITY_CHANGED"


@pytest.mark.asyncio
async def test_task_local_grants_do_not_cross_requests_or_survive_children(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    other_mgr, other_backend, _, _, other_run, _, other_content = fixture(tmp_path, agent="other", store=store)
    original = backend.authorized_media_roots()
    captured = []
    released = asyncio.Event()
    async def child():
        await released.wait()
        captured.append(backend.authorized_media_roots())
    async def inside():
        task = asyncio.create_task(child())
        captured.append(task)
        result = await other_mgr.generate_response("synthetic", other_run["request_id"], request_content=other_content)
        assert result.is_success
        assert Path(content["parts"][0]["local_ref"]) not in other_backend.seen_roots
        assert Path(other_content["parts"][0]["local_ref"]) not in backend.authorized_media_roots()
    backend.before_read = inside
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert response.is_success
    released.set()
    await captured[0]
    assert captured[1] == original


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_grants_reset_on_exception_or_cancellation(tmp_path, cancel):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    original = backend.authorized_media_roots()
    entered = asyncio.Event()
    async def stop():
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("synthetic failure")
    backend.before_read = stop
    task = asyncio.create_task(mgr.generate_response("synthetic", run["request_id"], request_content=content))
    await asyncio.wait_for(entered.wait(), 3)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    assert backend.authorized_media_roots() == original


def transcript_fixture(mgr, store, run, content, *, status="released", safe=False):
    mgr.config.active_backend = "codex-cli"
    attachment_id = content["parts"][0]["attachment_id"]
    record = store.record_voice_transcript(request_id=run["request_id"], attachment_id=attachment_id,
        text="SYNTHETIC_VOICE_BLUE", provenance="local_stt", safe_voice_state=status)
    state = {"request_id":run["request_id"], "attachment_id":attachment_id, "attachment_ids":[attachment_id],
        "safe_voice":safe, "status":status, "text":"DO_NOT_TRUST_MEMORY", "ready_event":asyncio.Event(),
        "release_event":asyncio.Event(), "gate_lock":asyncio.Lock()}
    state["ready_event"].set()
    if status not in {"ready", "pending_confirmation"}:
        state["release_event"].set()
    mgr.runtime._native_voice_transcripts = {run["request_id"]:state, attachment_id:state}
    return record, state


@pytest.mark.asyncio
async def test_codex_consumes_only_same_run_durable_released_transcript(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    record, state = transcript_fixture(mgr, store, run, content)
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert response.is_success, response.error
    assert "SYNTHETIC_VOICE_BLUE" in backend.seen_prompt
    assert "DO_NOT_TRUST_MEMORY" not in backend.seen_prompt
    assert all(part["type"] != "media" for part in backend.seen_content["parts"])
    assert response.stream_metadata["multimodal_routing"][0]["transcript_id"] == record["transcript_id"]
    assert response.stream_metadata["multimodal_routing"][0]["route"] == "local_transcript"
    assert content["parts"][0]["type"] == "media"


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [True, False])
async def test_codex_waits_for_safevoice_confirmation_and_honors_discard(tmp_path, confirm):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    record, state = transcript_fixture(mgr, store, run, content, status="ready", safe=True)
    challenged = asyncio.Event()
    async def challenge():
        store.require_voice_transcript_confirmation(request_id=run["request_id"])
        state["status"] = "pending_confirmation"
        challenged.set()
    state["request_confirmation"] = challenge
    task = asyncio.create_task(mgr.generate_response("synthetic", run["request_id"], request_content=content))
    await asyncio.wait_for(challenged.wait(), 2)
    assert not hasattr(backend, "seen_prompt")
    store.decide_voice_transcript(request_id=run["request_id"], confirmed=confirm)
    state["status"] = "released" if confirm else "discarded"
    state["release_event"].set()
    response = await asyncio.wait_for(task, 2)
    assert response.is_success == confirm
    if not confirm:
        assert response.error_code == "MEDIA_FALLBACK_UNAVAILABLE"
        assert "discarded" in response.error
        assert not hasattr(backend, "seen_prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_state", "other_run", "other_attachment", "not_durable", "durable_unreleased", "unavailable"])
async def test_codex_voice_cannot_bypass_durable_gate(tmp_path, fault):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    record, state = transcript_fixture(mgr, store, run, content)
    if fault == "missing_state":
        mgr.runtime._native_voice_transcripts = {}
    elif fault == "other_run":
        state["request_id"] = "req-other"
    elif fault == "other_attachment":
        state["attachment_ids"] = ["att-other"]
    elif fault == "unavailable":
        state["status"] = "unavailable"
    else:
        with store._connection() as conn:
            if fault == "not_durable":
                conn.execute("DELETE FROM voice_transcripts WHERE transcript_id=?", (record["transcript_id"],))
            else:
                conn.execute("UPDATE voice_transcripts SET safe_voice_state='ready' WHERE transcript_id=?", (record["transcript_id"],))
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success
    assert not hasattr(backend, "seen_prompt")


@pytest.mark.asyncio
async def test_voice_role_cannot_be_forged_to_bypass_audio_file_policy(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    record, state = transcript_fixture(mgr, store, run, content)
    with store._connection() as conn:
        conn.execute("UPDATE session_attachments SET semantic_role='audio_attachment' WHERE attachment_id=?", (content["parts"][0]["attachment_id"],))
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert not response.is_success and response.error_code == "MEDIA_INTEGRITY_CHANGED"
    assert not hasattr(backend, "seen_prompt")


@pytest.mark.asyncio
async def test_released_voice_reaches_real_codex_cli_transport_without_raw_audio(tmp_path, monkeypatch):
    import json
    from adapters.codex_cli import CodexCLIAdapter
    from tests.test_codex_cli import _HangingProc
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    record, state = transcript_fixture(mgr, store, run, content)
    global_config = SimpleGlobalConfig()
    global_config.project_root = tmp_path
    mgr.config.model = "gpt-5.6-sol"
    adapter = CodexCLIAdapter(mgr.config, global_config)
    mgr.current_backend = adapter
    proc = _HangingProc([json.dumps({"type":"item.completed", "item":{"type":"agent_message", "text":"SYNTHETIC_VOICE_BLUE"}}),
        json.dumps({"type":"turn.completed"})])
    command = []
    async def spawn(*args, **kwargs):
        command.extend(args)
        proc.finish(0)
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert response.is_success, response.error
    assert response.text == "SYNTHETIC_VOICE_BLUE"
    prompt = command[command.index("--") + 1]
    if prompt == "-":
        prompt = proc.stdin.data.decode()
    assert "SYNTHETIC_VOICE_BLUE" in prompt
    assert content["parts"][0]["local_ref"] not in command
    assert "--image" not in command
    assert response.stream_metadata["multimodal_routing"][0]["reason"] == "local_stt_released"


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["instance", "agent", "runtime_agent", "terminal_run", "staged", "expired", "storage_name", "fifo"])
async def test_durable_scope_and_storage_lifecycle_fail_closed(tmp_path, fault):
    import os
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    part = content["parts"][0]
    with store._connection() as conn:
        asset_id = conn.execute("SELECT asset_id FROM session_attachments WHERE attachment_id=?", (part["attachment_id"],)).fetchone()[0]
    if fault == "instance":
        store.instance_id = "SYNTHETIC_OTHER_INSTANCE"
    elif fault == "agent":
        mgr.config.name = "other"
        mgr.runtime.name = "other"
    elif fault == "runtime_agent":
        mgr.runtime.name = "other"
    elif fault == "terminal_run":
        with store._connection() as conn:
            conn.execute("UPDATE runs SET state='cancelled' WHERE run_id=?", (run["run_id"],))
    elif fault == "staged":
        with store._connection() as conn:
            conn.execute("UPDATE session_attachments SET state='staged' WHERE attachment_id=?", (part["attachment_id"],))
    elif fault == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("This platform has no filesystem FIFO support")
        path = Path(part["local_ref"])
        path.unlink()
        os.mkfifo(path)
    else:
        metadata = store.audio_assets._read(asset_id)
        if fault == "expired":
            metadata["state"] = "expired"
        else:
            replacement = Path(part["local_ref"]).with_name("foreign.wav")
            replacement.write_bytes(Path(part["local_ref"]).read_bytes())
            metadata["storage_name"] = replacement.name
            part["local_ref"] = str(replacement)
        store.audio_assets._write(metadata)
    response = await asyncio.wait_for(mgr.generate_response("synthetic", run["request_id"], request_content=content), 3)
    assert not response.is_success
    assert not hasattr(backend, "seen_prompt")


@pytest.mark.asyncio
@pytest.mark.parametrize("within_existing_root", [False, True])
async def test_unsupported_platform_preserves_existing_roots_without_new_grants(tmp_path, monkeypatch, within_existing_root):
    import orchestrator.session_attachment_authorization as authority
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    if within_existing_root:
        backend.global_config.base_media_dir = Path(content["parts"][0]["local_ref"]).parent
    original = backend.authorized_media_roots()
    monkeypatch.setattr(authority, "session_attachment_grants_supported", lambda:False)
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert response.is_success == within_existing_root
    assert backend.authorized_media_roots() == original
    if within_existing_root:
        assert backend.seen_roots == original
    else:
        assert response.error_code == "MEDIA_PATH_NOT_AUTHORIZED"
        assert "unavailable on this platform" in response.error


@pytest.mark.asyncio
async def test_actual_backend_identity_overrides_stale_config_selection(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path, codex=True)
    record, state = transcript_fixture(mgr, store, run, content)
    mgr.config.active_backend = "her-v3"
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert response.is_success and "SYNTHETIC_VOICE_BLUE" in backend.seen_prompt
    # Switching the actual consumer away from Codex must retain its audio route.
    other = Consumer(mgr.config, backend.global_config)
    mgr.current_backend = other
    mgr.config.active_backend = "codex-cli"
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert response.is_success
    assert other.seen_content["parts"][0]["type"] == "media"
    assert "SYNTHETIC_VOICE_BLUE" not in other.seen_prompt


@pytest.mark.asyncio
async def test_active_run_lease_retains_audio_across_cleanup_deadline(tmp_path):
    mgr, backend, store, session, run, meta, content = fixture(tmp_path)
    with store._connection() as conn:
        asset_id = conn.execute("SELECT asset_id FROM session_attachments WHERE attachment_id=?", (content["parts"][0]["attachment_id"],)).fetchone()[0]
    metadata = store.audio_assets._read(asset_id)
    assert metadata["lease_count"] > 0
    metadata["retention_expires_at"] = "2000-01-01T00:00:00+00:00"
    store.audio_assets._write(metadata)
    assert not store.audio_assets.cleanup()
    response = await mgr.generate_response("synthetic", run["request_id"], request_content=content)
    assert response.is_success


@pytest.mark.asyncio
async def test_simultaneous_requests_on_same_backend_keep_distinct_file_grants(tmp_path):
    first, backend, store, session, run, meta, content = fixture(tmp_path)
    second, _, _, _, run2, meta2, content2 = fixture(tmp_path, owner="user:second", store=store)
    entered = []
    ready = asyncio.Event()
    paths = {run["request_id"]:Path(content["parts"][0]["local_ref"]),
        run2["request_id"]:Path(content2["parts"][0]["local_ref"])}
    class ParallelConsumer(Consumer):
        async def generate_response(self, prompt, request_id, *, request_content=None, **kwargs):
            entered.append(request_id)
            if len(entered) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 2)
            roots = self.authorized_media_roots()
            assert paths[request_id] in roots
            assert next(path for rid,path in paths.items() if rid != request_id) not in roots
            validate_authorized_media_references(request_content, authorized_roots=roots)
            return BackendResponse(text="parallel consumed", duration_ms=0, is_success=True)
    shared = ParallelConsumer(backend.config, backend.global_config)
    first.current_backend = second.current_backend = shared
    original = shared.authorized_media_roots()
    responses = await asyncio.gather(
        first.generate_response("synthetic", run["request_id"], request_content=content),
        second.generate_response("synthetic", run2["request_id"], request_content=content2))
    assert all(response.is_success for response in responses)
    assert shared.authorized_media_roots() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows handle sharing boundary")
def test_windows_attachment_parent_cannot_be_modified_during_secure_open(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    import orchestrator.session_attachment_authorization as authority

    parent = tmp_path / "attachments"
    parent.mkdir()
    path = parent / "fixture.bin"
    path.write_bytes(b"authorized bytes")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def writable_parent():
        handle = create(str(parent), 0x40000000, 0x7, None, 3, 0x02000000, None)
        if handle == ctypes.c_void_p(-1).value:
            return ctypes.get_last_error()
        kernel.CloseHandle(handle)
        return 0

    assert writable_parent() == 0
    real_stat = os.stat
    observed = []

    def check_pinned_parent(target, *args, **kwargs):
        if Path(target) == parent:
            observed.append(writable_parent())
            assert observed[-1] == 32  # ERROR_SHARING_VIOLATION prevents reparse edits.
        return real_stat(target, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(authority.os, "stat", check_pinned_parent)
        fd, _ = authority._open_no_symlinks(path, os.O_RDONLY)
        with os.fdopen(fd, "rb") as source:
            assert source.read() == b"authorized bytes"
    assert observed
    assert writable_parent() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,batch", [("photo", False), ("document", False),
                                       ("video", False), ("photo", True)])
async def test_telegram_download_reaches_backend_as_current_run_attachment(tmp_path, kind, batch):
    """Real Telegram intake -> persisted Message/Run -> backend read authority."""
    from orchestrator import runtime_long, runtime_media, runtime_session
    from tests.test_runtime_media import _runtime, _update

    mgr, backend, store, session, _, _, _ = fixture(tmp_path, agent="zelda", owner="user:1")
    runtime = _runtime(tmp_path)
    runtime.session_store = store
    runtime.global_config = SimpleNamespace(authorized_id=1)
    runtime.backend_manager = mgr
    runtime._request_meta_by_id = {}
    mgr.runtime = runtime
    backend.tool_registry = SimpleNamespace(is_allowed=lambda name: True)
    received = []

    async def download(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = (b"\xff\xd8\xff" + b"synthetic jpeg" if kind == "photo" else
                   b"%PDF-1.4\nsynthetic" if kind == "document" else
                   b"\x00\x00\x00\x18ftypmp42" + b"synthetic video")
        target.write_bytes(payload)
        runtime.app.bot.file.downloaded_to = target

    runtime.app.bot.file.download_to_drive = download

    async def enqueue(chat_id, prompt, source, summary, **kwargs):
        content = kwargs["request_content"]
        route, accepted, owner, _, _ = runtime_session.accept_request(
            runtime, request_id="req-telegram-media", chat_id=chat_id,
            prompt=prompt, source=source, request_metadata=kwargs["request_metadata"],
            request_content=content, idempotency_key="telegram-media",
        )
        fence = store.mark_request_running(accepted.request_id, worker_id="telegram-worker")
        meta = {"request_id": accepted.request_id, "hashi_session_id": route["session_id"],
                "hashi_run_id": accepted.run_id, "hashi_message_id": accepted.message_id,
                "hashi_fencing_token": fence, "owner_id": owner,
                "context_generation": route["context_generation"]}
        runtime._request_meta_by_id[accepted.request_id] = meta
        response = await mgr.generate_response(prompt, accepted.request_id, request_content=content)
        received.append((response, content, accepted))
        return accepted.request_id

    runtime.enqueue_request = enqueue
    fields = {"photo": [SimpleNamespace(file_id="image")]} if kind == "photo" else {
        kind: SimpleNamespace(file_id="media", file_name="sample.pdf" if kind == "document" else "sample.mp4")
    }
    update = _update(update_id=17, message_id=23, caption="inspect this attachment", **fields)
    if batch:
        runtime_long.begin_batch(runtime, 123)
        runtime_long.collect_text(runtime, 123, "compare these")
    await getattr(runtime_media, f"handle_{kind}")(runtime, update, SimpleNamespace())
    if batch:
        # A second photo in the same batch must retain its distinct binding.
        update.message.message_id = 24
        await runtime_media.handle_photo(runtime, update, SimpleNamespace())
        await runtime_long.cmd_end(runtime, update, SimpleNamespace())
        if runtime._long_finalize_task:
            await runtime._long_finalize_task

    assert received, runtime.error_logger.messages
    response, content, accepted = received[0]
    assert response.is_success, (response.error_code, response.error)
    parts = [part for part in content["parts"] if part["type"] == "media"]
    assert len(parts) == (2 if batch else 1)
    message = store.get_message(accepted.message_id, owner_id=session["owner_id"],
                                session_id=session["session_id"])
    bound_ids = {block["attachment_id"] for block in message["content"] if "attachment_id" in block}
    assert bound_ids == {part["attachment_id"] for part in parts}
    assert all(Path(part["local_ref"]).parent == store.attachment_files_root for part in parts)
    assert runtime.media_dir not in backend.seen_roots
    assert "inspect this attachment" in message["text"]
    # A valid committed attachment from the same Session but another Message
    # must still be rejected by this Run.
    foreign = intake(store, session)
    rejected = await mgr.generate_response("foreign", accepted.request_id,
        request_content={"type": "hashi.request-content", "version": 1, "parts": [foreign]})
    assert not rejected.is_success and rejected.error_code == "MEDIA_PATH_NOT_AUTHORIZED"
