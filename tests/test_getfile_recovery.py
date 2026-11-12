"""Projection recovery and literal-path/route boundaries."""
import os
from pathlib import Path
import pytest

from orchestrator.getfile_index import RecentFileIndex
from orchestrator.getfile_service import GetFileError, GetFileService
from tests.test_getfile_index import audit, record
from tests.test_getfile_delivery import target


def test_failed_projection_is_reconciled_without_repeating_committed_tool(tmp_path, monkeypatch):
    store = audit(tmp_path)
    index = RecentFileIndex(store)
    real = RecentFileIndex.ingest
    with monkeypatch.context() as patch:
        patch.setattr(RecentFileIndex, "ingest", lambda *a: (_ for _ in ()).throw(OSError("database busy")))
        event_id = record(store, tmp_path / "work", "committed.txt")
        assert event_id and index.dirty.exists()
    row, = RecentFileIndex(store).recent(owner_id="owner")
    assert row["relative_path"] == "committed.txt"
    assert not index.dirty.exists()
    # Successful bounded reconciliation is not performed on every menu open.
    monkeypatch.setattr(store, "recent_projection_events",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("repeated scan")))
    assert len(RecentFileIndex(store).recent(owner_id="owner")) == 1


@pytest.mark.asyncio
async def test_windows_wsl_mounted_literal_path_preserves_actual_bytes(target):
    if os.name != "nt":
        pytest.skip("Windows-host WSL /mnt translation")
    runtime, update, work, session = target
    path = work / "WSL style report.txt"
    path.write_bytes(b"literal-mounted-drive")
    wsl = "/mnt/" + path.drive[0].lower() + path.as_posix()[2:]
    result = (await GetFileService(runtime).transfer(update, raw_path=wsl))["getfile"]
    assert result["size_bytes"] == len(path.read_bytes())


@pytest.mark.asyncio
async def test_telegram_topic_fails_closed_instead_of_sending_to_general(target):
    runtime, update, work, session = target
    path = work / "topic.txt"; path.write_bytes(b"topic")
    update._hashi_session_surface = "telegram"
    update._hashi_session_channel_key = "7"
    update.effective_message.message_thread_id = 42
    with pytest.raises(GetFileError, match="unsupported_topic"):
        await GetFileService(runtime).transfer(update, raw_path=str(path))
    assert runtime.session_store.messages(session["session_id"], owner_id="user:7") == []


@pytest.mark.asyncio
async def test_permission_revocation_during_read_never_publishes(target, monkeypatch):
    runtime, update, work, session = target
    path = work / "revoked.txt"; path.write_bytes(b"body")
    from orchestrator import getfile_service
    original = getfile_service.snapshot
    def revoke(*args, **kwargs):
        value = original(*args, **kwargs)
        runtime._is_authorized_user = lambda _: False
        return value
    monkeypatch.setattr(getfile_service, "snapshot", revoke)
    with pytest.raises(GetFileError, match="denied"):
        await GetFileService(runtime).transfer(update, raw_path=str(path))
    assert runtime.session_store.messages(session["session_id"], owner_id="user:7") == []
