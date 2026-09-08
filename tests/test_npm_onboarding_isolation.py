from __future__ import annotations

import json
import os

from onboarding import onboarding_main


def test_npm_onboarding_writes_all_instance_state_beneath_bridge_home(
    tmp_path, monkeypatch
):
    bridge_home = tmp_path / "instance home"
    bridge_home.mkdir()
    (bridge_home / "agents.json").write_text(
        json.dumps(
            {
                "global": {
                    "instance_id": "isolated",
                    "workbench_port": 19100,
                    "api_gateway_port": 19101,
                },
                "agents": [],
            }
        ),
        encoding="utf-8",
    )
    language = {
        "_file": "english.json",
        "displayName": "English",
        "welcomePrompt": "Welcome",
        "continueToHatch": "Continue {style}now{reset}",
        "auditStart": "Audit",
        "auditResultCli": "Found {cli}",
        "hatcheryComplete": "Complete",
        "launching": "Launching",
    }
    answers = iter(["1", "I AGREE", ""])
    monkeypatch.setenv("BRIDGE_HOME", str(bridge_home))
    monkeypatch.setenv("HASHI_ONBOARD_NO_LAUNCH", "1")
    monkeypatch.setattr(onboarding_main, "_LOG_PATH", bridge_home / "onboarding_crash.log")
    monkeypatch.setattr(onboarding_main, "load_languages", lambda: [language])
    monkeypatch.setattr(
        onboarding_main, "audit_environment", lambda: ("Codex CLI", "codex-cli")
    )
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: next(answers))

    onboarding_main.run_onboarding()

    workspace = bridge_home / "workspaces" / "onboarding_agent"
    assert (workspace / "agent.md").is_file()
    assert (workspace / "initial.md").is_file()
    assert (workspace / "AGENT_FYI.md").is_file()
    assert (workspace / "transcript.jsonl").is_file()
    assert (workspace / "WAKEUP.prompt").is_file()
    config = json.loads((bridge_home / "agents.json").read_text(encoding="utf-8"))
    assert config["global"]["instance_id"] == "isolated"
    assert config["global"]["workbench_port"] == 19100
    assert config["agents"][0]["name"] == "hashiko"
    assert (bridge_home / "secrets.json").is_file()
    if os.name != "nt":
        assert (bridge_home / "secrets.json").stat().st_mode & 0o077 == 0
    assert (bridge_home / ".bridge_u_last_agents.txt").read_text(
        encoding="utf-8"
    ) == "selected|hashiko\n"
    assert (bridge_home / ".bridge_u_lang.txt").read_text(encoding="utf-8") == "en"


def test_declining_repeat_onboarding_does_not_reset_existing_instance_data(
    tmp_path, monkeypatch
):
    bridge_home = tmp_path / "existing"
    workspace = bridge_home / "workspaces" / "onboarding_agent"
    workspace.mkdir(parents=True)
    agents_path = bridge_home / "agents.json"
    secrets_path = bridge_home / "secrets.json"
    transcript_path = workspace / "transcript.jsonl"
    agents_path.write_text(
        '{"global":{"instance_id":"KEEP"},"agents":[{"name":"hashiko"}]}\n',
        encoding="utf-8",
    )
    secrets_path.write_text('{"openrouter_key":"keep-secret"}\n', encoding="utf-8")
    transcript_path.write_text('{"text":"keep-history"}\n', encoding="utf-8")
    before = {
        path: path.read_bytes()
        for path in (agents_path, secrets_path, transcript_path)
    }
    language = {
        "_file": "english.json",
        "displayName": "English",
        "alreadyCompleted": "Already complete",
        "resetConfirm": "Reset?",
        "enjoyMessage": "Enjoy",
    }
    answers = iter(["1", "I AGREE", "n"])
    monkeypatch.setenv("BRIDGE_HOME", str(bridge_home))
    monkeypatch.setattr(onboarding_main, "_LOG_PATH", bridge_home / "onboarding_crash.log")
    monkeypatch.setattr(onboarding_main, "load_languages", lambda: [language])
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: next(answers))

    onboarding_main.run_onboarding()

    assert {path: path.read_bytes() for path in before} == before
