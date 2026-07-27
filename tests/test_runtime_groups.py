from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator import runtime_groups


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

    def create_group(self, name, description):
        self.groups[name] = {"members": [], "description": description}
        return True, f"created {name}"

    def group_exists(self, name):
        return name in self.groups


def test_group_list_view_handles_empty_directory():
    text, markup = runtime_groups.group_list_view(_Directory())

    assert "<code>0</code> groups" in text
    assert markup.inline_keyboard[-1][0].callback_data == "group:new"


@pytest.mark.asyncio
async def test_group_command_creation_stays_in_group_module():
    directory = _Directory()
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

    assert directory.groups["reviewers"]["description"] == "Review team"
    assert replies[-1][0].startswith("✅ created reviewers")


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
