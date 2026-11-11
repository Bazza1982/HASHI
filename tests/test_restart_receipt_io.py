"""A concurrent observer cannot block/tear Remote's atomic progress snapshots."""
import json
import time
import pytest
from remote import restart_receipts as receipts

def test_brief_observer_does_not_lose_atomic_progress_or_tear_snapshot(tmp_path):
    import threading
    path=tmp_path/"receipt.json";temporary=tmp_path/"next.tmp"
    path.write_text('{"state":"accepted"}',encoding="utf-8")
    temporary.write_text('{"state":"verifying"}',encoding="utf-8")
    ready=threading.Event();release=threading.Event();old=[]
    def read():
        with receipts.open_restart_receipt(path) as snapshot:
            old.append(json.loads(snapshot.read()))
            ready.set()
            release.wait(2)
    observer=threading.Thread(target=read);observer.start()
    assert ready.wait(2)
    timer=threading.Timer(.03,release.set);timer.start()
    try:
        receipts.replace_restart_receipt(temporary,path)
    finally:
        release.set();observer.join(2);timer.cancel()
    assert old==[{"state":"accepted"}]
    assert json.loads(receipts.read_restart_receipt_text(path))=={"state":"verifying"}


def test_transient_windows_sharing_denial_retries_without_partial_publication(tmp_path,monkeypatch):
    path=tmp_path/"receipt.json";temporary=tmp_path/"next.tmp"
    path.write_text('{"state":"accepted"}',encoding="utf-8")
    temporary.write_text('{"state":"completed"}',encoding="utf-8")
    actual=receipts.os.replace;calls=[]
    def replace(a,b):
        calls.append(1)
        if len(calls)<3:
            error=PermissionError("brief sharing denial");error.winerror=32;raise error
        return actual(a,b)
    monkeypatch.setattr(receipts.os,"replace",replace)
    receipts.replace_restart_receipt(temporary,path)
    assert json.loads(path.read_text(encoding="utf-8"))["state"]=="completed"

def test_persistent_denial_is_bounded_and_preserves_last_committed_record(tmp_path,monkeypatch):
    path=tmp_path/"receipt.json";temporary=tmp_path/"next.tmp"
    path.write_text('{"state":"accepted"}',encoding="utf-8")
    temporary.write_text('{"state":"completed"}',encoding="utf-8")
    def denied(*_):
        error=PermissionError("denied");error.winerror=5;raise error
    monkeypatch.setattr(receipts.os,"replace",denied)
    started=time.monotonic()
    with pytest.raises(PermissionError):receipts.replace_restart_receipt(temporary,path)
    assert time.monotonic()-started<2
    assert json.loads(path.read_text(encoding="utf-8"))["state"]=="accepted"

def test_snapshot_read_enforces_size_bound(tmp_path):
    path=tmp_path/"receipt.json";path.write_bytes(b"x"*100)
    with pytest.raises(ValueError,match="limit"):
        receipts.read_restart_receipt_text(path,limit=10)
