from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import runtime_sys_prompts
from orchestrator.bridge_memory import (
    BridgeContextAssembler,
    SysPromptManager,
    global_sys_prompt_state_path,
)


class _EmptyMemoryStore:
    def get_recent_turns(self, *, limit: int):
        return []

    def retrieve_memories(self, _query: str, *, limit: int):
        return []

    def get_last_user_turn_ts(self):
        return None


class _Message:
    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(text=text)


class _Query:
    def __init__(self, data: str, user_id: int = 123) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = _Message()
        self.edits: list[tuple[str, dict]] = []
        self.answers: list[tuple[str | None, bool]] = []

    async def edit_message_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))

    async def answer(self, text: str | None = None, show_alert: bool = False):
        self.answers.append((text, show_alert))


def _global_config(
    bridge_home: Path, *, instance_id: str = "HASHI1"
) -> SimpleNamespace:
    return SimpleNamespace(
        bridge_home=bridge_home,
        project_root=bridge_home / "code-root-must-not-own-instance-state",
        instance_id=instance_id,
        authorized_id=123,
    )


def _runtime(tmp_path: Path) -> SimpleNamespace:
    workspace = tmp_path / "workspaces" / "zelda"
    workspace.mkdir(parents=True)
    global_config = _global_config(tmp_path)
    return SimpleNamespace(
        global_config=global_config,
        sys_prompt_manager=SysPromptManager(workspace),
        global_sys_prompt_manager=SysPromptManager.for_instance(global_config),
        _is_authorized_user=lambda user_id: user_id == 123,
        _is_command_allowed=lambda command: command == "sys",
    )


def _update(user_id: int = 123) -> SimpleNamespace:
    message = _Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=456),
        message=message,
    )


def _context(*args: str) -> SimpleNamespace:
    return SimpleNamespace(args=list(args))


def test_global_sys_state_is_scoped_to_bridge_home_and_instances_are_isolated(
    tmp_path: Path,
) -> None:
    first_config = _global_config(tmp_path / "instance-one", instance_id="HASHI1")
    second_config = _global_config(tmp_path / "instance-two", instance_id="HASHI2")

    first = SysPromptManager.for_instance(first_config)
    second = SysPromptManager.for_instance(second_config)
    first.save("1", "Use Chinese.")
    first.activate("1")

    assert global_sys_prompt_state_path(first_config) == (
        first_config.bridge_home / "state" / "global_sys_prompts.json"
    )
    assert first.get_active_texts() == ["Use Chinese."]
    assert second.get_active_texts() == []
    assert not global_sys_prompt_state_path(second_config).exists()


def test_shared_global_managers_refresh_without_agent_restart(tmp_path: Path) -> None:
    config = _global_config(tmp_path)
    zelda_view = SysPromptManager.for_instance(config)
    momo_view = SysPromptManager.for_instance(config)

    assert momo_view.get_active_texts() == []
    zelda_view.save("3", "Answer in Chinese.")
    zelda_view.activate("3")

    assert momo_view.get_active_entries() == [
        {"slot": "3", "active": True, "text": "Answer in Chinese."}
    ]


def test_concurrent_global_slot_updates_do_not_overwrite_each_other(
    tmp_path: Path,
) -> None:
    config = _global_config(tmp_path)

    def save_slot(index: int) -> None:
        SysPromptManager.for_instance(config).save(str(index), f"rule-{index}")

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(save_slot, range(1, 11)))

    slots = SysPromptManager.for_instance(config).list_slots()
    assert [item["text"] for item in slots] == [
        f"rule-{index}" for index in range(1, 11)
    ]


def test_prompt_assembler_injects_global_before_agent_local_sys(tmp_path: Path) -> None:
    local = SysPromptManager(tmp_path / "workspace")
    global_manager = SysPromptManager.for_instance(_global_config(tmp_path))
    local.save("2", "Use the Zelda local reporting style.")
    local.activate("2")
    global_manager.save("1", "Always answer in Chinese.")
    global_manager.activate("1")
    assembler = BridgeContextAssembler(
        _EmptyMemoryStore(),
        system_md=None,
        sys_prompt_manager=local,
        global_sys_prompt_manager=global_manager,
    )

    payload = assembler.build_prompt_payload(
        "Current task", "codex-cli", incremental=True
    )
    prompt = payload["final_prompt"]

    assert prompt.count("--- ADDITIONAL SYSTEM CONTEXT ---") == 1
    assert "INSTANCE-GLOBAL /sys rules apply" in prompt
    assert "[Global /sys slot 1]\nAlways answer in Chinese." in prompt
    assert "AGENT-LOCAL /sys rules follow:" in prompt
    assert prompt.index("Always answer in Chinese.") < prompt.index(
        "Zelda local reporting style"
    )
    assert payload["audit"]["sections"][0]["item_count"] == 2


def test_same_global_rule_reaches_two_agents_but_local_rules_do_not_cross(
    tmp_path: Path,
) -> None:
    config = _global_config(tmp_path)
    global_one = SysPromptManager.for_instance(config)
    global_two = SysPromptManager.for_instance(config)
    global_one.save("1", "Shared instance rule.")
    global_one.activate("1")
    zelda_local = SysPromptManager(tmp_path / "zelda")
    momo_local = SysPromptManager(tmp_path / "momo")
    zelda_local.save("1", "Zelda only.")
    zelda_local.activate("1")
    momo_local.save("1", "Momo only.")
    momo_local.activate("1")

    zelda_prompt = BridgeContextAssembler(
        _EmptyMemoryStore(),
        None,
        sys_prompt_manager=zelda_local,
        global_sys_prompt_manager=global_one,
    ).build_prompt("task", "codex-cli", incremental=True)
    momo_prompt = BridgeContextAssembler(
        _EmptyMemoryStore(),
        None,
        sys_prompt_manager=momo_local,
        global_sys_prompt_manager=global_two,
    ).build_prompt("task", "codex-cli", incremental=True)

    assert (
        "Shared instance rule." in zelda_prompt
        and "Shared instance rule." in momo_prompt
    )
    assert "Zelda only." in zelda_prompt and "Zelda only." not in momo_prompt
    assert "Momo only." in momo_prompt and "Momo only." not in zelda_prompt


