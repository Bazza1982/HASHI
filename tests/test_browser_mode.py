from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
import sys

sys.modules.setdefault("edge_tts", SimpleNamespace())

from orchestrator.admin_local_testing import supported_commands
from orchestrator.browser_mode import (
    build_browser_task_prompt,
    get_browser_examples_text,
    get_browser_menu_text,
    get_browser_status_text,
)
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


class _FakeMessage:
    def __init__(self):
        self.replies: list[str] = []
        self.reply_kwargs: list[dict] = []

    async def reply_text(self, text: str, **kwargs):
        self.replies.append(text)
        self.reply_kwargs.append(kwargs)
        return SimpleNamespace(ok=True)


class _FakeUpdate:
    def __init__(self, user_id: int = 123, chat_id: int = 456):
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=chat_id)
        self.message = _FakeMessage()


class _BrowserRuntime:
    def __init__(self):
        self.config = SimpleNamespace(active_backend="codex-cli")
        self.global_config = SimpleNamespace(secrets_path=None)
        self.backend_manager = SimpleNamespace(secrets={"brave_api_key": "test-key"})
        self.secrets = self.backend_manager.secrets
        self.logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
        self.enqueued: list[dict] = []

    def _is_authorized_user(self, user_id: int | None) -> bool:
        return user_id == 123

    async def _reply_text(self, update, text: str, **kwargs):
        return await update.message.reply_text(text, **kwargs)

    async def enqueue_request(self, chat_id, prompt, source, summary):
        self.enqueued.append(
            {
                "chat_id": chat_id,
                "prompt": prompt,
                "source": source,
                "summary": summary,
            }
        )

    async def cmd_browser(self, update, context):
        return await FlexibleAgentRuntime.cmd_browser(self, update, context)


class BrowserModeTests(unittest.IsolatedAsyncioTestCase):
    def test_menu_and_examples_text(self):
        menu = get_browser_menu_text()
        self.assertIn("/browser <1-4> <task>", menu)
        self.assertIn("Routes", menu)
        self.assertIn("🟢 *1 HEADLESS* - available", menu)
        self.assertIn("🟡 *3 SEARCH*", menu)
        self.assertIn("🟡 *4 LOGGED-IN*", menu)
        self.assertIn("confirmed online", menu)
        self.assertIn("HASHI extension", menu)

        examples = get_browser_examples_text()
        self.assertIn("HASHI /browser examples", examples)
        self.assertIn("/browser 3", examples)
        self.assertIn("🔐 *4 Logged-in browser work*", examples)
        self.assertNotIn("/broswer", examples)

    def test_status_text_labels_backend_and_keys(self):
        text = get_browser_status_text(
            active_backend="codex-cli",
            brave_configured=True,
            extension_bridge_configured=False,
        )
        self.assertIn("available", text)
        self.assertIn("🟢 *2 NATIVE*", text)
        self.assertIn("🟢 *3 SEARCH*", text)
        self.assertIn("🔴 *4 LOGGED-IN*", text)
        self.assertIn("configured", text)
        self.assertIn("bridge socket not detected", text)

    def test_status_text_uses_yellow_for_unknowns(self):
        text = get_browser_status_text()
        self.assertIn("🟢 *1 HEADLESS* - available", text)
        self.assertIn("🟡 *2 NATIVE*", text)
        self.assertIn("🟡 *3 SEARCH* - not checked", text)
        self.assertIn("🟡 *4 LOGGED-IN* - not checked", text)

    def test_build_browser_task_prompt(self):
        prompt, source, summary = build_browser_task_prompt("4", "Open the logged-in library page")
        self.assertEqual(source, "browser:extension")
        self.assertIn("logged-in Windows browser", prompt)
        self.assertIn("Open the logged-in library page", prompt)
        self.assertIn("Browser task", summary)

    def test_invalid_route_or_missing_task_is_rejected(self):
        with self.assertRaises(ValueError):
            build_browser_task_prompt("5", "Do web research")
        with self.assertRaises(ValueError):
            build_browser_task_prompt("1", "")

    async def test_command_status_and_task_enqueue(self):
        runtime = _BrowserRuntime()
        update = _FakeUpdate()

        await runtime.cmd_browser(update, SimpleNamespace(args=["status"]))
        self.assertIn("HASHI /browser", update.message.replies[-1])
        self.assertIn("🟢 *3 SEARCH* - configured", update.message.replies[-1])
        self.assertEqual(update.message.reply_kwargs[-1].get("parse_mode"), "Markdown")

        await runtime.cmd_browser(
            update,
            SimpleNamespace(args=["3", "Find", "recent", "CSR", "sources"]),
        )
        self.assertIn("Running in /browser route 3", update.message.replies[-1])
        self.assertEqual(len(runtime.enqueued), 1)
        self.assertEqual(runtime.enqueued[0]["source"], "browser:brave")
        self.assertIn("Find recent CSR sources", runtime.enqueued[0]["prompt"])

    async def test_command_without_args_shows_menu(self):
        runtime = _BrowserRuntime()
        update = _FakeUpdate()
        await runtime.cmd_browser(update, SimpleNamespace(args=[]))
        self.assertIn("HASHI /browser", update.message.replies[-1])
        self.assertIn("Routes", update.message.replies[-1])
        self.assertIn("🟢 *3 SEARCH* - configured", update.message.replies[-1])
        self.assertEqual(update.message.reply_kwargs[-1].get("parse_mode"), "Markdown")

    async def test_command_status_refreshes_secrets_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            secrets_path = Path(tmp) / "secrets.json"
            secrets_path.write_text(json.dumps({"brave_api_key": "new-key"}), encoding="utf-8")
            runtime = _BrowserRuntime()
            runtime.global_config.secrets_path = secrets_path
            runtime.backend_manager.secrets = {}
            runtime.secrets = {}
            update = _FakeUpdate()

            await runtime.cmd_browser(update, SimpleNamespace(args=["status"]))

        self.assertIn("🟢 *3 SEARCH* - configured", update.message.replies[-1])
        self.assertEqual(runtime.backend_manager.secrets.get("brave_api_key"), "new-key")

    def test_supported_commands_include_browser(self):
        commands = supported_commands(_BrowserRuntime())
        self.assertIn("browser", commands)


if __name__ == "__main__":
    unittest.main()
