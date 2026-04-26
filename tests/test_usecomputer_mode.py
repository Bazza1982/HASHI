from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys

sys.modules.setdefault("edge_tts", SimpleNamespace())

from orchestrator.admin_local_testing import supported_commands
from orchestrator.agent_runtime import BridgeAgentRuntime
from orchestrator.bridge_memory import SysPromptManager
from orchestrator.usecomputer_mode import (
    USECOMPUTER_SLOT,
    USECOMPUTER_SYSTEM_PROMPT,
    build_usecomputer_task_prompt,
    get_usecomputer_examples_text,
    get_usecomputer_status,
    set_usecomputer_mode,
)


class _FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        return SimpleNamespace(ok=True)


class _FakeUpdate:
    def __init__(self, user_id: int = 123, chat_id: int = 456):
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.message = _FakeMessage()


class _UsecomputerRuntime:
    def __init__(self, workspace_dir: Path):
        self.global_config = SimpleNamespace(authorized_id=123)
        self.sys_prompt_manager = SysPromptManager(workspace_dir)
        self.enqueued: list[dict] = []

    async def enqueue_request(self, chat_id, prompt, source, summary):
        self.enqueued.append(
            {
                "chat_id": chat_id,
                "prompt": prompt,
                "source": source,
                "summary": summary,
            }
        )


class _SupportedRuntime:
    async def cmd_usecomputer(self, update, context):
        return None

    async def cmd_usercomputer(self, update, context):
        return None


class UsecomputerModeTests(unittest.IsolatedAsyncioTestCase):
    def test_slot_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SysPromptManager(Path(tmp))
            self.assertIn("OFF", get_usecomputer_status(mgr))

            on_message = set_usecomputer_mode(mgr, True)
            self.assertIn("ON", on_message)
            self.assertEqual(mgr._slot(USECOMPUTER_SLOT).get("text"), USECOMPUTER_SYSTEM_PROMPT)
            self.assertTrue(mgr._slot(USECOMPUTER_SLOT).get("active"))

            off_message = set_usecomputer_mode(mgr, False)
            self.assertIn("OFF", off_message)
            self.assertEqual(mgr._slot(USECOMPUTER_SLOT).get("text"), "")
            self.assertFalse(mgr._slot(USECOMPUTER_SLOT).get("active"))

    def test_build_task_prompt(self):
        prompt = build_usecomputer_task_prompt("Use NVivo with mouse and keyboard")
        self.assertIn("/usecomputer mode", prompt)
        self.assertIn("NVivo", prompt)

    def test_examples_text(self):
        text = get_usecomputer_examples_text()
        self.assertIn("/usecomputer on", text)
        self.assertIn("/usercomputer", text)

    async def test_command_status_and_task_enqueue(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _UsecomputerRuntime(Path(tmp))
            update = _FakeUpdate()

            await BridgeAgentRuntime.cmd_usecomputer(runtime, update, SimpleNamespace(args=["status"]))
            self.assertIn("/usecomputer is OFF", update.message.replies[-1])

            await BridgeAgentRuntime.cmd_usecomputer(
                runtime,
                update,
                SimpleNamespace(args=["Please", "code", "this", "in", "NVivo"]),
            )
            self.assertIn("Running in /usecomputer mode", update.message.replies[-1])
            self.assertEqual(len(runtime.enqueued), 1)
            self.assertEqual(runtime.enqueued[0]["source"], "usecomputer")
            self.assertIn("NVivo", runtime.enqueued[0]["prompt"])
            self.assertTrue(runtime.sys_prompt_manager._slot(USECOMPUTER_SLOT).get("active"))

    async def test_examples_subcommand(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _UsecomputerRuntime(Path(tmp))
            update = _FakeUpdate()
            await BridgeAgentRuntime.cmd_usecomputer(runtime, update, SimpleNamespace(args=["examples"]))
            self.assertIn("/usecomputer on", update.message.replies[-1])

    def test_supported_commands_include_aliases(self):
        commands = supported_commands(_SupportedRuntime())
        self.assertIn("usecomputer", commands)
        self.assertIn("usercomputer", commands)


if __name__ == "__main__":
    unittest.main()
