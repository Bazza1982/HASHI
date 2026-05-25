import json
from datetime import datetime, timedelta

from scripts import hashi_remote_watchdog as watchdog


def _peer(instance_id: str, *, live_status: str = "online", agents: list[str] | None = None) -> dict:
    return {
        "instance_id": instance_id,
        "canonical": {
            "instance_id": instance_id,
            "properties": {
                "live_status": live_status,
                "remote_agents": [{"agent_name": name} for name in (agents or [])],
            },
        },
    }


def test_select_probe_targets_includes_hashi1_hashi9_and_online_intel():
    status = {
        "peers": [
            _peer("HASHI1", agents=["lily"]),
            _peer("HASHI9", agents=["hashiko"]),
            _peer("INTEL", live_status="online", agents=["agent1"]),
            _peer("MSI", live_status="offline", agents=["ying"]),
        ]
    }

    targets = watchdog._select_probe_targets(status)

    assert [item.address for item in targets] == [
        "lily@HASHI1",
        "hashiko@HASHI9",
        "agent1@INTEL",
    ]


def test_pick_best_agent_prefers_known_mapping_then_first_available():
    peer_state = _peer("MSI", agents=["agent2", "ying", "agent9"])

    assert watchdog._pick_agent("MSI", peer_state) == "ying"
    assert watchdog._pick_agent("UNKNOWN", peer_state) == "agent2"


def test_pick_best_agent_falls_back_to_default_name_when_directory_is_stale():
    assert watchdog._pick_agent("HASHI9", _peer("HASHI9", agents=[])) == "hashiko"


def test_disable_heartbeat_turns_job_off(tmp_path, monkeypatch):
    tasks_path = tmp_path / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "heartbeats": [
                    {"id": "job-1", "enabled": True, "loop_meta": {}},
                    {"id": "job-2", "enabled": True},
                ],
                "crons": [],
                "nudges": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(watchdog, "TASKS_PATH", tasks_path)

    assert watchdog._disable_heartbeat("job-2", "stable_window_complete") is True

    payload = json.loads(tasks_path.read_text(encoding="utf-8"))
    job = next(item for item in payload["heartbeats"] if item["id"] == "job-2")
    assert job["enabled"] is False
    assert job["loop_meta"]["stopped_reason"] == "stable_window_complete"


def test_mark_bug_resets_deadline_forward():
    state = {
        "bugs_found_count": 0,
        "stable_since": "2026-05-25T13:52:51+10:00",
        "deadline_at": "2026-06-01T13:52:51+10:00",
    }

    watchdog._mark_bug(state, "probe failed")

    assert state["bugs_found_count"] == 1
    assert state["last_bug_summary"] == "probe failed"
    deadline = datetime.fromisoformat(state["deadline_at"])
    bug_time = datetime.fromisoformat(state["last_bug_at"])
    assert deadline >= bug_time + timedelta(days=6, hours=23)


def test_mark_bug_does_not_reset_window_for_same_unresolved_bug():
    state = {
        "bugs_found_count": 2,
        "stable_since": "2026-05-26T01:56:49.884177+10:00",
        "deadline_at": "2026-06-02T01:56:49.884177+10:00",
        "last_bug_at": "2026-05-26T01:56:49.884177+10:00",
        "last_bug_summary": "Probe failed for hashiko@HASHI9: check_ok=True send_ok=False",
        "last_run_status": "probe_failed",
    }

    watchdog._mark_bug(state, "Probe failed for hashiko@HASHI9: check_ok=True send_ok=False")

    assert state["bugs_found_count"] == 2
    assert state["stable_since"] == "2026-05-26T01:56:49.884177+10:00"
    assert state["deadline_at"] == "2026-06-02T01:56:49.884177+10:00"
    assert state["last_bug_at"] == "2026-05-26T01:56:49.884177+10:00"


def test_peer_summary_extracts_runtime_route_fields():
    peer = _peer("HASHI9", agents=["hashiko"])
    peer["canonical"]["host"] = "192.168.0.211"
    peer["canonical"]["port"] = 35821
    peer["canonical"]["workbench_port"] = 18819
    peer["canonical"]["properties"].update(
        {
            "handshake_state": "handshake_accepted",
            "preferred_backend": "lan",
        }
    )

    summary = watchdog._peer_summary(peer)

    assert summary == {
        "instance_id": "HASHI9",
        "remote_port": 35821,
        "workbench_port": 18819,
        "live_status": "online",
        "handshake_state": "handshake_accepted",
        "preferred_backend": "lan",
        "host": "192.168.0.211",
        "agents": ["hashiko"],
    }


def test_run_watchdog_marks_hashi9_offline_as_issue(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "watchdog.jsonl"
    monkeypatch.setattr(watchdog, "STATE_PATH", state_path)
    monkeypatch.setattr(watchdog, "LOG_PATH", log_path)
    monkeypatch.setattr(watchdog, "_runtime_claim_port", lambda: 8767)
    monkeypatch.setattr(
        watchdog,
        "_remote_status",
        lambda port: (
            True,
            {
                "peers": [
                    _peer("HASHI1", live_status="online", agents=["lily"]),
                    _peer("HASHI9", live_status="offline", agents=["hashiko"]),
                ]
            },
            None,
        ),
    )
    monkeypatch.setattr(
        watchdog,
        "_probe_target",
        lambda sender, target: {
            "instance_id": target.instance_id,
            "agent": target.agent_name,
            "required_online": target.required_online,
            "check_ok": target.instance_id != "HASHI9",
            "check_stdout": "",
            "check_stderr": "",
            "send_ok": target.instance_id != "HASHI9",
            "send_stdout": "",
            "send_stderr": "",
        },
    )

    result = watchdog.run_watchdog("job-1", "lin_yueru", "2026-06-01T13:52:51+10:00")

    assert result["ok"] is False
    assert result["peer_statuses"]["HASHI9"]["live_status"] == "offline"
    assert any("HASHI9 reported offline" in issue for issue in result["issues"])


def test_run_watchdog_allows_stale_offline_if_probe_succeeds(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "watchdog.jsonl"
    monkeypatch.setattr(watchdog, "STATE_PATH", state_path)
    monkeypatch.setattr(watchdog, "LOG_PATH", log_path)
    monkeypatch.setattr(watchdog, "TASKS_PATH", tmp_path / "tasks.json")
    monkeypatch.setattr(watchdog, "_runtime_claim_port", lambda: 8767)
    monkeypatch.setattr(
        watchdog,
        "_remote_status",
        lambda port: (
            True,
            {
                "peers": [
                    _peer("HASHI1", live_status="unknown", agents=[]),
                    _peer("HASHI9", live_status="offline", agents=[]),
                ]
            },
            None,
        ),
    )
    monkeypatch.setattr(
        watchdog,
        "_probe_target",
        lambda sender, target: {
            "instance_id": target.instance_id,
            "agent": target.agent_name,
            "required_online": target.required_online,
            "check_ok": True,
            "check_stdout": "",
            "check_stderr": "",
            "send_ok": True,
            "send_stdout": "",
            "send_stderr": "",
        },
    )

    result = watchdog.run_watchdog("job-1", "lin_yueru", "2026-06-01T13:52:51+10:00")

    assert result["ok"] is True
    assert [item["agent"] for item in result["probe_targets"]] == ["lily", "hashiko"]
    assert result["issues"] == []
