"""Comprehensive integration tests for LiveVoiceManager and WorkbenchApiServer live voice routes."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
            **self.scope,
            "action": "heartbeat",
            "idempotency_key": "ctrl-hb-1",
        }))
        self.assertTrue(hb["ok"])
        self.assertTrue(hb["renewed"])

        mute = asyncio.run(self.manager._op_control(self.owner_id, {
            **self.scope,
            "action": "mute",
            "idempotency_key": "ctrl-mute-1",
        }))
        self.assertTrue(mute["ok"])
        self.assertFalse(mute["applied"])

        end = asyncio.run(self.manager._op_control(self.owner_id, {
            **self.scope,
            "action": "end",
            "idempotency_key": "ctrl-end-1",
        }))
        self.assertTrue(end["ok"])
        self.assertEqual(end["phase"], "interrupted")
        self.assertEqual(end["provider_close_state"], "unconfirmed")

        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "interrupted")

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
            })
            return muted, ended

        muted, ended = asyncio.run(scenario())
        self.assertTrue(muted["applied"])
        self.assertEqual(sent[0]["type"], "session.input_audio.mute")
        self.assertEqual(sent[1]["type"], "session.close")
        self.assertEqual(ended["provider_close_state"], "confirmed")
        self.assertEqual(ended["usage"], {"input_tokens": 12})

    def test_startup_recovery_closes_orphaned_provider_session(self):
        calls = []

        async def close_provider(key, provider_id, **_kwargs):
            calls.append((key, provider_id))
            return "confirmed", {"total_tokens": 7}

        self.manager._close_provider_session = close_provider

        async def scenario():
            await self.manager.start()
            await self.manager.shutdown()

        asyncio.run(scenario())
        self.assertEqual(calls, [("sk-test-fake", "prov-1")])
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "interrupted")
        self.assertEqual(snap["snapshot"]["provider_close_state"], "confirmed")
        self.assertEqual(snap["snapshot"]["usage"], {"total_tokens": 7})

    def test_shutdown_sends_provider_close_before_socket_teardown(self):
        sent = []
        manager = self.manager
        binding = self.binding

        class FakeSocket:
            closed = False

            async def send_json(self, value):
                sent.append(value)
                manager._mark_terminal(
                    binding, "ended", provider_close_state="confirmed",
                    summary="Live call ended",
                )
                manager._session_closed_events.setdefault(binding.call_id, asyncio.Event()).set()

            async def close(self):
                self.closed = True

        async def scenario():
            manager._active_sockets[binding.call_id] = FakeSocket()
            await manager.shutdown()

        asyncio.run(scenario())
        self.assertEqual([item["type"] for item in sent], ["session.close"])
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "ended")
        self.assertEqual(snap["snapshot"]["provider_close_state"], "confirmed")

    def test_unexpected_sideband_disconnect_closes_provider(self):
        close_calls = []

        class DisconnectedSocket:
            closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def close(self):
                self.closed = True

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return DisconnectedSocket()

        async def fake_close(key, provider_session_id, **_kwargs):
            close_calls.append((key, provider_session_id))
            return "confirmed", {"total_tokens": 3}

        self.manager._close_provider_session = fake_close
        with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
             patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
            asyncio.run(self.manager._run_sideband(self.binding, "sk-test-fake", "prov-1"))
        self.assertEqual(close_calls, [("sk-test-fake", "prov-1")])
        snap = asyncio.run(self.manager._op_snapshot(self.owner_id, self.scope))
        self.assertEqual(snap["snapshot"]["phase"], "interrupted")
        self.assertEqual(snap["snapshot"]["provider_close_state"], "confirmed")

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

    def test_sideband_exception_keeps_full_failure_evidence_outside_session_events(self):
        class ProviderSocket:
            closed = False

            def __init__(self):
                self.sent = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.sent:
                    raise StopAsyncIteration
                self.sent = True
                return type("Message", (), {
                    "type": aiohttp.WSMsgType.TEXT,
                    "data": json.dumps({
                        "type": "session.input_transcript.delta",
                        "event_id": "provider-fragment-race-1",
                        "delta": "hello",
                        "start_ms": 0,
                        "end_ms": 100,
                    }),
                })()

            async def close(self):
                self.closed = True

        @asynccontextmanager
        async def fake_http():
            yield object()

        async def fake_attach(_http, *, key, provider_session_id):
            return ProviderSocket()

        async def fail_persistence(_binding, _event):
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: run_events.session_id, run_events.sequence"
            )

        async def fake_close(_key, _provider_session_id, **_kwargs):
            return "unconfirmed", None

        self.manager.service.on_provider_event = fail_persistence
        self.manager._close_provider_session = fake_close
        with patch("orchestrator.frontend_live_voice.manager.provider_http_session", fake_http), \
             patch("orchestrator.frontend_live_voice.manager.attach_provider", fake_attach):
            asyncio.run(self.manager._run_sideband(self.binding, "sk-test-fake", "prov-1"))

        audit_text = self.manager.audit.path_for(self.binding).read_text(encoding="utf-8")
        self.assertNotIn("hello", audit_text)
        records = [
            json.loads(line)
            for line in audit_text.splitlines()
        ]
        failure = next(item for item in records if item["event"] == "sideband.exception")
        self.assertEqual(failure["detail"]["exception_type"], "IntegrityError")
        self.assertIn("run_events.sequence", failure["detail"]["exception_message"])

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
        self.assertEqual(snap["snapshot"]["phase"], "ended")
        self.assertEqual(snap["snapshot"]["provider_close_state"], "confirmed")

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

    def test_database_enforces_one_active_call_per_owner_agent(self):
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

    def test_schema_upgrade_closes_duplicate_active_calls_before_unique_index(self):
        newer_id = "call-newer-migration"
        with self.store._lock, self.store._connection() as conn:
            conn.execute("DROP INDEX one_live_call_per_owner_agent")
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
                (newer_id, self.owner_id, self.session_id, self.agent_id,
                 "2099-01-01T00:00:00Z", "2099-01-01T00:00:00Z", "2099-01-01T00:30:00Z"),
            )
        reopened = SessionStore(self.db_path, instance_id="HASHI")
        with reopened._lock, reopened._connection() as conn:
            rows = conn.execute(
                "SELECT call_id, phase FROM live_calls WHERE owner_id = ? AND agent_id = ? ORDER BY call_id",
                (self.owner_id, self.agent_id),
            ).fetchall()
            index = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'one_live_call_per_owner_agent'"
            ).fetchone()
        self.assertEqual([row["call_id"] for row in rows if row["phase"] == "active"], [newer_id])
        self.assertEqual([row["call_id"] for row in rows if row["phase"] == "interrupted"], [self.call_id])
        self.assertIsNotNone(index)

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
