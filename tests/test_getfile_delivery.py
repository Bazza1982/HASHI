"""Real SessionStore/byte acquisition with controlled transport endpoints."""
from __future__ import annotations
import asyncio
import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from orchestrator.canonical_audit import CanonicalAuditStore
from orchestrator.getfile_service import GetFileError, GetFileService, resolve_path
from orchestrator.session_store import SessionStore


@pytest.fixture
def target(tmp_path):
    home = tmp_path / "instance"; home.mkdir()
    work = tmp_path / "work"; work.mkdir()
    store = SessionStore(home / "state" / "sessions.sqlite3", instance_id="GETFILE-QA")
    session = store.resolve_primary_session(owner_id="user:7", agent_id="alice", establish=True)
    runtime = NS(name="alice", workspace_dir=work, session_store=store, telegram_connected=False,
        global_config=NS(bridge_home=home, project_root=home, instance_id="GETFILE-QA", authorized_id=7),
        canonical_audit=CanonicalAuditStore(home, instance_id="GETFILE-QA", agent_id="alice"),
        _is_authorized_user=lambda user: user == 7,
        logger=logging.getLogger("getfile-test"), error_logger=logging.getLogger("getfile-test"))
    update = NS(effective_user=NS(id=7), effective_chat=NS(id=7), update_id=123,
        effective_message=NS(message_id=123), _hashi_session_surface="workbench",
        _hashi_session_channel_key="test-connection", _hashi_session_owner_id="user:7",
        _hashi_session_id=session["session_id"], _hashi_session_context_generation=1)
    return runtime, update, work, session


def observe(runtime, work, name):
    runtime.canonical_audit.record("tool_call", {"tool_name": "file_write", "is_error": False,
        "arguments": {"path": str(work / name)}, "details": {"file_effect": {
            "path": str(work / name), "action": "created", "verified": True}}},
        provenance={"workspace_dir": str(work), "access_roots": [str(work)], "owner_id": "user:7"})


@pytest.mark.asyncio
async def test_current_bytes_changed_notice_reentry_and_no_mirrors_or_run(target, monkeypatch):
    runtime, update, work, session = target
    (work / "a report.docx").write_bytes(b"old")
    observe(runtime, work, "a report.docx")
    service = GetFileService(runtime)
    token, rows = service.menu(update)
    (work / "a report.docx").write_bytes(b"current\x00bytes")
    monkeypatch.setattr("orchestrator.frontend_delivery.configured_owner_mirror_destinations",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("mirror policy invoked")))
    result = (await service.transfer(update, token=token, index=0))["getfile"]
    assert result["changed"] is True and result["delivery_state"] == "published"
    assert result["sha256"] == hashlib.sha256(b"current\x00bytes").hexdigest()
    _, payload = runtime.session_store.attachment_bytes(session_id=session["session_id"], owner_id="user:7",
                                                      attachment_id=result["attachment_id"])
    assert payload == b"current\x00bytes"
    (work / "a report.docx").unlink()
    same = (await GetFileService(runtime).transfer(update, token=token, index=0))["getfile"]
    assert same["message_id"] == result["message_id"]
    rows = runtime.session_store.messages(session["session_id"], owner_id="user:7")
    assert len([r for r in rows if r["source"].endswith(".explicit-media")]) == 1
    assert not runtime.session_store.recent_session_runs(session["session_id"], owner_id="user:7")


@pytest.mark.asyncio
async def test_direct_quoted_path_and_duplicate_clicks_publish_once(target):
    runtime, update, work, session = target
    path = work / "with spaces.txt"; path.write_bytes(b"\x00\xffraw")
    service = GetFileService(runtime)
    a, b = await asyncio.gather(
        service.transfer(update, raw_path='"' + str(path) + '"'),
        GetFileService(runtime).transfer(update, raw_path='"' + str(path) + '"'))
    assert a["getfile"]["message_id"] == b["getfile"]["message_id"]
    assert len(runtime.session_store.messages(session["session_id"], owner_id="user:7")) == 1


