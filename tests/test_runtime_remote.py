import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orchestrator import runtime_pending, runtime_remote
from orchestrator.agent_move.package import AgentMoveError


class _Query:
    def __init__(self, data: str = "move:cancel"):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append({"text": text, **kwargs})

    async def answer(self, text=None, **kwargs):
        self.answers.append({"text": text, **kwargs})


def _runtime(tmp_path):
    replies = []
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = SimpleNamespace(
        global_config=SimpleNamespace(project_root=tmp_path, instance_id="HASHI_TEST"),
        _is_authorized_user=lambda user_id: user_id == 1,
        _load_instances=lambda: {"hashi2": {"display_name": "HASHI2"}},
        _do_move=None,
        _reply_text=lambda update, text, **kwargs: _reply(replies, text, kwargs),
        _send_text=lambda chat_id, text, **kwargs: _reply(replies, text, kwargs),
        replies=replies,
        _fetch_remote_json=AsyncMock(return_value=({"ok": True, "peers": [{
            "instance_id": "HASHI2", "display_name": "HASHI2", "host": "127.0.0.1", "port": 8767,
            "capabilities": ["agent_move_receive_v1"],
            "properties": {"live_status": "online", "handshake_state": "handshake_accepted"},
        }]}, "http://127.0.0.1/peers")),
    )
    runtime._format_remote_age = lambda value: FlexibleAgentRuntime._format_remote_age(runtime, value)
    runtime._remote_peer_presence = lambda peer: FlexibleAgentRuntime._remote_peer_presence(runtime, peer)
    return runtime


async def _reply(replies, text, kwargs):
    replies.append({"text": text, **kwargs})


def test_load_instances_reads_first_available_file(tmp_path):
    missing = tmp_path / "missing.json"
    path = tmp_path / "instances.json"
    path.write_text(
        json.dumps({"instances": {"hashi2": {"display_name": "HASHI2"}}}),
        encoding="utf-8",
    )

    assert runtime_remote.load_instances([missing, path]) == {
        "hashi2": {"display_name": "HASHI2"}
    }


def test_transfer_instance_resolution_uses_real_ids_and_never_magic_local():
    instances = {
        "hashi2": {"instance_id": "HASHI2", "display_name": "Current"},
        "local": {"instance_id": "LOCAL", "display_name": "Lab"},
    }

    instance_id, _entry = runtime_remote.resolve_transfer_instance(
        instances,
        "local",
    )
    assert instance_id == "LOCAL"

    with pytest.raises(AgentMoveError, match="unknown target instance 'local'"):
        runtime_remote.resolve_transfer_instance(
            {"hashi2": instances["hashi2"]},
            "local",
        )


def test_transfer_instance_resolution_rejects_ambiguous_display_name():
    instances = {
        "one": {"instance_id": "HASHI1", "display_name": "Shared"},
        "two": {"instance_id": "HASHI2", "display_name": "shared"},
    }
    with pytest.raises(AgentMoveError, match="ambiguous target instance"):
        runtime_remote.resolve_transfer_instance(instances, "SHARED")


