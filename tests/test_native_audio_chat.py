from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import multiprocessing
import os
import sqlite3
import shutil
import subprocess
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BackendResponse
from adapters.her_v2 import HERv2Adapter
from adapters.her_v2_provider import HashiStageProvider, _AdapterDelivery
from adapters.openrouter_api import OpenRouterAdapter
from orchestrator import runtime_media, workbench_api
from orchestrator.audio_assets import (
    AudioAssetError,
    AudioAssetNotFound,
    AudioAssetStore,
    AudioAssetUnauthorized,
)
from orchestrator.her_v2.config import HERv2Config, ProviderProfile
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from orchestrator.multimodal_contract import (
    InputCapability,
    attachment_manifest,
    canonical_request_content,
    resolve_input_capability,
    route_request_content,
)
from orchestrator.native_audio_delivery import native_reply_content_policy
from orchestrator.session_store import SessionConflict, SessionStore
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_transcript_gate import (
    await_authorized_transcript,
    complete_native_audio_response,
)
from tests.test_session_api import _Request, _server


def _wav_bytes(path: Path, *, sample: int = 0) -> bytes:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(int(sample).to_bytes(2, "little", signed=True) * 160)
    return path.read_bytes()


def _voice_content(path: Path, *, attachment_id: str = "att-voice") -> dict:
    payload = path.read_bytes()
    return canonical_request_content(
        [
            {
                "type": "media",
                "item_index": 1,
                "attachment_id": attachment_id,
                "modality": "audio",
                "kind": "voice",
                "semantic_role": "voice_message",
                "mime_type": "audio/wav",
                "filename": path.name,
                "caption": "",
                "local_ref": str(path),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "transport": {},
            }
        ]
    )


def _accept_audio_process(
    db_path: str,
    session_id: str,
    attachment_id: str,
    ready,
    release,
    results,
    pause_in_admission: bool,
) -> None:
    store = SessionStore(Path(db_path))
    if pause_in_admission:
        original_claim = store.audio_assets.claim

        def gated_claim(*args, **kwargs):
            metadata = original_claim(*args, **kwargs)
            ready.set()
            if not release.wait(20):
                raise RuntimeError("admission test gate timed out")
            return metadata

        store.audio_assets.claim = gated_claim
    try:
        accepted = store.accept_run(
            session_id=session_id,
            owner_id="user:7",
            agent_id="arale",
            request_id="request-audio-race",
            text="voice turn",
            source="test",
            idempotency_key="idem-audio-race",
            content=[
                {
                    "type": "audio",
                    "attachment_id": attachment_id,
                    "semantic_role": "voice_message",
                    "mime_type": "audio/wav",
                }
            ],
        )
    except Exception as exc:  # pragma: no cover - child-process evidence
        results.put(("accept_error", type(exc).__name__, str(exc)))
    else:
        results.put(("accepted", accepted.run_id, accepted.message_id))


def _cleanup_audio_process(
    db_path: str,
    now_iso: str,
    started,
    ready,
    release,
    results,
    mode: str,
) -> None:
    started.set()
    store = SessionStore(Path(db_path))
    if mode in {"pause_after_claim", "crash_after_claim"}:
        original_begin = store.audio_assets.begin_delete_claim

        def controlled_begin(*args, **kwargs):
            metadata = original_begin(*args, **kwargs)
            ready.set()
            if mode == "crash_after_claim":
                os._exit(17)
            if not release.wait(20):
                raise RuntimeError("cleanup claim test gate timed out")
            return metadata

        store.audio_assets.begin_delete_claim = controlled_begin
    elif mode == "crash_after_unlink":
        original_finish = store.audio_assets.finish_delete_claim

        def controlled_finish(*args, **kwargs):
            original_finish(*args, **kwargs)
            ready.set()
            os._exit(18)

        store.audio_assets.finish_delete_claim = controlled_finish
    try:
        expired = store.cleanup_audio_assets(
            now=datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        )
    except Exception as exc:  # pragma: no cover - child-process evidence
        results.put(("cleanup_error", type(exc).__name__, str(exc)))
    else:
        results.put(("cleaned", [row["asset_id"] for row in expired]))


def _prepare_expiring_audio(tmp_path: Path):
    db_path = tmp_path / "state" / "sessions.sqlite3"
    store = SessionStore(db_path)
    session = store.create_session(owner_id="user:7", agent_id="arale")
    payload = _wav_bytes(tmp_path / "race.wav")
    staged = store.stage_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        filename="race.wav",
        media_type="audio/wav",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        semantic_role="voice_message",
        retention_seconds=60,
    )
    store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
        payload=payload,
    )
    store.commit_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
    )
    return db_path, session["session_id"], staged["attachment_id"], payload


def _prepare_legacy_output_audio(tmp_path: Path):
    db_path = tmp_path / "state" / "sessions.sqlite3"
    store = SessionStore(db_path)
    owner_id = "user:7"
    session = store.create_session(owner_id=owner_id, agent_id="arale")
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id=owner_id,
        agent_id="arale",
        request_id="request-legacy-output-audio",
        text="reply with native audio",
        source="test",
        idempotency_key="legacy-output-audio",
    )
    payload = _wav_bytes(tmp_path / "legacy-output.wav")
    output = store.audio_assets.create(
        payload,
        owner_id=owner_id,
        session_id=session["session_id"],
        direction="output",
        mime_type="audio/wav",
        audio_format="wav",
        filename="legacy-output.wav",
        correlation={
            "run_id": accepted.run_id,
            "request_id": accepted.request_id,
            "provider": "legacy-audio-provider",
            "model": "legacy-audio-model",
        },
    )
    content = [
        {"type": "text", "text": "Legacy native reply."},
        {
            "type": "audio",
            "asset_id": output["asset_id"],
            "mime_type": output["mime_type"],
            "format": output["format"],
            "size_bytes": output["size_bytes"],
            "sha256": output["sha256"],
        },
    ]
    finished = store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="Legacy native reply.",
        assistant_content=content,
        assistant_source="legacy-audio-provider",
    )
    assert finished["state"] == "completed"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM session_attachments WHERE asset_id=?",
            (output["asset_id"],),
        ).fetchone()[0] == 0
        connection.execute(
            "UPDATE schema_metadata SET value='25' WHERE key='schema_version'"
        )
    return db_path, owner_id, session["session_id"], output, payload, store


def _replace_audio_storage_name(
    store: AudioAssetStore, asset_id: str, storage_name: str
) -> dict:
    metadata = store._read(asset_id)
    metadata["storage_name"] = storage_name
    store._write(metadata)
    return metadata


def _join_spawned(process, *, expected_exit: int = 0) -> None:
    process.join(30)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("spawned audio lifecycle process did not terminate")
    assert process.exitcode == expected_exit


def test_voice_manager_native_controls_are_persistent_and_legacy_tts_stays_separate(
    tmp_path,
):
    manager = VoiceManager(tmp_path / "workspace", tmp_path / "media")

    assert manager.native_policy["mode"] == "off"
    assert manager.is_enabled() is False
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    manager.set_native_mode("auto")
    manager.set_native_voice("alloy")
    manager.set_native_format("wav")
    manager.set_native_reply_content("both")
    manager.set_native_fallback("local_chain")
    manager.set_native_retention("90")

    reloaded = VoiceManager(tmp_path / "workspace", tmp_path / "media")
    assert reloaded.native_audio_enabled() is True
    assert reloaded.native_policy == {
        "mode": "native",
        "reply_trigger": "voice_message",
        "reply_content": "audio_and_text",
        "provider": "openrouter-api",
        "model": "openai/gpt-audio-mini",
        "voice": "alloy",
        "format": "wav",
        "fallback": "local_chain",
        "input_transcript_echo": False,
        "output_transcript_echo": True,
        "retention_seconds": 5_400,
        "audio_model_tools": False,
        "terminal_overrides": {},
    }
    assert reloaded.is_enabled() is False


def test_terminal_and_conversation_native_presentation_precedence(tmp_path):
    manager = VoiceManager(tmp_path / "workspace", tmp_path / "media")
    manager.set_native_target("openrouter-api", "openai/gpt-audio-mini")
    manager.set_native_mode("auto")
    manager.set_native_terminal_override(
        "telegram",
        {
            "reply_content": "audio_only",
            "retention_seconds": 120,
        },
    )
    manager.set_native_terminal_override("workbench", {"mode": "off"})
    with pytest.raises(RuntimeError, match="Unsupported native terminal override"):
        manager.set_native_terminal_override(
            "telegram",
            {"provider": "untrusted-terminal-provider"},
        )

    telegram = manager.native_policy_for_terminal("telegram")
    assert telegram["reply_content"] == "audio_only"
    assert telegram["retention_seconds"] == 120
    assert manager.native_audio_enabled("telegram") is True
    assert manager.native_audio_enabled("workbench") is False

    runtime = SimpleNamespace(voice_manager=manager)
    item = SimpleNamespace(
        session_surface="telegram",
        source="voice",
        request_metadata={
            "response_preferences": {
                "assistant_audio": True,
                "assistant_text": True,
            }
        },
    )
    assert native_reply_content_policy(runtime, item) == "audio_and_text"

    manager.set_native_mode("off")
    manager.set_native_terminal_override("telegram", {"mode": "auto"})
    assert manager.native_audio_enabled("telegram") is False


def test_disabling_safe_voice_discards_waiting_native_path_and_releases_waiter():
    pending_release = asyncio.Event()
    inflight_release = asyncio.Event()
    decisions = []
    pending = {
        "request_id": "req-pending",
        "status": "pending_confirmation",
        "safe_voice": True,
        "release_event": pending_release,
    }
    inflight = {
        "request_id": "req-inflight",
        "status": "pending",
        "safe_voice": True,
        "release_event": inflight_release,
    }
    runtime = SimpleNamespace(
        _native_voice_transcripts={
            "att-pending": pending,
            "req-pending": pending,
            "att-inflight": inflight,
        },
        session_store=SimpleNamespace(
            decide_voice_transcript=lambda **values: decisions.append(values)
        ),
        error_logger=SimpleNamespace(warning=lambda *_args: None),
    )

    runtime_media.disable_safe_voice(runtime)

    assert decisions == [{"request_id": "req-pending", "confirmed": False}]
    assert pending["status"] == "discarded"
    assert pending_release.is_set() is True
    assert inflight["safe_voice"] is False
    assert inflight_release.is_set() is False


