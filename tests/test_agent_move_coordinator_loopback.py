"""Real source coordinator and Remote receiver, with disposable fixture state.

The health service represents absent Workers; this is not full live acceptance.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from orchestrator.agent_move import coordinator
from orchestrator.agent_move.remote_client import connect_agent_move_receiver
from orchestrator.session_store import SessionStore
from tests.test_agent_move_live_loopback import _free_port, _history, _Receiver, _root, _TOKEN


@pytest.fixture
def transfer_pair(tmp_path, monkeypatch):
    home = tmp_path / "private-home"
    home.mkdir()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    for key in list(os.environ):
        if key.upper().startswith(("HASHI_", "BRIDGE_", "KASUMI_")):
            monkeypatch.delenv(key, raising=False)

    source = _root(tmp_path / "source", "HASHI1", "owner:source", with_agent=True)
    target = _root(tmp_path / "target", "HASHI2", "owner:target", with_agent=False)

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/api/health":
                self.send_error(404)
                return
            data = json.dumps({"ok": True, "agents": [], "agent_runtime_presence": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    health = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    health.daemon_threads = True
    thread = threading.Thread(target=health.serve_forever, daemon=True)
    thread.start()
    for root in (source, target):
        directory = root / "state"
        directory.mkdir(exist_ok=True)
        (directory / "service_endpoints.json").write_text(json.dumps({
            "services": {"workbench": {"base_url": f"http://127.0.0.1:{health.server_port}"}},
        }), encoding="utf-8")
    receiver = _Receiver(target, _free_port(), workbench_port=health.server_port)
    try:
        receiver.start()
        yield source, target, {"HASHI2": {"host": "127.0.0.1", "remote_port": receiver.port}}
    finally:
        receiver.close()
        health.shutdown()
        health.server_close()
        thread.join(timeout=10)


@pytest.mark.parametrize("operation", ["clone", "move"])
def test_source_coordinator_transfers_only_selected_history_to_inactive_target(transfer_pair, operation):
    source, target, instances = transfer_pair
    source_store = SessionStore(source / "state" / "sessions.sqlite3", instance_id="HASHI1")
    selected = _history(source_store, "owner:source", "zelda", "selected")
    other = _history(source_store, "owner:other", "zelda", "other")
    target_store = SessionStore(target / "state" / "sessions.sqlite3", instance_id="HASHI2")
    unrelated = _history(target_store, "owner:target", "other-agent", "existing")

    if operation == "clone":
        args = dict(source_instance="HASHI1", target_agent_id="zelda-copy",
                    transfer_mode="workspace", history_mode="copy", desired_target_active=False)
        prepared = coordinator.prepare_outbound_clone(source, instances, "zelda", "HASHI2", **args)
        repeated = coordinator.prepare_outbound_clone(source, instances, "zelda", "HASHI2", **args)
        assert repeated["package_id"] == prepared["package_id"]
        agent = "zelda-copy"
    else:
        args = dict(source_instance="HASHI1", transfer_mode="identity_memory",
                    history_mode="move", desired_target_active=False)
        prepared = coordinator.prepare_outbound_move(source, instances, "zelda", "HASHI2", **args)
        agent = "zelda"

    package = prepared["package_id"]
    committed = coordinator.confirm_outbound_move(source, instances, package)
    if operation == "move":
        committed = coordinator.continue_outbound_move(source, instances, package)
    assert committed["status"] == "completed"
    assert committed["target_active"] is False
    assert coordinator.confirm_outbound_move(source, instances, package)["status"] == "completed"

    sessions = target_store.list_sessions(owner_id="owner:target", agent_id=agent, include_archived=True)
    assert len(sessions) == 1
    assert [row["text"] for row in target_store.messages(sessions[0]["session_id"], owner_id="owner:target")] == [
        "selected question", "selected answer",
    ]
    assert target_store.list_sessions(owner_id="owner:other", agent_id=agent, include_archived=True) == []
    assert [row["text"] for row in source_store.messages(other, owner_id="owner:other")] == [
        "other question", "other answer",
    ]
    assert [row["text"] for row in target_store.messages(unrelated, owner_id="owner:target")] == [
        "existing question", "existing answer",
    ]
    if operation == "clone":
        assert len(source_store.messages(selected, owner_id="owner:source")) == 2


@pytest.mark.parametrize("operation", ["clone", "move"])
@pytest.mark.parametrize("target_committed", [False, True])
def test_source_cancellation_rolls_back_real_receiver(transfer_pair, operation, target_committed):
    source, target, instances = transfer_pair
    source_store = SessionStore(source / "state" / "sessions.sqlite3", instance_id="HASHI1")
    selected = _history(source_store, "owner:source", "zelda", "selected")
    other = _history(source_store, "owner:other", "zelda", "other")
    target_store = SessionStore(target / "state" / "sessions.sqlite3", instance_id="HASHI2")
    unrelated = _history(target_store, "owner:target", "other-agent", "existing")
    if operation == "clone":
        prepared = coordinator.prepare_outbound_clone(
            source, instances, "zelda", "HASHI2", source_instance="HASHI1",
            target_agent_id="zelda-copy", transfer_mode="workspace", history_mode="copy",
            desired_target_active=False,
        )
        agent = "zelda-copy"
    else:
        prepared = coordinator.prepare_outbound_move(
            source, instances, "zelda", "HASHI2", source_instance="HASHI1",
            transfer_mode="identity_memory", history_mode="move", desired_target_active=False,
        )
        agent = "zelda"
    package = prepared["package_id"]
    client = connect_agent_move_receiver(instances, "HASHI2", source_instance="HASHI1", shared_token=_TOKEN)
    if target_committed:
        assert client.commit(package)["status"] == "committed_inactive"
    assert coordinator.cancel_outbound_move(source, instances, package)["status"] == "cancelled"
    assert coordinator.cancel_outbound_move(source, instances, package)["status"] == "cancelled"
    assert client.status(package)["status"] == "rolled_back"
    assert target_store.list_sessions(owner_id="owner:target", agent_id=agent, include_archived=True) == []
    assert len(source_store.messages(selected, owner_id="owner:source")) == 2
    assert len(source_store.messages(other, owner_id="owner:other")) == 2
    assert len(target_store.messages(unrelated, owner_id="owner:target")) == 2
