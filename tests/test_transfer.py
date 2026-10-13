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
            self.assertEqual(package["last_user_message"], "Please continue the parser refactor")
            self.assertEqual(package["last_assistant_message"], "I updated the tokenizer and was about to fix tests.")
            self.assertEqual(package["exchange_count"], 1)
            self.assertIn("RECENT CONVERSATION HANDOFF", package["recent_context_block"])
            self.assertIn("transfer_guidance", package)
            self.assertIn("task_state", package)

    def test_transfer_store_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TransferStore(Path(tmp) / "bridge_transfers.sqlite") as store:
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
                store.update_transfer(
                    "trf-456",
                    status="accepted",
                    request_id="req-0010",
                    ack_text="TRANSFER_ACCEPTED trf-456",
                )
                record = store.get_transfer("trf-456")
                self.assertIsNotNone(record)
                self.assertEqual(record["status"], "accepted")
                self.assertEqual(record["request_id"], "req-0010")
                self.assertEqual(record["events"][0]["event_type"], "received")
                store.update_package(
                    "trf-456", {**package, "handoff_summary": "summary"}
                )
                record = store.get_transfer("trf-456")
                self.assertEqual(record["package"]["handoff_summary"], "summary")
            store.close()

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

    def test_transfer_status_keeps_model_ack_authoritative_when_telegram_is_unavailable(self):
        server = WorkbenchApiServer.__new__(WorkbenchApiServer)
        status, telegram_notification = server._finalize_transfer_status(
            {"delivered": True},
            {"delivered": False, "reason": "telegram_disconnected"},
        )
        self.assertEqual(status, "accepted")
        self.assertEqual(telegram_notification["channel"], "telegram")
        self.assertEqual(telegram_notification["status"], "partial")
        self.assertEqual(
            telegram_notification["accepted"]["reason"],
            "telegram_disconnected",
        )


class TransferHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_ack_accepts_transfer_even_when_telegram_notices_are_not_delivered(self):
        package = {
            "transfer_id": "trf-accepted-without-telegram",
            "source_agent": "source",
            "source_instance": "HASHI2",
            "target_agent": "target",
            "target_instance": "HASHI3",
            "created_at": "2026-10-03T07:43:09+00:00",
            "recent_context_block": "ctx",
            "last_user_message": "u",
            "last_assistant_message": "a",
        }

        class _Request:
            async def json(self):
                return dict(package)

        class _Runtime:
            startup_success = True

            def has_active_transfer(self):
                return False

            async def enqueue_api_text(self, *_args, **_kwargs):
                return "req-transfer-accepted"

            def register_request_listener(self, _request_id, listener):
                import asyncio

                asyncio.create_task(
                    listener(
                        {
                            "success": True,
                            "text": (
                                "TRANSFER_ACCEPTED trf-accepted-without-telegram\n"
                                "Continuing the transferred task."
                            ),
                        }
                    )
                )

        notices = iter(
            [
                {"delivered": False, "reason": "telegram_disconnected", "chunks": 0},
                {"delivered": False, "reason": "telegram_disconnected", "chunks": 0},
            ]
        )

        async def _notify(*_args, **_kwargs):
            return next(notices)

        with tempfile.TemporaryDirectory() as tmp:
            store = TransferStore(Path(tmp) / "bridge_transfers.sqlite")
            server = WorkbenchApiServer.__new__(WorkbenchApiServer)
            server.TRANSFER_ACCEPT_PREFIX = "TRANSFER_ACCEPTED "
            server.global_config = SimpleNamespace(instance_id="HASHI3")
            server.transfer_store = store
            server._validate_transfer_payload = lambda payload: dict(payload)
            server._runtime_map = lambda: {"target": _Runtime()}
            server._notify_transfer_chat = _notify
            server._build_transfer_prompt = lambda _package: "transfer prompt"

            response = await server.handle_bridge_transfer(_Request())
            body = json.loads(response.text)
            record = store.get_transfer(package["transfer_id"])
            store.close()

        self.assertEqual(response.status, 200)
        self.assertEqual(body["status"], "accepted")
        self.assertNotIn("target_chat_status", body)
        self.assertEqual(
            body["telegram_notification"]["status"], "not_delivered"
        )
        self.assertEqual(record["status"], "accepted")
        accepted = next(
            event for event in record["events"] if event["event_type"] == "accepted"
        )
        self.assertEqual(
            accepted["details"]["telegram_notification"],
            body["telegram_notification"],
        )

    async def test_target_enqueue_exception_preserves_unknown_receipt_as_json(self):
        package = {
            "transfer_id": "trf-enqueue-unknown",
            "source_agent": "source",
            "source_instance": "HASHI2",
            "target_agent": "target",
            "target_instance": "HASHI3",
            "created_at": "2026-10-03T07:43:09+00:00",
            "recent_context_block": "ctx",
            "last_user_message": "u",
            "last_assistant_message": "a",
        }

        class _Request:
            async def json(self):
                return dict(package)

        class _Runtime:
            startup_success = True

            def has_active_transfer(self):
                return False

            async def enqueue_api_text(self, *_args, **_kwargs):
                raise ValueError("unregistered frontend connector")

        async def _notify(*_args, **_kwargs):
            return {"delivered": False, "reason": "telegram_disconnected", "chunks": 0}

        with tempfile.TemporaryDirectory() as tmp:
            store = TransferStore(Path(tmp) / "bridge_transfers.sqlite")
            server = WorkbenchApiServer.__new__(WorkbenchApiServer)
            server.global_config = SimpleNamespace(instance_id="HASHI3")
            server.transfer_store = store
            server._validate_transfer_payload = lambda payload: dict(payload)
            server._runtime_map = lambda: {"target": _Runtime()}
            server._notify_transfer_chat = _notify
            server._build_transfer_prompt = lambda _package: "transfer prompt"

            response = await server.handle_bridge_transfer(_Request())
            body = json.loads(response.text)
            record = store.get_transfer(package["transfer_id"])
            store.close()

        self.assertEqual(response.status, 503)
        self.assertEqual(body["error_code"], "enqueue_outcome_unknown")
        self.assertNotIn("redirect", body)
        self.assertEqual(record["status"], "received")
        self.assertEqual(record["error_code"], "enqueue_outcome_unknown")
        self.assertIsNone(record["request_id"])
        self.assertEqual(
            [event["event_type"] for event in record["events"]],
            ["received", "incoming_notice", "enqueue_outcome_unknown"],
        )
        event_types = [event["event_type"] for event in record["events"]]
        self.assertNotIn("queued_on_target", event_types)
        self.assertNotIn("failed", event_types)


if __name__ == "__main__":
    unittest.main()