@pytest.mark.asyncio
async def test_sys_g_alias_manages_global_slots_without_touching_local_slots(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    update = _update()

    await runtime_sys_prompts.cmd_sys(
        runtime,
        update,
        _context("g", "1", "save", "必须使用中文回复"),
    )

    assert runtime.global_sys_prompt_manager.get_slot("1") == {
        "text": "必须使用中文回复",
        "active": False,
    }
    assert runtime.sys_prompt_manager.get_slot("1") == {"text": "", "active": False}
    assert update.message.replies[-1][1]["reply_markup"] is not None
    audit_path = tmp_path / "state" / "global_sys_prompt_audit.jsonl"
    audit = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert audit["agent"] == "unknown"
    assert audit["args_redacted"][0:2] == ["action=save", "slot=1"]
    assert "必须使用中文回复" not in audit_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_global_activation_and_active_replace_require_explicit_confirmation(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.global_sys_prompt_manager.save("1", "Original global rule.")
    update = _update()

    await runtime_sys_prompts.cmd_sys(runtime, update, _context("global", "1", "on"))
    assert runtime.global_sys_prompt_manager.get_slot("1")["active"] is False
    assert "ACTIVATE GLOBAL SYSTEM PROMPT" in update.message.replies[-1][0]

    await runtime_sys_prompts.cmd_sys(
        runtime, update, _context("g", "1", "on", "CONFIRM")
    )
    assert runtime.global_sys_prompt_manager.get_slot("1")["active"] is True

    await runtime_sys_prompts.cmd_sys(
        runtime,
        update,
        _context("global", "1", "replace", "Unconfirmed change."),
    )
    assert (
        runtime.global_sys_prompt_manager.get_slot("1")["text"]
        == "Original global rule."
    )

    await runtime_sys_prompts.cmd_sys(
        runtime,
        update,
        _context("g", "1", "replace", "CONFIRM", "Confirmed change."),
    )
    assert runtime.global_sys_prompt_manager.get_slot("1") == {
        "text": "Confirmed change.",
        "active": True,
    }


@pytest.mark.asyncio
async def test_global_save_cannot_overwrite_an_existing_slot(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.global_sys_prompt_manager.save("2", "Keep this rule.")
    runtime.global_sys_prompt_manager.activate("2")
    update = _update()

    await runtime_sys_prompts.cmd_sys(
        runtime,
        update,
        _context("g", "2", "save", "Accidental overwrite."),
    )

    assert runtime.global_sys_prompt_manager.get_slot("2") == {
        "text": "Keep this rule.",
        "active": True,
    }
    assert "already configured" in update.message.replies[-1][0]


@pytest.mark.asyncio
async def test_global_button_activation_requires_confirmation_and_then_updates_shared_state(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.global_sys_prompt_manager.save("4", "Shared button rule.")
    query = _Query("sys:on:global:4")
    update = SimpleNamespace(callback_query=query)

    await runtime_sys_prompts.callback_sys(runtime, update, SimpleNamespace())
    assert runtime.global_sys_prompt_manager.get_slot("4")["active"] is False
    assert "ACTIVATE GLOBAL SYSTEM PROMPT" in query.edits[-1][0]
    callbacks = [
        button.callback_data
        for row in query.edits[-1][1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "sys:confirm_on:global:4" in callbacks

    query.data = "sys:confirm_on:global:4"
    await runtime_sys_prompts.callback_sys(runtime, update, SimpleNamespace())
    assert runtime.global_sys_prompt_manager.get_slot("4")["active"] is True


@pytest.mark.asyncio
async def test_sys_callback_honors_limited_agent_command_policy(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime._is_command_allowed = lambda _command: False
    query = _Query("sys:menu:global")

    await runtime_sys_prompts.callback_sys(
        runtime,
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    assert query.edits == []
    assert query.answers == [("/sys is disabled for this Agent.", True)]


@pytest.mark.asyncio
async def test_flexible_and_legacy_runtimes_delegate_sys_to_the_same_module(
    monkeypatch,
) -> None:
    from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
    from orchestrator.legacy.bridge_agent_runtime import BridgeAgentRuntime

    calls: list[tuple[str, object, object, object]] = []

    async def fake_cmd(runtime, update, context) -> None:
        calls.append(("command", runtime, update, context))

    async def fake_callback(runtime, update, context) -> None:
        calls.append(("callback", runtime, update, context))

    monkeypatch.setattr(runtime_sys_prompts, "cmd_sys", fake_cmd)
    monkeypatch.setattr(runtime_sys_prompts, "callback_sys", fake_callback)

    for runtime_class in (FlexibleAgentRuntime, BridgeAgentRuntime):
        runtime = object.__new__(runtime_class)
        update = object()
        context = object()

        await runtime.cmd_sys(update, context)
        await runtime.callback_sys(update, context)

        assert calls[-2:] == [
            ("command", runtime, update, context),
            ("callback", runtime, update, context),
        ]
