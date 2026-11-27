import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from orchestrator import runtime_workzone as workzone_runtime
from orchestrator.agent_slot_transfer import (
    SourceSlot,
    plan_slot_transfer,
    source_slots_from_records,
)
from orchestrator.bridge_memory import SysPromptManager
from orchestrator.function_worker_host import RemoteRuntimeReference
from orchestrator.runtime_sys_prompts import cmd_sys
from orchestrator.workbench_api import WorkbenchApiServer

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
    # A real instance always has the Agent configuration on disk; a stopped
    # Agent is resolved from it instead of from a naming convention.
    (project / "agents.json").write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "agent", "workspace_dir": "workspaces/agent"},
                    {"name": "helper", "workspace_dir": "workspaces/helper"},
                ]
            }
        ),
        encoding="utf-8",
    )
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


# ── review regressions: real receiving topology, concurrency, directories ────


@pytest.mark.asyncio
async def test_workzone_copy_defers_for_a_running_worker_view(tmp_path):
    """A Worker-side view of a running Agent has no local adoption hooks."""

    runtime = _runtime(tmp_path)
    _set_slot(runtime, "main", runtime.zone)
    remote = RemoteRuntimeReference(
        SimpleNamespace(),
        {
            "name": "helper",
            "workspace_dir": str(
                runtime.global_config.project_root / "workspaces" / "helper"
            ),
            "online": True,
        },
    )
    runtime.orchestrator._runtime_map = lambda: {"agent": runtime, "helper": remote}

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("copy", "helper"))

    assert _paths(_workzone_state(runtime, "helper"))["main"] == str(runtime.zone.resolve())
    assert "next request" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_workzone_copy_rejects_a_source_directory_that_vanished(tmp_path):
    runtime = _runtime(tmp_path)
    gone = tmp_path / "gone"
    gone.mkdir()
    _set_slot(runtime, "main", gone)
    gone.rmdir()

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("copy", "helper"))

    assert _workzone_state(runtime, "helper")["slots"] == []
    assert "Not copied" in runtime.replies[-1]["text"]
    assert "does not exist" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_workzone_copy_rejects_the_receiving_agent_home(tmp_path):
    runtime = _runtime(tmp_path)
    helper_home = runtime.global_config.project_root / "workspaces" / "helper"
    _set_slot(runtime, "main", helper_home)

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("copy", "helper"))

    assert _workzone_state(runtime, "helper")["slots"] == []
    assert "Agent home workspace" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_workzone_copy_reports_a_concurrent_writer_without_overwriting(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    other = runtime.global_config.project_root / "other"
    other.mkdir()
    _set_slot(runtime, "main", runtime.zone)
    store = _store(runtime)
    original = store.set_agent_workzone_slot

    def concurrent(
        *, owner_id, agent_id, slot_id, path=None, enabled=None, label=None,
        expected_revision=None, source="telegram",
    ):
        if slot_id == "main":
            # Another writer lands a slot after the target profile was observed.
            original(
                owner_id=owner_id, agent_id=agent_id, slot_id="5",
                path=str(other), enabled=True,
            )
        return original(
            owner_id=owner_id, agent_id=agent_id, slot_id=slot_id, path=path,
            enabled=enabled, label=label, expected_revision=expected_revision,
            source=source,
        )

    monkeypatch.setattr(store, "set_agent_workzone_slot", concurrent)

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("copy", "helper"))

    assert _paths(_workzone_state(runtime, "helper")) == {"5": str(other.resolve())}
    assert "changed while this transfer was running" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_sys_copy_uses_a_stopped_agents_configured_workspace(tmp_path):
    runtime = _runtime(tmp_path)
    project = runtime.global_config.project_root
    custom = project / "custom-helper"
    custom.mkdir()
    (project / "agents.json").write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "agent", "workspace_dir": "workspaces/agent"},
                    {"name": "helper", "workspace_dir": "custom-helper"},
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime.sys_prompt_manager.save("2", "second")

    await cmd_sys(runtime, _sys_update("/sys copy helper"), _context())

    configured = SysPromptManager(state_path=custom / "sys_prompts.json")
    assert _texts(configured) == {"2": "second"}
    assumed = SysPromptManager(
        state_path=project / "workspaces" / "helper" / "sys_prompts.json"
    )
    assert _texts(assumed) == {}


