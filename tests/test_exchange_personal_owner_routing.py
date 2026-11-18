"""Exchange actor and target-session routing must use verified identities."""
from __future__ import annotations

import asyncio
import copy

from orchestrator.config import GlobalConfig
from orchestrator.workbench_api import WorkbenchApiServer
from tests.test_exchange_ingress import _address, _configure, _delivery, _evidence


class _Runtime:
    name = "reviewer"
    startup_success = True

    def __init__(self):
        self.calls = []

    async def enqueue_api_text(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return f"request-{len(self.calls)}"


def _api(root, owner):
    config = GlobalConfig(
        0, project_root=root, bridge_home=root, instance_id="HASHI2",
        workbench_port=0,
    )
    config.owner_scope_id = owner
    runtime = _Runtime()
    api = WorkbenchApiServer(root / "agents.json", config, runtimes=[runtime],
                             secrets={}, reconcile_session_runs=False)
    return api, runtime, config


def test_same_exchange_actor_uses_frozen_personal_owner_despite_other_historical_owners(tmp_path):
    _configure(tmp_path)
    api, runtime, config = _api(tmp_path, "user:current")
    current = api.session_store.create_session(owner_id="user:current", agent_id="reviewer")
    api.session_store.bind_primary_session(
        owner_id="user:current", agent_id="reviewer", session_id=current["session_id"],
    )
    api.session_store.create_session(owner_id="user:old", agent_id="reviewer")
    delivery = _delivery()
    delivery["sender"] = _address(
        actor="actor_barry", instance="instance_home", agent="planner",
        address="planner@home.barry",
    )
    prompt, evidence = _evidence(tmp_path, delivery)
    service = api._exchange_ingress_service()
    accepted = service.accept(evidence=evidence, prompt=prompt)
    assert accepted.admission_owner_id == "user:current"

    # Acceptance is durable: later configuration changes and service reopen
    # cannot silently redirect the already accepted request.
    config.owner_scope_id = "user:changed"
    reopened_api, reopened_runtime, _ = _api(tmp_path, "user:changed")
    reopened = reopened_api._exchange_ingress_service()
    assert reopened.accept(evidence=evidence, prompt=prompt).replayed
    result = asyncio.run(reopened.schedule(evidence=evidence, prompt=prompt))
    assert result.state == "scheduled"
    metadata = reopened_runtime.calls[0][1]["request_metadata"]
    assert metadata["owner_id"] == "user:current"
    assert metadata["session_id"] == current["session_id"]
    assert runtime.calls == []


def test_distinct_exchange_actors_do_not_enter_the_local_personal_session(tmp_path):
    _configure(tmp_path)
    api, runtime, _ = _api(tmp_path, "user:current")
    current = api.session_store.create_session(owner_id="user:current", agent_id="reviewer")
    api.session_store.bind_primary_session(
        owner_id="user:current", agent_id="reviewer", session_id=current["session_id"],
    )
    service = api._exchange_ingress_service()
    alice = _delivery()
    bob = copy.deepcopy(alice)
    bob["delivery_id"] = "delivery_bob"
    bob["message_id"] = "message_bob"
    bob["sender"] = _address(
        actor="actor_bob", instance="instance_bob", agent="planner",
        address="planner@home.bob",
    )
    for delivery in (alice, bob):
        prompt, evidence = _evidence(tmp_path, delivery)
        service.accept(evidence=evidence, prompt=prompt)
        asyncio.run(service.schedule(evidence=evidence, prompt=prompt))

    metadata = [call[1]["request_metadata"] for call in runtime.calls]
    assert [row["owner_id"] for row in metadata] == [
        "exchange:authority_1:actor_alice", "exchange:authority_1:actor_bob",
    ]
    assert len({row["session_id"] for row in metadata}) == 2
    assert all(row["session_id"] != current["session_id"] for row in metadata)



def test_selected_confirmation_uses_acceptance_owner_even_after_config_changes(tmp_path):
    import json
    import time

    from orchestrator.ingress_confirmation import IngressConfirmationService
    from remote.exchange_protocol import utc_timestamp

    _configure(tmp_path)
    path = tmp_path / "agents.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["global"]["hchat_request_age_policy"] = {"mode": "confirm", "max_age_seconds": 60}
    path.write_text(json.dumps(document), encoding="utf-8")
    api, _runtime, config = _api(tmp_path, "user:current")
    other = api.session_store.create_session(owner_id="user:old", agent_id="reviewer")
    delivery = _delivery(time.time() - 120)
    delivery["authorization_expires_at"] = utc_timestamp(time.time() + 120)
    delivery["expires_at"] = utc_timestamp(time.time() + 120)
    delivery["sender"] = _address(
        actor="actor_barry", instance="instance_home", agent="planner",
        address="planner@home.barry",
    )
    prompt, evidence = _evidence(tmp_path, delivery)
    service = api._exchange_ingress_service()
    accepted = service.accept(evidence=evidence, prompt=prompt)
    assert accepted.admission_owner_id == "user:current"
    config.owner_scope_id = "user:changed"
    result = asyncio.run(service.recover_pending())[0]
    assert result.state == "pending_confirmation"
    assert result.confirmation_owner_id == "user:current"
    confirmation = IngressConfirmationService(api.session_store).get(
        result.confirmation_id, owner_id="user:current",
    )
    assert confirmation["owner_id"] == "user:current"
    assert confirmation["session_id"] != other["session_id"]



def test_personal_request_does_not_scan_historical_owners(tmp_path, monkeypatch):
    import pytest

    _configure(tmp_path)
    api, runtime, _ = _api(tmp_path, "user:current")
    monkeypatch.setattr(
        api.session_store, "conversation_owner_ids",
        lambda **_kwargs: pytest.fail("current owner routing scanned historical partitions"),
    )
    delivery = _delivery()
    delivery["sender"] = _address(
        actor="actor_barry", instance="instance_home", agent="planner",
        address="planner@home.barry",
    )
    prompt, evidence = _evidence(tmp_path, delivery)
    service = api._exchange_ingress_service()
    service.accept(evidence=evidence, prompt=prompt)
    assert asyncio.run(service.schedule(evidence=evidence, prompt=prompt)).state == "scheduled"
    assert runtime.calls[0][1]["request_metadata"]["owner_id"] == "user:current"



def test_inbox_upgrade_keeps_legacy_rows_unbound_and_new_rows_bound(tmp_path):
    import sqlite3

    from orchestrator.exchange_ingress import ExchangeInbox

    path = tmp_path / "state" / "inbox.sqlite3"
    inbox = ExchangeInbox(path)
    first = _delivery()
    legacy = inbox.accept(first)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE exchange_inbox DROP COLUMN admission_owner_id")

    upgraded = ExchangeInbox(path)
    restored = upgraded.get(legacy.inbox_key)
    assert restored.admission_owner_id is None
    assert restored.payload_digest == legacy.payload_digest
    second = copy.deepcopy(first)
    second["message_id"] = "message-after-upgrade"
    second["delivery_id"] = "delivery-after-upgrade"
    saved = upgraded.accept(second, admission_owner_id="user:current")
    assert ExchangeInbox(path).get(saved.inbox_key).admission_owner_id == "user:current"