@pytest.mark.asyncio
async def test_local_clone_directory_survives_remote_sidecar_unavailability(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._fetch_remote_json = AsyncMock(return_value=(None, None))

    instances = await runtime_remote.load_clone_instances(runtime)

    assert list(instances) == ["hashi_test"]
    assert instances["hashi_test"]["instance_id"] == "HASHI_TEST"
    assert instances["hashi_test"]["local"] is True


@pytest.mark.asyncio
async def test_move_show_agent_picker_lists_agents(tmp_path):
    (tmp_path / "agents.json").write_text(
        json.dumps({"agents": [{"name": "zelda"}, {"name": "akane"}]}),
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path)

    await runtime_remote.move_show_agent_picker(runtime, SimpleNamespace(), {})

    assert "<b>MOVE AGENT</b>" in runtime.replies[-1]["text"]
    assert "HASHI_TEST" in runtime.replies[-1]["text"]
    assert "Select the exact agent" in runtime.replies[-1]["text"]
    buttons = [
        button
        for row in runtime.replies[-1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert [button.callback_data for button in buttons] == [
        "move:agent:zelda",
        "move:agent:akane",
    ]


@pytest.mark.asyncio
async def test_move_picker_uses_bounded_callback_reference_for_long_agent_id(tmp_path):
    long_name = "a" * 100
    (tmp_path / "agents.json").write_text(
        json.dumps({"agents": [{"name": long_name}]}),
        encoding="utf-8",
    )
    runtime = _runtime(tmp_path)

    await runtime_remote.move_show_agent_picker(runtime, SimpleNamespace(), {})

    button = runtime.replies[-1]["reply_markup"].inline_keyboard[0][0]
    assert len(button.callback_data.encode("utf-8")) <= 64
    assert button.callback_data.startswith("move:ref:")
    update = SimpleNamespace(callback_query=_Query(button.callback_data))
    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())
    target_button = update.callback_query.edits[-1]["reply_markup"].inline_keyboard[0][0]
    assert len(target_button.callback_data.encode("utf-8")) <= 64


@pytest.mark.asyncio
async def test_move_show_target_picker_lists_instances(tmp_path):
    runtime = _runtime(tmp_path)

    await runtime_remote.move_show_target_picker(
        runtime,
        SimpleNamespace(),
        "zelda",
        {"hashi2": {"display_name": "HASHI2"}},
    )

    assert "Select the target instance" in runtime.replies[-1]["text"]
    button = runtime.replies[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "move:target:zelda:hashi2"


@pytest.mark.asyncio
async def test_move_show_options_edits_callback_message(tmp_path):
    runtime = _runtime(tmp_path)
    update = SimpleNamespace(callback_query=_Query())

    await runtime_remote.move_show_options(runtime, update, "zelda", "hashi2")

    callbacks = [
        button.callback_data
        for row in update.callback_query.edits[-1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "move:exec:zelda:hashi2:identity_memory" in callbacks
    assert "move:exec:zelda:hashi2:workspace" in callbacks
    assert "move:cancel" in callbacks


@pytest.mark.asyncio
async def test_handle_move_callback_cancel(tmp_path):
    runtime = _runtime(tmp_path)
    update = SimpleNamespace(callback_query=_Query("move:cancel"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    assert update.callback_query.answers[-1]["text"] is None
    assert update.callback_query.edits[-1]["text"] == "Move cancelled."


@pytest.mark.asyncio
async def test_handle_move_callback_agent_lists_targets(tmp_path):
    runtime = _runtime(tmp_path)
    update = SimpleNamespace(callback_query=_Query("move:agent:zelda"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    assert "Select the target instance" in update.callback_query.edits[-1]["text"]
    button = update.callback_query.edits[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "move:target:zelda:hashi2"


@pytest.mark.asyncio
async def test_stale_keep_source_callback_redirects_to_clone_without_staging(tmp_path):
    calls = []
    runtime = _runtime(tmp_path)

    async def _do_move(update, agent_id, target, instances, **kwargs):
        calls.append((agent_id, target, instances, kwargs))

    runtime._do_move = _do_move
    update = SimpleNamespace(callback_query=_Query("move:exec:zelda:hashi2:keep"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    assert calls == []
    assert "/clone" in update.callback_query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_handle_move_callback_confirmation_hands_off_to_background_owner(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime.global_config.project_root = tmp_path / "code-generation"
    submit = AsyncMock(
        return_value={"accepted": True, "operation": {"status": "accepted"}}
    )
    runtime.orchestrator = SimpleNamespace(
        runtimes=[],
        submit_agent_move=submit,
    )

    monkeypatch.setattr(
        runtime_remote,
        "get_outbound_move",
        lambda *args, **kwargs: {
            "agent_id": "zelda",
            "target_instance": "HASHI2",
        },
    )
    update = SimpleNamespace(callback_query=_Query("move:commit:12345678-abcd"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    submit.assert_awaited_once()
    assert submit.await_args.args[0] == "12345678-abcd"
    assert submit.await_args.args[1]["hashi2"]["host"] == "127.0.0.1"
    assert "12345678-abcd" in update.callback_query.edits[-1]["text"]
    assert "<code>accepted</code>" in update.callback_query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_handle_move_callback_other_agent_uses_worker_preflight(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime.global_config.project_root = tmp_path / "code-generation"
    selected = SimpleNamespace(name="sunny")
    preflight = AsyncMock(return_value={"busy": False, "delayed_count": 0})
    runtime.orchestrator = SimpleNamespace(
        runtimes=[selected],
        agent_move_preflight=preflight,
        submit_agent_move=AsyncMock(
            return_value={"accepted": True, "operation": {"status": "accepted"}}
        ),
    )
    monkeypatch.setattr(
        runtime_pending,
        "delayed_count",
        AsyncMock(side_effect=AssertionError("must not cross-read Scheduler records")),
    )
    monkeypatch.setattr(
        runtime_remote,
        "get_outbound_move",
        lambda *args, **kwargs: {
            "agent_id": "sunny",
            "target_instance": "HASHI2",
        },
    )
    update = SimpleNamespace(callback_query=_Query("move:commit:12345678-abcd"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    preflight.assert_awaited_once_with("sunny")
    runtime.orchestrator.submit_agent_move.assert_awaited_once()
    assert selected._agent_move_quiesced is True


@pytest.mark.asyncio
async def test_handle_move_callback_failure_keeps_recovery_actions(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)

    async def _fail(*args, **kwargs):
        raise AgentMoveError("target response uncertain")

    runtime.orchestrator = SimpleNamespace(
        runtimes=[],
        submit_agent_move=_fail,
    )

    monkeypatch.setattr(
        runtime_remote,
        "get_outbound_move",
        lambda *args, **kwargs: {
            "agent_id": "zelda",
            "target_instance": "HASHI2",
        },
    )
    update = SimpleNamespace(callback_query=_Query("move:commit:12345678-abcd"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    last = update.callback_query.edits[-1]
    assert "target response uncertain" in last["text"]
    callbacks = [
        button.callback_data
        for row in last["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == [
        "move:commit:12345678-abcd",
        "move:abort:12345678-abcd",
    ]


@pytest.mark.asyncio
async def test_handle_clone_confirmation_uses_same_background_owner(tmp_path):
    runtime = _runtime(tmp_path)
    submit = AsyncMock(
        return_value={"accepted": True, "operation": {"status": "accepted"}}
    )
    runtime.orchestrator = SimpleNamespace(
        runtimes=[],
        submit_agent_move=submit,
    )
    update = SimpleNamespace(callback_query=_Query("clone:commit:clone-1234"))

    await runtime_remote.handle_clone_callback(
        runtime,
        update,
        SimpleNamespace(),
    )

    submit.assert_awaited_once()
    assert submit.await_args.args[0] == "clone-1234"
    assert "hashi_test" in submit.await_args.args[1]
    assert "clone-1234" in update.callback_query.edits[-1]["text"]
    assert "<code>accepted</code>" in update.callback_query.edits[-1]["text"]


@pytest.mark.asyncio
async def test_handle_move_callback_rechecks_agent_busy_before_cutover(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    selected = SimpleNamespace(name="zelda", _backend_busy=lambda: True)
    runtime.orchestrator = SimpleNamespace(
        runtimes=[selected],
        submit_agent_move=AsyncMock(
            side_effect=AssertionError("busy Agent must not reach cutover")
        ),
    )
    monkeypatch.setattr(runtime_pending, "delayed_count", AsyncMock(return_value=0))
    monkeypatch.setattr(
        runtime_remote,
        "get_outbound_move",
        lambda *args, **kwargs: {
            "agent_id": "zelda",
            "target_instance": "HASHI2",
        },
    )

    update = SimpleNamespace(callback_query=_Query("move:commit:12345678-abcd"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    assert selected._agent_move_quiesced is False
    runtime.orchestrator.submit_agent_move.assert_not_awaited()
    assert "busy" in update.callback_query.edits[-1]["text"]
    assert update.callback_query.edits[-1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_do_move_dry_run_never_stages_target(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime.global_config.project_root = tmp_path / "code-generation"
    runtime.orchestrator = SimpleNamespace(runtimes=[])
    monkeypatch.setattr(runtime_pending, "delayed_count", AsyncMock(return_value=0))
    calls = []

    def _preview(root, instances, agent_id, target, *, source_instance, transfer_mode):
        assert transfer_mode == "workspace"
        calls.append((root, instances, agent_id, target, source_instance))
        return {
            "agent_id": agent_id,
            "target_instance": "HASHI2",
            "source_environment": "wsl",
            "target_environment": "windows",
            "package_bytes": 1024,
            "workspace_files": 5,
            "schedule_count": 1,
        }

    monkeypatch.setattr(runtime_remote, "preview_outbound_move", _preview)
    prepare = Mock(side_effect=AssertionError("dry-run must not stage a target"))
    monkeypatch.setattr(runtime_remote, "prepare_outbound_move", prepare)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=99))

    await runtime_remote.do_move(
        runtime,
        update,
        "zelda",
        "hashi2",
        {"hashi2": {"display_name": "HASHI2"}},
        dry_run=True,
        transfer_mode="workspace",
    )

    assert calls
    prepare.assert_not_called()
    assert calls[0][0] == tmp_path
    assert calls[0][2:] == ("zelda", "HASHI2", "HASHI_TEST")
    assert (
        "Source and target configuration were not changed"
        in runtime.replies[-1]["text"]
    )


@pytest.mark.asyncio
async def test_do_move_other_agent_uses_worker_preflight(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.global_config.bridge_home = tmp_path
    runtime.global_config.project_root = tmp_path / "code-generation"
    selected = SimpleNamespace(name="sunny")
    preflight = AsyncMock(return_value={"busy": False, "delayed_count": 0})
    runtime.orchestrator = SimpleNamespace(
        runtimes=[selected],
        agent_move_preflight=preflight,
    )
    monkeypatch.setattr(
        runtime_pending,
        "delayed_count",
        AsyncMock(side_effect=AssertionError("must not cross-read Scheduler records")),
    )
    preview = Mock(
        return_value={
            "agent_id": "sunny",
            "target_instance": "HASHI2",
            "source_environment": "windows",
            "target_environment": "wsl",
            "package_bytes": 1024,
            "workspace_files": 5,
            "schedule_count": 0,
        }
    )
    monkeypatch.setattr(runtime_remote, "preview_outbound_move", preview)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=99))

    await runtime_remote.do_move(
        runtime,
        update,
        "sunny",
        "hashi2",
        {"hashi2": {"display_name": "HASHI2"}},
        dry_run=True,
        transfer_mode="workspace",
    )

    preflight.assert_awaited_once_with("sunny")
    preview.assert_called_once()
    assert "AGENT MOVE PREVIEW" in runtime.replies[-1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preflight_result", "expected"),
    [
        ({"busy": False, "delayed_count": 1}, "Move is blocked"),
        ({"busy": True, "delayed_count": 0}, "busy"),
    ],
)
async def test_do_move_worker_preflight_preserves_move_guards(
    tmp_path,
    monkeypatch,
    preflight_result,
    expected,
):
    runtime = _runtime(tmp_path)
    runtime.orchestrator = SimpleNamespace(
        runtimes=[SimpleNamespace(name="sunny")],
        agent_move_preflight=AsyncMock(return_value=preflight_result),
    )
    preview = Mock(side_effect=AssertionError("blocked move must not be previewed"))
    monkeypatch.setattr(runtime_remote, "preview_outbound_move", preview)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=99))

    await runtime_remote.do_move(
        runtime,
        update,
        "sunny",
        "hashi2",
        {"hashi2": {"display_name": "HASHI2"}},
        dry_run=True,
        transfer_mode="workspace",
    )

    preview.assert_not_called()
    assert expected in runtime.replies[-1]["text"]


@pytest.mark.asyncio
async def test_remote_list_reports_unavailable_when_refresh_and_cache_both_fail(
    tmp_path, mock_fetch_remote_peers_none
):
    replies = []
    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _remote_config_snapshot=lambda: {
            "root": tmp_path,
            "port": 8767,
            "use_tls": False,
            "backend": "lan",
        },
        _remote_process=None,
        _fetch_remote_json=mock_fetch_remote_peers_none,
        _reply_text=lambda update, text, **kwargs: _reply(replies, text, kwargs),
    )
    runtime._remote_peer_presence = lambda peer: (0, "🟢 online", "handshake_accepted")
    runtime._render_remote_peer_block = lambda peer: [str(peer.get("instance_id"))]
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    context = SimpleNamespace(args=["list"])

    await runtime_remote.cmd_remote(runtime, update, context)

    assert replies[-1]["text"] == "⚠️ Remote peer view is currently unavailable."


@pytest.fixture
def mock_fetch_remote_peers_none():
    async def _fetch_none(_path):
        return None, None

    return _fetch_none


@pytest.mark.asyncio
async def test_remote_status_includes_peer_list(tmp_path, monkeypatch):
    replies = []

    async def _fetch_remote_json(path):
        if path == "/health":
            return (
                {
                    "ok": True,
                    "instance": {"instance_id": "HASHI2"},
                    "peers": [
                        {
                            "instance_id": "HASHI9",
                            "properties": {"handshake_state": "handshake_accepted"},
                        },
                        {
                            "instance_id": "MSI",
                            "properties": {"handshake_state": "handshake_timed_out"},
                        },
                    ],
                },
                "http://127.0.0.1:8767/health",
            )
        if path == "/protocol/status":
            return (
                {
                    "ok": True,
                    "inflight_count": 11,
                    "protocol_auth_mode": "shared-token",
                    "shared_token_configured": True,
                    "lan_mode": False,
                    "rescue_start_enabled": True,
                },
                "http://127.0.0.1:8767/protocol/status",
            )
        return None, None

    monkeypatch.setattr(
        runtime_remote.remote_lifecycle,
        "load_settings",
        lambda root: SimpleNamespace(
            enabled=True, supervised=True, disabled_path=root / ".disabled"
        ),
    )
    monkeypatch.setattr(
        runtime_remote.remote_lifecycle, "read_disabled_state", lambda root: None
    )

    runtime = SimpleNamespace(
        _is_authorized_user=lambda user_id: True,
        _remote_config_snapshot=lambda: {
            "root": tmp_path,
            "port": 8767,
            "use_tls": False,
            "backend": "lan",
        },
        _remote_process=None,
        _fetch_remote_json=_fetch_remote_json,
        _reply_text=lambda update, text, **kwargs: _reply(replies, text, kwargs),
        _remote_peer_presence=lambda peer: (
            0
            if str((peer.get("properties") or {}).get("handshake_state"))
            == "handshake_accepted"
            else 3,
            "",
            "",
        ),
        _render_remote_peer_block=lambda peer: [f"peer:{peer.get('instance_id')}"],
        global_config=SimpleNamespace(project_root=tmp_path),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))
    context = SimpleNamespace(args=[])

    await runtime_remote.cmd_remote(runtime, update, context)

    text = replies[-1]["text"]
    assert "<b>HASHI REMOTE</b>" in text
    assert "Peers:" not in text
    assert "Inflight:" not in text
    assert "Rescue:" not in text
    assert "📡 <b>REMOTE INSTANCES</b>" not in text
    assert "<b>Current</b> · <code>1</code> online" in text
    assert "<b>Attention</b> · <code>0</code>" in text
    assert "<b>Offline</b> · <code>1</code>" in text
    assert "peer:HASHI9" in text
    assert "peer:MSI" in text


@pytest.mark.asyncio
async def test_remote_on_uses_unified_supervisor_lifecycle(tmp_path, monkeypatch):
    replies = []
    lifecycle = SimpleNamespace(
        enabled=True,
        supervised=True,
        disabled_path=tmp_path / "state" / "remote_disabled.json",
        root=tmp_path,
        port=8767,
        use_tls=False,
        backend="lan",
    )
    cleared = []

    async def ensure_remote_started(root):
        assert root == tmp_path
        return {
            "ok": True,
            "action": "started_supervisor",
            "port": 8767,
            "health_host": "127.0.0.1",
        }

    monkeypatch.setattr(runtime_remote.remote_lifecycle, "load_settings", lambda _root: lifecycle)
    monkeypatch.setattr(runtime_remote.remote_lifecycle, "read_disabled_state", lambda _root: None)
    monkeypatch.setattr(
        runtime_remote.remote_lifecycle,
        "clear_disabled_state",
        lambda root: cleared.append(root),
    )
    monkeypatch.setattr(
        runtime_remote.remote_lifecycle,
        "ensure_remote_started",
        ensure_remote_started,
    )
    runtime = SimpleNamespace(
        _is_authorized_user=lambda _user_id: True,
        _remote_config_snapshot=lambda: {
            "root": tmp_path,
            "port": 8767,
            "use_tls": False,
            "backend": "lan",
        },
        _remote_process=None,
        _reply_text=lambda update, text, **kwargs: _reply(replies, text, kwargs),
    )

    await runtime_remote.cmd_remote(
        runtime,
        SimpleNamespace(effective_user=SimpleNamespace(id=1)),
        SimpleNamespace(args=["on"]),
    )

    assert cleared == [tmp_path]
    assert "HASHI Remote is active" in replies[-1]["text"]
    assert "Lifecycle supervisor" in replies[-1]["text"]


@pytest.mark.asyncio
async def test_remote_off_persists_disable_and_stops_supervisor(tmp_path, monkeypatch):
    replies = []
    lifecycle = SimpleNamespace(
        enabled=True,
        supervised=True,
        disabled_path=tmp_path / "state" / "remote_disabled.json",
    )
    stopped = []

    async def stop_remote(root):
        stopped.append(root)
        return {"ok": True, "action": "supervisor_stopped"}

    monkeypatch.setattr(runtime_remote.remote_lifecycle, "load_settings", lambda _root: lifecycle)
    monkeypatch.setattr(runtime_remote.remote_lifecycle, "read_disabled_state", lambda _root: None)
    monkeypatch.setattr(runtime_remote.remote_lifecycle, "stop_remote", stop_remote)
    runtime = SimpleNamespace(
        _is_authorized_user=lambda _user_id: True,
        _remote_config_snapshot=lambda: {
            "root": tmp_path,
            "port": 8767,
            "use_tls": False,
            "backend": "lan",
        },
        _remote_process=None,
        _reply_text=lambda update, text, **kwargs: _reply(replies, text, kwargs),
    )

    await runtime_remote.cmd_remote(
        runtime,
        SimpleNamespace(effective_user=SimpleNamespace(id=1)),
        SimpleNamespace(args=["off"]),
    )

    assert stopped == [tmp_path]
    state = json.loads((tmp_path / "state" / "remote_disabled.json").read_text())
    assert state["disabled"] is True
    assert "stopped and disabled" in replies[-1]["text"]


def test_load_instances_uses_instance_root_not_generation(tmp_path, monkeypatch):
    root = tmp_path / "instance"
    root.mkdir()
    snapshot = tmp_path / "generation" / "orchestrator" / "runtime_remote.py"
    monkeypatch.setattr(runtime_remote, "__file__", str(snapshot))
    monkeypatch.setattr(runtime_remote.Path, "home", lambda: tmp_path / "home")
    expected = {"hashi3": {"display_name": "HASHI3"}}
    (root / "instances.json").write_text(json.dumps({"instances": expected}))
    assert runtime_remote.load_instances(project_root=root) == expected


@pytest.mark.asyncio
async def test_move_reads_bom_configuration_from_separate_instance_home(tmp_path):
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    runtime = _runtime(tmp_path / "source")
    runtime.global_config.bridge_home = tmp_path
    (tmp_path / "agents.json").write_text(
        json.dumps({"agents": [{"name": "live-agent"}]}), encoding="utf-8-sig"
    )
    (tmp_path / "instances.json").write_text(
        json.dumps({"instances": {"peer": {"instance_id": "PEER"}}}), encoding="utf-8-sig"
    )
    await runtime_remote.move_show_agent_picker(runtime, SimpleNamespace(), {})
    button = runtime.replies[-1]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == "move:agent:live-agent"
    assert FlexibleAgentRuntime._load_instances(runtime) == {"peer": {"instance_id": "PEER"}}


@pytest.mark.parametrize("contents", ['{', '[]', '{"instances": []}', '{"instances": {"bad": null}}'])
def test_load_instances_rejects_invalid_config(tmp_path, contents):
    path = tmp_path / "instances.json"
    path.write_text(contents)
    with pytest.raises(runtime_remote.InstanceConfigurationError):
        runtime_remote.load_instances([path])


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["en", "zh-CN"])
async def test_move_discovers_connected_peers_without_legacy_file(tmp_path, monkeypatch, locale):
    from aiohttp import web
    from orchestrator import ui_language
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime

    peer = {
        "instance_id": "HASHI9", "display_name": "Destination <9>",
        "host": "old-host", "port": 9999,
        "resolved_route_host": "127.0.0.1", "resolved_route_port": 18769,
        "capabilities": ["agent_move_receive_v1"],
        "properties": {"live_status": "online", "handshake_state": "handshake_accepted"},
    }
    response = {"ok": True, "peers": [peer, {**peer, "instance_id": "HASHI_TEST"}]}
    requests = []

    async def peers_handler(request):
        requests.append(request.path)
        return web.json_response(response)

    app = web.Application()
    app.router.add_get("/peers", peers_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        runtime = _runtime(tmp_path)
        runtime._load_instances = lambda: FlexibleAgentRuntime._load_instances(runtime)
        runtime._remote_urls = lambda path: [f"http://127.0.0.1:{port}{path}"]
        runtime._fetch_remote_json = lambda path: FlexibleAgentRuntime._fetch_remote_json(runtime, path)
        runtime._format_remote_age = lambda value: FlexibleAgentRuntime._format_remote_age(runtime, value)
        runtime._remote_peer_presence = lambda p: FlexibleAgentRuntime._remote_peer_presence(runtime, p)
        runtime._move_show_target_picker = lambda u, a, i: runtime_remote.move_show_target_picker(runtime, u, a, i)
        runtime._do_move = lambda u, a, t, i, **kw: runtime_remote.do_move(runtime, u, a, t, i, **kw)
        update = SimpleNamespace(effective_user=SimpleNamespace(id=1), effective_chat=SimpleNamespace(id=1))
        context = SimpleNamespace(args=["zelda"])
        monkeypatch.setattr(runtime_remote, "__file__", str(tmp_path / "generation" / "orchestrator" / "runtime_remote.py"))
        monkeypatch.setattr(runtime_pending, "delayed_count", AsyncMock(return_value=0))
        with ui_language.language_scope(runtime, locale=locale):
            await FlexibleAgentRuntime.cmd_move(runtime, update, context)
            buttons = runtime.replies[-1]["reply_markup"].inline_keyboard
            assert [b.callback_data for row in buttons for b in row] == ["move:target:zelda:hashi9"]
            assert "Destination <9>" == buttons[0][0].text.removeprefix("📦 ")
            assert requests == ["/peers"]
            assert not (tmp_path / "instances.json").exists()
            (tmp_path / "instances.json").write_text("{")
            directory = await runtime_remote.load_move_instances(runtime)
            assert directory["hashi9"]["host"] == "127.0.0.1"
            assert directory["hashi9"]["remote_port"] == 18769
            context.args = ["list"]
            await FlexibleAgentRuntime.cmd_move(runtime, update, context)
            assert "Destination &lt;9&gt;" in runtime.replies[-1]["text"]
            peer["capabilities"] = []
            query = _Query("move:target:zelda:hashi9")
            await runtime_remote.handle_move_callback(runtime, SimpleNamespace(callback_query=query), context)
            assert query.edits[-1]["text"] == ui_language.tr("remote.move.target_unsupported", target="hashi9")
            peer["capabilities"] = ["agent_move_receive_v1"]
            peer["properties"]["live_status"] = "offline"
            query = _Query("move:target:zelda:hashi9")
            await runtime_remote.handle_move_callback(runtime, SimpleNamespace(callback_query=query), context)
            assert query.edits[-1]["text"] == ui_language.tr("remote.move.target_unavailable", target="hashi9")
            prepare = AsyncMock(side_effect=AssertionError("Disconnected target must not be staged"))
            monkeypatch.setattr(runtime_remote, "prepare_outbound_move", prepare)
            query = _Query("move:exec:zelda:hashi9:move")
            await runtime_remote.handle_move_callback(runtime, SimpleNamespace(callback_query=query, effective_chat=update.effective_chat), context)
            assert runtime.replies[-1]["text"] == ui_language.tr("remote.move.target_unavailable", target="hashi9")
            prepare.assert_not_called()
            context.args = ["zelda"]
            await FlexibleAgentRuntime.cmd_move(runtime, update, context)
            assert runtime.replies[-1]["text"] == ui_language.tr("remote.move.no_targets")
            response["peers"] = []
            await FlexibleAgentRuntime.cmd_move(runtime, update, context)
            assert runtime.replies[-1]["text"] == ui_language.tr("remote.move.no_targets")
            response["trusted_view"] = False
            await FlexibleAgentRuntime.cmd_move(runtime, update, context)
            assert runtime.replies[-1]["text"] == ui_language.tr("move.remote_untrusted")
            response.clear()
            response.update({"ok": False, "error": "service unavailable"})
            await FlexibleAgentRuntime.cmd_move(runtime, update, context)
            assert runtime.replies[-1]["text"] == ui_language.tr("move.remote_unavailable")
    finally:
        await runner.cleanup()
