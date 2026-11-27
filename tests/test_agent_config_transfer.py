from types import SimpleNamespace

import pytest

from orchestrator import runtime_workzone as workzone_runtime
from orchestrator.agent_slot_transfer import (
    SourceSlot,
    plan_slot_transfer,
    source_slots_from_records,
)
from orchestrator.bridge_memory import SysPromptManager
from orchestrator.runtime_sys_prompts import cmd_sys

WORKZONE_ORDER = ("main", "1", "2", "3", "4", "5", "6", "7", "8", "9")


# ── pure placement rules ──────────────────────────────────────────────────────


def test_transfer_prefers_the_source_slot_when_free():
    plan = plan_slot_transfer(
        source_slots=[SourceSlot("main", "/work/a"), SourceSlot("2", "/work/b")],
        target_slot_ids=[],
        target_values=[],
        order=WORKZONE_ORDER,
        as_path=True,
    )

    assert [(item.source_slot, item.target_slot) for item in plan] == [
        ("main", "main"),
        ("2", "2"),
    ]
    assert all(item.outcome == "copied" for item in plan)


def test_transfer_shifts_to_the_next_free_slot_without_overwriting():
    plan = plan_slot_transfer(
        source_slots=[SourceSlot("main", "/work/a"), SourceSlot("1", "/work/b")],
        target_slot_ids=["main"],
        target_values=["/other"],
        order=WORKZONE_ORDER,
        as_path=True,
    )

    assert [(item.source_slot, item.target_slot) for item in plan] == [
        ("main", "1"),
        ("1", "2"),
    ]


def test_transfer_skips_values_already_present_on_the_target():
    plan = plan_slot_transfer(
        source_slots=[SourceSlot("main", "/work/a")],
        target_slot_ids=["3"],
        target_values=["/WORK/A/"],
        order=WORKZONE_ORDER,
        as_path=True,
    )

    assert plan[0].outcome == "duplicate"
    assert plan[0].target_slot is None


def test_transfer_reports_full_targets_instead_of_overwriting():
    plan = plan_slot_transfer(
        source_slots=[SourceSlot("main", "/work/a"), SourceSlot("1", "/work/b")],
        target_slot_ids=list(WORKZONE_ORDER[:-1]),
        target_values=["/other"],
        order=WORKZONE_ORDER,
        as_path=True,
    )

    assert plan[0].outcome == "copied"
    assert plan[0].target_slot == "9"
    assert plan[1].outcome == "no_free_slot"


def test_transfer_can_select_one_slot_only():
    plan = plan_slot_transfer(
        source_slots=[SourceSlot("main", "/work/a"), SourceSlot("1", "/work/b")],
        target_slot_ids=[],
        target_values=[],
        order=WORKZONE_ORDER,
        slot="1",
        as_path=True,
    )

    assert [item.source_slot for item in plan] == ["1"]


def test_source_records_drop_empty_slots_and_keep_state():
    entries = source_slots_from_records(
        [
            {"slot": "1", "text": "hello", "active": True},
            {"slot": "2", "text": "   ", "active": True},
            {"slot": "3", "text": "bye", "active": False},
        ],
        slot_key="slot",
        value_key="text",
        enabled_key="active",
    )

    assert [(entry.slot, entry.enabled) for entry in entries] == [
        ("1", True),
        ("3", False),
    ]


# ── runtime fixtures ─────────────────────────────────────────────────────────


def _runtime(tmp_path):
    replies = []
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = project / "repo"
    workspace.mkdir(parents=True)
    zone.mkdir()
    (project / "workspaces" / "helper").mkdir(parents=True)
    backend = SimpleNamespace(
        config=SimpleNamespace(extra={}, resolve_access_root=lambda: project),
        capabilities=SimpleNamespace(supports_files=True, supports_sessions=False),
        tool_registry=SimpleNamespace(workspace_dir=workspace, access_root=project),
    )
    runtime = SimpleNamespace(
        name="agent",
        config=SimpleNamespace(extra={}),
        global_config=SimpleNamespace(
            project_root=project,
            bridge_home=project,
            instance_id="HASHI1",
            authorized_id=1,
        ),
        workspace_dir=workspace,
        _workzone_dir=None,
        backend_manager=SimpleNamespace(current_backend=backend),
        _is_authorized_user=lambda user_id: user_id == 1,
        _backend_busy=lambda: False,
        _sync_workzone_to_backend_config=lambda: None,
        _reply_text=lambda update, text, **kwargs: _reply(replies, text, kwargs),
        replies=replies,
        zone=zone,
        backend=backend,
    )
    runtime.orchestrator = SimpleNamespace(
        configured_agent_names=lambda: ["agent", "helper"],
        _runtime_map=lambda: {"agent": runtime},
    )
    runtime.sys_prompt_manager = SysPromptManager(runtime.workspace_dir)
    return runtime


