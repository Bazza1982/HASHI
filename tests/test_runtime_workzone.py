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
        config=SimpleNamespace(extra={}),
        global_config=SimpleNamespace(project_root=project),
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


def _update():
    return SimpleNamespace(effective_user=SimpleNamespace(id=1))


def test_sync_workzone_to_backend_config_updates_registry(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._workzone_dir = runtime.zone

    runtime_workzone.sync_workzone_to_backend_config(runtime)

    assert runtime.config.extra["workzone_dir"] == str(runtime.zone)
    assert runtime.backend.config.extra["workzone_dir"] == str(runtime.zone)
    assert runtime.backend.tool_registry.workspace_dir == runtime.zone


@pytest.mark.asyncio
async def test_cmd_workzone_status_set_and_off(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)

    await runtime_workzone.cmd_workzone(runtime, _update(), SimpleNamespace(args=[]))
    assert "Workzone is OFF" in runtime.replies[-1]["text"]

    await runtime_workzone.cmd_workzone(runtime, _update(), SimpleNamespace(args=["repo"]))
    assert "Workzone ON" in runtime.replies[-1]["text"]
    assert runtime._workzone_dir == runtime.zone.resolve()

    await runtime_workzone.cmd_workzone(runtime, _update(), SimpleNamespace(args=["off"]))
    assert "Workzone OFF" in runtime.replies[-1]["text"]
    assert runtime._workzone_dir is None


def test_workzone_prompt_section_uses_backend_capabilities(tmp_path):
    runtime = _runtime(tmp_path)
    runtime._sync_workzone_to_backend_config = lambda: runtime_workzone.sync_workzone_to_backend_config(runtime)
    runtime._workzone_dir = runtime.zone
    from orchestrator.workzone import save_workzone

    save_workzone(runtime.workspace_dir, runtime.zone)

    sections = runtime_workzone.workzone_prompt_section(runtime)

    assert sections
    assert sections[0][0] == "WORKZONE"
    assert str(runtime.zone.resolve()) in sections[0][1]
