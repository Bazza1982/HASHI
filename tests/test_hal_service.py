"""HAL service end-to-end: real feed SQL projection + recording dispatcher.

These tests run the actual ``orchestrator.automation_events.terminal_events_page``
SQL against a minimal in-memory Session schema, so cursor handling, owner scope,
automation-origin filtering and correlation behave exactly as in production.
Dispatch is recorded (never executed) — live dispatch is exercised in the
non-production PIT acceptance.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from hal.dispatch import DispatchResult, RecordingDispatchAdapter
from hal.service import HalService, handle_hal_rules_get, handle_hal_rules_post, handle_hal_status, handle_hal_validate, handle_hal_rule_enable, handle_hal_rule_disable, handle_hal_runs_get
from hal.store import HalStore, HalStoreError

SCHEMA = """
CREATE TABLE automation_event_index (
    feed_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT, run_id TEXT, session_id TEXT, kind TEXT,
    created_at TEXT, agent_id TEXT, owner_id TEXT
);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, status TEXT);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, idempotency_key TEXT, session_id TEXT);
"""


class FakeSessionStore:
    """Minimal SessionStore surface the real feed projection needs."""

    def __init__(self, instance_id="TESTINST"):
        self.instance_id = instance_id
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self.active_rows: list = []
        self._seq = 0

    @contextmanager
    def _connection(self):
        yield self._conn

    def add_event(
        self,
        *,
        owner_id="user:7",
        agent_id="lily",
        kind="run.completed",
        key=None,
        session_id=None,
    ):
        self._seq += 1
        session_id = session_id or f"sess-{self._seq}"
        run_id = f"run-{self._seq}"
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, status) VALUES (?, 'active')",
            (session_id,),
        )
        self._conn.execute(
            "INSERT OR IGNORE INTO runs (run_id, idempotency_key, session_id) VALUES (?,?,?)",
            (run_id, key or "", session_id),
        )
        self._conn.execute(
            "INSERT INTO automation_event_index (event_id, run_id, session_id, kind, "
            "created_at, agent_id, owner_id) VALUES (?,?,?,?,?,?,?)",
            (
                f"evt-{self._seq}",
                run_id,
                session_id,
                kind,
                "2026-11-23T00:00:00Z",
                agent_id,
                owner_id,
            ),
        )
        self._conn.commit()
        return f"evt-{self._seq}"

    def list_active_runs(self, *, owner_id, session_id=None):
        return self.active_rows


def make_service(tmp_path, feed):
    store = HalStore(tmp_path / "hal" / "hal_v1.sqlite")
    adapter = RecordingDispatchAdapter()
    service = HalService(store=store, session_store=feed, dispatch=adapter, logger=logging.getLogger("hal.test"))
    return service, adapter


def make_rule(service, owner_id, script, name="rule", rule_id=None):
    return service.upsert_rule(owner_id=owner_id, name=name, script=script, rule_id=rule_id)[0]


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ service


def test_entry_prompt_dispatch_binds_trigger_agent(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service, "user:7",
        'if @agent_any #turn_finish then @alice $prompt "hi @agent_trigger".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(agent_id="lily")

    run(service._process_owner("user:7"))

    assert len(adapter.prompt_calls) == 1
    call = adapter.prompt_calls[0]
    assert call["target_agent"] == "alice"
    assert call["text"] == "hi @lily@TESTINST"
    assert call["idempotency_key"].startswith("hal:hx")
    execs = service.store.list_execs("user:7")
    assert len(execs) == 1
    assert execs[0]["state"] == "completed"
    assert execs[0]["stage"] == 1
    # Second cycle must not re-dispatch.
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1


def test_automation_origin_event_never_starts_a_rule(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(key="hal:hx1:0")  # HAL-dispatched run completing

    run(service._process_owner("user:7"))
    assert adapter.prompt_calls == []


def test_bootstrap_adopts_cursor_without_replaying_history(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    feed.add_event()  # existed before HAL was enabled
    assert service.store.get_cursor("user:7") == -1

    # Enabling anchors the cursor: pre-enable history must never fire.
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    assert service.store.get_cursor("user:7") >= 1
    run(service._process_owner("user:7"))
    assert adapter.prompt_calls == []
    # New events after enable do fire.
    feed.add_event()
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1


def test_dependent_chain_advances_only_on_correlated_run(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice $prompt "start"; '
        'if @agent_any #turn_finish then @bob $prompt "next".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(key="user-run-1")

    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1
    exec_id = adapter.prompt_calls[0]["idempotency_key"].split(":")[1]

    # Unrelated completion starts its own entry execution (correct semantics)
    # but must NOT advance the first workflow's dependent stage.
    feed.add_event(key="someone-else")
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 2
    chain_exec = next(
        row for row in service.store.list_execs("user:7")
        if row["trigger_event_id"] == "evt-1"
    )
    assert chain_exec["stage"] == 1
    assert chain_exec["state"] == "active"

    # Correlated completion of the HAL-dispatched run: advance + complete.
    feed.add_event(key=f"hal:{exec_id}:0")
    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 3
    assert adapter.prompt_calls[2]["target_agent"] == "bob"
    updated = service.store.get_exec("user:7", chain_exec["exec_id"])
    assert updated["state"] == "completed"
    assert updated["stage"] == 2


def test_unavailable_dependent_stage_waits_honestly(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(
        service,
        "user:7",
        'if @agent_any #turn_finish then @alice $prompt "start"; '
        'if #reboot_ok then @bob $prompt "after reboot".',
    )
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    run(service._process_owner("user:7"))
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "active"
    assert "no reboot-completion" in (execs[0]["detail"] or "")


def test_system_idle_edge_fires_once_and_binds_none(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if #system_idle then @alice $prompt "cleanup".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.active_rows = []  # nothing active
    feed.add_event()       # one real non-automation terminal event
    feed.add_event(key="x")  # same transition batch: must not fire twice

    run(service._process_owner("user:7"))
    assert len(adapter.prompt_calls) == 1
    assert adapter.prompt_calls[0]["text"] == "cleanup"


def test_system_idle_does_not_fire_while_runs_active(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if #system_idle then @alice $prompt "cleanup".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.active_rows = [{"run_id": "run-x"}]
    feed.add_event()
    run(service._process_owner("user:7"))
    assert adapter.prompt_calls == []


def test_idle_trigger_prompt_with_unbound_trigger_fails_honestly(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if #system_idle then @alice $prompt "go @agent_trigger".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.active_rows = []
    feed.add_event()
    run(service._process_owner("user:7"))
    assert adapter.prompt_calls == []
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "failed"
    assert "no Agent" in (execs[0]["detail"] or "")


def test_transient_prompt_failure_retries_same_key(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    calls = {"n": 0}

    def script(key):
        calls["n"] += 1
        if calls["n"] == 1:
            return DispatchResult(accepted=False, transient=True, detail="busy")
        return DispatchResult(accepted=True, transient=False, detail="ok")

    adapter._prompt_script = script
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    run(service._process_owner("user:7"))
    # first attempt transient -> retried within the same cycle via retry pass
    assert len(adapter.prompt_calls) == 2
    assert adapter.prompt_calls[0]["idempotency_key"] == adapter.prompt_calls[1]["idempotency_key"]
    assert service.store.list_execs("user:7")[0]["state"] == "completed"


def test_command_rejection_fails_stage_honestly(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    adapter._command_results = {
        "/reboot max": DispatchResult(accepted=False, transient=False, detail="command not supported or disabled")
    }
    rule = make_rule(service, "user:7", 'if @lily@TESTINST #turn_finish then /reboot max.')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event(agent_id="lily")
    run(service._process_owner("user:7"))
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "failed"
    assert "not supported" in (execs[0]["detail"] or "")


def test_ambiguous_command_never_retries_after_interruption(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if @lily #turn_finish then /reboot max.')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    # Simulate: receipt pre-written, crash before outcome recorded.
    service.store.create_exec(
        owner_id="user:7", rule_id=rule["rule_id"], rule_revision=rule["revision"],
        rule_name="rule", workflow_index=0, exec_id="hx1", trigger_event_id="evt-1",
        trigger_agent="@lily@TESTINST", trigger_kind="run.completed",
        program_blob=__import__("hal").compile_hal('if @lily #turn_finish then /reboot max.'),
    )
    service.store.upsert_receipt(
        exec_id="hx1", stage=0, idempotency_key="hal:hx1:0", kind="native_command",
        target=None, payload="/reboot max", state="unknown", detail=None,
    )
    run(service._retry_pending("user:7", [], {"hx1": service.store.get_exec("user:7", "hx1")}, {(("hx1", 0)): service.store.get_receipt("hx1", 0)}))
    assert adapter.command_calls == []  # never re-executed
    execs = service.store.list_execs("user:7")
    assert execs[0]["state"] == "failed"
    assert "unknown after interruption" in (execs[0]["detail"] or "")


def test_enable_rejects_unavailable_entry_event(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    rule = make_rule(service, "user:7", 'if #turn_commentary then @alice $prompt "x".')
    with pytest.raises(HalStoreError):
        service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    assert service.store.get_rule("user:7", rule["rule_id"])["enabled"] == 0


# ------------------------------------------------------------------ API


class FakeServer:
    def __init__(self, service, admin=True):
        self.session_store = service.session_store
        self._hal_service = service
        self.global_config = SimpleNamespace(bridge_home=str(service.store.path.parent.parent))
        self.config_path = Path(service.store.path.parent.parent) / "config.json"
        self.logger = logging.getLogger("hal.test.api")
        self._admin = admin

    def _v1_owner_id(self, request):
        return "user:7"

    def _check_admin_auth(self, request):
        return self._admin


def api_client(server: FakeServer):
    app = web.Application()
    app.router.add_get("/hal/status", lambda r: handle_hal_status(server, r))
    app.router.add_post("/hal/validate", lambda r: handle_hal_validate(server, r))
    app.router.add_get("/hal/rules", lambda r: handle_hal_rules_get(server, r))
    app.router.add_post("/hal/rules", lambda r: handle_hal_rules_post(server, r))
    app.router.add_post("/hal/rules/{rule_id}/enable", lambda r: handle_hal_rule_enable(server, r))
    app.router.add_post("/hal/rules/{rule_id}/disable", lambda r: handle_hal_rule_disable(server, r))
    app.router.add_get("/hal/runs", lambda r: handle_hal_runs_get(server, r))
    return TestClient(TestServer(app))


def test_api_status_and_validate(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    server = FakeServer(service)

    async def scenario():
        async with api_client(server) as client:
            response = await client.get("/hal/status")
            payload = await response.json()
            assert response.status == 200
            assert payload["schema"] == "hashi.hal/v1"
            assert payload["event_availability"]["#turn_commentary"]["available"] is False
            assert payload["event_availability"]["#turn_finish"]["available"] is True

            response = await client.post("/hal/validate", json={"script": 'if #system_idle then @a $prompt "x".'})
            payload = await response.json()
            assert response.status == 200
            assert payload["ok"] is True
            assert payload["workflows"][0]["entry_available"] is True

            response = await client.post("/hal/validate", json={"script": "if #system_idle then"})
            payload = await response.json()
            assert payload["ok"] is False

            response = await client.post("/hal/validate", data=b"x" * (64 * 1024 + 1), headers={"Content-Type": "application/json"})
            assert response.status == 413

    asyncio.run(scenario())


def test_api_rule_lifecycle_and_revision_conflict(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    server = FakeServer(service)
    script = 'if @agent_any #turn_finish then @alice $prompt "hi".'

    async def scenario():
        async with api_client(server) as client:
            response = await client.post("/hal/rules", json={"name": "n1", "script": script})
            payload = await response.json()
            assert response.status == 201
            rule_id = payload["rule"]["rule_id"]
            assert payload["rule"]["revision"] == 1
            assert payload["rule"]["enabled"] is False

            # Enable works for available entry events (script revision unchanged).
            response = await client.post(f"/hal/rules/{rule_id}/enable", json={})
            assert (await response.json())["rule"]["enabled"] is True

            # Valid edit with matching revision bumps it.
            response = await client.post(
                "/hal/rules",
                json={"rule_id": rule_id, "name": "n1b", "script": script, "expected_revision": 1},
            )
            payload = await response.json()
            assert response.status == 200
            assert payload["rule"]["revision"] == 2

            # Stale edit now conflicts.
            response = await client.post(
                "/hal/rules",
                json={"rule_id": rule_id, "name": "n1c", "script": script, "expected_revision": 1},
            )
            assert response.status == 409

            response = await client.get("/hal/rules")
            payload = await response.json()
            assert len(payload["rules"]) == 1
            assert payload["rules"][0]["validation"]["ok"] is True

            response = await client.post(f"/hal/rules/{rule_id}/disable", json={})
            assert (await response.json())["rule"]["enabled"] is False

    asyncio.run(scenario())


def test_api_requires_admin_auth(tmp_path):
    feed = FakeSessionStore()
    service, _ = make_service(tmp_path, feed)
    server = FakeServer(service, admin=False)

    async def scenario():
        async with api_client(server) as client:
            response = await client.get("/hal/status")
            assert response.status == 401

    asyncio.run(scenario())


def test_api_runs_listing(tmp_path):
    feed = FakeSessionStore()
    service, adapter = make_service(tmp_path, feed)
    server = FakeServer(service)
    rule = make_rule(service, "user:7", 'if @agent_any #turn_finish then @alice $prompt "hi".')
    service.set_enabled(owner_id="user:7", rule_id=rule["rule_id"], enabled=True)
    feed.add_event()
    asyncio.run(service._process_owner("user:7"))

    async def scenario():
        async with api_client(server) as client:
            response = await client.get("/hal/runs")
            payload = await response.json()
            assert response.status == 200
            assert len(payload["runs"]) == 1
            assert payload["runs"][0]["state"] == "completed"
            assert payload["runs"][0]["trigger_agent"] == "@lily@TESTINST"

    asyncio.run(scenario())
# HASHI-Fingerprint: hfp_9717a97af9924e26925d98bfde62bb3d
