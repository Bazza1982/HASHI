from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat

import pytest

from orchestrator import config_json
from orchestrator.backend_timeout import (
    clear_timeout_override,
    read_timeout_override,
    set_timeout_override,
)

from orchestrator import workspace_state
from orchestrator.process_resources import path_lock as process_path_lock
from orchestrator.workspace_state import WorkspaceStateStore


def test_workspace_state_update_preserves_unowned_blocks(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"active_backend": "codex-cli", "memory_plus": {"enabled": True}})

    store.update(lambda state: state.update({"privacy_level": 1}))

    assert store.read() == {
        "active_backend": "codex-cli",
        "memory_plus": {"enabled": True},
        "privacy_level": 1,
    }


def test_workspace_state_replace_is_valid_json_and_leaves_no_temp_file(tmp_path):
    store = WorkspaceStateStore(tmp_path)

    store.replace({"agent_mode": "dual-brain", "label": "月如"})

    assert json.loads(store.path.read_text(encoding="utf-8"))["label"] == "月如"
    assert list(tmp_path.glob(".state.json.tmp-*")) == []


def test_workspace_state_lock_is_owned_by_stable_core_registry(tmp_path):
    path = tmp_path / "state.json"

    assert workspace_state._path_lock(path) is process_path_lock(path)


def test_process_path_lock_is_stable_when_target_appears(tmp_path):
    path = tmp_path / "new-parent" / "state.json"
    before = process_path_lock(path)

    path.parent.mkdir()
    path.write_text("{}\n", encoding="utf-8")

    assert process_path_lock(path) is before


