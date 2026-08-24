from types import SimpleNamespace

import pytest

from orchestrator.memory_search_mode import (
    apply_memory_search_preference,
    is_memory_search_enabled,
    set_memory_search_enabled,
)
from orchestrator.runtime_workspace import cmd_memory


def test_memory_search_defaults_off_and_persists_user_setting(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()

    first = SimpleNamespace(saved_memory_injection_enabled=True)
    assert apply_memory_search_preference(first, workspace) is False
    assert first.saved_memory_injection_enabled is False

    assert set_memory_search_enabled(workspace, True) is True
    second = SimpleNamespace(saved_memory_injection_enabled=False)
    assert apply_memory_search_preference(second, workspace) is True
    assert second.saved_memory_injection_enabled is True

    assert set_memory_search_enabled(workspace, False) is False
    third = SimpleNamespace(saved_memory_injection_enabled=True)
    assert apply_memory_search_preference(third, workspace) is False
    assert third.saved_memory_injection_enabled is False


@pytest.mark.asyncio
async def test_memory_search_command_changes_runtime_and_persisted_state(tmp_path):
    workspace = tmp_path / "agent"
    workspace.mkdir()
    assembler = SimpleNamespace(
        turns_injection_enabled=True,
        saved_memory_injection_enabled=False,
    )
    replies = []
    runtime = SimpleNamespace(
        workspace_dir=workspace,
        context_assembler=assembler,
        _is_authorized_user=lambda _user_id: True,
    )

    async def reply(_update, text, **_kwargs):
        replies.append(text)

    runtime._reply_text = reply
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await cmd_memory(
        runtime,
        update,
        SimpleNamespace(args=["search", "on"]),
    )
    assert assembler.saved_memory_injection_enabled is True
    assert is_memory_search_enabled(workspace) is True
    assert "persistent" in replies[-1]

    await cmd_memory(
        runtime,
        update,
        SimpleNamespace(args=["search", "off"]),
    )
    assert assembler.saved_memory_injection_enabled is False
    assert is_memory_search_enabled(workspace) is False
    assert "retrieve_memories() is not called" in replies[-1]
