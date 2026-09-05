from types import SimpleNamespace

import pytest

from orchestrator import runtime_workzone


def _runtime(tmp_path):
    replies = []
    project = tmp_path / "project"
    workspace = project / "workspaces" / "agent"
    zone = project / "repo"
    workspace.mkdir(parents=True)
    zone.mkdir()
    backend = SimpleNamespace(
        config=SimpleNamespace(extra={}, resolve_access_root=lambda: project),
        capabilities=SimpleNamespace(supports_files=True, supports_sessions=False),
        tool_registry=SimpleNamespace(workspace_dir=workspace, access_root=project),
    )
    return SimpleNamespace(
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


async def _reply(replies, text, kwargs):
    replies.append({"text": text, **kwargs})


def _update(chat_id=123):
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=chat_id),
    )


class _CallbackMessage:
    def __init__(self, chat_id=123):
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id)
        self.prompt_message_id = 700
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append({"text": text, **kwargs})
        return SimpleNamespace(message_id=self.prompt_message_id)


class _CallbackQuery:
    def __init__(self, data, chat_id=123):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = _CallbackMessage(chat_id)
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append({"text": text, **kwargs})

    async def edit_message_text(self, text, **kwargs):
        self.edits.append({"text": text, **kwargs})


def _callback_update(query):
    return SimpleNamespace(
        effective_user=query.from_user,
        effective_chat=query.message.chat,
        callback_query=query,
    )


def test_sync_workzone_to_backend_config_updates_registry(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._workzone_dir = runtime.zone

    runtime_workzone.sync_workzone_to_backend_config(runtime)

    assert runtime.config.extra["workzone_dir"] == str(runtime.zone)
    assert runtime.backend.config.extra["workzone_dir"] == str(runtime.zone)
    assert runtime.config.extra["workzone_dirs"] == [str(runtime.zone)]
    assert runtime.backend.tool_registry.workspace_dir == runtime.zone
    assert runtime.backend.tool_registry.access_roots == (runtime.zone,)


@pytest.mark.asyncio
async def test_cmd_workzone_status_set_and_off(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)

    await runtime_workzone.cmd_workzone(runtime, _update(), SimpleNamespace(args=[]))
    assert "<b>WORKZONES</b>" in runtime.replies[-1]["text"]
    assert "<b>Current</b> · <code>0/10</code> active" in runtime.replies[-1]["text"]
    assert runtime.replies[-1]["parse_mode"] == "HTML"
    assert runtime.replies[-1]["reply_markup"] is not None

    await runtime_workzone.cmd_workzone(runtime, _update(), SimpleNamespace(args=["repo"]))
    assert "<b>Current</b> · <b>ON</b>" in runtime.replies[-1]["text"]
    assert runtime._workzone_dir == runtime.zone.resolve()

    await runtime_workzone.cmd_workzone(runtime, _update(), SimpleNamespace(args=["off"]))
    assert "<b>Current</b> · <b>OFF</b>" in runtime.replies[-1]["text"]
    assert runtime._workzone_dir is None


@pytest.mark.asyncio
async def test_numbered_workzones_are_attached_without_changing_main_cwd(tmp_path):
    runtime = _runtime(tmp_path)
    attached = runtime.global_config.project_root / "shared"
    attached.mkdir()
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)

    await runtime_workzone.cmd_workzone(
        runtime, _update(), SimpleNamespace(args=["repo"])
    )
    await runtime_workzone.cmd_workzone(
        runtime, _update(), SimpleNamespace(args=["1", "shared"])
    )

    assert runtime._workzone_dir == runtime.zone.resolve()
    assert runtime.config.extra["workzone_dirs"] == [
        str(runtime.zone.resolve()),
        str(attached.resolve()),
    ]
    assert runtime.backend.tool_registry.access_roots == (
        runtime.zone.resolve(),
        attached.resolve(),
    )

    await runtime_workzone.cmd_workzone(
        runtime, _update(), SimpleNamespace(args=["off"])
    )
    assert runtime._workzone_dir is None
    assert runtime.backend.tool_registry.workspace_dir == runtime.workspace_dir
    assert runtime.workspace_dir in runtime.backend.tool_registry.access_roots
    assert attached.resolve() in runtime.backend.tool_registry.access_roots