@pytest.mark.asyncio
async def test_sys_clear_keeps_a_slot_rewritten_after_it_was_listed(tmp_path):
    runtime = _runtime(tmp_path)
    manager = runtime.sys_prompt_manager
    manager.save("1", "first")
    manager.save("2", "second")
    original_delete = manager.delete

    def concurrent_delete(slot, **kwargs):
        if slot == "1":
            SysPromptManager(state_path=manager.state_path).replace("1", "rewritten")
        return original_delete(slot, **kwargs)

    manager.delete = concurrent_delete

    await cmd_sys(runtime, _sys_update("/sys clear"), _context())

    texts = _texts(SysPromptManager(state_path=manager.state_path))
    assert texts == {"1": "rewritten"}
    assert "Cleared 1 local /sys slot(s)" in runtime.replies[-1]["text"]
    assert "left untouched" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_sys_copy_reports_a_target_slot_filled_after_planning(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.sys_prompt_manager.save("1", "one")
    original_save = SysPromptManager.save

    def concurrent_save(self, slot, text, *, expected_slot=None):
        if slot == "1" and expected_slot is not None and not expected_slot.get("text"):
            SysPromptManager(state_path=self.state_path).save("1", "someone else")
        return original_save(self, slot, text, expected_slot=expected_slot)

    monkeypatch.setattr(SysPromptManager, "save", concurrent_save)

    await cmd_sys(runtime, _sys_update("/sys copy helper"), _context())

    helper = _helper_manager(runtime)
    assert _texts(helper) == {"1": "someone else"}
    assert "changed after the slots were listed" in runtime.replies[-1]["text"]


# ── governed Agent-management gate for cross-Agent transfers ─────────────────


class _GovernedIdentity:
    def get_session_user(self, token):
        return SimpleNamespace(id=token) if token in {"admin", "member", "outside"} else None

    def list_project_memberships(self, *, user_id):
        if user_id == "admin":
            return [{"role": "org_admin", "project_id": "p1"}]
        if user_id == "member":
            return [{"role": "member", "project_id": "p1"}]
        return [{"role": "member", "project_id": "p2"}]


@pytest.fixture
def governed_api():
    # Guard-only server: the full bootstrap imports the governed approval stack,
    # which is not part of this behaviour and needs an extra dependency.
    server = WorkbenchApiServer.__new__(WorkbenchApiServer)
    server.global_config = SimpleNamespace(deployment_profile="governed")
    server.identity_service = _GovernedIdentity()
    server._load_agent_rows = lambda **_: [
        {"name": "agent", "project_id": "p1"},
        {"name": "helper", "project_id": "p1"},
    ]
    return server


def test_transfer_command_names_cover_all_six_verbs():
    names = WorkbenchApiServer._config_transfer_agent_names
    assert names("workzone", ["copy", "helper"]) == ("helper",)
    assert names("workzone", ["import", "helper", "3"]) == ("helper",)
    assert names("workzone", ["clear"]) == ()
    assert names("sys", ["copy", "helper"]) == ("helper",)
    assert names("sys", ["local", "import", "helper"]) == ("helper",)
    assert names("sys", ["global", "copy", "helper"]) == ()
    assert names("sys", ["clear"]) == ()
    assert names("sys", ["3", "on"]) == ()
    assert names("model", ["copy", "helper"]) == ()
    assert names("sys", ["copy"]) == ()


@pytest.mark.asyncio
async def test_governed_command_invocation_gates_cross_agent_transfers(
    tmp_path, monkeypatch
):
    """A member's session command cannot reach /sys copy on another Agent."""

    import sys

    from orchestrator import admin_local_testing, command_interaction_bridge

    monkeypatch.setitem(
        sys.modules,
        "orchestrator.permissions.human_approval",
        SimpleNamespace(register_human_approval=lambda *a, **k: None),
    )
    workspace = tmp_path / "workspaces" / "agent"
    workspace.mkdir(parents=True)
    config = tmp_path / "agents.json"
    config.write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "agent", "project_id": "p1"},
                    {"name": "helper", "project_id": "p1"},
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(name="agent", workspace_dir=workspace)
    settings = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        deployment_profile="governed",
        authorized_id=1,
        instance_id="HASHI1",
        workbench_port=18800,
        persistent_session_v1=True,
    )
    server = WorkbenchApiServer(
        config_path=config,
        global_config=settings,
        orchestrator=SimpleNamespace(
            runtimes=[runtime], scheduler=SimpleNamespace(state={})
        ),
        secrets={"workbench_admin_token": "test-owner-token"},
        reconcile_session_runs=False,
    )
    server.identity_service = _GovernedIdentity()
    monkeypatch.setattr(
        admin_local_testing, "supported_commands", lambda _runtime: {"sys"}
    )

    dispatched: list[str] = []

    async def fake_dispatch(_runtime, _operation, metadata, **_kwargs):
        dispatched.append(str(metadata.get("owner_id")))
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(
        command_interaction_bridge, "dispatch_command_interaction", fake_dispatch
    )
    async with TestClient(TestServer(server.app)) as client:
        for token in ("member", "admin"):
            session = server.session_store.create_session(
                owner_id=f"enterprise:{token}", agent_id="agent"
            )
            body = {
                "command": "sys",
                "arguments": ["copy", "helper"],
                "client_id": f"client-{token}-0001",
                "request_id": f"request-{token}-0001",
                "ui_locale": "en",
                "context_generation": int(session["context_generation"] or 1),
            }
            response = await client.post(
                f"/api/v1/sessions/{session['session_id']}/commands",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
            if token == "member":
                assert response.status == 403
            else:
                assert response.status in {200, 202}

    assert dispatched == ["enterprise:admin"]


class _ScopedTransferIdentity:
    """A team admin of p1 who can see p2 but cannot manage it."""

    def get_session_user(self, token):
        return SimpleNamespace(id=token) if token in {"teamlead", "orgadmin"} else None

    def list_project_memberships(self, *, user_id):
        if user_id == "orgadmin":
            return [{"role": "org_admin", "project_id": "p1"}]
        return [
            {"role": "team_admin", "project_id": "p1"},
            {"role": "member", "project_id": "p2"},
        ]


@pytest.mark.asyncio
async def test_structured_entry_gates_the_command_line_it_executes(
    tmp_path, monkeypatch
):
    """Whitespace must not hide the sibling Agent from the authorization gate.

    The command endpoint re-serializes its arguments into one slash command line
    that the executor parses again.  Authorizing the raw argument list let
    ``["copy", "   helper"]`` read as an unknown Agent while the handler resolved
    ``helper`` and transferred its configuration.
    """

    import sys

    from orchestrator import admin_local_testing, command_interaction_bridge

    monkeypatch.setitem(
        sys.modules,
        "orchestrator.permissions.human_approval",
        SimpleNamespace(register_human_approval=lambda *a, **k: None),
    )
    workspace = tmp_path / "workspaces" / "agent"
    workspace.mkdir(parents=True)
    config = tmp_path / "agents.json"
    config.write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "agent", "project_id": "p1"},
                    {"name": "helper", "project_id": "p2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(name="agent", workspace_dir=workspace)
    settings = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        deployment_profile="governed",
        authorized_id=1,
        instance_id="HASHI1",
        workbench_port=18800,
        persistent_session_v1=True,
    )
    server = WorkbenchApiServer(
        config_path=config,
        global_config=settings,
        orchestrator=SimpleNamespace(
            runtimes=[runtime], scheduler=SimpleNamespace(state={})
        ),
        secrets={"workbench_admin_token": "test-owner-token"},
        reconcile_session_runs=False,
    )
    server.identity_service = _ScopedTransferIdentity()
    monkeypatch.setattr(
        admin_local_testing, "supported_commands", lambda _runtime: {"sys"}
    )

    dispatched: list[str] = []

    async def fake_dispatch(_runtime, _operation, metadata, **_kwargs):
        dispatched.append(str(metadata.get("owner_id")))
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(
        command_interaction_bridge, "dispatch_command_interaction", fake_dispatch
    )

    # The same transfer written four ways must reach the same verdict: neither
    # the normal form nor any whitespace variant may slip past the gate.
    variants = (
        ["copy", "helper"],
        ["copy", "   helper"],
        ["local", "copy", " helper"],
    )
    async with TestClient(TestServer(server.app)) as client:
        for index, arguments in enumerate(variants):
            for token, allowed in (("teamlead", False), ("orgadmin", True)):
                session = server.session_store.create_session(
                    owner_id=f"enterprise:{token}", agent_id="agent"
                )
                body = {
                    "command": "sys",
                    "arguments": list(arguments),
                    "client_id": f"client-{token}-{index:04d}",
                    "request_id": f"request-{token}-{index:04d}",
                    "ui_locale": "en",
                    "context_generation": int(session["context_generation"] or 1),
                }
                response = await client.post(
                    f"/api/v1/sessions/{session['session_id']}/commands",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
                if allowed:
                    assert response.status in {200, 202}, (token, arguments)
                else:
                    assert response.status == 403, (token, arguments)

    assert dispatched == ["enterprise:orgadmin"] * len(variants)


@pytest.mark.asyncio
async def test_oversized_arguments_are_refused_before_any_formatting(
    tmp_path, monkeypatch
):
    """A refused payload must be rejected before it is serialized and re-parsed.

    The structured command entry point runs on the shared API event loop, so the
    existing argument-length bound has to stay ahead of the command-text
    formatting the cross-Agent authorization gate needs.
    """

    import sys

    from orchestrator import (
        admin_local_testing,
        command_interaction_bridge,
        workbench_api,
    )

    monkeypatch.setitem(
        sys.modules,
        "orchestrator.permissions.human_approval",
        SimpleNamespace(register_human_approval=lambda *a, **k: None),
    )
    workspace = tmp_path / "workspaces" / "agent"
    workspace.mkdir(parents=True)
    config = tmp_path / "agents.json"
    config.write_text(
        json.dumps(
            {"agents": [{"name": "agent", "project_id": "p1"}]},
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(name="agent", workspace_dir=workspace)
    settings = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        deployment_profile="governed",
        authorized_id=1,
        instance_id="HASHI1",
        workbench_port=18800,
        persistent_session_v1=True,
    )
    server = WorkbenchApiServer(
        config_path=config,
        global_config=settings,
        orchestrator=SimpleNamespace(
            runtimes=[runtime], scheduler=SimpleNamespace(state={})
        ),
        secrets={"workbench_admin_token": "test-owner-token"},
        reconcile_session_runs=False,
    )
    server.identity_service = _ScopedTransferIdentity()
    monkeypatch.setattr(
        admin_local_testing, "supported_commands", lambda _runtime: {"workzone"}
    )

    async def fake_dispatch(_runtime, _operation, metadata, **_kwargs):
        return {"ok": True, "accepted": True}

    monkeypatch.setattr(
        command_interaction_bridge, "dispatch_command_interaction", fake_dispatch
    )

    reached: list[str] = []

    def _record_format(*_args, **_kwargs):
        reached.append("format")
        raise AssertionError("oversized arguments reached the command formatter")

    def _record_parse(*_args, **_kwargs):
        reached.append("parse")
        raise AssertionError("oversized arguments reached the command parser")

    monkeypatch.setattr(
        admin_local_testing, "_format_slash_command_line", _record_format
    )
    monkeypatch.setattr(workbench_api, "parse_slash_command_text", _record_parse)

    session = server.session_store.create_session(
        owner_id="enterprise:orgadmin", agent_id="agent"
    )
    body = {
        "command": "workzone",
        "arguments": ["copy", "x" * 5000],
        "client_id": "client-oversized",
        "request_id": "request-oversized",
        "ui_locale": "en",
        "context_generation": int(session["context_generation"] or 1),
    }
    async with TestClient(TestServer(server.app)) as client:
        response = await client.post(
            f"/api/v1/sessions/{session['session_id']}/commands",
            headers={"Authorization": "Bearer orgadmin"},
            json=body,
        )

    # Refused by the argument bound itself, before any command text exists.
    assert response.status == 400, response.status
    assert reached == []


@pytest.mark.asyncio
async def test_governed_member_cannot_transfer_another_agents_config(governed_api):
    server = governed_api

    async def probe(request):
        denied = server._guard_config_transfer_access(request, "agent", ("helper",))
        if denied is not None:
            return denied
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/probe", probe)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/probe")).status == 401
        member = await client.get(
            "/probe", headers={"Authorization": "Bearer member"}
        )
        assert member.status == 403
        outside = await client.get(
            "/probe", headers={"Authorization": "Bearer outside"}
        )
        assert outside.status == 404
        admin = await client.get("/probe", headers={"Authorization": "Bearer admin"})
        assert admin.status == 200
        # An unknown Agent is reported by the command, not by the gate.
        assert server._guard_config_transfer_access(
            _Request("admin"), "agent", ("ghost",)
        ) is None


