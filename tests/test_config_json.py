"""Configuration byte, publication, and cross-process conflict contracts."""
from __future__ import annotations

import copy
import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

from orchestrator import config_json
from orchestrator.config_json import (
    ConfigConflictError,
    ConfigDurabilityError,
    read_config_json,
    write_config_json,
)


def _competing_writer(path, barrier, results, value):
    snapshot = read_config_json(path)
    snapshot["winner"] = value
    barrier.wait(timeout=10)
    try:
        write_config_json(path, snapshot)
    except ConfigConflictError:
        results.put("conflict")
    else:
        results.put("committed")


def _lock_holder(path, entered, release):
    with config_json._write_lock(Path(path), 5):
        entered.set()
        release.wait(timeout=10)


def test_bom_read_is_pure_and_write_is_utf8_lf(tmp_path):
    path = tmp_path / "agents.json"
    original = b'\xef\xbb\xbf{\r\n  "name": "example"\r\n}\r\n'
    path.write_bytes(original)
    before_mtime = path.stat().st_mtime_ns
    document = read_config_json(path)
    assert dict(document) == {"name": "example"}
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before_mtime
    assert list(tmp_path.iterdir()) == [path]
    document["name"] = "小能"
    write_config_json(path, document)
    raw = path.read_bytes()
    assert not raw.startswith(b'\xef\xbb\xbf')
    assert b'\r' not in raw
    assert raw.endswith(b'\n')
    assert json.loads(raw.decode("utf-8")) == {"name": "小能"}
    assert json.loads(json.dumps(document)) == {"name": "小能"}