def test_voice_origin_snapshot_keeps_the_selected_text_model():
    config = HERv2Config.from_mapping(
        {
            "main": {"provider": "text-api", "model": "text-main"},
        }
    )

    assert config.profile_for(Stage.DIRECT).model == "text-main"
    assert config.profile_for(Stage.EXECUTION).model == "text-main"

    voice = config.activate_voice_origin(
        {
            "provider": "text-api",
            "model": "audio-model",
            "voice": "alloy",
            "format": "wav",
            "retention_seconds": 3600,
            "fallback": "local_chain",
        }
    )
    direct = voice.profile_for(Stage.DIRECT)
    immediate = voice.profile_for(Stage.IMMEDIATE_RESPONSE)

    assert (direct.engine, direct.model) == ("text-api", "audio-model")
    assert (immediate.engine, immediate.model) == ("text-api", "audio-model")
    assert direct.options["_voice_fallback_model"] == "text-main"
    assert direct.options["audio_model_tools"] is False
    assert direct.options["native_audio_voice"] == "alloy"
    assert voice.profile_for(Stage.EXECUTION).model == "text-main"
    assert config.profile_for(Stage.DIRECT).model == "text-main"


def test_her_capability_probe_applies_exact_voice_overlay():
    resolved = HERv2Config.from_mapping(
        {
            "main": {
                "provider": "openrouter-api",
                "model": "configured-audio-model",
            }
        }
    )
    audio_row = {
        "engine": "openrouter-api",
        "model": "configured-audio-model",
        "input_modalities": ["text", "audio"],
        "input_transports": {"audio": ["inline"]},
        "output_modalities": ["text", "audio"],
        "output_formats": {"audio": ["wav"]},
        "output_streaming": "sse",
        "api_surface": "chat_completions",
        "supported_voices": ["alloy"],
    }

    class _BackendManager:
        def _select_backend_cfg(self, engine, target_model=None):
            if engine == "openrouter-api" and target_model == audio_row["model"]:
                return audio_row
            return {
                "engine": engine,
                "model": target_model,
                "input_modalities": ["text"],
            }

    policy = {
        "mode": "auto",
        "provider": "openrouter-api",
        "model": audio_row["model"],
        "voice": "alloy",
        "format": "wav",
    }
    runtime = SimpleNamespace(
        voice_manager=SimpleNamespace(
            native_audio_enabled=lambda: True,
            native_policy=policy,
        ),
        backend_manager=_BackendManager(),
    )
    adapter = object.__new__(HERv2Adapter)
    adapter._v2_config = resolved
    adapter.config = SimpleNamespace(extra={}, _hashi_runtime=runtime)

    assert adapter.accepts_media_input("audio") is True
    assert adapter.supports_media_output("audio") is True


def test_audio_asset_cleanup_is_authorized_lease_aware_and_archive_safe(tmp_path):
    source = tmp_path / "input.wav"
    payload = _wav_bytes(source)
    store = AudioAssetStore(tmp_path / "assets")
    asset = store.create(
        payload,
        owner_id="user:7",
        session_id="session-a",
        direction="input",
        mime_type="audio/wav",
        audio_format="wav",
        retention_seconds=60,
    )
    assert asset["duration_ms"] == 10
    with pytest.raises(AudioAssetUnauthorized):
        store.read_bytes(
            asset["asset_id"], owner_id="user:7", session_id="session-b"
        )

    future = datetime.now(timezone.utc) + timedelta(minutes=2)
    store.acquire(
        asset["asset_id"], owner_id="user:7", session_id="session-a"
    )
    assert store.cleanup(now=future) == []
    store.release(
        asset["asset_id"], owner_id="user:7", session_id="session-a"
    )
    assert [row["asset_id"] for row in store.cleanup(now=future)] == [
        asset["asset_id"]
    ]
    with pytest.raises(AudioAssetNotFound):
        store.read_bytes(
            asset["asset_id"], owner_id="user:7", session_id="session-a"
        )

    archived = store.create(
        payload,
        owner_id="user:7",
        session_id="session-a",
        direction="input",
        mime_type="audio/wav",
        audio_format="wav",
        retention_seconds=60,
        retention_indefinite=True,
    )
    assert store.cleanup(now=future) == []
    assert store.read_bytes(
        archived["asset_id"], owner_id="user:7", session_id="session-a"
    )[1] == payload


def test_audio_asset_paths_require_canonical_flat_storage_names(tmp_path):
    payload = _wav_bytes(tmp_path / "flat-name.wav")
    store = AudioAssetStore(tmp_path / "assets")
    asset = store.create(
        payload,
        owner_id="user:7",
        session_id="session-a",
        direction="input",
        mime_type="audio/wav",
        audio_format="wav",
    )
    asset_id = asset["asset_id"]
    outside = store.root / "outside.wav"
    outside.write_bytes(payload)
    unsafe_names = [
        "../outside.wav",
        str(outside.resolve()),
        f"nested/{asset_id}.wav",
        f"nested\\{asset_id}.wav",
        r"C:\escape.wav",
        r"\\server\share\escape.wav",
        f"{asset_id}.wav:alternate",
    ]

    for storage_name in unsafe_names:
        _replace_audio_storage_name(store, asset_id, storage_name)
        with pytest.raises(AudioAssetError, match="managed flat file"):
            store.read_bytes(
                asset_id, owner_id="user:7", session_id="session-a"
            )
        with pytest.raises(AudioAssetError, match="managed flat file"):
            store.authorized_path(
                asset_id, owner_id="user:7", session_id="session-a"
            )

    assert outside.read_bytes() == payload
    _replace_audio_storage_name(store, asset_id, f"{asset_id}.wav")
    assert store.read_bytes(
        asset_id, owner_id="user:7", session_id="session-a"
    )[1] == payload


def test_audio_asset_paths_reject_non_regular_files(tmp_path):
    payload = _wav_bytes(tmp_path / "non-regular.wav")
    store = AudioAssetStore(tmp_path / "assets")
    asset = store.create(
        payload,
        owner_id="user:7",
        session_id="session-a",
        direction="input",
        mime_type="audio/wav",
        audio_format="wav",
    )
    metadata = store._read(asset["asset_id"])
    managed_path = store.files_root / metadata["storage_name"]
    managed_path.unlink()
    managed_path.mkdir()

    with pytest.raises(AudioAssetError, match="regular managed file"):
        store.read_bytes(
            asset["asset_id"], owner_id="user:7", session_id="session-a"
        )
    with pytest.raises(AudioAssetError, match="regular managed file"):
        store.authorized_path(
            asset["asset_id"], owner_id="user:7", session_id="session-a"
        )


def test_audio_asset_paths_reject_links_to_files_outside_the_store(tmp_path):
    payload = _wav_bytes(tmp_path / "link-source.wav")
    store = AudioAssetStore(tmp_path / "assets")
    asset = store.create(
        payload,
        owner_id="user:7",
        session_id="session-a",
        direction="input",
        mime_type="audio/wav",
        audio_format="wav",
    )
    metadata = store._read(asset["asset_id"])
    managed_path = store.files_root / metadata["storage_name"]
    outside = tmp_path / "outside-link-target.wav"
    outside.write_bytes(payload)
    managed_path.unlink()
    try:
        managed_path.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(AudioAssetError, match="regular managed file"):
        store.read_bytes(
            asset["asset_id"], owner_id="user:7", session_id="session-a"
        )
    with pytest.raises(AudioAssetError, match="regular managed file"):
        store.authorized_path(
            asset["asset_id"], owner_id="user:7", session_id="session-a"
        )
    assert outside.read_bytes() == payload