@pytest.mark.asyncio
async def test_telegram_offline_is_pending_not_fake_success(target):
    runtime, update, work, session = target
    path = work / "file.bin"; path.write_bytes(b"body")
    update._hashi_session_surface = "telegram"; update._hashi_session_channel_key = "7"
    result = (await GetFileService(runtime).transfer(update, raw_path=str(path)))["getfile"]
    assert result["delivery_state"] == "queued"
    assert not runtime.session_store.frontend_delivery_receipts(session_id=session["session_id"],
        owner_id="user:7", event_id=result["delivery_event_id"])


@pytest.mark.asyncio
async def test_menu_binding_expiry_and_permission_are_rechecked(target):
    runtime, update, work, session = target
    path = work / "file.txt"; path.write_text("ok"); observe(runtime, work, path.name)
    service = GetFileService(runtime); token, _ = service.menu(update)
    update._hashi_session_channel_key = "another-chat"
    with pytest.raises(GetFileError, match="stale"):
        await service.transfer(update, token=token, index=0)
    update._hashi_session_channel_key = "test-connection"
    runtime._is_authorized_user = lambda _: False
    with pytest.raises(GetFileError, match="denied"):
        await service.transfer(update, token=token, index=0)
    runtime._is_authorized_user = lambda _: True
    with service.connect() as db:
        db.execute("UPDATE menus SET expires=0")
    with pytest.raises(GetFileError, match="expired"):
        await service.transfer(update, token=token, index=0)


@pytest.mark.asyncio
async def test_missing_outside_scope_and_large_files_never_publish(target, monkeypatch):
    runtime, update, work, session = target
    service = GetFileService(runtime)
    for path, code in [(work / "missing.txt", "missing"), (work.parent / "outside.txt", "denied")]:
        with pytest.raises(GetFileError, match=code):
            await service.transfer(update, raw_path=str(path))
    path = work / "big"; path.write_bytes(b"abc")
    monkeypatch.setattr("orchestrator.getfile_service.MAX_SESSION_ATTACHMENT_BYTES", 2)
    with pytest.raises(GetFileError, match="too_large"):
        await service.transfer(update, raw_path=str(path))
    assert runtime.session_store.messages(session["session_id"], owner_id="user:7") == []


@pytest.mark.asyncio
async def test_reparse_or_symlink_is_never_followed(target):
    runtime, update, work, session = target
    path = work / "real"; path.write_bytes(b"secret")
    link = work / "link"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("OS account cannot create symbolic links")
    with pytest.raises(GetFileError, match="unavailable"):
        await GetFileService(runtime).transfer(update, raw_path=str(link))
    assert runtime.session_store.messages(session["session_id"], owner_id="user:7") == []


@pytest.mark.asyncio
async def test_native_and_shell_activity_have_honest_coverage(target):
    runtime, update, work, session = target
    runtime.canonical_audit.record("tool_call", {"tool_name": "shell", "is_error": False,
        "details": {"file_observations": [{"path": str(work / "shell.txt")}] }},
        provenance={"workspace_dir": str(work), "access_roots": [str(work)], "owner_id": "user:7"})
    runtime.canonical_audit.record("provider_stream_event", {"kind": "file_edit",
        "file_path": "one.txt", "metadata": {"file_paths": ["one.txt", "two.txt"]}},
        provenance={"getfile_scope": {"roots": [str(work)], "cwd": str(work), "owner_id": "user:7"}})
    from orchestrator.getfile_index import RecentFileIndex
    rows = RecentFileIndex(runtime.canonical_audit).recent(owner_id="user:7")
    assert {r["relative_path"] for r in rows} == {"one.txt", "two.txt", "shell.txt"}
    assert all(r["action"] == "touched" for r in rows)
    assert {r["evidence"] for r in rows} == {"engine_reported", "interval_observation"}
