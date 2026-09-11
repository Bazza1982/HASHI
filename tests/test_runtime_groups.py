from __future__ import annotations

import copy
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import config_json, runtime_groups
from orchestrator.agent_directory import AgentDirectory


class _Directory:
    def __init__(self):
        self.groups = {}
        self._agent_rows = {}

    def list_groups(self):
        return self.groups

    def resolve_group(self, name, exclude_self=None):
        members = self.groups.get(name, {}).get("members", [])
        if members == "@active":
            members = list(self._agent_rows)
        return [member for member in members if member != exclude_self]

    def get_agent_row(self, name):
        return self._agent_rows.get(name)

    def group_exists(self, name):
        return name in self.groups


def test_group_list_view_handles_empty_directory():
    text, markup = runtime_groups.group_list_view(_Directory())

    assert "<code>0</code> groups" in text
    assert markup.inline_keyboard[-1][0].callback_data == "group:new"


@pytest.mark.asyncio
async def test_group_command_creation_stays_in_group_module(configured):
    path, directory, _ = configured
    latest = config_json.read_config_json(path)
    latest["groups"]["other"] = {"members": ["peer@OTHER"]}
    config_json.write_config_json(path, latest)
    replies = []

    async def reply(_update, text, **kwargs):
        replies.append((text, kwargs))

    runtime = SimpleNamespace(
        agent_directory=directory,
        _is_authorized_user=lambda user_id: user_id == 1,
        _reply_text=reply,
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await runtime_groups.cmd_group(
        runtime,
        update,
        SimpleNamespace(args=["new", "reviewers", "Review", "team"]),
    )

    saved = config_json.read_config_json(path)
    assert saved["groups"]["reviewers"]["description"] == "Review team"
    assert saved["groups"]["other"] == {"members": ["peer@OTHER"]}
    assert directory.list_groups() == saved["groups"]
    assert replies[-1][0].startswith("✅ ")


@pytest.mark.asyncio
async def test_group_command_rejects_missing_directory():
    replies = []

    async def reply(_update, text, **kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        agent_directory=None,
        _is_authorized_user=lambda user_id: True,
        _reply_text=reply,
    )

    await runtime_groups.cmd_group(
        runtime,
        SimpleNamespace(effective_user=SimpleNamespace(id=1)),
        SimpleNamespace(args=[]),
    )

    assert replies == ["❌ Agent directory unavailable."]


def _directory(path: Path) -> AgentDirectory:
    return AgentDirectory(path, path.with_name("absent-capabilities.json"), [])


@pytest.fixture
def configured(tmp_path):
    path = tmp_path / "agents.json"
    initial = {
        "global": {"instance_id": "TEST", "ports": {"api": 43172}},
        "agents": [{"name": "alice", "is_active": True}],
        "unknown": {"preserve": [1, "原样"]},
        "groups": {
            "team": {
                "description": "Review", "members": ["alice", "peer@OTHER"],
                "exclude_from_broadcast": ["peer@OTHER"], "extension": {"v": 2},
            },
        },
    }
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(initial, ensure_ascii=False, indent=2).replace("\n", "\r\n").encode())
    return path, _directory(path), initial


def _mutate(directory, operation):
    return {
        "create": lambda: directory.create_group("new", "新增"),
        "delete": lambda: directory.delete_group("team"),
        "rename": lambda: directory.group_rename("team", "renamed"),
        "add": lambda: directory.group_add_member("team", "bob"),
        "remove": lambda: directory.group_remove_member("team", "alice"),
    }[operation]()


@pytest.mark.parametrize("operation", ["create", "delete", "rename", "add", "remove"])
def test_mutations_preserve_updates_since_directory_was_loaded(configured, operation):
    path, directory, initial = configured
    latest = config_json.read_config_json(path)
    latest["global"]["ports"]["api"] = 45218
    latest["groups"]["other"] = {"members": "@active", "extension": ["keep"]}
    latest["groups"]["team"]["members"].append("carol")
    config_json.write_config_json(path, latest)
    expected = copy.deepcopy(dict(latest))
    groups = expected["groups"]
    if operation == "create":
        groups["new"] = {"description": "新增", "members": [], "exclude_from_broadcast": []}
    elif operation == "delete":
        del groups["team"]
    elif operation == "rename":
        groups["renamed"] = groups.pop("team")
    elif operation == "add":
        groups["team"]["members"].append("bob")
    else:
        groups["team"]["members"].remove("alice")

    ok, message = _mutate(directory, operation)

    assert ok is True and isinstance(message, str) and message
    assert dict(config_json.read_config_json(path)) == expected
    assert directory.list_groups() == expected["groups"]
    assert _directory(path).list_groups() == expected["groups"]
    assert expected["agents"] == initial["agents"]
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw and raw.endswith(b"\n")