async def _reply(replies, text, kwargs):
    replies.append({"text": text, **kwargs})
    return SimpleNamespace(message_id=700)


def _update(text="", chat_id=123):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=SimpleNamespace(text=text),
    )


def _context(*args):
    return SimpleNamespace(args=list(args))


def _store(runtime):
    from orchestrator import runtime_session

    return runtime_session.ensure_store(runtime)


def _workzone_state(runtime, agent="agent"):
    return _store(runtime).get_agent_workzone_set(owner_id="user:1", agent_id=agent)


def _set_slot(runtime, slot, path, *, agent="agent", enabled=True):
    _store(runtime).set_agent_workzone_slot(
        owner_id="user:1", agent_id=agent, slot_id=slot, path=str(path), enabled=enabled
    )


def _paths(state):
    return {item["slot_id"]: item["path"] for item in state["slots"]}


# ── /workzone copy ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workzone_copy_places_slots_on_an_empty_target(tmp_path):
    runtime = _runtime(tmp_path)
    _set_slot(runtime, "main", runtime.zone)

    await workzone_runtime.cmd_workzone(
        runtime, _update(), _context("copy", "helper")
    )

    assert _paths(_workzone_state(runtime, "helper"))["main"] == str(runtime.zone.resolve())
    reply = runtime.replies[-1]["text"]
    assert "main" in reply and "helper" in reply


@pytest.mark.asyncio
async def test_workzone_copy_shifts_when_the_target_slot_is_taken(tmp_path):
    runtime = _runtime(tmp_path)
    other = runtime.global_config.project_root / "other"
    other.mkdir()
    _set_slot(runtime, "main", runtime.zone)
    _set_slot(runtime, "main", other, agent="helper")

    await workzone_runtime.cmd_workzone(
        runtime, _update(), _context("copy", "helper")
    )

    target = _paths(_workzone_state(runtime, "helper"))
    assert target["main"] == str(other.resolve())
    assert target["1"] == str(runtime.zone.resolve())


@pytest.mark.asyncio
async def test_workzone_copy_skips_a_directory_the_target_already_has(tmp_path):
    runtime = _runtime(tmp_path)
    _set_slot(runtime, "main", runtime.zone)
    _set_slot(runtime, "4", runtime.zone, agent="helper")

    await workzone_runtime.cmd_workzone(
        runtime, _update(), _context("copy", "helper")
    )

    assert list(_paths(_workzone_state(runtime, "helper"))) == ["4"]
    assert "Skipped" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_workzone_copy_reports_a_full_target_honestly(tmp_path):
    runtime = _runtime(tmp_path)
    first = runtime.global_config.project_root / "first"
    second = runtime.global_config.project_root / "second"
    first.mkdir()
    second.mkdir()
    _set_slot(runtime, "main", runtime.zone)
    _set_slot(runtime, "1", first)
    for slot in list(WORKZONE_ORDER)[:9]:
        _set_slot(runtime, slot, runtime.global_config.project_root / f"h{slot}", agent="helper")

    await workzone_runtime.cmd_workzone(
        runtime, _update(), _context("copy", "helper")
    )

    target = _paths(_workzone_state(runtime, "helper"))
    assert target["9"] == str(runtime.zone.resolve())
    assert str(first.resolve()) not in target.values()
    assert "no free Workzone slot" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_workzone_copy_can_transfer_a_single_slot(tmp_path):
    runtime = _runtime(tmp_path)
    other = runtime.global_config.project_root / "other"
    other.mkdir()
    _set_slot(runtime, "main", runtime.zone)
    _set_slot(runtime, "2", other)

    await workzone_runtime.cmd_workzone(
        runtime, _update(), _context("copy", "helper", "2")
    )

    assert list(_paths(_workzone_state(runtime, "helper"))) == ["2"]


@pytest.mark.asyncio
async def test_workzone_import_pulls_from_a_sibling(tmp_path):
    runtime = _runtime(tmp_path)
    other = runtime.global_config.project_root / "other"
    other.mkdir()
    _set_slot(runtime, "1", other, agent="helper")

    await workzone_runtime.cmd_workzone(
        runtime, _update(), _context("import", "helper")
    )

    assert _paths(_workzone_state(runtime))["1"] == str(other.resolve())


@pytest.mark.asyncio
async def test_workzone_transfer_rejects_unknown_and_self_targets(tmp_path):
    runtime = _runtime(tmp_path)

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("copy", "ghost"))
    assert "ghost" in runtime.replies[-1]["text"]

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("copy", "agent"))
    assert "nothing to copy" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_workzone_transfer_rejects_an_empty_or_invalid_slot(tmp_path):
    runtime = _runtime(tmp_path)
    _set_slot(runtime, "main", runtime.zone)

    await workzone_runtime.cmd_workzone(
        runtime, _update(), _context("copy", "helper", "7")
    )
    assert "7" in runtime.replies[-1]["text"]
    assert _workzone_state(runtime, "helper")["slots"] == []

    await workzone_runtime.cmd_workzone(
        runtime, _update(), _context("copy", "helper", "12")
    )
    assert "main or 1-9" in runtime.replies[-1]["text"]


