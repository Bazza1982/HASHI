"""Comprehensive integration tests for LiveVoiceManager and WorkbenchApiServer live voice routes."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from orchestrator.frontend_live_voice.manager import LiveVoiceManager
from orchestrator.frontend_live_voice.protocol import CallBinding, Fragment, LiveVoiceError
from orchestrator.frontend_live_voice.routes import register_live_voice_routes
from orchestrator.session_store import SessionStore


PHONE_REVISION = "a" * 64


def resolved_phone_session(_agent_id="zelda", **_scope):
    return {
        "provider": "openai",
        "model": "gpt-live-1",
        "voice": "willow",
        "instructions": "Safe projected test Persona with explicit client delegation.",
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "HCC test context."}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Remember our last turn."}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "I remember it."}],
            },
        ],
        "public": {
            "revision": PHONE_REVISION,
            "provider": "openai",
            "provider_label": "OpenAI",
            "model": "gpt-live-1",
            "model_label": "GPT Live 1",
            "voice": "willow",
            "voice_label": "Willow",
            "voice_presentation": "feminine",
            "language": "auto",
            "language_label": "Automatic",
            "style": "natural",
            "style_label": "Natural",
            "custom_style": False,
            "persona_projected": True,
        },
    }


class LiveVoiceManagerStoreTests(unittest.TestCase):
    async def _admit_run(self, binding, proposal, idempotency_key):
        self.admit_calls.append(idempotency_key)
        accepted = self.store.accept_run(
            session_id=binding.session_id,
            owner_id=binding.owner_id,
            agent_id=binding.agent_id,
            request_id=f"req-{idempotency_key}",
            text=proposal.text,
            source="session-api",
            idempotency_key=idempotency_key,
            expected_context_generation=binding.context_generation,
        )
        return {
            "request_id": accepted.request_id,
            "run_id": accepted.run_id,
            "message_id": accepted.message_id,
        }

    def setUp(self):
        self.admit_calls = []
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = Path(self.temp_dir) / "state" / "sessions.sqlite3"
        self.store = SessionStore(self.db_path, instance_id="HASHI")
        self.owner_id = "test-owner"
        self.agent_id = "zelda"
        self.session = self.store.create_session(
            owner_id=self.owner_id,
            agent_id=self.agent_id,
            title="Live Voice Test Session",
        )
        self.session_id = self.session["session_id"]
        self.global_config = type("GlobalConfig", (), {
            "instance_id": "HASHI",
            "instance_generation": "1",
            "live_voice_v1": True,
        })()
        self.manager = LiveVoiceManager(
            session_store=self.store,
            global_config=self.global_config,
            secrets={"openai_api_key": "sk-test-fake"},
            admit_run=self._admit_run,
            resolve_phone_session=resolved_phone_session,
            proposal_grace_seconds=0,
            close_timeout_seconds=0.01,
        )
        self.call_id = f"call-{uuid4().hex[:12]}"
        self.binding = CallBinding(
            owner_id=self.owner_id,
            instance_id="HASHI",
            instance_generation="1",
            agent_id=self.agent_id,
            session_id=self.session_id,
            context_generation=1,
            call_id=self.call_id,
            call_epoch=1,
            provider_session_id="prov-1",
        )

        with self.store._lock, self.store._connection() as conn:
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            conn.execute(
                """
                INSERT INTO live_calls(
                    call_id, owner_id, session_id, agent_id, instance_id,
                    instance_generation, context_generation, call_epoch,
                    provider_session_id, phase, controller_lease, lease_expiry, started_at
                    , max_ends_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    self.call_id,
                    self.owner_id,
                    self.session_id,
                    self.agent_id,
                    "HASHI",
                    "1",
                    1,
                    1,
                    "prov-1",
                    "lease-1",
                    "2099-01-01T00:00:00Z",
                    now_iso,
                    "2099-01-01T00:00:00Z",
                ),
            )
        self.scope = self.binding.public_scope()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_op_context(self):
        result = asyncio.run(self.manager._op_context(self.owner_id, {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "context_generation": 1,
        }))
        self.assertTrue(result["ok"])
        self.assertEqual(result["binding"]["session_id"], self.session_id)
        self.assertEqual(result["binding"]["agent_id"], self.agent_id)
        self.assertTrue(result["capability"]["available"])
        self.assertEqual(result["capability"]["phone"]["voice"], "willow")
        self.assertEqual(result["capability"]["phone"]["voice_presentation"], "feminine")

    def test_append_fragment_deduplication(self):
        frag = Fragment(provider_event_id="evt-1", speaker="user", text="hello world", start_ms=0, end_ms=500)
        res1 = asyncio.run(self.manager.append_fragment_once(self.binding, frag))
        self.assertTrue(bool(res1))
        self.assertEqual(res1["kind"], "voice.live.transcript.fragment")

        res2 = asyncio.run(self.manager.append_fragment_once(self.binding, frag))
        self.assertEqual(res2, {})

        with self.store._lock, self.store._connection() as conn:
            rows = conn.execute("SELECT * FROM live_fragments WHERE call_id = ?", (self.call_id,)).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertNotIn("text", rows[0].keys())

    def test_delegation_and_proposal_lifecycle(self):
        frag1 = Fragment("e1", "user", "build ", 0, 500)
        frag2 = Fragment("e2", "user", "a website", 500, 1000)
        asyncio.run(self.manager.append_fragment_once(self.binding, frag1))
        asyncio.run(self.manager.append_fragment_once(self.binding, frag2))

        reg1 = asyncio.run(self.manager.register_delegation_once(self.binding, "del-1", 1200))
        self.assertTrue(reg1)
        reg2 = asyncio.run(self.manager.register_delegation_once(self.binding, "del-1", 1200))
        self.assertFalse(reg2)

        asyncio.run(self.manager.schedule_proposal(self.binding, "del-1", 1200))
        proposal = asyncio.run(self.manager.read_proposal(self.binding, "del-1"))
        self.assertEqual(proposal.delegation_id, "del-1")
        self.assertEqual(proposal.text, "build a website")
        self.assertFalse(proposal.ambiguous)
        self.assertEqual(len(proposal.source_event_ids), 2)
        self.assertEqual(len(self.admit_calls), 1)
        self.assertTrue(self.admit_calls[0].startswith("live-delegation-live-auto-"))
        with self.store._lock, self.store._connection() as conn:
            row = conn.execute(
                "SELECT decision, accepted_run_id FROM live_delegations WHERE call_id = ? AND delegation_id = ?",
                (self.call_id, "del-1"),
            ).fetchone()
        self.assertEqual(row["decision"], "admitted")
        self.assertTrue(row["accepted_run_id"])

    def test_automatic_admission_is_idempotent(self):
        frag = Fragment("e1", "user", "deploy to production", 0, 500)
        asyncio.run(self.manager.append_fragment_once(self.binding, frag))
        asyncio.run(self.manager.register_delegation_once(self.binding, "del-deploy", 600))
        asyncio.run(self.manager.schedule_proposal(self.binding, "del-deploy", 600))
        asyncio.run(self.manager.schedule_proposal(self.binding, "del-deploy", 600))
        self.assertEqual(len(self.admit_calls), 1)
        with self.store._lock, self.store._connection() as conn:
            receipt = conn.execute(
                "SELECT operation, receipt_json FROM live_control_receipts WHERE call_id = ?",
                (self.call_id,),
            ).fetchone()
        self.assertEqual(receipt["operation"], "admission")
        self.assertTrue(json.loads(receipt["receipt_json"])["accepted"])

    def test_ambiguous_spoken_request_is_not_admitted(self):
        fragment = Fragment("crosses-cutoff", "user", "check the", 500, 700)
        asyncio.run(self.manager.append_fragment_once(self.binding, fragment))
        asyncio.run(self.manager.register_delegation_once(self.binding, "del-ambiguous", 600))
        asyncio.run(self.manager.schedule_proposal(self.binding, "del-ambiguous", 600))

        proposal = asyncio.run(self.manager.read_proposal(self.binding, "del-ambiguous"))
        self.assertTrue(proposal.ambiguous)
        self.assertEqual(self.admit_calls, [])

    def test_phone_specific_decision_operation_is_not_exposed(self):
        with self.assertRaises(LiveVoiceError) as caught:
            asyncio.run(self.manager.invoke("decision", {"owner_id": self.owner_id}, self.scope))
        self.assertEqual(caught.exception.code, "live_operation_unsupported")

    def test_control_operations(self):
        hb = asyncio.run(self.manager._op_control(self.owner_id, {
            **self.scope, "action": "heartbeat", "idempotency_key": "ctrl-hb-1",
        }))
        self.assertTrue(hb["ok"])
        self.assertTrue(hb["renewed"])

        mute = asyncio.run(self.manager._op_control(self.owner_id, {
            **self.scope, "action": "mute", "idempotency_key": "ctrl-mute-1",
        }))
        self.assertTrue(mute["ok"])
        self.assertFalse(mute["applied"])

        fault = asyncio.run(self.manager._op_control(self.owner_id, {
            **self.scope, "action": "end", "idempotency_key": "ctrl-fault-1",
            "termination_initiator": "client_fault", "termination_reason": "media_track_ended",
        }))
        self.assertTrue(fault["ok"])
        self.assertFalse(fault["applied"])
        self.assertEqual(fault["phase"], "recovering")
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "recovering")
        self.assertTrue(snap["snapshot"]["resume_required"])

    def test_stale_transport_fault_cannot_recover_a_newer_call_epoch(self):
        with self.store._lock, self.store._connection() as connection:
            connection.execute(
                "UPDATE live_calls SET call_epoch = 2, provider_session_id = 'prov-new', phase = 'active' WHERE call_id = ?",
                (self.call_id,),
            )
        with self.assertRaisesRegex(LiveVoiceError, "live_transport_epoch_changed"):
            asyncio.run(self.manager._op_control(self.owner_id, {
                **self.scope,
                "action": "end",
                "idempotency_key": "stale-transport-fault",
                "termination_initiator": "client_fault",
                "termination_reason": "network_offline",
            }))
        with self.store._lock, self.store._connection() as connection:
            row = connection.execute(
                "SELECT phase, call_epoch, provider_session_id FROM live_calls WHERE call_id = ?",
                (self.call_id,),
            ).fetchone()
        self.assertEqual((row["phase"], row["call_epoch"], row["provider_session_id"]),
                         ("active", 2, "prov-new"))

    def test_passive_start_cancel_retains_the_logical_call_for_recovery(self):
        attempt_id = "passive-start-cancel"
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self.store._lock, self.store._connection() as connection:
            connection.execute(
                """INSERT INTO live_call_attempts(
                     attempt_id, owner_id, session_id, agent_id, instance_id,
                     instance_generation, context_generation, request_digest,
                     state, provider_id, call_id, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'provider_created', ?, ?, ?, ?)""",
                (attempt_id, self.owner_id, self.session_id, self.agent_id, "HASHI", "1", 1,
                 "digest", "prov-1", self.call_id, now, now),
            )
        result = asyncio.run(self.manager._op_cancel_start(self.owner_id, {
            **self.scope,
            "idempotency_key": attempt_id,
            "termination_initiator": "client_fault",
            "termination_reason": "startup_failed",
        }))
        self.assertFalse(result["cancelled"])
        self.assertEqual(result["phase"], "recovering")
        with self.store._lock, self.store._connection() as connection:
            row = connection.execute(
                "SELECT phase, ended_at, termination_initiator, termination_reason FROM live_calls WHERE call_id = ?",
                (self.call_id,),
            ).fetchone()
        self.assertEqual(row["phase"], "recovering")
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["termination_initiator"])
        self.assertIsNone(row["termination_reason"])

    def test_provider_rollover_recovers_before_transport_deadline(self):
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")
        with self.store._lock, self.store._connection() as connection:
            connection.execute(
                "UPDATE live_calls SET max_ends_at = ? WHERE call_id = ?", (deadline, self.call_id)
            )
        asyncio.run(self.manager._sweep_expiring_transports())
        with self.store._lock, self.store._connection() as connection:
            row = connection.execute(
                "SELECT phase, ended_at FROM live_calls WHERE call_id = ?", (self.call_id,)
            ).fetchone()
            events = connection.execute(
                "SELECT detail_json FROM run_events WHERE session_id = ? AND kind = 'voice.live.call.state'",
                (self.session_id,),
            ).fetchall()
        self.assertEqual(row["phase"], "recovering")
        self.assertIsNone(row["ended_at"])
        self.assertTrue(any(json.loads(event["detail_json"]).get("reason") == "provider_rollover_due"
                            for event in events))

    def test_events_reading(self):
        frag = Fragment("e1", "user", "check status", 0, 500)
        asyncio.run(self.manager.append_fragment_once(self.binding, frag))

        events_res = asyncio.run(self.manager._op_events(self.owner_id, {
            **self.scope,
            "after": 0,
            "limit": 10,
        }))
        self.assertTrue(events_res["ok"])
        self.assertGreater(len(events_res["events"]), 0)
        found_frag = any(e["kind"] == "transcript.fragment" for e in events_res["events"])
        self.assertTrue(found_frag)

    def test_event_feed_suppresses_superseded_terminal_event_during_recovery(self):
        with self.store._lock, self.store._connection() as conn:
            conn.execute(
                "UPDATE live_calls SET phase = 'recovering', ended_at = NULL WHERE call_id = ?",
                (self.call_id,),
            )
            self.store._append_event(
                conn, session_id=self.session_id, run_id=None,
                kind="voice.live.call.state", summary="Legacy passive close",
                detail={"scope": self.scope, "phase": "interrupted", "reason": "legacy_close"},
            )
            recovery = self.store._append_event(
                conn, session_id=self.session_id, run_id=None,
                kind="voice.live.call.state", summary="Legacy recovery",
                detail={"scope": self.scope, "phase": "recovering",
                        "reason": "legacy_terminal_recovery"},
            )

        result = asyncio.run(self.manager._op_events(self.owner_id, {
            **self.scope, "after": 0, "limit": 20,
        }))
        self.assertEqual(
            [event["detail"].get("phase") for event in result["events"]],
            ["recovering"],
        )
        self.assertEqual(result["next_after"], recovery["sequence"])

    def test_event_feed_keeps_user_hangup_added_after_stale_phase_read(self):
        with self.store._lock, self.store._connection() as conn:
            conn.execute(
                "UPDATE live_calls SET phase = 'ended', ended_at = ? WHERE call_id = ?",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), self.call_id),
            )
            ended = self.store._append_event(
                conn, session_id=self.session_id, run_id=None,
                kind="voice.live.call.state", summary="User ended the call",
                detail={"scope": self.scope, "phase": "ended",
                        "termination_initiator": "user", "termination_reason": "user_hangup"},
            )

        original = self.manager._call_binding

        def stale_phase(*args, **kwargs):
            binding, row = original(*args, **kwargs)
            stale = dict(row)
            stale["phase"] = "recovering"
            return binding, stale

        self.manager._call_binding = stale_phase
        result = asyncio.run(self.manager._op_events(self.owner_id, {
            **self.scope, "after": 0, "limit": 20,
        }))
        self.assertIn(
            ended["sequence"],
            [event["sequence"] for event in result["events"]],
        )
        self.assertEqual(result["next_after"], ended["sequence"])

    def test_event_pagination_never_skips_scanned_matching_rows(self):
        for index in range(5):
            asyncio.run(self.manager.append_fragment_once(
                self.binding, Fragment(f"page-{index}", "user", str(index), index, index + 1)
            ))
        after = 0
        seen = []
        for _ in range(10):
            page = asyncio.run(self.manager._op_events(self.owner_id, {
                **self.scope, "after": after, "limit": 2,
            }))
            seen.extend(
                item["detail"].get("provider_event_id")
                for item in page["events"] if item["kind"] == "transcript.fragment"
            )
            if not page["events"]:
                break
            self.assertGreater(page["next_after"], after)
            after = page["next_after"]
        self.assertEqual(seen, [f"page-{index}" for index in range(5)])

    def test_proposal_grace_does_not_block_late_sideband_fragments(self):
        async def scenario():
            self.manager._proposal_grace_seconds = 0.05
            await self.manager.service.on_provider_event(self.binding, {
                "type": "session.delegation.created",
                "event_id": "grace-event-1",
                "offset_ms": 1000,
                "delegation": {"id": "grace-delegation", "target": "client"},
            })
            await self.manager.append_fragment_once(
                self.binding, Fragment("late-during-grace", "user", "late words", 500, 900)
            )
            await asyncio.sleep(0.08)
            return await self.manager.read_proposal(self.binding, "grace-delegation")

        proposal = asyncio.run(scenario())
        self.assertEqual(proposal.text, "late words")
        self.assertFalse(proposal.ambiguous)

    def test_all_call_operations_enforce_full_stored_scope(self):
        with self.assertRaises(LiveVoiceError) as caught:
            asyncio.run(self.manager._op_snapshot(self.owner_id, {
                **self.scope,
                "context_generation": 2,
            }))
        self.assertEqual(caught.exception.code, "live_scope_changed")

    def test_call_event_reader_does_not_leak_another_call(self):
        other_call = f"call-{uuid4().hex[:12]}"
        other = CallBinding(
            self.owner_id, "HASHI", "1", self.agent_id, self.session_id, 1,
            other_call, 1, "prov-2",
        )
        with self.store._lock, self.store._connection() as conn:
            conn.execute(
                """
                INSERT INTO live_calls(
                    call_id, owner_id, session_id, agent_id, instance_id,
                    instance_generation, context_generation, call_epoch,
                    provider_session_id, phase, controller_lease, lease_expiry,
                    started_at, max_ends_at, ended_at
                ) VALUES (?, ?, ?, ?, 'HASHI', '1', 1, 1, 'prov-2', 'ended',
                          'lease-2', ?, ?, ?, ?)
                """,
                (other_call, self.owner_id, self.session_id, self.agent_id,
                 "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
                 "2026-01-01T00:30:00Z", "2026-01-01T00:01:00Z"),
            )
        asyncio.run(self.manager.append_fragment_once(self.binding, Fragment("mine", "user", "mine", 0, 1)))
        asyncio.run(self.manager.append_fragment_once(other, Fragment("other", "user", "secret", 0, 1)))
        result = asyncio.run(self.manager._op_events(self.owner_id, {**self.scope, "after": 0, "limit": 50}))
        provider_ids = [event["detail"].get("provider_event_id") for event in result["events"]]
        self.assertIn("mine", provider_ids)
        self.assertNotIn("other", provider_ids)

    def test_provider_controls_wait_for_ack_and_close_confirmation(self):
        sent = []
        manager = self.manager
        binding = self.binding

        class FakeSocket:
            closed = False

            async def send_json(self, value):
                sent.append(value)
                if value["type"] in {"session.input_audio.mute", "session.input_audio.unmute"}:
                    manager._control_waiters[(binding.call_id, value["event_id"])].set_result(True)
                elif value["type"] == "session.close":
                    manager._mark_terminal(
                        binding, "ended", provider_close_state="confirmed",
                        summary="Live call ended", usage={"input_tokens": 12},
                    )
                    manager._session_closed_events.setdefault(binding.call_id, asyncio.Event()).set()

        async def scenario():
            manager._active_sockets[binding.call_id] = FakeSocket()
            muted = await manager._op_control(self.owner_id, {
                **self.scope, "action": "mute", "idempotency_key": "provider-mute-1",
            })
            ended = await manager._op_control(self.owner_id, {
                **self.scope, "action": "end", "idempotency_key": "provider-end-1",
                "termination_initiator": "user", "termination_reason": "user_hangup",
            })
            return muted, ended

        muted, ended = asyncio.run(scenario())
        self.assertTrue(muted["applied"])
        self.assertEqual(sent[0]["type"], "session.input_audio.mute")
        self.assertEqual(sent[1]["type"], "session.close")
        self.assertEqual(ended["provider_close_state"], "confirmed")
        self.assertEqual(ended["usage"], {"input_tokens": 12})

    def test_startup_recovery_preserves_orphaned_provider_session(self):
        close_calls = []
        async def close_provider(key, provider_id, **_kwargs):
            close_calls.append((key, provider_id))
            return "confirmed", {"total_tokens": 7}
        self.manager._close_provider_session = close_provider
        class DisconnectedSocket:
            closed = False
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
            async def close(self): self.closed = True
        @asynccontextmanager
        async def fake_http(): yield object()
        async def fake_attach(_http, *, key, provider_session_id): return DisconnectedSocket()
        async def scenario():
            with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
                 patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
                await self.manager.start()
                await asyncio.sleep(0)
                await self.manager.shutdown()
        asyncio.run(scenario())
        self.assertEqual(close_calls, [])
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "recovering")
        self.assertEqual(snap["snapshot"]["provider_close_state"], "unconfirmed")
        self.assertTrue(snap["snapshot"]["resume_required"])

    def test_shutdown_preserves_call_and_closes_only_the_sideband_socket(self):
        sent = []
        manager = self.manager
        binding = self.binding
        class FakeSocket:
            closed = False
            async def send_json(self, value): sent.append(value)
            async def close(self): self.closed = True
        socket = FakeSocket()
        async def scenario():
            manager._active_sockets[binding.call_id] = socket
            await manager.shutdown()
        asyncio.run(scenario())
        self.assertEqual(sent, [])
        self.assertTrue(socket.closed)
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "recovering")
        self.assertEqual(snap["snapshot"]["provider_close_state"], "unconfirmed")

    def test_unexpected_sideband_disconnect_preserves_logical_call(self):
        close_calls = []
        class DisconnectedSocket:
            closed = False
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
            async def close(self): self.closed = True
        @asynccontextmanager
        async def fake_http(): yield object()
        async def fake_attach(_http, *, key, provider_session_id): return DisconnectedSocket()
        async def fake_close(key, provider_session_id, **_kwargs):
            close_calls.append((key, provider_session_id))
            return "confirmed", {"total_tokens": 3}
        self.manager._close_provider_session = fake_close
        with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
             patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
            asyncio.run(self.manager._run_sideband(self.binding, "sk-test-fake", "prov-1"))
        self.assertEqual(close_calls, [])
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "recovering")
        self.assertEqual(snap["snapshot"]["provider_close_state"], "unconfirmed")

    def test_transport_resume_fences_old_provider_and_replays_after_lost_response(self):
        with self.store._lock, self.store._connection() as connection:
            connection.execute("UPDATE live_calls SET phase = 'recovering' WHERE call_id = ?", (self.call_id,))
        manager = self.manager
        async def fit(_http, *, key, model, input_messages, required_message_count, history_unit_message_counts):
            return list(input_messages), {"input_tokens_exact": 123, "history_omitted_units": 0}
        provider_creates = []
        async def create(_http, *, key, request):
            provider_creates.append(request)
            return {"provider_session_id": "prov-2", "sdp_answer": "v=0\r\n"}
        original_sideband = manager._run_sideband
        class StaleStartedSocket:
            closed = False
            def __init__(self):
                self.items = [type("Message", (), {
                    "type": aiohttp.WSMsgType.TEXT,
                    "data": json.dumps({"type": "session.started", "event_id": "late-old-start"}),
                })()]
            def __aiter__(self): return self
            async def __anext__(self):
                if self.items:
                    return self.items.pop(0)
                raise StopAsyncIteration
            async def close(self): self.closed = True
        async def fake_attach(_http, *, key, provider_session_id):
            self.assertEqual(provider_session_id, "prov-1")
            return StaleStartedSocket()
        async def close(_key, provider_id, **_kwargs):
            self.assertEqual(provider_id, "prov-1")
            await original_sideband(self.binding, "sk-test-fake", provider_id)
            with self.store._lock, self.store._connection() as connection:
                current = connection.execute(
                    "SELECT provider_session_id, call_epoch, phase FROM live_calls WHERE call_id = ?", (self.call_id,)
                ).fetchone()
            self.assertEqual((current["provider_session_id"], current["call_epoch"], current["phase"]),
                             ("prov-1", 2, "connecting"))
            return "confirmed", None
        async def sideband(binding, _key, _provider_id):
            manager._sideband_ready_events[binding.call_id].set()
        @asynccontextmanager
        async def fake_http(): yield object()
        manager._close_provider_session = close
        manager._run_sideband = sideband
        resume_payload = {
            **self.scope, "sdp": "v=0\r\n", "idempotency_key": "resume-attempt-1",
            "phone_revision": PHONE_REVISION,
        }
        with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
             patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach), \
             patch("orchestrator.frontend_live_voice.manager.fit_live_session_input", fit), \
             patch("orchestrator.frontend_live_voice.manager.create_provider_session", create):
            result = asyncio.run(manager._op_resume(self.owner_id, resume_payload))
            replay = asyncio.run(manager._op_resume(self.owner_id, resume_payload))
        self.assertEqual(replay["sdp_answer"], result["sdp_answer"])
        self.assertEqual(len(provider_creates), 1)
        self.assertEqual(result["call_id"], self.call_id)
        self.assertEqual(result["binding"]["call_epoch"], 2)
        self.assertEqual(result["provider_session_id"], "prov-2")
        self.assertFalse(manager._mark_recovering(
            self.binding, reason="sideband_disconnected", summary="late old socket close",
        ))
        snap = asyncio.run(manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "connecting")
        self.assertEqual(snap["snapshot"]["scope"]["call_epoch"], 2)

    def test_provider_close_reason_is_preserved_in_independent_call_audit(self):
        class ProviderSocket:
            closed = False

            def __init__(self):
                self.messages = iter((type("Message", (), {
                    "type": aiohttp.WSMsgType.TEXT,
                    "data": json.dumps({
                        "type": "session.closed",
                        "event_id": "provider-close-reason-1",
                        "session": {"reason": "connection_lost"},
                    }),
                })(),))

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.messages)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

            async def close(self):
                self.closed = True

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return ProviderSocket()

        with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
             patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
            asyncio.run(self.manager._run_sideband(self.binding, "sk-test-fake", "prov-1"))

        audit_text = self.manager.audit.path_for(self.binding).read_text(encoding="utf-8")
        records = [
            json.loads(line)
            for line in audit_text.splitlines()
        ]
        closed = next(item for item in records if item["event"] == "provider.session_closed")
        self.assertEqual(closed["detail"]["provider_reason"], "connection_lost")

    def test_sideband_keeps_reading_close_while_provider_event_persistence_retries(self):
        class ProviderSocket:
            closed = False

            def __init__(self):
                self.events = iter([
                    {
                        "type": "session.input_transcript.delta",
                        "event_id": "provider-fragment-race-1",
                        "delta": "hello",
                        "start_ms": 0,
                        "end_ms": 100,
                    },
                    {"type": "session.closed", "session": {"reason": "connection_lost"}},
                ])

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    event = next(self.events)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc
                return type("Message", (), {
                    "type": aiohttp.WSMsgType.TEXT,
                    "data": json.dumps(event),
                })()

            async def close(self):
                self.closed = True

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return ProviderSocket()

        persistence_attempts = 0
        fourth_attempt = asyncio.Event()
        allow_success = asyncio.Event()
        original_append = self.manager.append_fragment_once

        async def fail_persistence(binding, fragment):
            nonlocal persistence_attempts
            persistence_attempts += 1
            if persistence_attempts >= 4:
                fourth_attempt.set()
            if not allow_success.is_set():
                raise sqlite3.IntegrityError(
                    "UNIQUE constraint failed: run_events.session_id, run_events.sequence"
                )
            return await original_append(binding, fragment)

        self.manager.append_fragment_once = fail_persistence

        async def scenario():
            with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
                 patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
                sideband = asyncio.create_task(
                    self.manager._run_sideband(self.binding, "sk-test-fake", "prov-1")
                )
                await asyncio.wait_for(sideband, timeout=3)
                await asyncio.wait_for(fourth_attempt.wait(), timeout=3)
                self.assertTrue(sideband.done(), "sideband must read session.closed during retries")
                records = [
                    json.loads(line)
                    for line in self.manager.audit.path_for(self.binding).read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]
                self.assertTrue(any(item["event"] == "provider.session_closed" for item in records))
                allow_success.set()
                await asyncio.wait_for(self.manager._drain_provider_events(self.call_id), timeout=3)
                worker = self.manager._provider_event_tasks.get(self.call_id)
                if worker is not None:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(scenario())

        audit_text = self.manager.audit.path_for(self.binding).read_text(encoding="utf-8")
        self.assertNotIn("hello", audit_text)
        records = [json.loads(line) for line in audit_text.splitlines()]
        failures = [item for item in records if item["event"] == "provider.event_processing_failed"]
        recovered = [item for item in records if item["event"] == "provider.event_persistence_recovered"]
        with self.store._lock, self.store._connection() as conn:
            fragment_count = conn.execute(
                "SELECT COUNT(*) FROM live_fragments WHERE provider_event_id = ?",
                ("provider-fragment-race-1",),
            ).fetchone()[0]
        self.assertEqual(fragment_count, 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["detail"]["attempt"], 1)
        self.assertEqual(failures[0]["detail"]["exception_type"], "IntegrityError")
        self.assertIn("run_events.sequence", failures[0]["detail"]["exception_message"])
        self.assertEqual(recovered[-1]["detail"]["attempts"], 5)
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "recovering")

    def test_provider_event_persistence_survives_sideband_cancellation(self):
        class ProviderSocket:
            closed = False

            def __init__(self):
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    await asyncio.Future()
                self.sent = True
                return type("Message", (), {
                    "type": aiohttp.WSMsgType.TEXT,
                    "data": json.dumps({
                        "type": "session.input_transcript.delta",
                        "event_id": "provider-fragment-rollover-1",
                        "delta": "still saved",
                        "start_ms": 100,
                        "end_ms": 200,
                    }),
                })()

            async def close(self):
                self.closed = True

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return ProviderSocket()

        first_attempt = asyncio.Event()
        allow_success = asyncio.Event()
        original_append = self.manager.append_fragment_once

        async def flaky_persistence(binding, fragment):
            first_attempt.set()
            if not allow_success.is_set():
                raise sqlite3.IntegrityError("temporary database write failure")
            return await original_append(binding, fragment)

        self.manager.append_fragment_once = flaky_persistence

        async def scenario():
            with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
                 patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
                sideband = asyncio.create_task(
                    self.manager._run_sideband(self.binding, "sk-test-fake", "prov-1")
                )
                await asyncio.wait_for(first_attempt.wait(), timeout=3)
                sideband.cancel()
                await asyncio.gather(sideband, return_exceptions=True)
                worker = self.manager._provider_event_tasks.get(self.call_id)
                self.assertIsNotNone(worker)
                self.assertFalse(worker.done(), "transport cancellation must not cancel persistence")
                allow_success.set()
                await asyncio.wait_for(self.manager._drain_provider_events(self.call_id), timeout=3)
                worker = self.manager._provider_event_tasks.get(self.call_id)
                if worker is not None:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(scenario())

        with self.store._lock, self.store._connection() as conn:
            fragment_count = conn.execute(
                "SELECT COUNT(*) FROM live_fragments WHERE provider_event_id = ?",
                ("provider-fragment-rollover-1",),
            ).fetchone()[0]
        self.assertEqual(fragment_count, 1)
        audit_text = self.manager.audit.path_for(self.binding).read_text(encoding="utf-8")
        self.assertNotIn("still saved", audit_text)

    def test_staged_provider_transcript_replays_after_manager_replacement(self):
        first_attempt = asyncio.Event()

        async def fail_append(_binding, _fragment):
            first_attempt.set()
            raise sqlite3.IntegrityError("temporary Session projection failure")

        self.manager.append_fragment_once = fail_append
        event = {
            "type": "session.input_transcript.delta",
            "event_id": "provider-fragment-replay-1",
            "delta": "recover this after replacement",
            "start_ms": 300,
            "end_ms": 400,
        }

        async def scenario():
            self.manager._enqueue_provider_event(self.binding, event)
            await asyncio.wait_for(first_attempt.wait(), timeout=3)
            pending = self.store.pending_live_provider_fragments()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["provider_event_id"], "provider-fragment-replay-1")
            worker = self.manager._provider_event_tasks.get(self.call_id)
            self.assertIsNotNone(worker)
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

            replacement = LiveVoiceManager(
                session_store=self.store,
                global_config=self.global_config,
                secrets={"openai_api_key": "sk-test-fake"},
                resolve_phone_session=resolved_phone_session,
            )
            replacement._requeue_pending_provider_events()
            await asyncio.wait_for(replacement._drain_provider_events(self.call_id), timeout=3)
            worker = replacement._provider_event_tasks.get(self.call_id)
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(scenario())

        with self.store._lock, self.store._connection() as conn:
            fragments = conn.execute(
                "SELECT COUNT(*) FROM live_fragments WHERE provider_event_id = ?",
                ("provider-fragment-replay-1",),
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM live_provider_event_inbox WHERE provider_event_id = ?",
                ("provider-fragment-replay-1",),
            ).fetchone()[0]
        self.assertEqual(fragments, 1)
        self.assertEqual(pending, 0)
        audit_text = self.manager.audit.path_for(self.binding).read_text(encoding="utf-8")
        self.assertNotIn("recover this after replacement", audit_text)

    def test_sideband_stages_transcript_before_process_queue_for_recovery(self):
        class ProviderSocket:
            closed = False

            def __init__(self):
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    await asyncio.Future()
                self.sent = True
                return type("Message", (), {
                    "type": aiohttp.WSMsgType.TEXT,
                    "data": json.dumps({
                        "type": "session.input_transcript.delta",
                        "event_id": "provider-fragment-stage-before-queue",
                        "delta": "staged before process queue",
                        "start_ms": 510,
                        "end_ms": 620,
                    }),
                })()

            async def close(self):
                self.closed = True

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return ProviderSocket()

        staged_before_queue = asyncio.Event()
        original_enqueue = self.manager._enqueue_provider_event

        def simulate_replacement_after_durable_stage(binding, event):
            pending = self.store.pending_live_provider_events()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["provider_event_id"], event["event_id"])
            staged_before_queue.set()
            # Model Function replacement in the exact gap after durable stage
            # and before the in-memory projection queue accepts this event.

        self.manager._enqueue_provider_event = simulate_replacement_after_durable_stage

        async def scenario():
            with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
                 patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
                sideband = asyncio.create_task(
                    self.manager._run_sideband(self.binding, "sk-test-fake", "prov-1")
                )
                await asyncio.wait_for(staged_before_queue.wait(), timeout=3)
                sideband.cancel()
                await asyncio.gather(sideband, return_exceptions=True)

            replacement = LiveVoiceManager(
                session_store=self.store,
                global_config=self.global_config,
                secrets={"openai_api_key": "sk-test-fake"},
                resolve_phone_session=resolved_phone_session,
            )
            replacement._requeue_pending_provider_events()
            await asyncio.wait_for(replacement._drain_provider_events(self.call_id), timeout=3)
            worker = replacement._provider_event_tasks.get(self.call_id)
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(scenario())
        self.manager._enqueue_provider_event = original_enqueue

        with self.store._lock, self.store._connection() as conn:
            projected = conn.execute(
                "SELECT COUNT(*) FROM live_fragments WHERE provider_event_id = ?",
                ("provider-fragment-stage-before-queue",),
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM live_provider_event_inbox WHERE provider_event_id = ?",
                ("provider-fragment-stage-before-queue",),
            ).fetchone()[0]
        self.assertEqual(projected, 1)
        self.assertEqual(pending, 0)

    def test_invalid_provider_content_is_logged_and_does_not_stall_sideband(self):
        class ProviderSocket:
            closed = False

            def __init__(self):
                self.messages = iter([
                    {
                        "type": "session.input_transcript.delta",
                        "event_id": "invalid-transcript-1",
                        "delta": "never-write-this-transcript-marker",
                        "start_ms": 1,
                        "end_ms": "not-a-timestamp",
                    },
                    {
                        "type": "session.delegation.created",
                        "event_id": "invalid-delegation-event-1",
                        "delegation": {"target": "client", "id": ""},
                        "offset_ms": 10,
                    },
                    {"type": "session.closed", "event_id": "provider-closed-1"},
                ])

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    event = next(self.messages)
                except StopIteration:
                    raise StopAsyncIteration
                return type("Message", (), {
                    "type": aiohttp.WSMsgType.TEXT,
                    "data": json.dumps(event),
                })()

            async def close(self):
                self.closed = True

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return ProviderSocket()

        async def scenario():
            with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
                 patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
                await asyncio.wait_for(
                    self.manager._run_sideband(self.binding, "sk-test-fake", "prov-1"),
                    timeout=3,
                )

        asyncio.run(scenario())
        audit_text = self.manager.audit.path_for(self.binding).read_text(encoding="utf-8")
        records = [json.loads(line) for line in audit_text.splitlines()]
        rejected = [item for item in records if item["event"] == "provider.event_staging_rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(any(item["event"] == "provider.session_closed" for item in records))
        self.assertNotIn("never-write-this-transcript-marker", audit_text)

    def test_mixed_provider_inbox_replays_in_one_persisted_order(self):
        with self.store._lock, self.store._connection() as conn:
            conn.execute(
                """INSERT INTO live_provider_delegation_inbox(
                       owner_id, provider_event_id, call_id, call_epoch, session_id,
                       delegation_id, offset_ms, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.owner_id, "legacy-delegation-1", self.call_id, 1,
                 self.session_id, "delegation-1", 100, "2026-09-30T01:00:00.000001Z"),
            )
            conn.execute(
                """INSERT INTO live_provider_fragment_inbox(
                       owner_id, provider_event_id, call_id, call_epoch, session_id,
                       speaker, text, start_ms, end_ms, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (self.owner_id, "legacy-transcript-2", self.call_id, 1,
                 self.session_id, "user", "later transcript", 110, 120,
                 "2026-09-30T01:00:00.000002Z"),
            )
        reopened_store = SessionStore(self.db_path, instance_id="HASHI")
        pending = reopened_store.pending_live_provider_events()
        self.assertEqual(
            [(item["event_type"], item["provider_event_id"]) for item in pending],
            [("delegation", "legacy-delegation-1"), ("transcript", "legacy-transcript-2")],
        )

        async def scenario():
            replacement = LiveVoiceManager(
                session_store=reopened_store,
                global_config=self.global_config,
                secrets={"openai_api_key": "sk-test-fake"},
                resolve_phone_session=resolved_phone_session,
            )
            replacement._requeue_pending_provider_events()
            queue = replacement._provider_event_queues[self.call_id]
            events = [queue.get_nowait()[1] for _ in range(2)]
            self.assertEqual(
                [event["event_id"] for event in events],
                ["legacy-delegation-1", "legacy-transcript-2"],
            )
            worker = replacement._provider_event_tasks.get(self.call_id)
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(scenario())

    def test_provider_rest_cleanup_confirms_close_without_asyncio_timeout_context(self):
        class ProviderSocket:
            closed = False

            async def send_json(self, _payload):
                return None

            def __aiter__(self):
                self.sent = False
                return self

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return type("Message", (), {
                    "type": aiohttp.WSMsgType.TEXT,
                    "data": json.dumps({
                        "type": "session.closed",
                        "session": {"reason": "user_hangup", "usage": {"total_tokens": 7}},
                    }),
                })()

            async def close(self):
                self.closed = True

        socket = ProviderSocket()

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return socket

        async def scenario():
            with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
                 patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
                return await self.manager._close_provider_session(
                    "sk-test-fake", "prov-1", binding=self.binding
                )

        result = asyncio.run(scenario())
        self.assertEqual(result, ("confirmed", {"total_tokens": 7}))
        self.assertTrue(socket.closed)

    def test_user_hangup_does_not_wait_for_provider_event_persistence(self):
        class ProviderSocket:
            closed = False

            async def send_json(self, _payload):
                return None

            async def close(self):
                self.closed = True

        socket = ProviderSocket()
        first_attempt = asyncio.Event()
        allow_success = asyncio.Event()
        original_append = self.manager.append_fragment_once
        cleanup_calls = []

        async def flaky_persistence(binding, fragment):
            first_attempt.set()
            if not allow_success.is_set():
                raise sqlite3.IntegrityError("temporary database write failure")
            return await original_append(binding, fragment)

        async def provider_cleanup(_key, provider_id, **_kwargs):
            cleanup_calls.append(provider_id)
            return "confirmed", {"total_tokens": 7}

        self.manager.append_fragment_once = flaky_persistence
        self.manager._close_provider_session = provider_cleanup
        self.manager._active_sockets[self.call_id] = socket

        async def scenario():
            self.manager._enqueue_provider_event(self.binding, {
                "type": "session.input_transcript.delta",
                "event_id": "provider-fragment-hangup-1",
                "delta": "hangup continues",
                "start_ms": 200,
                "end_ms": 300,
            })
            await asyncio.wait_for(first_attempt.wait(), timeout=3)
            receipt = await asyncio.wait_for(
                self.manager._finish_call(
                    self.binding, reason="user_hangup", initiator="user"
                ),
                timeout=1,
            )
            self.assertEqual(receipt["phase"], "ended")
            self.assertEqual(receipt["provider_close_state"], "confirmed")
            self.assertTrue(socket.closed)
            self.assertEqual(cleanup_calls, ["prov-1"])
            allow_success.set()
            await asyncio.wait_for(self.manager._drain_provider_events(self.call_id), timeout=3)
            worker = self.manager._provider_event_tasks.get(self.call_id)
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

        asyncio.run(scenario())

        with self.store._lock, self.store._connection() as conn:
            call = conn.execute(
                "SELECT phase FROM live_calls WHERE call_id = ?", (self.call_id,)
            ).fetchone()
            fragment_count = conn.execute(
                "SELECT COUNT(*) FROM live_fragments WHERE provider_event_id = ?",
                ("provider-fragment-hangup-1",),
            ).fetchone()[0]
        self.assertEqual(call["phase"], "ended")
        self.assertEqual(fragment_count, 1)
        with self.store._lock, self.store._connection() as conn:
            call_record = conn.execute(
                "SELECT text FROM messages WHERE session_id = ? AND source = 'live-phone'",
                (self.session_id,),
            ).fetchone()
        self.assertIsNotNone(call_record)
        self.assertIn("hangup continues", call_record["text"])
        audit_text = self.manager.audit.path_for(self.binding).read_text(encoding="utf-8")
        self.assertNotIn("hangup continues", audit_text)

    def test_user_hangup_bounds_stalled_websocket_send_and_close(self):
        class StuckSocket:
            closed = False

            async def send_json(self, _payload):
                await asyncio.Future()

            async def close(self):
                await asyncio.Future()

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return StuckSocket()

        self.manager._active_sockets[self.call_id] = StuckSocket()

        async def scenario():
            with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
                 patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
                return await asyncio.wait_for(
                    self.manager._finish_call(
                        self.binding, reason="user_hangup", initiator="user"
                    ),
                    timeout=1,
                )

        receipt = asyncio.run(scenario())
        self.assertEqual(receipt["phase"], "ended")
        with self.store._lock, self.store._connection() as conn:
            row = conn.execute(
                "SELECT termination_initiator, termination_reason, phase FROM live_calls WHERE call_id = ?",
                (self.call_id,),
            ).fetchone()
        self.assertEqual(tuple(row), ("user", "user_hangup", "ended"))
        audit_text = self.manager.audit.path_for(self.binding).read_text(encoding="utf-8")
        records = [json.loads(line) for line in audit_text.splitlines()]
        self.assertTrue(any(item["event"] == "provider.close_request_failed" for item in records))
        self.assertTrue(any(item["event"] == "provider.cleanup_failed" for item in records))
        self.assertTrue(any(item["event"] == "provider.socket_operation_failed" for item in records))

    def test_client_lifecycle_observation_is_audited_without_transcript_content(self):
        result = asyncio.run(self.manager._op_observe(self.owner_id, {
            **self.scope,
            "event": "client.peer_connection_state",
            "event_id": "client-observation-1",
            "observed_at": "2026-09-30T00:00:00Z",
            "client_sequence": 3,
            "detail": {"state": "failed", "reason": "transport_failure"},
        }))
        self.assertTrue(result["ok"])
        records = [
            json.loads(line)
            for line in self.manager.audit.path_for(self.binding).read_text(encoding="utf-8").splitlines()
        ]
        observed = next(item for item in records if item["event"] == "client.peer_connection_state")
        self.assertEqual(observed["detail"]["state"], "failed")
        self.assertEqual(observed["detail"]["client_sequence"], 3)

    def test_end_control_records_explicit_user_hangup_authority(self):
        async def fake_close(_key, _provider_id, **_kwargs): return "unconfirmed", None
        self.manager._close_provider_session = fake_close
        result = asyncio.run(self.manager._op_control(self.owner_id, {
            **self.scope,
            "action": "end",
            "idempotency_key": "user-hangup-1",
            "termination_initiator": "user",
            "termination_reason": "user_hangup",
        }))
        self.assertTrue(result["ok"])
        records = [
            json.loads(line)
            for line in self.manager.audit.path_for(self.binding).read_text(encoding="utf-8").splitlines()
        ]
        request = next(item for item in records if item["event"] == "termination.requested")
        self.assertEqual(request["detail"]["initiator"], "user")
        self.assertEqual(request["detail"]["reason"], "user_hangup")

    def test_non_user_fault_cannot_be_labelled_as_user_hangup(self):
        with self.assertRaisesRegex(LiveVoiceError, "live_termination_invalid"):
            asyncio.run(self.manager._op_control(self.owner_id, {
                **self.scope,
                "action": "end",
                "idempotency_key": "false-user-hangup-1",
                "termination_initiator": "client_fault",
                "termination_reason": "user_hangup",
            }))

    def test_append_error_is_scoped_to_the_update_and_call_stays_alive(self):
        manager = self.manager
        binding = self.binding

        class ProviderSocket:
            closed = False

            def __init__(self):
                self.messages = iter((
                    type("Message", (), {
                        "type": aiohttp.WSMsgType.TEXT,
                        "data": json.dumps({
                            "type": "error",
                            "event_id": "provider-error-1",
                            "error": {
                                "code": "context_append_content_too_long",
                                "client_event_id": "update-too-large",
                            },
                        }),
                    })(),
                    type("Message", (), {
                        "type": aiohttp.WSMsgType.TEXT,
                        "data": json.dumps({
                            "type": "session.closed",
                            "event_id": "provider-close-1",
                            "usage": {"total_tokens": 5},
                        }),
                    })(),
                ))

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.messages)
                except StopIteration as exc:
                    raise StopAsyncIteration from exc

            async def close(self):
                self.closed = True

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return ProviderSocket()

        async def scenario():
            waiter = asyncio.get_running_loop().create_future()
            manager._update_waiters[(binding.call_id, "update-too-large")] = waiter
            with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
                 patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
                await manager._run_sideband(binding, "sk-test-fake", "prov-1")
            return waiter.result()

        self.assertFalse(asyncio.run(scenario()))
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "recovering")
        self.assertEqual(snap["snapshot"]["provider_close_state"], "confirmed")
        self.assertTrue(snap["snapshot"]["resume_required"])

    def test_automatic_admission_retry_reuses_reserved_pao_idempotency(self):
        calls = []

        async def flaky(binding, frozen, key):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("response lost")
            return await self._admit_run(binding, frozen, key)

        self.manager._admit_run = flaky
        asyncio.run(self.manager.append_fragment_once(
            self.binding, Fragment("retry-frag", "user", "inspect logs", 0, 500)
        ))
        asyncio.run(self.manager.register_delegation_once(self.binding, "retry-delegation", 600))
        asyncio.run(self.manager.schedule_proposal(self.binding, "retry-delegation", 600))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertTrue(calls[0].startswith("live-delegation-live-auto-"))
        with self.store._lock, self.store._connection() as conn:
            row = conn.execute(
                "SELECT decision FROM live_delegations WHERE call_id = ? AND delegation_id = ?",
                (self.call_id, "retry-delegation"),
            ).fetchone()
        self.assertEqual(row["decision"], "admitted")

    def test_background_messages_and_events_are_delivered_in_source_time_order(self):
        manager = self.manager
        binding = self.binding
        sent = []
        message = {
            "inbox_id": "message-later", "source_session_id": "background",
            "source_message_id": "message-1", "role": "assistant", "source": "cron",
            "text": "later background message", "created_at": "2026-09-30T00:00:02Z",
        }
        event = {
            "inbox_id": "event-earlier", "source_session_id": "background",
            "source_event_id": "event-1", "kind": "job.completed",
            "summary": "earlier background event", "created_at": "2026-09-30T00:00:01Z",
        }
        self.store.pending_live_foreground_items = lambda *_args, **_kwargs: [
            {**event, "item_type": "event"},
            {**message, "item_type": "message"},
        ]
        self.store.mark_live_foreground_message_delivered = lambda *_args, **_kwargs: True
        self.store.mark_live_foreground_event_delivered = lambda *_args, **_kwargs: True

        async def send_update(_binding, *, kind, content, delegation_id):
            sent.append((kind, content))
            if len(sent) == 2:
                manager._closing = True
            return True

        async def scenario():
            manager._closing = False
            manager._active_sockets[binding.call_id] = object()
            manager._send_provider_update = send_update
            try:
                await manager._run_foreground_router(binding)
            finally:
                manager._active_sockets.pop(binding.call_id, None)

        asyncio.run(scenario())
        self.assertIn("earlier background event", sent[0][1])
        self.assertIn("later background message", sent[1][1])

    def test_agent_result_is_returned_to_same_live_voice(self):
        sent = []
        manager = self.manager
        binding = self.binding

        async def poll_activity(_binding, _request_id, _after, _limit):
            return {
                "ok": True,
                "latest_sequence": 2,
                "events": [
                    {
                        "sequence": 1,
                        "delivery_class": "technical",
                        "presentation_channel": "verbose",
                        "presentation_enabled": False,
                        "summary": "Checking service health.",
                    },
                    {
                        "sequence": 2,
                        "delivery_class": "reasoning",
                        "presentation_channel": "thinking",
                        "presentation_enabled": True,
                        "summary": "private reasoning must stay hidden",
                    },
                ],
            }

        class FakeSocket:
            closed = False

            async def send_json(self, value):
                sent.append(value)
                event_id = value.get("event_id")
                waiter = manager._update_waiters.get((binding.call_id, event_id))
                if waiter is not None and not waiter.done():
                    waiter.set_result(True)

        async def scenario():
            manager._active_sockets[binding.call_id] = FakeSocket()
            manager._poll_run_activity = poll_activity
            await manager.append_fragment_once(
                binding, Fragment("relay-frag", "user", "check the service", 0, 500)
            )
            await manager.register_delegation_once(binding, "relay-delegation", 600)
            await manager.schedule_proposal(binding, "relay-delegation", 600)
            request_id = f"req-{self.admit_calls[-1]}"
            token = self.store.mark_request_running(request_id, worker_id="test-worker")
            self.assertIsNotNone(token)
            self.store.finish_request(
                request_id,
                success=True,
                assistant_text="The service is healthy.",
                assistant_source="test",
                fencing_token=token,
            )
            if manager._relay_tasks:
                await asyncio.gather(*tuple(manager._relay_tasks))

        asyncio.run(scenario())
        thinking = [item for item in sent if item["type"] == "session.thinking.append"]
        commentary = [item for item in sent if item["type"] == "session.commentary.append"]
        self.assertTrue(any("Checking service health" in item["content"] for item in thinking))
        self.assertFalse(any("private reasoning" in item["content"] for item in sent))
        self.assertTrue(any("service is healthy" in item["content"] for item in commentary))
        self.assertTrue(all(item["delegation_id"] == "relay-delegation" for item in commentary))

    def test_terminal_call_creates_one_complete_chat_record(self):
        asyncio.run(self.manager.append_fragment_once(
            self.binding, Fragment("record-user", "user", "Please check it.", 0, 500)
        ))
        asyncio.run(self.manager.append_fragment_once(
            self.binding, Fragment("record-agent", "assistant", "I am checking it.", 600, 1100)
        ))
        live_history = self.store.recent_history_messages(
            self.session_id,
            owner_id=self.owner_id,
            context_generation=1,
        )
        self.assertEqual(
            [(item["role"], item["text"]) for item in live_history],
            [("user", "Please check it."), ("assistant", "I am checking it.")],
        )
        live_exchanges = self.store.recent_exchanges(self.session_id)
        self.assertEqual(live_exchanges[-1]["user_text"], "Please check it.")
        self.assertEqual(live_exchanges[-1]["assistant_text"], "I am checking it.")
        with self.store._lock, self.store._connection() as connection:
            connection.execute(
                "UPDATE live_calls SET phase = 'ending', termination_initiator = 'user', termination_reason = 'user_hangup' WHERE call_id = ?",
                (self.call_id,),
            )
        self.manager._mark_terminal(
            self.binding,
            "ended",
            provider_close_state="confirmed",
            summary="Live call ended",
        )
        self.manager._persist_call_record(self.binding)
        records = [
            item for item in self.store.messages(self.session_id, owner_id=self.owner_id)
            if item.get("source") == "live-phone"
        ]
        self.assertEqual(len(records), 1)
        self.assertIn("Please check it.", records[0]["text"])
        self.assertIn("I am checking it.", records[0]["text"])
        self.assertTrue(records[0]["message_context"]["live_call_record"])

    def test_initial_sideband_failure_keeps_logical_call_recoverable(self):
        manager = self.manager
        with self.store._lock, self.store._connection() as connection:
            connection.execute(
                "UPDATE live_calls SET phase = 'interrupted', ended_at = ? WHERE call_id = ?",
                (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), self.call_id),
            )

        async def fit(_http, *, input_messages, **_kwargs):
            return list(input_messages), {"input_tokens_exact": 123, "history_omitted_units": 0}

        async def create(_http, *, request, **_kwargs):
            return {"provider_session_id": "prov-initial-fail", "sdp_answer": "v=0\r\n"}

        async def sideband(binding, _key, _provider_id):
            manager._sideband_failures[binding.call_id] = "live_sideband_timeout"
            manager._sideband_ready_events[binding.call_id].set()

        async def close(_key, _provider_id, **_kwargs):
            return "confirmed", None

        @asynccontextmanager
        async def fake_http():
            yield object()

        manager._run_sideband = sideband
        manager._close_provider_session = close
        scope = {key: self.scope[key] for key in (
            "instance_id", "instance_generation", "agent_id", "session_id", "context_generation"
        )}
        payload = {**scope, "sdp": "v=0\r\noffer", "idempotency_key": "start-sideband-fault",
                   "phone_revision": PHONE_REVISION}
        with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
             patch("orchestrator.frontend_live_voice.manager.fit_live_session_input", fit), \
             patch("orchestrator.frontend_live_voice.manager.create_provider_session", create):
            with self.assertRaisesRegex(LiveVoiceError, "live_sideband_timeout"):
                asyncio.run(manager._op_start(self.owner_id, payload))
        with self.store._lock, self.store._connection() as connection:
            row = connection.execute(
                """SELECT phase, ended_at, termination_initiator, termination_reason
                   FROM live_calls WHERE owner_id = ? ORDER BY started_at DESC LIMIT 1""",
                (self.owner_id,),
            ).fetchone()
        self.assertEqual(row["phase"], "recovering")
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["termination_initiator"])
        self.assertIsNone(row["termination_reason"])

    def test_start_contract_waits_for_sideband_and_never_persists_sdp(self):
        with self.store._lock, self.store._connection() as conn:
            conn.execute(
                "UPDATE live_calls SET phase = 'ended', ended_at = ? WHERE call_id = ?",
                ("2026-01-01T00:00:00Z", self.call_id),
            )
        provider_calls = []

        class FakeContent:
            async def iter_chunked(self, _size):
                yield b'{"input_tokens":42}'

        class FakeResponse:
            status = 200
            headers = {"x-request-id": "req-count-contract"}
            content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class FakeHttp:
            def post(self, *_args, **_kwargs):
                return FakeResponse()

        @asynccontextmanager
        async def fake_http():
            yield FakeHttp()

        async def fake_create(_http, *, key, request):
            provider_calls.append((key, request))
            return {"provider_session_id": "live-contract-1", "sdp_answer": "v=0\r\nanswer"}

        async def fake_sideband(binding, _key, _provider_id):
            self.manager._sideband_ready_events[binding.call_id].set()
            await asyncio.Event().wait()

        self.manager._run_sideband = fake_sideband
        start_scope = {key: self.scope[key] for key in (
            "instance_id", "instance_generation", "agent_id", "session_id", "context_generation"
        )}
        payload = {**start_scope, "sdp": "v=0\r\noffer", "idempotency_key": "start-contract-1",
                   "phone_revision": PHONE_REVISION}
        with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
             patch("orchestrator.frontend_live_voice.manager.create_provider_session", fake_create):
            first = asyncio.run(self.manager._op_start(self.owner_id, payload))
            replay = asyncio.run(self.manager._op_start(self.owner_id, payload))
        self.assertEqual(len(provider_calls), 1)
        self.assertEqual(provider_calls[0][1]["session"]["audio"]["output"]["voice"], "willow")
        self.assertEqual(
            [item["role"] for item in provider_calls[0][1]["session"]["input"]],
            ["developer", "user", "assistant"],
        )
        self.assertEqual(first["sdp_answer"], "v=0\r\nanswer")
        self.assertEqual(first["binding"]["call_id"], first["call_id"])
        self.assertEqual(replay["call_id"], first["call_id"])
        with self.store._lock, self.store._connection() as conn:
            attempt = conn.execute(
                """SELECT outcome_json, phone_config_json
                   FROM live_call_attempts WHERE attempt_id = 'start-contract-1'"""
            ).fetchone()
            call = conn.execute(
                "SELECT phone_config_json FROM live_calls WHERE call_id = ?",
                (first["call_id"],),
            ).fetchone()
        stored = attempt["outcome_json"]
        self.assertNotIn("sdp", stored.lower())
        self.assertNotIn("Safe projected test Persona", stored)
        for raw_snapshot in (attempt["phone_config_json"], call["phone_config_json"]):
            snapshot = json.loads(raw_snapshot)
            self.assertEqual(snapshot["public"]["voice"], "willow")
            self.assertEqual(snapshot["public"]["revision"], PHONE_REVISION)
            self.assertEqual(len(snapshot["instructions_sha256"]), 64)
            self.assertEqual(snapshot["input_tokens_exact"], 42)
            self.assertEqual(snapshot["provider_count_requests"], 1)
            self.assertNotIn("instructions", snapshot)
            self.assertNotIn("Safe projected test Persona", raw_snapshot)

    def test_start_rejects_stale_phone_revision_before_provider_create(self):
        provider_called = False

        @asynccontextmanager
        async def fake_http():
            nonlocal provider_called
            provider_called = True
            yield object()

        payload = {
            **{key: self.scope[key] for key in (
                "instance_id", "instance_generation", "agent_id", "session_id", "context_generation"
            )},
            "sdp": "v=0\r\noffer",
            "idempotency_key": "start-stale-phone",
            "phone_revision": "b" * 64,
        }
        with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http):
            with self.assertRaises(LiveVoiceError) as caught:
                asyncio.run(self.manager._op_start(self.owner_id, payload))
        self.assertEqual(caught.exception.code, "live_phone_configuration_changed")
        self.assertFalse(provider_called)
        with self.store._lock, self.store._connection() as conn:
            attempt = conn.execute(
                "SELECT 1 FROM live_call_attempts WHERE attempt_id = 'start-stale-phone'"
            ).fetchone()
        self.assertIsNone(attempt)

    def test_database_enforces_one_active_call_per_owner(self):
        with self.assertRaises(sqlite3.IntegrityError), self.store._lock, self.store._connection() as conn:
            conn.execute(
                """
                INSERT INTO live_calls(
                    call_id, owner_id, session_id, agent_id, instance_id,
                    instance_generation, context_generation, call_epoch,
                    provider_session_id, phase, controller_lease, lease_expiry,
                    started_at, max_ends_at
                ) VALUES ('call-conflict', ?, ?, ?, 'HASHI', '1', 1, 1,
                          'prov-conflict', 'active', 'lease-conflict', ?, ?, ?)
                """,
                (self.owner_id, self.session_id, self.agent_id,
                 "2099-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2099-01-01T00:00:00Z"),
            )

    def test_context_exposes_recovering_foreground_without_browser_hint(self):
        with self.store._lock, self.store._connection() as conn:
            conn.execute(
                "UPDATE live_calls SET phase = 'recovering', ended_at = NULL WHERE call_id = ?",
                (self.call_id,),
            )
        result = asyncio.run(self.manager._op_context(self.owner_id, {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "context_generation": 1,
        }))
        foreground = result["foreground_call"]
        self.assertEqual(foreground["call_id"], self.call_id)
        self.assertEqual(foreground["phase"], "recovering")
        self.assertEqual(foreground["binding"]["call_id"], self.call_id)
        self.assertEqual(foreground["binding"]["call_epoch"], 1)

    def test_legacy_unconfirmed_terminal_calls_recover_but_user_hangup_stays_ended(self):
        user_call_id = "call-legacy-user-ended"
        with self.store._lock, self.store._connection() as conn:
            conn.execute(
                "UPDATE schema_metadata SET value = '18' WHERE key = 'schema_version'"
            )
            conn.execute(
                "UPDATE live_calls SET phase = 'ended', ended_at = ? WHERE call_id = ?",
                ("2026-09-30T00:00:00Z", self.call_id),
            )
            conn.execute(
                """INSERT INTO live_calls(
                    call_id, owner_id, session_id, agent_id, instance_id,
                    instance_generation, context_generation, call_epoch,
                    provider_session_id, phase, foreground, controller_lease,
                    lease_expiry, started_at, max_ends_at, ended_at
                ) VALUES (?, ?, ?, ?, 'HASHI', '1', 1, 1, 'prov-legacy-user',
                          'ended', 1, 'lease-legacy-user', '2099-01-01T00:00:00Z',
                          '2026-09-30T00:00:00Z', '2026-09-30T00:30:00Z', '2026-09-30T00:10:00Z')""",
                (user_call_id, self.owner_id, self.session_id, self.agent_id),
            )
            self.store._append_event(
                conn,
                session_id=self.session_id,
                run_id=None,
                kind="voice.live.call.state",
                summary="User requested hangup",
                detail={
                    "scope": {
                        "instance_id": "HASHI", "instance_generation": "1",
                        "agent_id": self.agent_id, "session_id": self.session_id,
                        "context_generation": 1, "call_id": user_call_id, "call_epoch": 1,
                    },
                    "phase": "ending",
                    "termination_initiator": "user",
                    "termination_reason": "user_hangup",
                },
            )
        reopened = SessionStore(self.db_path, instance_id="HASHI")
        with reopened._lock, reopened._connection() as conn:
            passive = conn.execute(
                "SELECT phase, ended_at, foreground FROM live_calls WHERE call_id = ?",
                (self.call_id,),
            ).fetchone()
            user_ended = conn.execute(
                "SELECT phase, ended_at, foreground FROM live_calls WHERE call_id = ?",
                (user_call_id,),
            ).fetchone()
            migration_count = conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE session_id = ? AND kind = 'voice.live.call.state' "
                "AND json_extract(detail_json, '$.reason') = 'legacy_terminal_recovery'",
                (self.session_id,),
            ).fetchone()[0]
        self.assertEqual(passive["phase"], "recovering")
        self.assertIsNone(passive["ended_at"])
        self.assertEqual(passive["foreground"], 1)
        self.assertEqual(user_ended["phase"], "ended")
        self.assertIsNotNone(user_ended["ended_at"])
        self.assertEqual(migration_count, 1)
        SessionStore(self.db_path, instance_id="HASHI")
        with reopened._lock, reopened._connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE session_id = ? AND kind = 'voice.live.call.state' "
                "AND json_extract(detail_json, '$.reason') = 'legacy_terminal_recovery'",
                (self.session_id,),
            ).fetchone()[0], 1)
    def test_schema_upgrade_backgrounds_duplicate_calls_without_reactivating_them(self):
        newer_id = "call-newer-migration"
        with self.store._lock, self.store._connection() as conn:
            conn.execute("DROP INDEX IF EXISTS one_live_call_per_owner_agent")
            conn.execute("DROP INDEX IF EXISTS one_live_call_per_owner")
            conn.execute(
                """
                INSERT INTO live_calls(
                    call_id, owner_id, session_id, agent_id, instance_id,
                    instance_generation, context_generation, call_epoch,
                    provider_session_id, phase, controller_lease, lease_expiry,
                    started_at, max_ends_at
                ) VALUES (?, ?, ?, ?, 'HASHI', '1', 1, 1, 'prov-newer',
                          'active', 'lease-newer', ?, ?, ?)
                """,
                (newer_id, self.owner_id, self.session_id, "zelda",
                 "2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z", "2099-01-01T00:30:00Z"),
            )
        reopened = SessionStore(self.db_path, instance_id="HASHI")
        with reopened._lock, reopened._connection() as conn:
            rows = conn.execute(
                "SELECT call_id, phase, ended_at, foreground FROM live_calls WHERE owner_id = ? ORDER BY call_id",
                (self.owner_id,),
            ).fetchall()
            index = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'one_live_call_per_owner'"
            ).fetchone()
        foreground = [row for row in rows if row["foreground"] == 1]
        parked = next(row for row in rows if row["call_id"] == self.call_id)
        self.assertEqual([row["call_id"] for row in foreground], [newer_id])
        self.assertEqual(parked["phase"], "recovering")
        self.assertIsNone(parked["ended_at"])
        self.assertEqual(parked["foreground"], 0)
        with reopened._lock, reopened._connection() as conn:
            migration_event = conn.execute(
                "SELECT detail_json FROM run_events WHERE session_id = ? AND kind = 'voice.live.call.state' ORDER BY sequence DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
        self.assertEqual(json.loads(migration_event["detail_json"])["reason"], "owner_singleton_migration")
        self.assertIsNotNone(index)

        with reopened._lock, reopened._connection() as conn:
            conn.execute(
                "UPDATE live_calls SET phase = 'ended', ended_at = ? WHERE call_id = ?",
                ("2026-09-30T00:00:00Z", newer_id),
            )
        reopened_again = SessionStore(self.db_path, instance_id="HASHI")
        with reopened_again._lock, reopened_again._connection() as conn:
            active = conn.execute(
                "SELECT call_id FROM live_calls WHERE owner_id = ? AND foreground = 1 "
                "AND phase IN ('connecting', 'active', 'ending', 'recovering')",
                (self.owner_id,),
            ).fetchall()
            parked_again = conn.execute(
                "SELECT phase, ended_at, foreground FROM live_calls WHERE call_id = ?",
                (self.call_id,),
            ).fetchone()
            migration_event_count = conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE session_id = ? AND kind = 'voice.live.call.state' "
                "AND json_extract(detail_json, '$.reason') = 'owner_singleton_migration'",
                (self.session_id,),
            ).fetchone()[0]
        self.assertEqual(active, [])
        self.assertEqual(parked_again["phase"], "recovering")
        self.assertIsNone(parked_again["ended_at"])
        self.assertEqual(parked_again["foreground"], 0)
        self.assertEqual(migration_event_count, 1)

    def test_availability_requires_feature_flag_and_credential(self):
        disabled = type("GlobalConfig", (), {
            "instance_id": "HASHI", "instance_generation": "1", "live_voice_v1": False,
        })()
        self.assertFalse(LiveVoiceManager(self.store, disabled, {"openai_api_key": "sk-test"}).available)
        enabled = type("GlobalConfig", (), {
            "instance_id": "HASHI", "instance_generation": "1", "live_voice_v1": True,
        })()
        self.assertFalse(LiveVoiceManager(self.store, enabled, {}).available)