@pytest.mark.parametrize("operation", ["create", "delete", "rename", "add", "remove"])
def test_noop_decisions_use_current_document_and_do_not_publish(configured, operation):
    path, directory, _ = configured
    latest = config_json.read_config_json(path)
    if operation == "create":
        latest["groups"]["new"] = {"members": ["elsewhere"], "description": "Not ours"}
    elif operation == "delete":
        del latest["groups"]["team"]
    elif operation == "rename":
        latest["groups"]["renamed"] = {"members": ["elsewhere"]}
    elif operation == "add":
        latest["groups"]["team"]["members"].append("bob")
    else:
        latest["groups"]["team"]["members"].remove("alice")
    # A no-op must retain even a legacy representation and its mtime.
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(latest, indent=2).replace("\n", "\r\n").encode())
    before, stamp = path.read_bytes(), path.stat().st_mtime_ns

    ok, message = _mutate(directory, operation)

    assert ok is False and isinstance(message, str)
    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert directory.list_groups() == latest["groups"]
    assert not path.with_name(".agents.json.lock").exists()


@pytest.mark.parametrize("operation", ["create", "delete", "rename", "add", "remove"])
def test_prepublication_error_leaves_disk_and_visible_groups_unchanged(configured, monkeypatch, operation):
    path, directory, _ = configured
    before, visible = path.read_bytes(), directory.list_groups()

    def denied(candidate):
        # Fail at the actual empty candidate protection boundary, before payload.
        assert candidate.read_bytes() == b""
        raise PermissionError("private candidate denied")

    monkeypatch.setattr(config_json, "_protect_candidate", denied)
    with pytest.raises(PermissionError, match="private candidate denied"):
        _mutate(directory, operation)
    assert path.read_bytes() == before
    assert directory.list_groups() == visible
    assert not list(path.parent.glob(".agents.json.*.tmp"))


def test_committed_durability_error_is_visible_without_retry_or_rollback(configured, monkeypatch):
    path, directory, _ = configured
    publications = []
    replace = config_json.os.replace

    def publish(source, target):
        publications.append(str(target))
        return replace(source, target)

    def fail_sync(_parent):
        raise OSError("directory sync failed")

    with monkeypatch.context() as patch:
        patch.setattr(config_json.os, "replace", publish)
        patch.setattr(config_json, "_sync_directory", fail_sync)
        with pytest.raises(config_json.ConfigDurabilityError) as error:
            directory.create_group("new")
    assert error.value.committed is True
    assert len(publications) == 1
    saved = config_json.read_config_json(path)
    assert "new" in saved["groups"]
    assert directory.list_groups() == saved["groups"]
    assert not list(path.parent.glob(".agents.json.*.tmp"))
    # A later, deliberate operation reads the committed state, not a rollback.
    assert directory.group_add_member("new", "alice")[0]
    assert config_json.read_config_json(path)["groups"]["new"]["members"] == ["alice"]


def test_group_views_are_detached_from_the_persistence_source(configured):
    path, directory, _ = configured
    before = path.read_bytes()
    view = directory.list_groups()
    view["team"]["members"].append("injected")
    view["team"]["extension"]["v"] = 999
    assert directory.resolve_group("team") == ["alice"]
    assert directory.list_groups()["team"]["extension"] == {"v": 2}
    assert path.read_bytes() == before
    assert directory.create_group("new")[0]
    assert "injected" not in config_json.read_config_json(path)["groups"]["team"]["members"]


@pytest.mark.parametrize("bad", [b'{"groups":', b'[]', b'{"groups":null}', b'{"groups":[]}'])
def test_unreadable_or_non_object_configuration_cannot_be_overwritten(configured, bad):
    path, directory, _ = configured
    visible = directory.list_groups()
    path.write_bytes(bad)
    with pytest.raises(ValueError):
        directory.create_group("new")
    assert path.read_bytes() == bad
    assert directory.list_groups() == visible


def test_removed_configuration_is_not_recreated_from_cached_groups(configured):
    path, directory, _ = configured
    visible = directory.list_groups()
    path.unlink()
    with pytest.raises(FileNotFoundError):
        directory.create_group("new")
    assert not path.exists()
    assert directory.list_groups() == visible


def test_group_edit_does_not_resurrect_move_cleanup_or_modify_remote_addresses(configured):
    path, directory, _ = configured
    latest = config_json.read_config_json(path)
    latest["groups"]["dynamic"] = {
        "members": "@active", "exclude_from_broadcast": ["alice", "peer@OTHER"],
    }
    AgentDirectory.remove_local_memberships(latest, "alice")
    config_json.write_config_json(path, latest)
    assert directory.create_group("new")[0]
    saved = config_json.read_config_json(path)
    assert saved["groups"]["team"]["members"] == ["peer@OTHER"]
    assert saved["groups"]["dynamic"] == {
        "members": "@active", "exclude_from_broadcast": ["peer@OTHER"],
    }
    before = path.read_bytes()
    assert directory.group_add_member("dynamic", "bob")[0] is False
    assert directory.group_remove_member("dynamic", "bob")[0] is False
    assert path.read_bytes() == before