# ── review round 2: entry coverage, clear races, effective config, planning ──


@pytest.mark.asyncio
async def test_governed_chat_text_entry_gates_cross_agent_transfers(
    tmp_path, monkeypatch
):
    """A member typing the command into the chat box cannot reach another Agent."""

    import sys

    monkeypatch.setitem(
        sys.modules,
        "orchestrator.permissions.human_approval",
        SimpleNamespace(register_human_approval=lambda *a, **k: None),
    )
    workspace = tmp_path / "workspaces" / "agent"
    workspace.mkdir(parents=True)
    (tmp_path / "workspaces" / "helper").mkdir(parents=True)
    config = tmp_path / "agents.json"
    config.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "name": "agent",
                        "workspace_dir": "workspaces/agent",
                        "project_id": "p1",
                    },
                    {
                        "name": "helper",
                        "workspace_dir": "workspaces/helper",
                        "project_id": "p1",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime = SimpleNamespace(name="agent", workspace_dir=workspace)
    settings = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        deployment_profile="governed",
        authorized_id=1,
        instance_id="HASHI1",
        workbench_port=18800,
        persistent_session_v1=True,
    )
    server = WorkbenchApiServer(
        config_path=config,
        global_config=settings,
        orchestrator=SimpleNamespace(
            runtimes=[runtime], scheduler=SimpleNamespace(state={})
        ),
        secrets={"workbench_admin_token": "test-owner-token"},
        reconcile_session_runs=False,
    )
    server.identity_service = _GovernedIdentity()

    async with TestClient(TestServer(server.app)) as client:
        for text in ("/sys copy helper", "/workzone import helper"):
            member = await client.post(
                "/api/chat",
                headers={"Authorization": "Bearer member"},
                json={"agent": "agent", "text": text, "source": "api"},
            )
            assert member.status == 403
            body = await member.json()
            assert body["error"] == "management access required"
            # The administrator clears the gate; this harness has no live Agent
            # runtime, so admission fails afterwards with a non-authorization error.
            admin = await client.post(
                "/api/chat",
                headers={"Authorization": "Bearer admin"},
                json={"agent": "agent", "text": text, "source": "api"},
            )
            assert admin.status != 403