# ── /workzone clear ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workzone_clear_removes_configuration_only(tmp_path):
    runtime = _runtime(tmp_path)
    _set_slot(runtime, "main", runtime.zone)
    _set_slot(runtime, "3", runtime.zone)

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("clear"))

    assert _workzone_state(runtime)["slots"] == []
    assert runtime.zone.is_dir()
    assert "No directory or file on disk was deleted" in runtime.replies[-1]["text"]
    assert "Cleared 2" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_workzone_clear_on_an_empty_profile_is_idempotent(tmp_path):
    runtime = _runtime(tmp_path)

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("clear"))

    assert _workzone_state(runtime)["slots"] == []
    assert "no configured Workzone slots" in runtime.replies[-1]["text"]


# ── /sys transfer ────────────────────────────────────────────────────────────


def _sys_update(text):
    return _update(text)


def _helper_manager(runtime):
    return SysPromptManager(
        state_path=runtime.global_config.bridge_home / "workspaces" / "helper" / "sys_prompts.json"
    )


def _texts(manager):
    return {item["slot"]: item["text"] for item in manager.list_slots() if item["text"]}


@pytest.mark.asyncio
async def test_sys_copy_moves_local_slots_and_keeps_the_enabled_state(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.sys_prompt_manager.save("2", "second")
    runtime.sys_prompt_manager.activate("2")
    runtime.sys_prompt_manager.save("3", "third")

    await cmd_sys(runtime, _sys_update("/sys copy helper"), _context())

    helper = _helper_manager(runtime)
    assert _texts(helper) == {"2": "second", "3": "third"}
    assert helper.get_slot("2")["active"] is True
    assert helper.get_slot("3")["active"] is False


@pytest.mark.asyncio
async def test_sys_copy_shifts_and_skips_duplicates(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.sys_prompt_manager.save("1", "one")
    runtime.sys_prompt_manager.save("2", "two")
    helper = _helper_manager(runtime)
    helper.save("1", "occupied")
    helper.save("5", "two")

    await cmd_sys(runtime, _sys_update("/sys copy helper"), _context())

    texts = _texts(_helper_manager(runtime))
    assert texts["1"] == "occupied"
    assert texts["2"] == "one"
    assert texts["5"] == "two"
    assert sorted(texts) == ["1", "2", "5"]


@pytest.mark.asyncio
async def test_sys_import_pulls_local_slots_and_never_touches_global(tmp_path):
    runtime = _runtime(tmp_path)
    helper = _helper_manager(runtime)
    helper.save("4", "from helper")
    helper.activate("4")
    runtime.global_sys_prompt_manager = SysPromptManager.for_instance(
        runtime.global_config
    )
    runtime.global_sys_prompt_manager.save("4", "global text")

    await cmd_sys(runtime, _sys_update("/sys import helper"), _context())

    assert _texts(runtime.sys_prompt_manager)["4"] == "from helper"
    assert runtime.sys_prompt_manager.get_slot("4")["active"] is True
    assert runtime.global_sys_prompt_manager.get_slot("4")["text"] == "global text"
    assert runtime.global_sys_prompt_manager.get_slot("4")["active"] is False


@pytest.mark.asyncio
async def test_sys_clear_only_clears_local_slots(tmp_path):
    runtime = _runtime(tmp_path)
    runtime.sys_prompt_manager.save("1", "local one")
    runtime.sys_prompt_manager.save("9", "local nine")
    runtime.global_sys_prompt_manager = SysPromptManager.for_instance(
        runtime.global_config
    )
    runtime.global_sys_prompt_manager.save("1", "global one")

    await cmd_sys(runtime, _sys_update("/sys clear"), _context())

    assert _texts(runtime.sys_prompt_manager) == {}
    assert runtime.global_sys_prompt_manager.get_slot("1")["text"] == "global one"
    assert "Cleared 2" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_sys_clear_on_empty_state_says_so(tmp_path):
    runtime = _runtime(tmp_path)

    await cmd_sys(runtime, _sys_update("/sys clear"), _context())

    assert "no configured local" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_sys_transfer_refuses_the_global_scope_and_unknown_agents(tmp_path):
    runtime = _runtime(tmp_path)

    await cmd_sys(runtime, _sys_update("/sys global clear"), _context())
    assert "local /sys slots only" in runtime.replies[-1]["text"]

    await cmd_sys(runtime, _sys_update("/sys copy ghost"), _context())
    assert "ghost" in runtime.replies[-1]["text"]

    await cmd_sys(runtime, _sys_update("/sys copy agent"), _context())
    assert "nothing to copy" in runtime.replies[-1]["text"]
# HASHI-Fingerprint: hfp_0828c557fceb4507977fdbcd97891213
