from __future__ import annotations

import json

import pytest

from orchestrator import telegram_delivery_state as state_store
from orchestrator.config_json import ConfigConflictError


def _owner(instance="HASHI1", lifecycle="1" * 32, token="bot-a"):
    return {
        "instance_id": instance,
        "agent_lifecycle_id": lifecycle,
        "telegram_bot_fingerprint": state_store.telegram_bot_fingerprint(token),
    }


def _record(owner=None, incident="incident-1"):
    return {
        "owner": dict(owner or _owner()),
        "status": "recovery_due",
        "incident_id": incident,
        "per_chat": {"123": {"undelivered_request_ids": ["req-1"]}},
    }


def test_mutation_reloads_after_revision_conflict_without_losing_other_agent(
    tmp_path, monkeypatch
):
    path = tmp_path / "state" / "telegram_delivery_health.json"
    real_write = state_store.write_config_json
    injected = False

    def interleaved_write(target, document):
        nonlocal injected
        if not injected:
            injected = True
            other = state_store.read_state(target)
            other["agents"]["other"] = _record(_owner(lifecycle="2" * 32))
            real_write(target, other)
            raise ConfigConflictError("injected stale snapshot")
        return real_write(target, document)

    monkeypatch.setattr(state_store, "write_config_json", interleaved_write)

    def add_agent(document):
        document["agents"]["kasumi"] = _record()
        return "done", True

    assert state_store.mutate_state(path, add_agent) == "done"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved["agents"]) == {"other", "kasumi"}


def test_corrupt_state_is_never_overwritten(tmp_path):
    path = tmp_path / "state" / "telegram_delivery_health.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{broken")

    with pytest.raises(state_store.DeliveryStateError):
        state_store.mutate_state(path, lambda document: (None, True))

    assert path.read_bytes() == b"{broken"


def test_clone_quarantines_target_residue_and_never_imports_source(tmp_path):
    path = tmp_path / "state" / "telegram_delivery_health.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "agents": {"clone": _record(_owner(lifecycle="3" * 32))},
                "quarantine": [],
            }
        ),
        encoding="utf-8",
    )

    result = state_store.adopt_transferred_state(
        tmp_path,
        "clone",
        target_owner=_owner(lifecycle="4" * 32, token="new-bot"),
        transferred_record=_record(),
        operation="clone",
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert result["imported"] is False
    assert "clone" not in saved["agents"]
    assert saved["quarantine"][-1]["reason"] == (
        "target_state_replaced_by_agent_transfer"
    )


def test_move_rebinds_instance_only_after_lifecycle_and_bot_match(tmp_path):
    source_owner = _owner(instance="HASHI1")
    target_owner = _owner(instance="HASHI3")

    result = state_store.adopt_transferred_state(
        tmp_path,
        "kasumi",
        target_owner=target_owner,
        transferred_record=_record(source_owner),
        operation="move",
    )

    assert result["imported"] is True
    saved = state_store.read_state(
        tmp_path / "state" / "telegram_delivery_health.json"
    )
    assert saved["agents"]["kasumi"]["owner"] == target_owner


def test_move_owner_mismatch_is_quarantined_and_never_activated(tmp_path):
    result = state_store.adopt_transferred_state(
        tmp_path,
        "kasumi",
        target_owner=_owner(instance="HASHI3", lifecycle="9" * 32),
        transferred_record=_record(_owner(instance="HASHI1")),
        operation="move",
    )

    saved = state_store.read_state(
        tmp_path / "state" / "telegram_delivery_health.json"
    )
    assert result["imported"] is False
    assert "kasumi" not in saved["agents"]
    assert saved["quarantine"][-1]["reason"] == "transferred_state_owner_mismatch"
    assert "token" not in saved["quarantine"][-1]["record"]


def test_quarantine_recursively_redacts_raw_telegram_credentials(tmp_path):
    raw_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghi"
    state = {
        "version": 2,
        "agents": {
            "kasumi": {
                **_record(),
                "token": raw_token,
                "nested": {
                    "bot_token": raw_token,
                    "error": f"https://api.telegram.org/bot{raw_token}/sendMessage",
                },
            }
        },
        "quarantine": [],
    }

    state_store.quarantine_record(state, "kasumi", reason="test")

    rendered = json.dumps(state)
    assert raw_token not in rendered
    assert "[REDACTED" in rendered


def test_transferred_state_rejects_malformed_target_owner(tmp_path):
    with pytest.raises(state_store.DeliveryStateError, match="owner is incomplete"):
        state_store.adopt_transferred_state(
            tmp_path,
            "kasumi",
            target_owner={"instance_id": "HASHI3"},
            transferred_record=_record(),
            operation="move",
        )