def test_audio_asset_paths_do_not_retrust_a_replaced_files_root(tmp_path):
    payload = _wav_bytes(tmp_path / "root-link-source.wav")
    store = AudioAssetStore(tmp_path / "assets")
    asset = store.create(
        payload,
        owner_id="user:7",
        session_id="session-a",
        direction="input",
        mime_type="audio/wav",
        audio_format="wav",
    )
    metadata = store._read(asset["asset_id"])
    original_root = store.root / "files-original"
    store.files_root.rename(original_root)
    outside_root = tmp_path / "outside-files"
    outside_root.mkdir()
    (outside_root / metadata["storage_name"]).write_bytes(payload)
    try:
        store.files_root.symlink_to(outside_root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(AudioAssetError, match="trusted managed directory"):
        store.read_bytes(
            asset["asset_id"], owner_id="user:7", session_id="session-a"
        )
    with pytest.raises(AudioAssetError, match="trusted managed directory"):
        store.authorized_path(
            asset["asset_id"], owner_id="user:7", session_id="session-a"
        )
    assert (outside_root / metadata["storage_name"]).read_bytes() == payload


def test_audio_delete_and_cleanup_preserve_unsafe_paths_and_state(tmp_path):
    payload = _wav_bytes(tmp_path / "unsafe-delete.wav")
    store = AudioAssetStore(tmp_path / "assets")
    claimed = store.create(
        payload,
        owner_id="user:7",
        session_id="session-a",
        direction="input",
        mime_type="audio/wav",
        audio_format="wav",
        canonical_managed=True,
    )
    token = "delete-claim-1"
    store.begin_delete_claim(
        claimed["asset_id"],
        owner_id="user:7",
        session_id="session-a",
        claim_token=token,
        claimed_at="2026-10-22T00:00:00Z",
    )
    delete_survivor = store.root / "must-survive.wav"
    delete_survivor.write_bytes(payload)
    _replace_audio_storage_name(
        store, claimed["asset_id"], "../must-survive.wav"
    )

    with pytest.raises(AudioAssetError, match="managed flat file"):
        store.finish_delete_claim(
            claimed["asset_id"],
            owner_id="user:7",
            session_id="session-a",
            claim_token=token,
            expired_at="2026-10-22T00:01:00Z",
        )
    claimed_metadata = store._read(claimed["asset_id"])
    assert claimed_metadata["state"] == "deleting"
    assert claimed_metadata["delete_claim_token"] == token
    assert delete_survivor.read_bytes() == payload

    canonical_name = f"{claimed['asset_id']}.wav"
    _replace_audio_storage_name(store, claimed["asset_id"], canonical_name)
    managed_path = store.files_root / canonical_name
    managed_path.unlink()
    try:
        managed_path.symlink_to(delete_survivor)
    except (NotImplementedError, OSError):
        pass
    else:
        with pytest.raises(AudioAssetError, match="regular managed file"):
            store.finish_delete_claim(
                claimed["asset_id"],
                owner_id="user:7",
                session_id="session-a",
                claim_token=token,
                expired_at="2026-10-22T00:01:00Z",
            )
        assert store._read(claimed["asset_id"])["state"] == "deleting"
        assert managed_path.is_symlink()
        assert delete_survivor.read_bytes() == payload

    legacy = store.create(
        payload,
        owner_id="user:7",
        session_id="session-a",
        direction="input",
        mime_type="audio/wav",
        audio_format="wav",
        retention_seconds=60,
    )
    cleanup_survivor = store.root / "cleanup-survivor.wav"
    cleanup_survivor.write_bytes(payload)
    _replace_audio_storage_name(
        store, legacy["asset_id"], "../cleanup-survivor.wav"
    )
    with pytest.raises(AudioAssetError, match="managed flat file"):
        store.cleanup(now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert store._read(legacy["asset_id"])["state"] == "available"
    assert cleanup_survivor.read_bytes() == payload


def test_session_audio_gc_keeps_an_unsafe_asset_claim_pending(tmp_path):
    db_path, _session_id, attachment_id, payload = _prepare_expiring_audio(
        tmp_path
    )
    store = SessionStore(db_path)
    survivor = store.audio_assets.root / "canonical-survivor.wav"
    survivor.write_bytes(payload)
    _replace_audio_storage_name(
        store.audio_assets, attachment_id, "../canonical-survivor.wav"
    )

    assert store.cleanup_audio_assets(
        now=datetime.now(timezone.utc) + timedelta(minutes=2)
    ) == []
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT state, audio_delete_claim_token FROM session_attachments "
            "WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone()
    assert row[0] == "deleting"
    assert row[1]
    metadata = store.audio_assets._read(attachment_id)
    assert metadata["state"] == "deleting"
    assert metadata["delete_claim_token"] == row[1]
    assert survivor.read_bytes() == payload


def test_session_audio_admission_wins_against_spawned_gc(tmp_path):
    db_path, session_id, attachment_id, payload = _prepare_expiring_audio(tmp_path)
    context = multiprocessing.get_context("spawn")
    admission_ready = context.Event()
    admission_release = context.Event()
    cleanup_started = context.Event()
    unused_cleanup_ready = context.Event()
    unused_cleanup_release = context.Event()
    results = context.Queue()
    future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    admission = context.Process(
        target=_accept_audio_process,
        args=(
            str(db_path),
            session_id,
            attachment_id,
            admission_ready,
            admission_release,
            results,
            True,
        ),
    )
    cleanup = context.Process(
        target=_cleanup_audio_process,
        args=(
            str(db_path),
            future,
            cleanup_started,
            unused_cleanup_ready,
            unused_cleanup_release,
            results,
            "normal",
        ),
    )
    admission.start()
    assert admission_ready.wait(20)
    cleanup.start()
    assert cleanup_started.wait(20)
    admission_release.set()
    _join_spawned(admission)
    _join_spawned(cleanup)

    outcomes = [results.get(timeout=5) for _ in range(2)]
    assert {outcome[0] for outcome in outcomes} == {"accepted", "cleaned"}
    assert next(outcome for outcome in outcomes if outcome[0] == "cleaned") == (
        "cleaned",
        [],
    )
    store = SessionStore(db_path)
    assert store.audio_asset_bytes(
        session_id=session_id,
        owner_id="user:7",
        asset_id=attachment_id,
    )[1] == payload
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT state, retention_indefinite FROM session_attachments "
            "WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone()
    assert row == ("committed", 1)


def test_session_audio_gc_wins_and_rejects_spawned_admission(tmp_path):
    db_path, session_id, attachment_id, _payload = _prepare_expiring_audio(tmp_path)
    context = multiprocessing.get_context("spawn")
    cleanup_started = context.Event()
    cleanup_ready = context.Event()
    cleanup_release = context.Event()
    unused_admission_ready = context.Event()
    unused_admission_release = context.Event()
    results = context.Queue()
    future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    cleanup = context.Process(
        target=_cleanup_audio_process,
        args=(
            str(db_path),
            future,
            cleanup_started,
            cleanup_ready,
            cleanup_release,
            results,
            "pause_after_claim",
        ),
    )
    admission = context.Process(
        target=_accept_audio_process,
        args=(
            str(db_path),
            session_id,
            attachment_id,
            unused_admission_ready,
            unused_admission_release,
            results,
            False,
        ),
    )
    cleanup.start()
    assert cleanup_started.wait(20)
    assert cleanup_ready.wait(20)
    admission.start()
    _join_spawned(admission)
    admission_result = results.get(timeout=5)
    assert admission_result[:2] == ("accept_error", "SessionConflict")
    cleanup_release.set()
    _join_spawned(cleanup)
    assert results.get(timeout=5) == ("cleaned", [attachment_id])

    with sqlite3.connect(db_path) as connection:
        state = connection.execute(
            "SELECT state FROM session_attachments WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone()[0]
        counts = connection.execute(
            """SELECT
                   (SELECT COUNT(*) FROM runs WHERE request_id='request-audio-race'),
                   (SELECT COUNT(*) FROM messages WHERE text='voice turn'),
                   (SELECT COUNT(*) FROM idempotency_records
                      WHERE idempotency_key='idem-audio-race')"""
        ).fetchone()
    assert state == "expired"
    assert counts == (0, 0, 0)
    with pytest.raises(SessionConflict, match="no longer available"):
        SessionStore(db_path).archive_audio_asset(
            session_id=session_id,
            owner_id="user:7",
            asset_id=attachment_id,
        )


@pytest.mark.parametrize(
    ("mode", "expected_exit", "bytes_exist_before_recovery"),
    [
        ("crash_after_claim", 17, True),
        ("crash_after_unlink", 18, False),
    ],
)
def test_session_audio_delete_claim_recovers_after_process_crash(
    tmp_path, mode, expected_exit, bytes_exist_before_recovery
):
    db_path, _session_id, attachment_id, _payload = _prepare_expiring_audio(tmp_path)
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    crashed = context.Event()
    unused_release = context.Event()
    results = context.Queue()
    future = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    cleanup = context.Process(
        target=_cleanup_audio_process,
        args=(
            str(db_path),
            future,
            started,
            crashed,
            unused_release,
            results,
            mode,
        ),
    )
    cleanup.start()
    assert started.wait(20)
    assert crashed.wait(20)
    _join_spawned(cleanup, expected_exit=expected_exit)

    store = SessionStore(db_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT state, audio_delete_claim_token FROM session_attachments "
            "WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone()
    assert row[0] == "deleting"
    assert row[1]
    metadata = store.audio_assets._read(attachment_id)
    asset_path = store.audio_assets.files_root / metadata["storage_name"]
    assert asset_path.exists() is bytes_exist_before_recovery

    recovered = store.cleanup_audio_assets(
        now=datetime.fromisoformat(future.replace("Z", "+00:00"))
    )
    assert [row["asset_id"] for row in recovered] == [attachment_id]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT state FROM session_attachments WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone()[0] == "expired"
    assert not asset_path.exists()


def test_audio_admission_rollback_has_no_phantom_lease(tmp_path, monkeypatch):
    db_path, session_id, attachment_id, _payload = _prepare_expiring_audio(tmp_path)
    store = SessionStore(db_path)
    original_append = store._append_event

    def fail_run_accepted(connection, **kwargs):
        if kwargs.get("kind") == "run.accepted":
            raise RuntimeError("injected admission rollback")
        return original_append(connection, **kwargs)

    monkeypatch.setattr(store, "_append_event", fail_run_accepted)
    with pytest.raises(RuntimeError, match="injected admission rollback"):
        store.accept_run(
            session_id=session_id,
            owner_id="user:7",
            agent_id="arale",
            request_id="request-rollback",
            text="voice turn",
            source="test",
            idempotency_key="idem-rollback",
            content=[
                {
                    "type": "audio",
                    "attachment_id": attachment_id,
                    "semantic_role": "voice_message",
                    "mime_type": "audio/wav",
                }
            ],
        )
    assert store.audio_assets._read(attachment_id)["lease_count"] == 0
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE request_id='request-rollback'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM idempotency_records "
            "WHERE idempotency_key='idem-rollback'"
        ).fetchone()[0] == 0


def test_audio_projection_failure_keeps_canonical_admission_and_repairs(
    tmp_path, monkeypatch
):
    db_path, session_id, attachment_id, payload = _prepare_expiring_audio(tmp_path)
    store = SessionStore(db_path)
    original_set_indefinite = store.audio_assets.set_indefinite
    monkeypatch.setattr(
        store.audio_assets,
        "set_indefinite",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AudioAssetNotFound(attachment_id)
        ),
    )
    accepted = store.accept_run(
        session_id=session_id,
        owner_id="user:7",
        agent_id="arale",
        request_id="request-projection-failure",
        text="voice turn",
        source="test",
        idempotency_key="idem-projection-failure",
        content=[
            {
                "type": "audio",
                "attachment_id": attachment_id,
                "semantic_role": "voice_message",
                "mime_type": "audio/wav",
            }
        ],
    )
    assert accepted.run_id
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT retention_indefinite, audio_projection_pending "
            "FROM session_attachments WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone() == (1, 1)
    monkeypatch.setattr(store.audio_assets, "set_indefinite", original_set_indefinite)
    assert store.cleanup_audio_assets(
        now=datetime.now(timezone.utc) + timedelta(minutes=2)
    ) == []
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT audio_projection_pending FROM session_attachments "
            "WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone()[0] == 0
    assert store.audio_asset_bytes(
        session_id=session_id,
        owner_id="user:7",
        asset_id=attachment_id,
    )[1] == payload


def test_audio_gc_aggregates_all_attachment_references(tmp_path):
    db_path, session_id, attachment_id, payload = _prepare_expiring_audio(tmp_path)
    with sqlite3.connect(db_path) as connection:
        source = connection.execute(
            "SELECT * FROM session_attachments WHERE attachment_id=?",
            (attachment_id,),
        ).fetchone()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(session_attachments)")]
        duplicate = dict(zip(columns, source, strict=True))
        duplicate["attachment_id"] = "att_shared_reference"
        duplicate["retention_indefinite"] = 1
        duplicate["retention_seconds"] = None
        duplicate["retention_expires_at"] = None
        connection.execute(
            f"INSERT INTO session_attachments({','.join(columns)}) "
            f"VALUES({','.join('?' for _ in columns)})",
            [duplicate[column] for column in columns],
        )
    store = SessionStore(db_path)
    assert store.cleanup_audio_assets(
        now=datetime.now(timezone.utc) + timedelta(minutes=2)
    ) == []
    assert store.audio_asset_bytes(
        session_id=session_id,
        owner_id="user:7",
        asset_id=attachment_id,
    )[1] == payload


def test_canonical_audio_acquire_and_archive_fence_cleanup(tmp_path):
    db_path, session_id, attachment_id, payload = _prepare_expiring_audio(tmp_path)
    store = SessionStore(db_path)
    future = datetime.now(timezone.utc) + timedelta(minutes=2)
    store.acquire_audio_asset(
        session_id=session_id,
        owner_id="user:7",
        asset_id=attachment_id,
    )
    assert store.cleanup_audio_assets(now=future) == []
    store.release_audio_asset(
        session_id=session_id,
        owner_id="user:7",
        asset_id=attachment_id,
    )
    archived = store.archive_audio_asset(
        session_id=session_id,
        owner_id="user:7",
        asset_id=attachment_id,
    )
    assert archived["retention_indefinite"] is True
    assert store.cleanup_audio_assets(now=future) == []
    assert store.audio_asset_bytes(
        session_id=session_id,
        owner_id="user:7",
        asset_id=attachment_id,
    )[1] == payload


def test_audio_lifecycle_schema_migrates_v24_without_rewriting_rows(tmp_path):
    db_path = tmp_path / "state" / "sessions.sqlite3"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_metadata(key, value) VALUES('schema_version', '24');
            CREATE TABLE session_attachments (
                attachment_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'staged',
                semantic_role TEXT NOT NULL DEFAULT '',
                duration_ms INTEGER,
                retention_seconds INTEGER,
                retention_indefinite INTEGER NOT NULL DEFAULT 0,
                upload_required INTEGER NOT NULL DEFAULT 0,
                asset_id TEXT,
                workspace_path TEXT,
                bound_message_id TEXT,
                uploaded_at TEXT,
                created_at TEXT NOT NULL,
                committed_at TEXT
            );
            INSERT INTO session_attachments(
                attachment_id,session_id,owner_id,filename,media_type,size_bytes,
                sha256,state,semantic_role,retention_seconds,asset_id,created_at
            ) VALUES(
                'att_legacy','ses_legacy','user:7','legacy.wav','audio/wav',4,
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'committed','voice_message',60,'att_legacy','2026-10-21T00:00:00Z'
            );
            """
        )
    SessionStore(db_path)
    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(session_attachments)")
        }
        legacy = connection.execute(
            "SELECT attachment_id,state,asset_id FROM session_attachments "
            "WHERE attachment_id='att_legacy'"
        ).fetchone()
        version = connection.execute(
            "SELECT value FROM schema_metadata WHERE key='schema_version'"
        ).fetchone()[0]
    assert {
        "retention_expires_at",
        "audio_delete_claim_token",
        "audio_delete_claimed_at",
        "audio_projection_pending",
    } <= columns
    assert legacy == ("att_legacy", "committed", "att_legacy")
    assert version == str(SessionStore.SCHEMA_VERSION)


def test_legacy_output_audio_upgrade_registers_exact_durable_owner(tmp_path):
    db_path, owner_id, session_id, output, payload, legacy = (
        _prepare_legacy_output_audio(tmp_path)
    )
    with pytest.raises(SessionConflict, match="not canonically registered"):
        legacy.audio_asset_bytes(
            session_id=session_id,
            owner_id=owner_id,
            asset_id=output["asset_id"],
        )

    upgraded = SessionStore(db_path)
    metadata, restored = upgraded.audio_asset_bytes(
        session_id=session_id,
        owner_id=owner_id,
        asset_id=output["asset_id"],
    )
    assert restored == payload
    assert hashlib.sha256(restored).hexdigest() == output["sha256"]
    assert metadata["asset_id"] == output["asset_id"]
    with pytest.raises(SessionConflict, match="another Session"):
        upgraded.audio_asset_bytes(
            session_id="wrong-session",
            owner_id=owner_id,
            asset_id=output["asset_id"],
        )
    with pytest.raises(SessionConflict, match="another Session"):
        upgraded.audio_asset_bytes(
            session_id=session_id,
            owner_id="user:wrong",
            asset_id=output["asset_id"],
        )

    reopened = SessionStore(db_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """SELECT COUNT(*), MIN(retention_indefinite),
                      MAX(audio_projection_pending), MIN(bound_message_id)
               FROM session_attachments WHERE asset_id=?""",
            (output["asset_id"],),
        ).fetchone()
    assert row[0:3] == (1, 1, 0)
    assert row[3]
    assert reopened.legacy_audio_migration_report()["registered"] == [
        output["asset_id"]
    ]


def test_legacy_output_audio_projection_recovers_idempotently_after_failure(
    tmp_path, monkeypatch
):
    db_path, owner_id, session_id, output, payload, _legacy = (
        _prepare_legacy_output_audio(tmp_path)
    )
    original = AudioAssetStore.set_indefinite
    attempts = 0

    def fail_once(asset_store, asset_id, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AudioAssetNotFound(asset_id)
        return original(asset_store, asset_id, **kwargs)

    monkeypatch.setattr(AudioAssetStore, "set_indefinite", fail_once)
    first = SessionStore(db_path)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT audio_projection_pending FROM session_attachments WHERE asset_id=?",
            (output["asset_id"],),
        ).fetchone()[0] == 1
    monkeypatch.setattr(AudioAssetStore, "set_indefinite", original)
    recovered = SessionStore(db_path)
    assert recovered.audio_asset_bytes(
        session_id=session_id,
        owner_id=owner_id,
        asset_id=output["asset_id"],
    )[1] == payload
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """SELECT COUNT(*), MAX(audio_projection_pending)
               FROM session_attachments WHERE asset_id=?""",
            (output["asset_id"],),
        ).fetchone() == (1, 0)
    assert first.legacy_audio_migration_report()["registered"] == [output["asset_id"]]


def test_legacy_output_audio_with_unproven_digest_is_preserved_and_reported(tmp_path):
    db_path, owner_id, session_id, output, payload, _legacy = (
        _prepare_legacy_output_audio(tmp_path)
    )
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT message_id, content_json FROM messages WHERE role='assistant'"
        ).fetchone()
        content = json.loads(row[1])
        next(part for part in content if part.get("type") == "audio")["sha256"] = "0" * 64
        encoded = json.dumps(
            content, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            "UPDATE messages SET content_json=?, content_hash=? WHERE message_id=?",
            (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), row[0]),
        )

    upgraded = SessionStore(db_path)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM session_attachments WHERE asset_id=?",
            (output["asset_id"],),
        ).fetchone()[0] == 0
    raw_metadata, raw_payload = upgraded.audio_assets.read_bytes(
        output["asset_id"], owner_id=owner_id, session_id=session_id
    )
    assert raw_metadata["sha256"] == hashlib.sha256(payload).hexdigest()
    assert raw_payload == payload
    unresolved = upgraded.legacy_audio_migration_report()["unresolved"]
    assert any(item.get("asset_id") == output["asset_id"] for item in unresolved)


def test_legacy_output_audio_with_unsafe_path_is_preserved_and_reported(tmp_path):
    db_path, owner_id, session_id, output, payload, legacy = (
        _prepare_legacy_output_audio(tmp_path)
    )
    original_metadata = legacy.audio_assets._read(output["asset_id"])
    original_path = (
        legacy.audio_assets.files_root / original_metadata["storage_name"]
    )
    outside = tmp_path / "legacy-outside.wav"
    outside.write_bytes(payload)
    _replace_audio_storage_name(
        legacy.audio_assets, output["asset_id"], str(outside.resolve())
    )

    upgraded = SessionStore(db_path)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM session_attachments WHERE asset_id=?",
            (output["asset_id"],),
        ).fetchone()[0] == 0
    assert original_path.read_bytes() == payload
    assert outside.read_bytes() == payload
    assert (
        upgraded.audio_assets._read(output["asset_id"])["canonical_managed"]
        is False
    )
    unresolved = upgraded.legacy_audio_migration_report()["unresolved"]
    assert any(
        item.get("asset_id") == output["asset_id"]
        and "managed flat file" in item.get("reason", "")
        for item in unresolved
    )
    with pytest.raises(SessionConflict, match="not canonically registered"):
        upgraded.audio_asset_bytes(
            session_id=session_id,
            owner_id=owner_id,
            asset_id=output["asset_id"],
        )


def test_audio_duration_limit_uses_verified_duration_instead_of_rejecting_all(
    tmp_path,
):
    source = tmp_path / "duration.wav"
    _wav_bytes(source)
    part = dict(_voice_content(source)["parts"][0])
    capability = InputCapability(
        provider="audio-api",
        model="audio-model",
        input_modalities=frozenset({"text", "audio"}),
        input_transports={"audio": ("inline",)},
        limits={"duration": 60},
        source="test",
    )

    part["duration_ms"] = 59_000
    admitted = route_request_content(canonical_request_content([part]), capability)
    assert admitted[0].route == "native"

    part["duration_ms"] = 61_000
    rejected = route_request_content(canonical_request_content([part]), capability)
    assert rejected[0].route == "unsupported"
    assert rejected[0].reason == "native_duration_limit_exceeded"


def test_first_ready_audio_transcript_is_canonical_and_direct_finish_reuses_it(
    tmp_path,
):
    from orchestrator.frontend_delivery import freeze_run_delivery_route

    store = SessionStore(tmp_path / "state" / "sessions.sqlite3")
    session = store.create_session(owner_id="user:7", agent_id="arale")
    route = freeze_run_delivery_route(
        message_source_id="telegram",
        session_surface="telegram",
        session_channel_key="7",
        chat_id=7,
        telegram_requested=False,
    )
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="arale",
        request_id="request-first-ready",
        text="voice turn",
        source="test",
        idempotency_key="idem-first-ready",
        delivery_route=route,
    )
    output = store.audio_assets.create(
        _wav_bytes(tmp_path / "reply.wav"),
        owner_id="",
        session_id="",
        direction="output",
        mime_type="audio/wav",
        audio_format="wav",
        correlation={"request_id": accepted.request_id},
    )
    content = [
        {
            "type": "text",
            "text": "I heard you.",
            "provenance": "provider_audio_transcript",
        },
        {
            "type": "audio",
            "asset_id": output["asset_id"],
            "mime_type": "audio/wav",
            "format": "wav",
            "sha256": output["sha256"],
        },
    ]

    available = store.append_native_audio_runtime_event(
        request_id=accepted.request_id,
        source_event_id="her:immediate",
        event_kind="immediate_response",
        summary="I heard you.",
        phase="immediate",
        content=content,
    )
    replay = store.append_native_audio_runtime_event(
        request_id=accepted.request_id,
        source_event_id="her:immediate",
        event_kind="immediate_response",
        summary="I heard you.",
        phase="immediate",
        content=content,
    )
    store.append_native_audio_runtime_event(
        request_id=accepted.request_id,
        source_event_id="her:resolution",
        event_kind="initial_resolution",
        summary="",
        phase="triage",
        resolution="final",
        target_event_id="her:immediate",
    )
    finished = store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="I heard you.",
        assistant_content=content,
        assistant_source="her-v2",
    )

    messages = store.messages(session["session_id"], owner_id="user:7")
    assistant = [message for message in messages if message["role"] == "assistant"]
    assert available["event_id"] == replay["event_id"]
    assert len(assistant) == 1
    assert assistant[0]["text"] == "I heard you."
    assert assistant[0]["content"] == content
    assert finished["final_message_id"] == assistant[0]["message_id"]
    assert [
        event["kind"]
        for event in store.events(session["session_id"], owner_id="user:7")
        if event["kind"].startswith("assistant.output")
    ] == ["assistant.output.available", "assistant.output.resolved"]
    durable_events = store.events(session["session_id"], owner_id="user:7")
    available_event = next(
        event
        for event in durable_events
        if event["kind"] == "assistant.output.available"
    )
    terminal_event = next(
        event for event in durable_events if event["kind"] == "run.completed"
    )
    assert len(
        store.claim_delivery_outbox(
            session_id=session["session_id"],
            owner_id="user:7",
            worker_id="native-audio-first-ready-worker",
            event_id=available_event["event_id"],
            limit=1,
        )
    ) == 1
    assert store.claim_delivery_outbox(
        session_id=session["session_id"],
        owner_id="user:7",
        worker_id="native-audio-terminal-worker",
        event_id=terminal_event["event_id"],
        limit=1,
    ) == []


def test_accepted_input_and_output_transcripts_enter_pcm_history_with_provenance(
    tmp_path,
):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3")
    session = store.create_session(owner_id="user:7", agent_id="arale")
    payload = _wav_bytes(tmp_path / "history.wav")
    staged = store.stage_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        filename="history.wav",
        media_type="audio/wav",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        semantic_role="voice_message",
    )
    store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
        payload=payload,
    )
    store.commit_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
    )
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="arale",
        request_id="request-history",
        text="Optional caption",
        source="test",
        idempotency_key="idem-history",
        content=[
            {
                "type": "audio",
                "attachment_id": staged["attachment_id"],
                "semantic_role": "voice_message",
                "mime_type": "audio/wav",
            },
            {"type": "text", "text": "Optional caption"},
        ],
    )
    store.record_voice_transcript(
        request_id=accepted.request_id,
        attachment_id=staged["attachment_id"],
        text="Spoken request",
        provenance="local_stt",
        safe_voice_state="ready",
    )
    assert store.messages(session["session_id"], owner_id="user:7")[0][
        "text"
    ] == "Optional caption"
    store.release_ready_voice_transcript(request_id=accepted.request_id)
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="Spoken reply",
        assistant_content=[
            {
                "type": "text",
                "text": "Spoken reply",
                "provenance": "provider_audio_transcript",
            }
        ],
        assistant_source="audio-model",
    )

    history = store.recent_exchanges(session["session_id"])
    assert history[0]["user_text"] == "Optional caption\n\nSpoken request"
    assert history[0]["user_transcript_provenance"] == "local_stt"
    assert history[0]["assistant_text"] == "Spoken reply"
    assert (
        history[0]["assistant_transcript_provenance"]
        == "provider_audio_transcript"
    )
    kinds = [
        event["kind"]
        for event in store.events(session["session_id"], owner_id="user:7")
    ]
    assert "voice.input.transcript_pending_confirmation" not in kinds
    assert "voice.input.transcript_released" in kinds


def test_completed_native_audio_beta_prompt_can_be_reconciled(tmp_path):
    store = SessionStore(tmp_path / "state" / "sessions.sqlite3")
    session = store.create_session(owner_id="user:7", agent_id="arale")
    payload = _wav_bytes(tmp_path / "input.wav")
    staged = store.stage_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        filename="input.wav",
        media_type="audio/wav",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        semantic_role="voice_message",
    )
    store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
        payload=payload,
    )
    store.commit_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
    )
    accepted = store.accept_run(
        session_id=session["session_id"],
        owner_id="user:7",
        agent_id="arale",
        request_id="request-beta-safevoice",
        text="",
        source="test",
        idempotency_key="idem-beta-safevoice",
        content=[
            {
                "type": "audio",
                "attachment_id": staged["attachment_id"],
                "semantic_role": "voice_message",
                "mime_type": "audio/wav",
            }
        ],
    )
    store.record_voice_transcript(
        request_id=accepted.request_id,
        attachment_id=staged["attachment_id"],
        text="The spoken request.",
        provenance="local_stt",
        safe_voice_state="pending_confirmation",
    )
    output = store.audio_assets.create(
        _wav_bytes(tmp_path / "output.wav"),
        owner_id="",
        session_id="",
        direction="output",
        mime_type="audio/wav",
        audio_format="wav",
        correlation={"request_id": accepted.request_id},
    )
    store.finish_request(
        accepted.request_id,
        success=True,
        assistant_text="Native answer.",
        assistant_content=[
            {"type": "text", "text": "Native answer."},
            {
                "type": "audio",
                "asset_id": output["asset_id"],
                "mime_type": "audio/wav",
            },
        ],
        assistant_source="audio-model",
    )

    reconciled = store.reconcile_completed_native_audio_transcript(
        request_id=accepted.request_id
    )

    assert reconciled["safe_voice_state"] == "released"
    assert store.messages(session["session_id"], owner_id="user:7")[0][
        "text"
    ] == "The spoken request."
    assert store.reconcile_completed_native_audio_transcript(
        request_id=accepted.request_id
    )["safe_voice_state"] == "released"


class _NativeBackend:
    def __init__(self, response: BackendResponse, *, roots: tuple[Path, ...], audio: bool):
        self.response = response
        self.roots = roots
        self.calls: list[dict] = []
        self.config = SimpleNamespace(extra={}, system_md=None, name="native-test")
        self.capabilities = SimpleNamespace(supports_tool_use=True)
        self.input_capability = InputCapability(
            provider="audio-api" if audio else "text-api",
            model="audio-model" if audio else "text-model",
            input_modalities=frozenset({"text", "audio"} if audio else {"text"}),
            input_transports={"audio": ("inline",)} if audio else {},
            source="test",
        )
        self.tool_registry = "not-set"
        self.sys_prompt = ""

    def resolve_input_capability(self):
        return self.input_capability

    def authorized_media_roots(self):
        return self.roots

    async def initialize(self):
        return True

    async def generate_response(self, prompt, request_id, **kwargs):
        self.calls.append(
            {
                "prompt": prompt,
                "request_id": request_id,
                "request_content": kwargs.get("request_content"),
            }
        )
        return self.response

    async def shutdown(self):
        return None


@pytest.mark.asyncio
async def test_hashi_stage_probe_preserves_audio_required_input_policy():
    class _CapabilityBackend:
        def __init__(self):
            self.config = SimpleNamespace(
                extra={
                    "input_modalities": ["text", "audio"],
                    "input_transports": {"audio": ["inline"]},
                    "input_policy": "audio_required",
                    "output_modalities": ["text"],
                },
                model="configured-audio-model",
            )
            self.capabilities = SimpleNamespace(
                output_modalities=frozenset({"text"}),
                input_policy="auto",
            )

        def _apply_declared_multimodal_capabilities(self, extra):
            self.capabilities.output_modalities = frozenset(
                extra.get("output_modalities") or {"text"}
            )
            self.capabilities.input_policy = str(
                extra.get("input_policy") or "auto"
            )

        def resolve_input_capability(self):
            return resolve_input_capability(
                "openrouter-api",
                self.config.model,
                config=self.config.extra,
            )

        async def shutdown(self):
            return None

    backend = _CapabilityBackend()

    class _Manager:
        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == (
                "openrouter-api",
                "configured-audio-model",
            )
            return backend

    provider = HashiStageProvider(backend_manager=_Manager())
    contract = await provider.resolve_stage_modalities(
        ProviderProfile(
            "lightweight",
            "openrouter-api",
            "configured-audio-model",
        )
    )

    assert contract["input_modalities"] == ("audio", "text")
    assert contract["output_modalities"] == ("text",)
    assert contract["input_policy"] == "audio_required"


@pytest.mark.asyncio
async def test_hashi_stage_text_audio_asset_is_derived_and_request_scoped(tmp_path):
    audio_path = tmp_path / "derived.ogg"
    audio_path.write_bytes(b"OggS" + b"\0" * 64)
    synthesis_calls = []

    async def synthesize(*args, **kwargs):
        synthesis_calls.append((args, kwargs))
        return SimpleNamespace(
            ogg_path=audio_path,
            mime_type="audio/ogg",
        )

    provider = HashiStageProvider(
        backend_manager=SimpleNamespace(),
        runtime_context=SimpleNamespace(
            name="test-agent",
            voice_manager=SimpleNamespace(synthesize_reply=synthesize),
        ),
    )

    content = await provider.materialize_text_audio(
        text="Speak this exact request.",
        turn_id="turn-derived",
        request_ref="hashi-request:req-derived",
    )

    manifest = attachment_manifest(content)
    assert manifest[0]["kind"] == "derived_tts"
    assert manifest[0]["semantic_role"] == "audio_attachment"
    assert synthesis_calls[0][1]["force"] is True
    assert synthesis_calls[0][1]["max_chars_override"] > len(
        "Speak this exact request."
    )
    await provider.cleanup_text_audio()
    assert audio_path.exists() is False


@pytest.mark.asyncio
async def test_audio_only_immediate_crosses_adapter_delivery_boundary():
    events = []

    async def capture(event):
        events.append(event)
        return True

    content = (
        {
            "type": "audio",
            "asset_id": "asset-audio-only",
            "mime_type": "audio/wav",
            "format": "wav",
        },
    )
    receipt = await _AdapterDelivery(
        capture,
        allow_immediate_response=True,
    ).deliver(
        kind="immediate",
        text="",
        event_id="event-audio-only",
        content=content,
    )

    assert receipt.delivered is True
    assert events[0].summary == ""
    assert events[0].metadata["content"] == list(content)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [Stage.TRIAGE, Stage.EXECUTION, Stage.DIRECT])
@pytest.mark.parametrize("sibling_image", [False, True])
@pytest.mark.parametrize("her_v3", [False, True])
async def test_text_voice_stage_waits_for_and_uses_released_local_transcript(
    tmp_path, stage, sibling_image, her_v3,
):
    source = tmp_path / "voice.wav"
    _wav_bytes(source)
    content = _voice_content(source)
    triage = _NativeBackend(
        BackendResponse(
            text=json.dumps(
                {
                    "classification": "DIRECT_RESPONSE",
                    "real_goal": "Answer the transcribed request.",
                    "relevant_habits": [],
                    "clarification": "",
                }
            ),
            duration_ms=1,
        ),
        roots=(tmp_path,),
        audio=False,
    )
    if sibling_image:
        image = tmp_path / "sibling.png"
        payload = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZfUAAAAASUVORK5CYII="
        )
        image.write_bytes(payload)
        content = canonical_request_content([*content["parts"], {
            "type": "media", "item_index": 2, "attachment_id": "att-sibling",
            "modality": "image", "kind": "image", "mime_type": "image/png",
            "filename": image.name, "caption": "", "local_ref": str(image),
            "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "transport": {},
        }])
        triage.input_capability = InputCapability(
            provider="text-api", model="text-model", source="test",
            input_modalities=frozenset({"text", "image"}),
            input_transports={"image": ("data_url",)},
        )

    class _Manager:
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == ("text-api", "text-model")
            return triage

    released = asyncio.Event()
    ready = asyncio.Event()
    ready.set()
    confirmation_requests: list[str] = []
    transcript_state = {
        "text": "Please check tomorrow's weather.",
        "status": "ready",
        "safe_voice": True,
        "ready_event": ready,
        "release_event": released,
        "gate_lock": asyncio.Lock(),
    }

    async def _confirm_for_test():
        assert triage.calls == []
        confirmation_requests.append("triage")
        transcript_state["status"] = "released"
        released.set()

    transcript_state["request_confirmation"] = _confirm_for_test
    provider = HashiStageProvider(
        backend_manager=_Manager(),
        runtime_context=SimpleNamespace(
            _native_voice_transcripts={
                "req-triage": transcript_state,
            }
        ),
    )
    request = StageRequest(
        turn_id="turn-triage",
        request_ref="hashi-request:req-triage",
        stage=stage,
        role="triage",
        attempt=1,
        goal="Respond to the attached voice message.",
        classification=None,
        effort=Effort.LOW,
        context=({"her_v3": True, "pcm_input": {
            "current_request": "Respond to the attached voice message.",
            "sections": [{"key": "permanent_system", "authority": "permanent_system",
                          "text": "CANARY_KEEP_TRUSTED_BOUNDARIES", "order": 0}],
        }} if her_v3 else {}),
        request_content=content,
        attachment_manifest=attachment_manifest(content),
        allow_tools=False,
        allow_side_effects=False,
    )
    response = await provider.invoke(
        ProviderProfile(
            "triage",
            "text-api",
            "text-model",
            options={"_voice_triage_input_policy": "auto"},
        ),
        request,
    )

    assert json.loads(response.text)["classification"] == "DIRECT_RESPONSE"
    assert [item["attachment_id"] for item in attachment_manifest(
        triage.calls[0]["request_content"]
    )] == (["att-sibling"] if sibling_image else [])
    assert "Please check tomorrow's weather." in triage.calls[0]["prompt"]
    assert "Respond to the attached voice message." in triage.calls[0]["prompt"]
    if her_v3 and stage is Stage.DIRECT:
        assert "CANARY_KEEP_TRUSTED_BOUNDARIES" in triage.sys_prompt
    assert response.media_routing[0]["route"] == "local_transcript"
    assert confirmation_requests == ["triage"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["unavailable", "discarded"])
async def test_voice_execution_without_released_transcript_starts_no_model(tmp_path, status):
    from orchestrator.her_v2.interfaces import StageInvocationError

    source = tmp_path / "voice.wav"
    _wav_bytes(source)
    content = _voice_content(source)
    backend = _NativeBackend(BackendResponse(text="must not run", duration_ms=1),
                             roots=(tmp_path,), audio=False)
    ready = asyncio.Event()
    release = asyncio.Event()
    ready.set()
    release.set()
    provider = HashiStageProvider(
        backend_manager=SimpleNamespace(
            privacy_level=1, create_ephemeral_backend=lambda *_args, **_kwargs: backend,
        ),
        runtime_context=SimpleNamespace(_native_voice_transcripts={"req-unreleased": {
            "status": status, "text": "", "safe_voice": True,
            "ready_event": ready, "release_event": release,
        }}),
    )
    with pytest.raises(StageInvocationError) as failure:
        await provider.invoke(
            ProviderProfile("main", "text-api", "text-model"),
            StageRequest(turn_id="turn-unreleased", request_ref="hashi-request:req-unreleased",
                         stage=Stage.EXECUTION, role="execution", attempt=1, goal="Voice request",
                         classification=None, effort=Effort.HIGH, request_content=content,
                         attachment_manifest=attachment_manifest(content)),
        )
    assert failure.value.code.value == "INPUT_MODALITY_CONVERSION_FAILED"
    assert failure.value.retryable is False
    assert backend.calls == []


@pytest.mark.asyncio
async def test_no_tool_native_direct_does_not_request_safe_voice(tmp_path):
    source = tmp_path / "voice.wav"
    _wav_bytes(source)
    content = _voice_content(source)
    native = _NativeBackend(
        BackendResponse(
            text="Native voice answer.",
            duration_ms=1,
            content=(
                {
                    "type": "audio",
                    "asset_id": "aud-direct",
                    "mime_type": "audio/wav",
                },
            ),
        ),
        roots=(tmp_path,),
        audio=True,
    )

    class _Manager:
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            assert (engine, target_model) == ("audio-api", "audio-model")
            return native

    confirmation_requests: list[str] = []
    state = {
        "text": "Local audit transcript.",
        "status": "ready",
        "safe_voice": True,
        "ready_event": asyncio.Event(),
        "release_event": asyncio.Event(),
        "gate_lock": asyncio.Lock(),
        "request_confirmation": lambda: confirmation_requests.append("prompt"),
    }
    state["ready_event"].set()
    provider = HashiStageProvider(
        backend_manager=_Manager(),
        tool_registry=SimpleNamespace(),
        runtime_context=SimpleNamespace(
            _native_voice_transcripts={"req-direct": state}
        ),
    )
    request = StageRequest(
        turn_id="turn-direct",
        request_ref="hashi-request:req-direct",
        stage=Stage.DIRECT,
        role="lightweight",
        attempt=1,
        goal="Respond to the attached voice message.",
        classification=None,
        effort=Effort.ZERO,
        context={},
        request_content=content,
        attachment_manifest=attachment_manifest(content),
        allow_tools=True,
        allow_side_effects=True,
    )

    response = await provider.invoke(
        ProviderProfile(
            "voice",
            "audio-api",
            "audio-model",
            options={
                "_native_audio_route": True,
                "audio_model_tools": False,
            },
        ),
        request,
    )

    assert response.text == "Native voice answer."
    assert native.calls[0]["request_content"] == content
    assert native.tool_registry is None
    assert confirmation_requests == []
    assert state["status"] == "ready"


@pytest.mark.asyncio
async def test_native_failure_uses_released_stt_once_and_emits_visible_warning(
    tmp_path,
):
    source = tmp_path / "voice.wav"
    _wav_bytes(source)
    content = _voice_content(source)
    native = _NativeBackend(
        BackendResponse(
            text="",
            duration_ms=1,
            error="native provider unavailable",
            is_success=False,
            error_code="PROVIDER_SERVER_ERROR",
            error_retryable=True,
        ),
        roots=(tmp_path,),
        audio=True,
    )
    fallback = _NativeBackend(
        BackendResponse(text="Fallback answer.", duration_ms=1),
        roots=(tmp_path,),
        audio=False,
    )

    class _Manager:
        privacy_level = 1

        def create_ephemeral_backend(self, engine, target_model=None):
            if (engine, target_model) == ("audio-api", "audio-model"):
                return native
            assert (engine, target_model) == ("text-api", "text-model")
            return fallback

    release = asyncio.Event()
    release.set()
    runtime_context = SimpleNamespace(
        _native_voice_transcripts={
            "req-voice": {
                "text": "Please answer me.",
                "status": "released",
                "release_event": release,
            }
        }
    )
    events = []

    async def _capture(event):
        events.append(event)
        return True

    provider = HashiStageProvider(
        backend_manager=_Manager(),
        tool_registry=SimpleNamespace(),
        on_stream_event=_capture,
        runtime_context=runtime_context,
    )
    request = StageRequest(
        turn_id="turn-voice",
        request_ref="hashi-request:req-voice",
        stage=Stage.DIRECT,
        role="lightweight",
        attempt=1,
        goal="Respond to the attached voice message.",
        classification=None,
        effort=Effort.ZERO,
        context={},
        request_content=content,
        attachment_manifest=attachment_manifest(content),
        allow_tools=True,
        allow_side_effects=True,
    )
    profile = ProviderProfile(
        "voice",
        "audio-api",
        "audio-model",
        options={
            "_native_audio_route": True,
            "_voice_fallback_enabled": True,
            "_voice_fallback_provider": "text-api",
            "_voice_fallback_model": "text-model",
            "audio_model_tools": False,
        },
    )

    response = await provider.invoke(profile, request)

    assert response.text == "Fallback answer."
    assert native.calls[0]["request_content"] == content
    assert fallback.calls == [
        {
            "prompt": "[Local voice transcription]\nPlease answer me.",
            "request_id": "turn-voice:direct:1:native-audio-fallback",
            "request_content": None,
        }
    ]
    assert native.config.extra["_native_audio_claim_request_id"] == "req-voice"
    assert native.tool_registry is None
    assert fallback.tool_registry is None
    assert [event.kind for event in events] == [
        "voice_fallback_started",
        "voice_warning",
    ]


async def _wait_for_event(server, session_id: str, kind: str) -> dict:
    for _attempt in range(100):
        events = server.session_store.events(session_id, owner_id="user:7")
        match = next((event for event in events if event["kind"] == kind), None)
        if match is not None:
            return match
        await asyncio.sleep(0.01)
    raise AssertionError(f"event {kind!r} was not produced")


@pytest.mark.asyncio
async def test_generic_session_audio_upload_run_transcript_replay_and_retrieval(
    tmp_path,
    monkeypatch,
):
    server, runtime = _server(tmp_path)
    monkeypatch.setattr(workbench_api, "PERSISTENT_SESSION_V1_QUALIFIED", True)
    server.global_config.persistent_session_v1 = True
    server.global_config.native_audio_chat_v1 = True

    class _Transcriber:
        async def transcribe(self, _path):
            return "generic session transcript"

    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber", lambda: _Transcriber()
    )
    created = json.loads(
        (
            await server.handle_v1_sessions_create(
                _Request({"agent_id": "lily", "title": "Native audio"})
            )
        ).text
    )
    session_id = created["session"]["session_id"]
    payload = b"OggSgeneric-voice"
    staged = json.loads(
        (
            await server.handle_v1_attachment_stage(
                _Request(
                    {
                        "filename": "voice.ogg",
                        "media_type": "audio/ogg",
                        "size_bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                        "semantic_role": "voice_message",
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
            headers={"Content-Type": "application/octet-stream"},
            body=payload,
        )
    )
    assert uploaded.status == 200
    await server.handle_v1_attachment_commit(
        _Request(
            match_info={
                "session_id": session_id,
                "attachment_id": staged["attachment_id"],
            }
        )
    )
    request_payload = {
        "idempotency_key": "native-audio-run",
        "surface": "generic-ui",
        "message": {
            "content": [
                {
                    "type": "audio",
                    "attachment_id": staged["attachment_id"],
                    "semantic_role": "voice_message",
                    "mime_type": "audio/ogg",
                }
            ]
        },
        "response_preferences": {
            "assistant_audio": True,
            "assistant_text": True,
        },
    }
    first = json.loads(
        (
            await server.handle_v1_session_runs_create(
                _Request(request_payload, match_info={"session_id": session_id})
            )
        ).text
    )
    replay = json.loads(
        (
            await server.handle_v1_session_runs_create(
                _Request(request_payload, match_info={"session_id": session_id})
            )
        ).text
    )
    assert replay["run_id"] == first["run_id"]
    transcript_event = await _wait_for_event(
        server, session_id, "voice.input.transcript_ready"
    )
    assert transcript_event["detail"]["text"] == "generic session transcript"
    run = server.session_store.get_run(first["run_id"], owner_id="user:7")
    assert run["response_preferences"] == {
        "assistant_audio": True,
        "assistant_text": True,
    }
    messages = server.session_store.messages(session_id, owner_id="user:7")
    assert messages[0]["text"] == "generic session transcript"
    assert messages[0]["content"] == request_payload["message"]["content"]

    retrieved = await server.handle_v1_audio_asset_get(
        _Request(
            match_info={
                "session_id": session_id,
                "asset_id": staged["attachment_id"],
            }
        )
    )
    assert retrieved.body == payload
    assert "base64" not in json.dumps(
        server.session_store.events(session_id, owner_id="user:7")
    ).casefold()
    await server.shutdown()


@pytest.mark.asyncio
async def test_generic_native_selection_bypasses_safe_voice_for_all_consumers(
    tmp_path,
    monkeypatch,
):
    server, runtime = _server(tmp_path)
    monkeypatch.setattr(workbench_api, "PERSISTENT_SESSION_V1_QUALIFIED", True)
    server.global_config.persistent_session_v1 = True
    server.global_config.native_audio_chat_v1 = True
    runtime._safevoice_enabled = True

    class _Transcriber:
        async def transcribe(self, _path):
            return "safe voice transcript"

    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber", lambda: _Transcriber()
    )
    session = server.session_store.create_session(
        owner_id="user:7", agent_id="lily", title="Safe Voice"
    )
    payload = b"OggSsafe-voice"
    staged = server.session_store.stage_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        filename="safe.ogg",
        media_type="audio/ogg",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        semantic_role="voice_message",
    )
    server.session_store.upload_attachment_bytes(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
        payload=payload,
    )
    server.session_store.commit_attachment(
        session_id=session["session_id"],
        owner_id="user:7",
        attachment_id=staged["attachment_id"],
    )
    run = json.loads(
        (
            await server.handle_v1_session_runs_create(
                _Request(
                    {
                        "idempotency_key": "safe-voice-run",
                        "message": {
                            "content": [
                                {
                                    "type": "audio",
                                    "attachment_id": staged["attachment_id"],
                                    "semantic_role": "voice_message",
                                    "mime_type": "audio/ogg",
                                }
                            ]
                        },
                    },
                    match_info={"session_id": session["session_id"]},
                )
            )
        ).text
    )
    ready = await _wait_for_event(
        server,
        session["session_id"],
        "voice.input.transcript_ready",
    )
    assert ready["detail"]["safe_voice_state"] == "released"
    assert all(
        event["kind"] != "voice.input.transcript_pending_confirmation"
        for event in server.session_store.events(
            session["session_id"], owner_id="user:7"
        )
    )
    state = runtime._native_voice_transcripts[run["request_id"]]
    assert state["safe_voice"] is False
    assert state["status"] == "released"
    assert state["release_event"].is_set() is True
    assert await await_authorized_transcript(state) == (
        "safe voice transcript",
        "released",
    )
    assert state["confirmation_requested"] is False
    assert all(
        event["kind"] != "voice.input.transcript_pending_confirmation"
        for event in server.session_store.events(
            session["session_id"], owner_id="user:7"
        )
    )
    await server.shutdown()


@pytest.mark.asyncio
async def test_native_audio_direct_completion_releases_ready_stt_without_prompt():
    calls: list[str] = []
    ready = asyncio.Event()
    ready.set()
    state = {
        "status": "ready",
        "safe_voice": True,
        "ready_event": ready,
        "release_event": asyncio.Event(),
        "gate_lock": asyncio.Lock(),
        "confirmation_requested": False,
        "confirmation_presented": False,
        "request_confirmation": lambda: calls.append("prompt"),
        "auto_release": lambda: calls.append("release"),
    }

    released = await complete_native_audio_response(
        state,
        {
            "success": True,
            "content": [
                {
                    "type": "audio",
                    "asset_id": "aud_native_reply",
                    "mime_type": "audio/wav",
                }
            ],
        },
    )

    assert released is True
    assert calls == ["release"]
    assert state["status"] == "released"
    assert state["release_event"].is_set() is True


@pytest.mark.asyncio
@pytest.mark.parametrize('native_model_ready', [True, False])
async def test_generic_turn_transcribes_each_voice_asset_once_and_keeps_order(
    tmp_path,
    monkeypatch,
    native_model_ready,
):
    server, runtime = _server(tmp_path)
    monkeypatch.setattr(workbench_api, "PERSISTENT_SESSION_V1_QUALIFIED", True)
    server.global_config.persistent_session_v1 = True
    server.global_config.native_audio_chat_v1 = True
    runtime._safevoice_enabled = False
    monkeypatch.setattr(server, '_runtime_native_audio_ready', lambda *args, **kwargs: native_model_ready)
    calls = []

    class _Transcriber:
        async def transcribe(self, path):
            calls.append(Path(path).name)
            return "transcript-first" if len(calls) == 1 else "transcript-second"

    transcriber = _Transcriber()
    monkeypatch.setattr(
        "orchestrator.voice_transcriber.get_transcriber", lambda: transcriber
    )
    session = server.session_store.create_session(
        owner_id="user:7", agent_id="lily", title="Two voice parts"
    )
    content = []
    attachment_ids = []
    for filename in ("first.wav", "second.wav"):
        payload = _wav_bytes(tmp_path / filename)
        staged = server.session_store.stage_attachment(
            session_id=session["session_id"],
            owner_id="user:7",
            filename=filename,
            media_type="audio/wav",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            semantic_role="voice_message",
        )
        server.session_store.upload_attachment_bytes(
            session_id=session["session_id"],
            owner_id="user:7",
            attachment_id=staged["attachment_id"],
            payload=payload,
        )
        server.session_store.commit_attachment(
            session_id=session["session_id"],
            owner_id="user:7",
            attachment_id=staged["attachment_id"],
        )
        attachment_ids.append(staged["attachment_id"])
        content.append(
            {
                "type": "audio",
                "attachment_id": staged["attachment_id"],
                "semantic_role": "voice_message",
                "mime_type": "audio/wav",
            }
        )

    run = json.loads(
        (
            await server.handle_v1_session_runs_create(
                _Request(
                    {
                        "idempotency_key": "two-voice-assets",
                        "message": {"content": content},
                    },
                    match_info={"session_id": session["session_id"]},
                )
            )
        ).text
    )
    assert run.get('ok') is True, run
    for _attempt in range(100):
        events = server.session_store.events(
            session["session_id"], owner_id="user:7"
        )
        ready = [
            event for event in events if event["kind"] == "voice.input.transcript_ready"
        ]
        if len(ready) == 2:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("both voice transcript Events were not produced")

    assert len(calls) == 2
    assert calls[0] != calls[1]
    state = runtime._native_voice_transcripts[run["request_id"]]
    assert state["attachment_ids"] == attachment_ids
    assert state["text"] == "transcript-first\ntranscript-second"
    message = server.session_store.messages(
        session["session_id"], owner_id="user:7"
    )[0]
    assert message["text"] == "transcript-first\n\ntranscript-second"
    await server.shutdown()


class _SSEOutput:
    def __init__(self, payload: bytes, *, split_base64: bool = False):
        self.payload = payload
        self.split_base64 = split_base64

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        encoded = base64.b64encode(self.payload).decode()
        chunks = [encoded]
        transcripts = ["normalized"]
        if self.split_base64:
            chunks = [encoded[:5], encoded[5:]]
            transcripts = ["norm", "alized"]
        for index, chunk in enumerate(chunks):
            yield "data: " + json.dumps(
                {
                    "choices": [
                        {
                            "delta": {
                                "audio": {
                                    "data": chunk,
                                    "transcript": transcripts[index],
                                }
                            },
                            "finish_reason": (
                                "stop" if index == len(chunks) - 1 else None
                            ),
                        }
                    ]
                }
            )
        yield "data: [DONE]"


class _SSEContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _SSEClient:
    is_closed = False

    def __init__(self, response):
        self.response = response
        self.payload = None

    def stream(self, _method, _url, *, json, headers):
        del headers
        self.payload = json
        return _SSEContext(self.response)

    async def aclose(self):
        self.is_closed = True


@pytest.mark.asyncio
@pytest.mark.platform
async def test_openrouter_normalizes_ogg_once_when_exact_model_requires_wav(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for provider-boundary normalization")
    wav = tmp_path / "source.wav"
    output_audio = _wav_bytes(wav, sample=1)
    ogg = tmp_path / "source.ogg"
    subprocess.run(
        [ffmpeg, "-y", "-i", str(wav), "-c:a", "libopus", str(ogg)],
        check=True,
        capture_output=True,
    )
    ogg_payload = ogg.read_bytes()
    content = canonical_request_content(
        [
            {
                **_voice_content(wav)["parts"][0],
                "filename": ogg.name,
                "mime_type": "audio/ogg",
                "local_ref": str(ogg),
                "size_bytes": len(ogg_payload),
                "sha256": hashlib.sha256(ogg_payload).hexdigest(),
            }
        ]
    )
    config = SimpleNamespace(
        name="format-test",
        engine="openrouter-api",
        model="configured-audio-model",
        workspace_dir=tmp_path,
        system_md=None,
        extra={
            "input_modalities": ["text", "audio"],
            "input_transports": {"audio": ["inline"]},
            "input_formats": {"audio": ["wav"]},
            "output_modalities": ["text", "audio"],
            "output_formats": {"audio": ["wav"]},
            "api_surface": "chat_completions",
            "output_streaming": "sse",
            "native_audio_voice": "alloy",
            "native_audio_format": "wav",
            "audio_model_tools": False,
            "_native_audio_claim_request_id": "outer-request",
        },
    )
    adapter = OpenRouterAdapter(
        config,
        SimpleNamespace(
            openrouter_url="https://openrouter.invalid/v1/chat/completions",
            base_media_dir=tmp_path,
        ),
        api_key="test",
    )
    client = _SSEClient(_SSEOutput(output_audio, split_base64=True))
    adapter.client = client

    response = await adapter.generate_response(
        "",
        "normalize-once",
        request_content=content,
        on_stream_event=lambda _event: asyncio.sleep(0),
    )

    assert response.is_success is True
    input_audio = client.payload["messages"][1]["content"][0]["input_audio"]
    assert input_audio["format"] == "wav"
    assert base64.b64decode(input_audio["data"]).startswith(b"RIFF")
    assert response.stream_metadata["audio_input_normalization"] == [
        {
            "attachment_id": "att-voice",
            "source_format": "ogg",
            "provider_format": "wav",
        }
    ]
    output_part = next(part for part in response.content if part["type"] == "audio")
    with pytest.raises(AudioAssetUnauthorized):
        adapter._native_audio_asset_store_instance().claim(
            output_part["asset_id"],
            owner_id="owner-1",
            session_id="session-1",
            request_id="stage-invocation",
        )
    claimed = adapter._native_audio_asset_store_instance().claim(
        output_part["asset_id"],
        owner_id="owner-1",
        session_id="session-1",
        request_id="outer-request",
    )
    assert claimed["session_id"] == "session-1"
    assert list((tmp_path / "native_audio_derivatives").glob("*")) == []


@pytest.mark.asyncio
async def test_openrouter_wraps_streaming_pcm16_output_for_terminal_delivery(tmp_path):
    input_path = tmp_path / "input.wav"
    _wav_bytes(input_path, sample=1)
    provider_pcm16 = b"\x01\x00" * 480
    config = SimpleNamespace(
        name="pcm16-output-test",
        engine="openrouter-api",
        model="configured-audio-model",
        workspace_dir=tmp_path,
        system_md=None,
        extra={
            "input_modalities": ["text", "audio"],
            "input_transports": {"audio": ["inline"]},
            "input_formats": {"audio": ["wav"]},
            "output_modalities": ["text", "audio"],
            "output_formats": {"audio": ["pcm16"]},
            "api_surface": "chat_completions",
            "output_streaming": "sse",
            "native_audio_voice": "alloy",
            "native_audio_format": "pcm16",
            "audio_model_tools": False,
            "_native_audio_claim_request_id": "outer-pcm16-request",
        },
    )
    adapter = OpenRouterAdapter(
        config,
        SimpleNamespace(
            openrouter_url="https://openrouter.invalid/v1/chat/completions",
            base_media_dir=tmp_path,
        ),
        api_key="test",
    )
    client = _SSEClient(_SSEOutput(provider_pcm16, split_base64=True))
    adapter.client = client

    response = await adapter.generate_response(
        "",
        "pcm16-stage-request",
        request_content=_voice_content(input_path),
        on_stream_event=lambda _event: asyncio.sleep(0),
    )

    assert client.payload["audio"] == {"voice": "alloy", "format": "pcm16"}
    output_part = next(part for part in response.content if part["type"] == "audio")
    assert output_part["format"] == "wav"
    assert output_part["mime_type"] == "audio/wav"
    claimed = adapter._native_audio_asset_store_instance().claim(
        output_part["asset_id"],
        owner_id="owner-1",
        session_id="session-1",
        request_id="outer-pcm16-request",
    )
    _, payload = adapter._native_audio_asset_store_instance().read_bytes(
        claimed["asset_id"], owner_id="owner-1", session_id="session-1"
    )
    assert payload.startswith(b"RIFF")
    with wave.open(io.BytesIO(payload), "rb") as source:
        assert source.getframerate() == 24_000
        assert source.getnchannels() == 1
        assert source.getsampwidth() == 2
        assert source.readframes(source.getnframes()) == provider_pcm16
    assert response.stream_metadata["native_audio"] == {
        "asset_id": output_part["asset_id"],
        "provider": "openrouter-api",
        "model": "configured-audio-model",
        "voice": "alloy",
        "provider_format": "pcm16",
        "format": "wav",
        "tools_enabled": False,
        "claimed": False,
    }
