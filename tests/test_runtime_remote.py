import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    return SimpleNamespace(
        global_config=SimpleNamespace(project_root=tmp_path, instance_id="HASHI_TEST"),
        _is_authorized_user=lambda user_id: user_id == 1,
        _load_instances=lambda: {"hashi2": {"display_name": "HASHI2"}},
        _do_move=None,
        _reply_text=lambda update, text, **kwargs: _reply(replies, text, kwargs),
        _send_text=lambda chat_id, text, **kwargs: _reply(replies, text, kwargs),
        replies=replies,
    )


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

    assert "Safe move transfers identity" in update.callback_query.edits[-1]["text"]
    callbacks = [
        button.callback_data
        for row in update.callback_query.edits[-1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "move:exec:zelda:hashi2:move" in callbacks
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
async def test_handle_move_callback_exec_invokes_runtime_do_move(tmp_path):
    calls = []
    runtime = _runtime(tmp_path)

    async def _do_move(update, agent_id, target, instances, **kwargs):
        calls.append((agent_id, target, instances, kwargs))

    runtime._do_move = _do_move
    update = SimpleNamespace(callback_query=_Query("move:exec:zelda:hashi2:keep"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    assert calls == [
        (
            "zelda",
            "hashi2",
            {"hashi2": {"display_name": "HASHI2"}},
            {"keep_source": True, "sync": False, "dry_run": False},
        )
    ]


@pytest.mark.asyncio
async def test_handle_move_callback_commit_runs_two_phase_cutover(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    result = {
        "status": "moved_pending_reboots",
        "agent_id": "zelda",
        "target_instance": "HASHI2",
        "source_instance": "HASHI_TEST",
        "reboot_order": ["HASHI_TEST", "HASHI2"],
    }

    def _confirm(root, instances, package_id):
        assert root == tmp_path
        assert instances == {"hashi2": {"display_name": "HASHI2"}}
        assert package_id == "12345678-abcd"
        return result

    monkeypatch.setattr(
        runtime_remote,
        "get_outbound_move",
        lambda *args, **kwargs: {
            "agent_id": "zelda",
            "target_instance": "HASHI2",
        },
    )
    monkeypatch.setattr(runtime_remote, "confirm_outbound_move", _confirm)
    update = SimpleNamespace(callback_query=_Query("move:commit:12345678-abcd"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    assert "AGENT MOVE COMMITTED" in update.callback_query.edits[-1]["text"]
    assert (
        "No reboot was started automatically" in update.callback_query.edits[-1]["text"]
    )


@pytest.mark.asyncio
async def test_handle_move_callback_failure_keeps_recovery_actions(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)

    def _fail(*args, **kwargs):
        raise AgentMoveError("target response uncertain")

    monkeypatch.setattr(
        runtime_remote,
        "get_outbound_move",
        lambda *args, **kwargs: {
            "agent_id": "zelda",
            "target_instance": "HASHI2",
        },
    )
    monkeypatch.setattr(runtime_remote, "confirm_outbound_move", _fail)
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
async def test_handle_move_callback_rechecks_agent_busy_before_cutover(
    tmp_path, monkeypatch
):
    runtime = _runtime(tmp_path)
    selected = SimpleNamespace(name="zelda", _backend_busy=lambda: True)
    runtime.orchestrator = SimpleNamespace(runtimes=[selected])
    monkeypatch.setattr(runtime_pending, "delayed_count", AsyncMock(return_value=0))
    monkeypatch.setattr(
        runtime_remote,
        "get_outbound_move",
        lambda *args, **kwargs: {
            "agent_id": "zelda",
            "target_instance": "HASHI2",
        },
    )

    def _unexpected_confirm(*args, **kwargs):
        raise AssertionError("busy Agent must not reach cutover")

    monkeypatch.setattr(runtime_remote, "confirm_outbound_move", _unexpected_confirm)
    update = SimpleNamespace(callback_query=_Query("move:commit:12345678-abcd"))

    await runtime_remote.handle_move_callback(runtime, update, SimpleNamespace())

    assert selected._agent_move_quiesced is False
    assert "busy" in update.callback_query.edits[-1]["text"]
    assert update.callback_query.edits[-1]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_do_move_dry_run_never_stages_target(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    runtime.orchestrator = SimpleNamespace(runtimes=[])
    monkeypatch.setattr(runtime_pending, "delayed_count", AsyncMock(return_value=0))
    calls = []

    def _preview(root, instances, agent_id, target, *, source_instance):
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
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=99))

    await runtime_remote.do_move(
        runtime,
        update,
        "zelda",
        "hashi2",
        {"hashi2": {"display_name": "HASHI2"}},
        dry_run=True,
    )

    assert calls and calls[0][2:] == ("zelda", "hashi2", "HASHI_TEST")
    assert (
        "Source and target configuration were not changed"
        in runtime.replies[-1]["text"]
    )


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
