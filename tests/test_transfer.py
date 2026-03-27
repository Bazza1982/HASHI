from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from orchestrator.admin_local_testing import supported_commands
from orchestrator.handoff_builder import HandoffBuilder
from orchestrator.transfer_store import TransferStore
from orchestrator.workbench_api import WorkbenchApiServer


class _TransferRuntime:
    async def cmd_transfer(self, update, context):
        return None


class TransferTests(unittest.TestCase):
    def test_handoff_builder_transfer_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            transcript = workspace / "transcript.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"role": "user", "text": "Please continue the parser refactor", "source": "text"}),
                        json.dumps({"role": "assistant", "text": "I updated the tokenizer and was about to fix tests.", "source": "text"}),
                        json.dumps({"role": "user", "text": "Move this to hashiko if needed", "source": "text"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            builder = HandoffBuilder(workspace)
            package = builder.build_transfer_package(
                transfer_id="trf-123",
                source_agent="lily",
                source_instance="HASHI1",
                target_agent="hashiko",
                target_instance="HASHI9",
                created_at="2026-03-27T14:20:00",
            )
            self.assertEqual(package["transfer_id"], "trf-123")
            self.assertEqual(package["last_user_message"], "Move this to hashiko if needed")
            self.assertEqual(package["last_assistant_message"], "I updated the tokenizer and was about to fix tests.")
            self.assertGreaterEqual(package["exchange_count"], 1)
            self.assertIn("RECENT CONVERSATION HANDOFF", package["recent_context_block"])
            self.assertIn("transfer_guidance", package)
            self.assertIn("task_state", package)

    def test_transfer_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TransferStore(Path(tmp) / "bridge_transfers.sqlite")
            package = {
                "transfer_id": "trf-456",
                "source_agent": "lily",
                "source_instance": "HASHI1",
                "target_agent": "hashiko",
                "target_instance": "HASHI9",
                "created_at": "2026-03-27T14:20:00",
                "recent_context_block": "ctx",
                "last_user_message": "u",
                "last_assistant_message": "a",
            }
            store.create_transfer(package, status="received")
            store.append_event("trf-456", "received", {"step": 1})
            store.update_transfer("trf-456", status="accepted", request_id="req-0010", ack_text="TRANSFER_ACCEPTED trf-456")
            record = store.get_transfer("trf-456")
            self.assertIsNotNone(record)
            self.assertEqual(record["status"], "accepted")
            self.assertEqual(record["request_id"], "req-0010")
            self.assertEqual(record["events"][0]["event_type"], "received")
            store.update_package("trf-456", {**package, "handoff_summary": "summary"})
            record = store.get_transfer("trf-456")
            self.assertEqual(record["package"]["handoff_summary"], "summary")

    def test_supported_commands_includes_transfer(self):
        commands = supported_commands(_TransferRuntime())
        self.assertIn("transfer", commands)

    def test_transfer_prompt_contains_identity_guard_and_ack(self):
        server = WorkbenchApiServer.__new__(WorkbenchApiServer)
        server.TRANSFER_ACCEPT_PREFIX = "TRANSFER_ACCEPTED "
        prompt = server._build_transfer_prompt(
            {
                "transfer_id": "trf-789",
                "source_agent": "lily",
                "source_instance": "HASHI1",
                "target_agent": "hashiko",
                "target_instance": "HASHI9",
                "created_at": "2026-03-27T14:20:00",
                "exchange_count": 2,
                "word_count": 42,
                "last_user_message": "Continue phase 2",
                "last_assistant_message": "I was editing the API layer",
                "recent_context_block": "Exchange 1:\nUSER: x\nASSISTANT: y",
                "transfer_guidance": {
                    "recent_turn_weighting": "Prefer the newest exchanges.",
                    "older_turn_weighting": "Treat older exchanges as background.",
                    "conflict_rule": "Prefer newer context on conflict.",
                },
                "task_state": {
                    "latest_user_request": "Continue phase 2",
                    "latest_source_reply": "I was editing the API layer",
                    "recent_exchange_count": 2,
                    "memory_files_available": ["tasks.md"],
                },
                "handoff_summary": "# Handoff Summary",
                "memory_files": {"tasks.md": "- finish API layer"},
            }
        )
        self.assertIn("You are NOT lily", prompt)
        self.assertIn("TRANSFER_ACCEPTED trf-789", prompt)
        self.assertIn("Continue directly from the next unfinished step", prompt)
        self.assertIn("CONTEXT WEIGHTING RULES", prompt)
        self.assertIn("Prefer newer context on conflict", prompt)

    def test_transfer_ack_classification_supports_implicit_ack(self):
        server = WorkbenchApiServer.__new__(WorkbenchApiServer)
        server.TRANSFER_ACCEPT_PREFIX = "TRANSFER_ACCEPTED "
        explicit = server._classify_transfer_ack("trf-789", {"success": True, "text": "TRANSFER_ACCEPTED trf-789\nContinuing now"})
        implicit = server._classify_transfer_ack("trf-789", {"success": True, "text": "I have the context and will continue phase 2 now."})
        self.assertTrue(explicit["ok"])
        self.assertEqual(explicit["ack_mode"], "explicit")
        self.assertTrue(implicit["ok"])
        self.assertEqual(implicit["ack_mode"], "implicit")

    def test_transfer_status_degrades_when_target_chat_offline(self):
        server = WorkbenchApiServer.__new__(WorkbenchApiServer)
        status, target_chat_status = server._finalize_transfer_status(
            {"delivered": True},
            {"delivered": False, "reason": "telegram_disconnected"},
        )
        self.assertEqual(status, "accepted_but_chat_offline")
        self.assertEqual(target_chat_status, "offline")


if __name__ == "__main__":
    unittest.main()