@pytest.mark.asyncio
async def test_workzone_clear_keeps_slots_rewritten_during_the_clear(
    tmp_path, monkeypatch
):
    """A slot written after the clear enumerated the profile must survive."""

    runtime = _runtime(tmp_path)
    _set_slot(runtime, "main", runtime.zone)
    _set_slot(runtime, "2", runtime.zone)
    other = runtime.global_config.project_root / "other"
    other.mkdir()
    store = _store(runtime)
    original = store.delete_agent_workzone_slot
    calls = {"n": 0}

    def concurrent(
        *, owner_id, agent_id, slot_id, expected_revision=None, source="telegram"
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            # A normal edit lands after the profile was enumerated: slot "2" is
            # now a different directory than the one this clear observed.
            store.set_agent_workzone_slot(
                owner_id=owner_id,
                agent_id=agent_id,
                slot_id="2",
                path=str(other),
                enabled=True,
            )
        return original(
            owner_id=owner_id,
            agent_id=agent_id,
            slot_id=slot_id,
            expected_revision=expected_revision,
            source=source,
        )

    monkeypatch.setattr(store, "delete_agent_workzone_slot", concurrent)

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("clear"))

    assert _paths(_workzone_state(runtime)) == {
        "main": str(runtime.zone.resolve()),
        "2": str(other.resolve()),
    }
    assert "Cleared 0" in runtime.replies[-1]["text"]
    assert "changed while this clear was running" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_sys_clear_deletes_only_the_text_it_enumerated(tmp_path):
    """Each slot is read once: a write landing between two reads must survive."""

    runtime = _runtime(tmp_path)
    manager = runtime.sys_prompt_manager
    manager.save("1", "first")
    manager.save("2", "second")
    original_get = manager.get_slot
    state = {"rewritten": False}

    def raced_get(slot):
        snapshot = original_get(slot)
        if slot == "1" and not state["rewritten"]:
            state["rewritten"] = True
            SysPromptManager(state_path=manager.state_path).replace("1", "rewritten")
        return snapshot

    manager.get_slot = raced_get

    await cmd_sys(runtime, _sys_update("/sys clear"), _context())

    texts = _texts(SysPromptManager(state_path=manager.state_path))
    assert texts == {"1": "rewritten"}
    assert "left untouched" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_sys_copy_reads_the_effective_config_when_it_lives_elsewhere(tmp_path):
    """The configured workspace wins over <bridge_home>/workspaces/<name>."""

    runtime = _runtime(tmp_path)
    config_dir = tmp_path / "instance-config"
    config_dir.mkdir()
    helper_home = config_dir / "helper-home"
    helper_home.mkdir()
    (config_dir / "agent-home").mkdir()
    config = config_dir / "agents.json"
    config.write_text(
        json.dumps(
            {
                "agents": [
                    {"name": "agent", "workspace_dir": "agent-home"},
                    {"name": "helper", "workspace_dir": "helper-home"},
                ]
            }
        ),
        encoding="utf-8",
    )
    runtime.global_config.config_path = config
    runtime.sys_prompt_manager.save("1", "one")

    await cmd_sys(runtime, _sys_update("/sys copy helper"), _context())

    assert _texts(SysPromptManager(state_path=helper_home / "sys_prompts.json")) == {
        "1": "one"
    }
    guessed = (
        runtime.global_config.bridge_home / "workspaces" / "helper" / "sys_prompts.json"
    )
    assert _texts(SysPromptManager(state_path=guessed)) == {}