def _after_real_read(monkeypatch, path, callback):
    """Interleave after the first actual config read, for both old/new readers."""
    original_bytes, original_text = Path.read_bytes, Path.read_text
    pending = True

    def observed(value, candidate):
        nonlocal pending
        if pending and candidate.resolve() == path.resolve():
            pending = False
            callback()
        return value

    monkeypatch.setattr(Path, "read_bytes", lambda p: observed(original_bytes(p), p))
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **kw: observed(original_text(p, *a, **kw), p))


def test_non_group_writer_conflict_preserves_winner_and_does_not_retry(configured, monkeypatch):
    path, directory, _ = configured
    visible = directory.list_groups()
    updates = []

    def another_writer():
        doc = config_json.read_config_json(path)
        doc["global"]["new_policy"] = "keep"
        config_json.write_config_json(path, doc)
        updates.append(path.read_bytes())

    with monkeypatch.context() as patch:
        _after_real_read(patch, path, another_writer)
        with pytest.raises(config_json.ConfigConflictError):
            directory.create_group("new")
    assert len(updates) == 1 and path.read_bytes() == updates[0]
    assert directory.list_groups() == visible
    assert directory.create_group("new")[0]
    assert config_json.read_config_json(path)["global"]["new_policy"] == "keep"


def _competing_writer(path_string, member, barrier, results):
    path = Path(path_string)
    directory = _directory(path)
    with pytest.MonkeyPatch.context() as patch:
        _after_real_read(patch, path, lambda: barrier.wait(timeout=15))
        try:
            ok, _ = directory.group_add_member("team", member)
        except config_json.ConfigConflictError:
            results.put((member, "conflict"))
        except Exception as exc:
            results.put((member, type(exc).__name__))
        else:
            results.put((member, "saved" if ok else "noop"))


def test_two_spawned_writers_cannot_both_commit_the_same_revision(configured):
    path, _, _ = configured
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [ctx.Process(target=_competing_writer, args=(str(path), member, barrier, results))
                 for member in ("bob", "carol")]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=25) for _ in processes]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sorted(status for _, status in outcomes) == ["conflict", "saved"]
        winner = next(member for member, status in outcomes if status == "saved")
        loser = next(member for member, status in outcomes if status == "conflict")
        assert config_json.read_config_json(path)["groups"]["team"]["members"] == ["alice", "peer@OTHER", winner]
        assert _directory(path).group_add_member("team", loser)[0]
        assert config_json.read_config_json(path)["groups"]["team"]["members"] == ["alice", "peer@OTHER", winner, loser]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
        results.close()
        results.join_thread()


@pytest.mark.asyncio
async def test_group_command_does_not_report_success_when_publication_fails(configured, monkeypatch):
    path, directory, _ = configured
    before, visible = path.read_bytes(), directory.list_groups()
    replies = []

    async def reply(_update, text, **kwargs):
        replies.append(text)

    def fail_sync(_descriptor):
        raise OSError("file sync failed")

    runtime = SimpleNamespace(
        agent_directory=directory, _is_authorized_user=lambda user_id: user_id == 1,
        _reply_text=reply,
    )
    monkeypatch.setattr(config_json.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="file sync failed"):
        await runtime_groups.cmd_group(
            runtime, SimpleNamespace(effective_user=SimpleNamespace(id=1)),
            SimpleNamespace(args=["new", "reviewers"]),
        )
    assert not replies
    assert path.read_bytes() == before
    assert directory.list_groups() == visible


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_invalid_target_members_are_rejected_without_changing_configuration(configured, operation):
    path, directory, _ = configured
    visible = directory.list_groups()
    bad = config_json.read_config_json(path)
    bad["groups"]["team"]["members"] = {"alice": True}
    config_json.write_config_json(path, bad)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="group members"):
        _mutate(directory, operation)
    assert path.read_bytes() == before
    assert directory.list_groups() == visible


def test_missing_groups_field_is_created_only_by_an_effective_edit(configured):
    path, directory, _ = configured
    latest = config_json.read_config_json(path)
    del latest["groups"]
    config_json.write_config_json(path, latest)
    before = path.read_bytes()
    assert directory.delete_group("team")[0] is False
    assert path.read_bytes() == before
    assert directory.list_groups() == {}
    assert directory.create_group("new")[0] is True
    expected = dict(latest)
    expected["groups"] = {"new": {"description": "", "members": [], "exclude_from_broadcast": []}}
    assert dict(config_json.read_config_json(path)) == expected