def test_legacy_bom_read_is_nonmutating_and_update_keeps_unowned_state(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    expected = {"active_backend": "test-engine", "extension": {"label": "原样"}}
    before = b"\xef\xbb\xbf" + json.dumps(expected, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode()
    store.path.write_bytes(before)
    stamp = store.path.stat().st_mtime_ns
    assert store.read() == expected
    assert type(store.read()) is dict
    assert store.path.read_bytes() == before and store.path.stat().st_mtime_ns == stamp

    result = store.update(lambda state: state.update({"privacy_level": 1}))
    expected["privacy_level"] = 1
    assert result == expected == store.read()
    raw = store.path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    assert raw.endswith(b"\n")


@pytest.mark.parametrize("bad", [b'{"active_backend":', b'[]', b'\xff'])
@pytest.mark.parametrize("operation", ["update", "replace"])
def test_bad_saved_state_cannot_be_overwritten_by_read_fallback(tmp_path, bad, operation):
    store = WorkspaceStateStore(tmp_path)
    store.path.write_bytes(bad)
    calls = []

    def edit(state):
        calls.append(True)
        state["privacy_level"] = 1

    assert store.read() == {}  # Existing display/read fallback is retained.
    with pytest.raises(ValueError):
        if operation == "update":
            store.update(edit)
        else:
            store.replace({"privacy_level": 1})
    assert not calls
    assert store.path.read_bytes() == bad
    assert not list(tmp_path.glob(".state.json*"))


def test_unreadable_saved_state_does_not_run_mutator(tmp_path, monkeypatch):
    store = WorkspaceStateStore(tmp_path)
    store.path.write_text('{"keep": true}\n', encoding="utf-8")
    before = store.path.read_bytes()
    read_bytes, read_text = type(store.path).read_bytes, type(store.path).read_text

    def deny_bytes(path):
        if path == store.path:
            raise PermissionError("state read denied")
        return read_bytes(path)

    def deny_text(path, *args, **kwargs):
        if path == store.path:
            raise PermissionError("state read denied")
        return read_text(path, *args, **kwargs)

    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(type(store.path), "read_bytes", deny_bytes)
        patch.setattr(type(store.path), "read_text", deny_text)
        assert store.read() == {}
        with pytest.raises(PermissionError, match="state read denied"):
            store.update(lambda state: calls.append(True))
    assert not calls and store.path.read_bytes() == before


def test_returned_plain_dict_keeps_original_revision_and_conflict_is_not_retried(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"keep": {"value": 1}})
    other = WorkspaceStateStore(tmp_path)
    calls = []

    def edit(state):
        calls.append(True)
        other.update(lambda latest: latest.update({"winner": True}))
        return {**state, "loser": True}  # Deliberately drops any dict-subclass metadata.

    with pytest.raises(config_json.ConfigConflictError):
        store.update(edit)
    assert calls == [True]
    assert store.read() == {"keep": {"value": 1}, "winner": True}
    store.update(lambda latest: latest.update({"loser": True}))
    assert store.read() == {"keep": {"value": 1}, "winner": True, "loser": True}


def _update_in_process(workspace, key, barrier, output):
    store = WorkspaceStateStore(workspace)
    calls = 0

    def edit(state):
        nonlocal calls
        calls += 1
        barrier.wait(timeout=15)
        state[key] = True

    try:
        store.update(edit)
    except config_json.ConfigConflictError:
        status = "conflict"
    except Exception as exc:
        status = type(exc).__name__
    else:
        status = "saved"
    output.put((key, status, calls))


@pytest.mark.parametrize("existing", [False, True])
def test_spawned_updates_have_one_winner_without_losing_prior_state(tmp_path, existing):
    workspace = tmp_path / "new-workspace"
    store = WorkspaceStateStore(workspace)
    initial = {"extension": {"keep": True}} if existing else {}
    if existing:
        store.replace(initial)
    ctx = multiprocessing.get_context("spawn")
    barrier, output = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_update_in_process, args=(workspace, key, barrier, output))
                 for key in ("first", "second")]
    try:
        for process in processes:
            process.start()
        results = [output.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(status for _, status, _ in results) == ["conflict", "saved"]
        assert all(calls == 1 for _, _, calls in results)
        winner = next(key for key, status, _ in results if status == "saved")
        loser = next(key for key, status, _ in results if status == "conflict")
        assert store.read() == {**initial, winner: True}
        # A new explicit operation is allowed, and observes the winner.
        store.update(lambda state: state.update({loser: True}))
        assert store.read() == {**initial, "first": True, "second": True}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        output.close()
        output.join_thread()


@pytest.mark.parametrize("failure", ["permissions", "file_sync", "replace"])
def test_prepublication_failure_preserves_file_and_cleans_private_candidate(tmp_path, monkeypatch, failure):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"keep": True})
    before = store.path.read_bytes()
    backup = tmp_path / "state.json.bak"
    backup.write_bytes(before)

    def denied(candidate):
        assert candidate.read_bytes() == b""
        raise PermissionError(errno.EACCES, "private file denied")

    def full(_descriptor):
        raise OSError(errno.ENOSPC, "disk full")

    def occupied(_source, _target):
        raise PermissionError(errno.EACCES, "destination occupied")

    with monkeypatch.context() as patch:
        if failure == "permissions":
            patch.setattr(config_json, "_protect_candidate", denied)
        elif failure == "file_sync":
            patch.setattr(config_json.os, "fsync", full)
        else:
            patch.setattr(config_json.os, "replace", occupied)
        with pytest.raises(OSError):
            store.update(lambda state: state.update({"new": True}))
    assert store.path.read_bytes() == before == backup.read_bytes()
    assert not list(tmp_path.glob(".state.json.*.tmp"))
    assert not list(tmp_path.glob(".state.json.tmp-*"))


