from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.commands.rebuild import RETIRED_NOTICE, command_rebuild


class _Runtime:
    def __init__(self, authorized: bool = True):
        self.global_config = SimpleNamespace(authorized_id=7)
        self.authorized = authorized
        self.messages: list[tuple[str, str | None]] = []

    def _is_authorized_user(self, user_id):
        return self.authorized and user_id == 7

    async def _reply_text(self, _update, text, parse_mode=None):
        self.messages.append((text, parse_mode))


def _update(user_id: int = 7):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [[], ["status"], ["anything"]])
async def test_rebuild_is_one_version_side_effect_free_retirement_notice(args):
    runtime = _Runtime()

    await command_rebuild(runtime, _update(), SimpleNamespace(args=args))

    assert runtime.messages == [(RETIRED_NOTICE, "HTML")]
    assert "No build, reload, or restart was performed" in RETIRED_NOTICE
    assert not hasattr(runtime, "orchestrator")


@pytest.mark.asyncio
async def test_rebuild_retirement_notice_keeps_owner_authorization():
    runtime = _Runtime(authorized=False)

    await command_rebuild(runtime, _update(), SimpleNamespace(args=[]))

    assert len(runtime.messages) == 1
    assert "restricted to the authorized owner" in runtime.messages[0][0]