@pytest.mark.asyncio
async def test_callback_path_entry_is_bound_to_exact_force_reply(tmp_path):
    runtime = _runtime(tmp_path)
    attached = runtime.global_config.project_root / "shared"
    attached.mkdir()
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)
    query = _CallbackQuery("wz:p:0:1")

    await runtime_workzone.callback_workzone(
        runtime, _callback_update(query), SimpleNamespace()
    )
    unrelated = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=123),
        effective_message=SimpleNamespace(
            text="shared", reply_to_message=SimpleNamespace(message_id=999)
        ),
    )
    assert await runtime_workzone.handle_pending_path_reply(runtime, unrelated) is False

    reply_message = SimpleNamespace(
        text="shared",
        reply_to_message=SimpleNamespace(message_id=query.message.prompt_message_id),
    )
    reply = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        effective_chat=SimpleNamespace(id=123),
        effective_message=reply_message,
        message=reply_message,
    )
    assert await runtime_workzone.handle_pending_path_reply(runtime, reply) is True

    configured = runtime.session_store.get_workzone_set(
        runtime._workzone_state["session_id"]
    )
    assert [(slot["slot_id"], slot["path"]) for slot in configured["slots"]] == [
        ("1", str(attached.resolve()))
    ]
    assert runtime._pending_workzone_paths == {}


@pytest.mark.asyncio
async def test_callback_rejects_stale_menu_revision_and_refreshes(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)
    await runtime_workzone.cmd_workzone(
        runtime, _update(), SimpleNamespace(args=["repo"])
    )
    query = _CallbackQuery("wz:v:0:main")

    await runtime_workzone.callback_workzone(
        runtime, _callback_update(query), SimpleNamespace()
    )

    assert query.answers[-1]["show_alert"] is True
    assert query.edits
    assert "<code>1/10</code> active" in query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_reset_revalidates_and_restarts_without_clearing_slot(tmp_path):
    runtime = _runtime(tmp_path)
    restarts = []

    async def handle_new_session():
        restarts.append(True)
        return True

    runtime.backend.capabilities.supports_sessions = True
    runtime.backend.handle_new_session = handle_new_session
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)
    await runtime_workzone.cmd_workzone(
        runtime, _update(), SimpleNamespace(args=["repo"])
    )
    session_id = runtime._workzone_state["session_id"]
    before = runtime.session_store.get_workzone_set(session_id)
    restarts.clear()

    await runtime_workzone.cmd_workzone(
        runtime, _update(), SimpleNamespace(args=["reset"])
    )

    after = runtime.session_store.get_workzone_set(session_id)
    assert after == before
    assert restarts == [True]


@pytest.mark.asyncio
async def test_workzone_changes_do_not_cross_session_bindings(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)

    await runtime_workzone.cmd_workzone(
        runtime, _update(123), SimpleNamespace(args=["repo"])
    )
    other = runtime.session_store.create_session(
        owner_id="user:1", agent_id="agent", title="Other"
    )
    runtime.session_store.bind_channel(
        owner_id="user:1",
        agent_id="agent",
        surface="telegram",
        channel_key="456",
        session_id=other["session_id"],
    )
    await runtime_workzone.cmd_workzone(
        runtime, _update(456), SimpleNamespace(args=[])
    )

    assert "<b>Current</b> · <code>0/10</code> active" in runtime.replies[-1]["text"]


def test_workzone_prompt_section_uses_backend_capabilities(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)
    runtime._workzone_dir = runtime.zone
    from orchestrator.workzone import save_workzone

    save_workzone(runtime.workspace_dir, runtime.zone)

    sections = runtime_workzone.workzone_prompt_section(runtime)

    assert sections
    assert sections[0][0] == "WORKZONES"
    assert str(runtime.zone.resolve()) in sections[0][1]
    assert sections[0][2]["key"] == "working_environment.workzones"
    assert sections[0][2]["protected"] is True
    assert "State revision" not in sections[0][1]


def test_all_workzones_off_omits_pcm_and_restores_agent_home(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._workzone_state = {"revision": 3, "slots": []}
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)

    sections = runtime_workzone.workzone_prompt_section(runtime)

    assert sections == []
    assert runtime._workzone_dir is None
    assert runtime.backend.tool_registry.workspace_dir == runtime.workspace_dir
    assert "workzone_dir" not in runtime.config.extra