@asynccontextmanager
async def live_voice_http_client():
    temp_dir = tempfile.mkdtemp()
    db_path = Path(temp_dir) / "state" / "sessions.sqlite3"
    store = SessionStore(db_path, instance_id="HASHI")
    owner_id = "test-owner"
    agent_id = "zelda"
    session = store.create_session(
        owner_id=owner_id,
        agent_id=agent_id,
        title="Http Test Session",
    )
    session_id = session["session_id"]
    global_config = type("GlobalConfig", (), {
        "instance_id": "HASHI",
        "instance_generation": "1",
        "live_voice_v1": True,
    })()
    manager = LiveVoiceManager(
        session_store=store,
        global_config=global_config,
        secrets={"openai_api_key": "sk-test-fake"},
        resolve_phone_session=resolved_phone_session,
    )
    app = web.Application()

    async def authorize(request, operation, payload):
        auth_header = request.headers.get("Authorization", "")
        if auth_header == "Bearer bad-token":
            raise LiveVoiceError("live_not_authenticated", 401)
        return {"owner_id": owner_id, "admin": True}

    register_live_voice_routes(app, manager, authorize, qualified=True)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, session_id, agent_id
    finally:
        await client.close()
        shutil.rmtree(temp_dir, ignore_errors=True)


async def test_capabilities_endpoint():
    async with live_voice_http_client() as (client, _session_id, _agent_id):
        resp = await client.get("/api/v1/live-voice/capabilities")
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["capability"]["available"] is True
        assert data["capability"]["protocol_version"] == "1.0"


async def test_context_endpoint():
    async with live_voice_http_client() as (client, session_id, agent_id):
        resp = await client.post(
            "/api/v1/live-voice/context",
            json={"session_id": session_id, "agent_id": agent_id, "context_generation": 1},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["binding"]["session_id"] == session_id


async def test_auth_failure_rejected():
    async with live_voice_http_client() as (client, _session_id, _agent_id):
        resp = await client.get(
            "/api/v1/live-voice/capabilities",
            headers={"Authorization": "Bearer bad-token"},
        )
        assert resp.status == 401
        data = await resp.json()
        assert data["ok"] is False
        assert data["error_code"] == "live_not_authenticated"


async def test_forbidden_authority_field_in_body():
    async with live_voice_http_client() as (client, session_id, agent_id):
        resp = await client.post(
            "/api/v1/live-voice/context",
            json={"session_id": session_id, "agent_id": agent_id, "owner_id": "attacker"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["error_code"] == "live_authority_field_forbidden"


if __name__ == "__main__":
    unittest.main()