@pytest.mark.asyncio
async def test_workzone_copy_validates_directories_before_choosing_slots(tmp_path):
    """An unusable source directory must not consume the last free slot."""

    runtime = _runtime(tmp_path)
    project = runtime.global_config.project_root
    gone = tmp_path / "gone"
    gone.mkdir()
    _set_slot(runtime, "main", gone)
    gone.rmdir()
    _set_slot(runtime, "1", runtime.zone)
    for slot in ("main", "1", "2", "3", "4", "5", "6", "7", "8"):
        target_dir = project / f"helper-{slot}"
        target_dir.mkdir()
        _set_slot(runtime, slot, target_dir, agent="helper")

    await workzone_runtime.cmd_workzone(runtime, _update(), _context("copy", "helper"))

    assert _paths(_workzone_state(runtime, "helper"))["9"] == str(
        runtime.zone.resolve()
    )
    assert "no free Workzone slot" not in runtime.replies[-1]["text"]
    assert "does not exist" in runtime.replies[-1]["text"]


def test_config_transfer_parser_matches_the_handler_grammar():
    from orchestrator.sys_command_contract import config_transfer_target_agent

    assert config_transfer_target_agent("workzone", ["copy", "helper"]) == "helper"
    assert config_transfer_target_agent("workzone", ["import", "helper", "3"]) == "helper"
    # The handler drops blank arguments before parsing its verb.
    assert (
        config_transfer_target_agent("workzone", ["copy", "", "helper"]) == "helper"
    )
    assert config_transfer_target_agent("sys", ["local", "copy", "helper"]) == "helper"
    assert config_transfer_target_agent("sys", ["L", "import", "helper"]) == "helper"
    assert config_transfer_target_agent("sys", ["global", "copy", "helper"]) is None
    assert config_transfer_target_agent("sys", ["clear"]) is None
    assert config_transfer_target_agent("workzone", ["clear"]) is None
    assert config_transfer_target_agent("model", ["copy", "helper"]) is None


class _Request:
    def __init__(self, token):
        self.headers = {"Authorization": f"Bearer {token}"}
# HASHI-Fingerprint: hfp_0828c557fceb4507977fdbcd97891213
