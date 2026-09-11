from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from tui.app import ChatHistory, ChatInput, HASHITuiApp, LogPanel
from tui.preferences import TuiPreferenceStore


class _QuietTui(HASHITuiApp):
    async def _run_startup_sequence(self):
        return


class _Client:
    proxied = False

    def __init__(self, agents=None):
        self.agents = list(agents or [])
        self.directory_error = ""
        self.sent = []
        self.status_checks = 0

    async def agents_info(self):
        if self.directory_error:
            return {"ok": False, "code": "connection_unavailable", "error": self.directory_error}
        return {"ok": True, "agents": list(self.agents)}

    async def send_chat(self, agent, text, **kwargs):
        self.sent.append((agent, text, kwargs))
        return {"ok": True, "session_id": "s", "run_id": "r", "request_id": "q"}

    async def run_info(self, *_args):
        self.status_checks += 1
        return {"ok": True, "run": {"state": "completed"}}

    async def get_recent_transcript(self, _agent, limit=20):
        return []

    def reset_offset(self, _agent):
        return None


def _agent(name: str, *, online: bool = True):
    return {
        "name": name,
        "display_name": name.title(),
        "online": online,
        "active_backend": "codex-cli",
    }


@pytest.mark.asyncio
async def test_directory_failure_is_not_empty_and_keeps_confirmed_selection(tmp_path):
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane")])
    app.api = client
    async with app.run_test() as pilot:
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        client.directory_error = "peer timed out"
        await app._handle_agents_cmd()
        await app._handle_to("/to all")
        await pilot.pause()

        assert app.current_agent == "akane"
        assert app.current_agent_display == "Akane"
        rendered = "\n".join(line.text for line in app.query_one(ChatHistory).lines)
        assert "directory unavailable" in rendered
        assert "No agents found" not in rendered


@pytest.mark.asyncio
async def test_zero_target_broadcast_does_not_change_selection_or_send(tmp_path):
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane", online=False)])
    app.api = client
    async with app.run_test() as pilot:
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        await app._handle_to("/to all")
        await pilot.pause()

        assert app.current_agent == "akane"
        assert app.current_agent_display == "Akane"
        assert client.sent == []


@pytest.mark.asyncio
async def test_broadcast_freezes_current_instance_targets_before_submission(tmp_path):
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane"), _agent("kasumi")])
    app.api = client
    async with app.run_test() as pilot:
        app.gateway_ok = True
        await app._handle_to("/to all")
        # A later directory mutation cannot expand or redirect this selection.
        client.agents = [_agent("other")]
        field = app.query_one(ChatInput)
        field.value = "scope marker"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert [call[0] for call in client.sent] == ["akane", "kasumi"]


@pytest.mark.asyncio
async def test_disabled_session_capability_never_polls_status_or_reports_failure(tmp_path):
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane")])
    app.api = client
    async with app.run_test() as pilot:
        app.gateway_ok = True
        app._persistent_session_available = False
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        field = app.query_one(ChatInput)
        field.value = "chat still succeeds"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert client.status_checks == 0
        rendered = "\n".join(line.text for line in app.query_one(ChatHistory).lines)
        assert "message was submitted" in " ".join(rendered.split())
        assert "Run status unavailable" not in rendered


@pytest.mark.asyncio
async def test_last_agent_is_namespaced_by_instance_and_never_persists_all(tmp_path):
    state = tmp_path / "state" / "tui_preferences.json"
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps({"last_agent_by_instance": {"HASHI1": "akane", "HASHI3": "agent1"}}),
        encoding="utf-8",
    )
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("kasumi"), _agent("akane")])
    app.api = client
    async with app.run_test() as pilot:
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        await app._load_agents(client=client, generation=0)
        await app._handle_to("/to all")
        await pilot.pause()

        assert app.current_agent_display == "ALL"
        saved = json.loads(state.read_text(encoding="utf-8"))
        assert saved["last_agent_by_instance"] == {"HASHI1": "akane", "HASHI3": "agent1"}


def test_preference_store_reads_bom_crlf_and_preserves_concurrent_fields(tmp_path):
    path = tmp_path / "preferences.json"
    path.write_bytes(b"\xef\xbb\xbf{\r\n  \"future\": 7\r\n}\r\n")
    store = TuiPreferenceStore(path)
    store.update(lambda value: value.update({"theme": "atm"}))

    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert json.loads(raw) == {"future": 7, "theme": "atm"}