def test_committed_durability_error_preserves_published_state_and_never_replays(tmp_path, monkeypatch):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"count": 1})
    calls, publications = [], []
    replace = config_json.os.replace

    def edit(state):
        calls.append(True)
        state["count"] += 1

    def publish(source, target):
        publications.append(target)
        return replace(source, target)

    def fail(_parent):
        raise OSError("directory sync failed")

    with monkeypatch.context() as patch:
        patch.setattr(config_json.os, "replace", publish)
        patch.setattr(config_json, "_sync_directory", fail)
        with pytest.raises(config_json.ConfigDurabilityError) as error:
            store.update(edit)
    assert error.value.committed is True
    assert calls == [True] and len(publications) == 1
    assert store.read() == {"count": 2}
    assert not list(tmp_path.glob(".state.json.*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows DACL requires native qualification")
def test_real_candidate_is_private_before_payload_and_remains_private(tmp_path, monkeypatch):
    store = WorkspaceStateStore(tmp_path)
    protect = config_json._protect_candidate
    observed = []

    def inspect(candidate):
        protect(candidate)
        observed.append(candidate.read_bytes())
        assert stat.S_IMODE(candidate.stat().st_mode) == 0o600

    monkeypatch.setattr(config_json, "_protect_candidate", inspect)
    store.replace({"preference": "private"})
    store.update(lambda state: state.update({"another": True}))
    assert observed == [b"", b""]
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_removal_after_read_is_not_undone_by_late_update(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"keep": True})

    def edit(state):
        store.path.unlink()
        state["late"] = True

    with pytest.raises(config_json.ConfigConflictError):
        store.update(edit)
    assert not store.path.exists()


def test_invalid_candidate_never_replaces_a_valid_file(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"keep": True})
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.update(lambda state: {**state, "invalid": float("nan")})
    assert store.path.read_bytes() == before


def test_mutator_failure_and_bad_return_never_write(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"nested": {"keep": True}})
    before = store.path.read_bytes()

    def fail(state):
        state["nested"]["keep"] = False
        raise RuntimeError("mutation failed")

    with pytest.raises(RuntimeError, match="mutation failed"):
        store.update(fail)
    with pytest.raises(TypeError, match="mutator"):
        store.update(lambda state: [])
    assert store.path.read_bytes() == before


def test_explicit_replace_keeps_whole_document_semantics_and_returns_plain_dict(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"remove": True})
    result = store.replace({"replacement": True})
    assert type(result) is dict
    assert store.read() == result == {"replacement": True}


def test_timeout_consumer_preserves_legacy_blocks_through_set_and_clear(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    initial = {"active_backend": "test-engine", "unowned": {"keep": "原样"}}
    store.path.write_bytes(b"\xef\xbb\xbf" + json.dumps(initial).encode())
    set_timeout_override(store, "first", idle_seconds=120)
    set_timeout_override(store, "second", idle_seconds=240)
    assert read_timeout_override(store, "second") == {"idle_timeout_sec": 240}
    clear_timeout_override(store, "first")
    assert store.read() == {**initial, "backend_timeouts": {"second": {"idle_timeout_sec": 240}}}


@pytest.mark.parametrize("operation", ["set", "clear"])
def test_timeout_consumer_cannot_overwrite_unreadable_state(tmp_path, operation):
    store = WorkspaceStateStore(tmp_path)
    before = b'{"backend_timeouts":'
    store.path.write_bytes(before)
    with pytest.raises(ValueError):
        if operation == "set":
            set_timeout_override(store, "first", idle_seconds=120)
        else:
            clear_timeout_override(store, "first")
    assert store.path.read_bytes() == before


def test_explicit_replace_rejects_a_shared_writer_after_its_read(tmp_path, monkeypatch):
    store = WorkspaceStateStore(tmp_path)
    store.replace({"keep": True})
    original = type(store.path).read_bytes
    intervened = []

    def read_then_interleave(path):
        raw = original(path)
        if path == store.path and not intervened:
            intervened.append(True)
            winner = config_json.read_config_json(path)
            winner["other_writer"] = True
            config_json.write_config_json(path, winner)
        return raw

    with monkeypatch.context() as patch:
        patch.setattr(type(store.path), "read_bytes", read_then_interleave)
        with pytest.raises(config_json.ConfigConflictError):
            store.replace({"replacement": True})
    assert intervened == [True]
    assert store.read() == {"keep": True, "other_writer": True}
