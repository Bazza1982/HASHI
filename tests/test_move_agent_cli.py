from __future__ import annotations

import sys

import pytest

from scripts import move_agent


def test_confirm_persists_work_for_shared_background_owner(monkeypatch):
    submit = []
    monkeypatch.setattr(
        move_agent,
        "enqueue_agent_move",
        lambda root, instances, package_id, **kwargs: submit.append(
            (root, instances, package_id, kwargs)
        )
        or {"accepted": True, "operation": {"status": "accepted"}},
    )

    result = move_agent._submit_confirmation(
        "move-1", {"hashi3": {"instance_id": "HASHI3"}}
    )

    assert result["accepted"] is True
    assert submit == [
        (
            move_agent.HASHI_ROOT,
            {"hashi3": {"instance_id": "HASHI3"}},
            "move-1",
            {"requested_by": "cli", "origin": {"surface": "cli"}},
        )
    ]


def test_confirmed_clone_uses_same_persistent_submission(monkeypatch):
    monkeypatch.setattr(
        move_agent,
        "enqueue_agent_move",
        lambda *_args, **_kwargs: {
            "accepted": True,
            "operation": {"package_id": "clone-1", "operation": "clone"},
        },
    )

    result = move_agent._submit_confirmation("clone-1", {})

    assert result["accepted"] is True
    assert result["operation"]["operation"] == "clone"


def test_confirm_cli_returns_persisted_acceptance_without_running_cutover(
    monkeypatch, capsys
):
    submit = []
    monkeypatch.setattr(move_agent, "load_instances", lambda *_args: {"hashi2": {}})
    monkeypatch.setattr(
        move_agent,
        "enqueue_agent_move",
        lambda root, instances, package_id, **kwargs: submit.append(package_id)
        or {
            "accepted": True,
            "duplicate": False,
            "operation": {"package_id": package_id, "status": "accepted"},
        },
    )
    monkeypatch.setattr(sys, "argv", ["move_agent.py", "--confirm", "move-9"])

    move_agent.main()

    assert submit == ["move-9"]
    output = capsys.readouterr().out
    assert '"accepted": true' in output
    assert '"status": "accepted"' in output


def test_status_cli_reads_shared_background_receipt(monkeypatch, capsys):
    monkeypatch.setattr(move_agent, "load_instances", lambda *_args: {})
    monkeypatch.setattr(
        move_agent,
        "get_agent_move_execution_status",
        lambda _root, package_id: {
            "package_id": package_id,
            "status": "completed",
        },
    )
    monkeypatch.setattr(sys, "argv", ["move_agent.py", "--status", "move-9"])

    move_agent.main()

    assert '"status": "completed"' in capsys.readouterr().out


def test_keep_source_compatibility_flag_stages_nothing(monkeypatch, capsys):
    monkeypatch.setattr(
        move_agent,
        "prepare_outbound_move",
        lambda *args, **kwargs: pytest.fail("retired copy mode staged a transfer"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["move_agent.py", "kasumi", "hashi3", "--keep-source"],
    )

    with pytest.raises(SystemExit):
        move_agent.main()

    assert "/clone" in capsys.readouterr().err
