"""Real file/public preference-operation regressions for HN-20260911-002.

No runtime, provider, Telegram connection, or production configuration is used.
The spawn barrier only fixes scheduling; reads and writes remain product code.
"""
from __future__ import annotations

import errno
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import config_json, ui_language


def _runtime(home: Path) -> SimpleNamespace:
    return SimpleNamespace(global_config=SimpleNamespace(
        bridge_home=home, project_root=home, authorized_id=42, ui_language="en",
    ))


def _seed(home: Path, raw: bytes) -> Path:
    path = ui_language.preferences_path(_runtime(home))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _change(runtime, operation: str, actor_id=42) -> Path:
    if operation == "reset":
        return ui_language.reset_preferred_locale(runtime, actor_id=actor_id)
    return ui_language.set_preferred_locale(runtime, "zh", actor_id=actor_id)


def test_bom_preferences_are_read_without_rewriting(tmp_path):
    raw = b'\xef\xbb\xbf{\r\n"version":1,"users":{"42":"zh-CN"}\r\n}\r\n'
    path = _seed(tmp_path, raw)
    assert ui_language.preferred_locale(_runtime(tmp_path), actor_id="user:42") == "zh-CN"
    assert path.read_bytes() == raw
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("operation", ["set", "reset"])
def test_update_preserves_unrelated_data_and_normalizes_only_output(tmp_path, operation):
    payload = {
        "version": 1,
        "users": {"user:42": "en", "84": "zh", "user:99": "future-locale"},
        "extension": {"label": "保留", "nested": [1, {"enabled": False}]},
    }
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8-sig")
    path = _seed(tmp_path, raw)
    assert _change(_runtime(tmp_path), operation) == path
    actual_bytes = path.read_bytes()
    assert not actual_bytes.startswith(b'\xef\xbb\xbf')
    assert b'\r' not in actual_bytes
    assert actual_bytes.endswith(b'\n')
    expected = dict(payload)
    expected["users"] = {"84": "zh", "user:99": "future-locale"}
    if operation == "set":
        expected["users"]["42"] = "zh-CN"
    assert json.loads(actual_bytes.decode("utf-8")) == expected
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize("raw", [
    b'{"users":',
    b'\xff',
    b'[]',
    b'{"version":1,"users":[]}',
    b'{"version":1,"users":null}',
    b'{"version":2,"users":{"42":"en"}}',
    b'{"version":true,"users":{"42":"en"}}',
])
@pytest.mark.parametrize("operation", ["set", "reset"])
def test_bad_preferences_are_not_replaced_by_display_fallback(tmp_path, raw, operation):
    path = _seed(tmp_path, raw)
    with pytest.raises((ValueError, UnicodeError)):
        _change(_runtime(tmp_path), operation)
    assert path.read_bytes() == raw
    assert not list(path.parent.glob("*.tmp"))


def test_truncated_preferences_display_fallback_is_read_only(tmp_path):
    raw = b'{"users":'
    path = _seed(tmp_path, raw)
    assert ui_language.preferred_locale(_runtime(tmp_path)) == "en"
    assert path.read_bytes() == raw
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ENOSPC])
def test_failed_publication_preserves_existing_bytes(tmp_path, monkeypatch, error_number):
    raw = b'{"version":1,"users":{"84":"en"},"keep":true}\n'
    path = _seed(tmp_path, raw)

    def fail_replace(*args, **kwargs):
        raise OSError(error_number, "injected publication failure")

    monkeypatch.setattr(config_json.os, "replace", fail_replace)
    with pytest.raises(OSError):
        ui_language.set_preferred_locale(_runtime(tmp_path), "zh")
    assert path.read_bytes() == raw
    assert not list(path.parent.glob("*.tmp"))


def test_post_publication_durability_error_is_not_rolled_back(tmp_path, monkeypatch):
    path = _seed(tmp_path, b'{"version":1,"users":{"84":"en"}}\n')

    def fail_sync(_parent):
        raise OSError(errno.EIO, "injected directory synchronization failure")

    monkeypatch.setattr(config_json, "_sync_directory", fail_sync)
    with pytest.raises(config_json.ConfigDurabilityError) as caught:
        ui_language.set_preferred_locale(_runtime(tmp_path), "zh")
    assert caught.value.committed is True
    assert json.loads(path.read_bytes())["users"] == {"84": "en", "42": "zh-CN"}
    assert not list(path.parent.glob("*.tmp"))


def _race_writer(home: str, actor: str, barrier, outcomes) -> None:
    # Arrange a real stale read in two independently imported processes.
    original = ui_language._read_preferences

    def synchronized_read(*args, **kwargs):
        payload = original(*args, **kwargs)
        barrier.wait(timeout=15)
        return payload

    ui_language._read_preferences = synchronized_read
    try:
        ui_language.set_preferred_locale(_runtime(Path(home)), "zh", actor_id=actor)
        outcomes.put((actor, "saved"))
    except config_json.ConfigConflictError:
        outcomes.put((actor, "conflict"))
    except Exception as exc:
        outcomes.put((actor, f"unexpected:{type(exc).__name__}:{exc}"))
    finally:
        ui_language._read_preferences = original


@pytest.mark.parametrize("existing", [False, True])
def test_two_processes_never_silently_overwrite_a_stale_snapshot(tmp_path, existing):
    if existing:
        _seed(tmp_path, b'{"version":1,"users":{"84":"en"},"keep":true}\n')
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    outcomes = context.Queue()
    processes = [context.Process(target=_race_writer, args=(str(tmp_path), actor, barrier, outcomes))
                 for actor in ("42", "43")]
    try:
        for process in processes:
            process.start()
        results = [outcomes.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        outcomes.close()
        outcomes.join_thread()
    assert sorted(status for _, status in results) == ["conflict", "saved"], results
    winner = next(actor for actor, status in results if status == "saved")
    loser = next(actor for actor, status in results if status == "conflict")
    path = ui_language.preferences_path(_runtime(tmp_path))
    expected_users = {"84": "en"} if existing else {}
    expected_users[winner] = "zh-CN"
    stored = json.loads(path.read_bytes())
    assert stored["users"] == expected_users
    if existing:
        assert stored["keep"] is True
    # A deliberate new operation reads the winner's revision; no hidden retry.
    ui_language.set_preferred_locale(_runtime(tmp_path), "zh", actor_id=loser)
    expected_users[loser] = "zh-CN"
    assert json.loads(path.read_bytes())["users"] == expected_users


def test_actor_alias_reset_and_instance_isolation(tmp_path):
    first, second = _runtime(tmp_path / "first"), _runtime(tmp_path / "second")
    ui_language.set_preferred_locale(first, "zh", actor_id="user:42")
    ui_language.set_preferred_locale(second, "zh", actor_id=42)
    ui_language.reset_preferred_locale(first, actor_id=42)
    assert ui_language.preferred_locale(first, actor_id="user:42") == "en"
    assert ui_language.preferred_locale(second, actor_id="user:42") == "zh-CN"


def test_legacy_document_without_version_remains_supported(tmp_path):
    path = _seed(tmp_path, b'{"users":{"84":"zh-CN"},"keep":true}\n')
    ui_language.set_preferred_locale(_runtime(tmp_path), "zh")
    assert json.loads(path.read_bytes()) == {
        "version": 1, "users": {"84": "zh-CN", "42": "zh-CN"}, "keep": True,
    }