@pytest.mark.parametrize("copy_kind", ["original", "shallow", "deep"])
def test_stale_snapshot_rejected_even_after_copy(tmp_path, copy_kind):
    path = tmp_path / "agents.json"
    path.write_text('{"value": 0}', encoding="utf-8")
    first = read_config_json(path)
    second = read_config_json(path)
    first["value"] = 1
    write_config_json(path, first)
    second["value"] = 2
    second = second.copy() if copy_kind == "shallow" else copy.deepcopy(second) if copy_kind == "deep" else second
    before = path.read_bytes()
    with pytest.raises(ConfigConflictError):
        write_config_json(path, second)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_successful_document_can_be_edited_and_saved_again(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text('{"value": 0}', encoding="utf-8")
    document = read_config_json(path)
    old_revision = document.revision
    document["value"] = 1
    assert write_config_json(path, document) == document.revision != old_revision
    document["value"] = 2
    write_config_json(path, document)
    assert read_config_json(path)["value"] == 2


def test_snapshot_cannot_be_written_to_another_file(tmp_path):
    source, other = tmp_path / "one.json", tmp_path / "two.json"
    source.write_text('{"private": "one"}', encoding="utf-8")
    other.write_text('{"private": "two"}', encoding="utf-8")
    before = other.read_bytes()
    with pytest.raises(ConfigConflictError):
        write_config_json(other, read_config_json(source))
    assert other.read_bytes() == before


@pytest.mark.parametrize("data", [b'\xef\xbb\xbf{"broken":', b'[]', b'null', b'\xff'])
def test_invalid_read_does_not_repair_or_create_files(tmp_path, data):
    path = tmp_path / "agents.json"
    path.write_bytes(data)
    with pytest.raises((ValueError, UnicodeError)):
        read_config_json(path)
    assert path.read_bytes() == data
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("payload", [{"value": float("nan")}, {"value": float("inf")}, {"value": object()}, []])
def test_invalid_candidate_rejected_before_any_files_are_created(tmp_path, payload):
    path = tmp_path / "agents.json"
    path.write_text('{"value": 0}', encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        write_config_json(path, payload)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("fault", ["permissions", "fsync", "replace"])
def test_prepublication_fault_preserves_original_and_cleans_candidate(tmp_path, monkeypatch, fault):
    path = tmp_path / "agents.json"
    path.write_bytes(b'\xef\xbb\xbf{"value": 0}\r\n')
    before = path.read_bytes()
    snapshot = read_config_json(path)
    snapshot["value"] = 1

    def fail(*args):
        if fault == "permissions":
            assert args[0].read_bytes() == b""
        raise OSError("injected storage failure")

    owner, name = (config_json, "_protect_candidate") if fault == "permissions" else (config_json.os, fault)
    monkeypatch.setattr(owner, name, fail)
    with pytest.raises(OSError, match="injected storage failure"):
        write_config_json(path, snapshot)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_postpublication_durability_failure_is_not_reported_as_rollback(tmp_path, monkeypatch):
    path = tmp_path / "agents.json"
    path.write_text('{"value": 0}', encoding="utf-8")
    snapshot = read_config_json(path)
    snapshot["value"] = 1

    def fail(_path):
        raise OSError("directory sync failed")

    monkeypatch.setattr(config_json, "_sync_directory", fail)
    with pytest.raises(ConfigDurabilityError) as caught:
        write_config_json(path, snapshot)
    assert caught.value.committed is True
    assert read_config_json(path)["value"] == 1
    assert snapshot.revision == read_config_json(path).revision


def test_plain_dictionary_can_supply_explicit_revision_or_require_absence(tmp_path):
    path = tmp_path / "agents.json"
    revision = write_config_json(path, {"value": 1}, expected_revision=None)
    with pytest.raises(ConfigConflictError):
        write_config_json(path, {"value": 2}, expected_revision=None)
    write_config_json(path, {"value": 2}, expected_revision=revision)
    with pytest.raises(ConfigConflictError):
        write_config_json(path, {"value": 3}, expected_revision=revision)
    assert read_config_json(path)["value"] == 2


def test_legacy_plain_dict_replacement_still_uses_the_byte_contract(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text('{"value": 0}', encoding="utf-8")
    write_config_json(path, {"value": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"value": 1}


def test_deleted_source_is_not_silently_recreated_by_stale_document(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text('{}', encoding="utf-8")
    snapshot = read_config_json(path)
    path.unlink()
    with pytest.raises(ConfigConflictError):
        write_config_json(path, snapshot)
    assert not path.exists()


def test_independent_processes_cannot_both_publish_same_revision(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text('{"winner": null}', encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_competing_writer, args=(str(path), barrier, results, value)) for value in (1, 2)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert sorted(results.get(timeout=3) for _ in processes) == ["committed", "conflict"]
        assert read_config_json(path)["winner"] in {1, 2}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()


def test_os_lock_times_out_without_writing_then_recovers(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text('{"value": 0}', encoding="utf-8")
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    process = ctx.Process(target=_lock_holder, args=(str(path), entered, release))
    process.start()
    try:
        assert entered.wait(timeout=10)
        before = path.read_bytes()
        with pytest.raises(TimeoutError):
            write_config_json(path, {"value": 1}, lock_timeout=0)
        assert path.read_bytes() == before
        assert not list(tmp_path.glob("*.tmp"))
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
    assert process.exitcode == 0
    write_config_json(path, {"value": 2}, lock_timeout=0)
    assert read_config_json(path)["value"] == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits; Windows uses DACLs")
def test_posix_candidate_and_published_file_are_private(tmp_path, monkeypatch):
    path = tmp_path / "agents.json"
    real_replace = os.replace
    observed = []

    def check_replace(source, target):
        observed.append(stat.S_IMODE(Path(source).stat().st_mode))
        return real_replace(source, target)

    monkeypatch.setattr(config_json.os, "replace", check_replace)
    write_config_json(path, {"private": "value"})
    assert observed == [0o600]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="native POSIX symlink contract")
def test_symlink_lock_is_rejected_without_touching_destination(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text('{}', encoding="utf-8")
    victim = tmp_path / "other"
    victim.write_text("unrelated", encoding="utf-8")
    (tmp_path / ".agents.json.lock").symlink_to(victim)
    with pytest.raises(OSError):
        write_config_json(path, {"value": 1})
    assert path.read_text(encoding="utf-8") == '{}'
    assert victim.read_text(encoding="utf-8") == "unrelated"
