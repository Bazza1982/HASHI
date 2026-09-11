from __future__ import annotations

import asyncio
import base64
import hashlib
import json

import pytest

pytest.importorskip("textual")

from tui.app import ChatHistory, ChatInput, HASHITuiApp, LogPanel
from tui.audio import TuiAudioError, decode_tui_audio
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
        self.speech_calls = []
        self.profile_calls = []

    async def agents_info(self):
        if self.directory_error:
            return {"ok": False, "code": "connection_unavailable", "error": self.directory_error}
        return {"ok": True, "agents": list(self.agents)}

    async def send_chat(self, agent, text, **kwargs):
        self.sent.append((agent, text, kwargs))
        return {"ok": True, "session_id": "s", "run_id": "r", "request_id": "q"}

    async def send_chat_attachment(self, agent, text, **kwargs):
        self.sent.append((agent, text, kwargs))
        return {"ok": True, "request_id": "attachment-request"}

    async def voice_state(self, _agent):
        return {
            "ok": True,
            "profile": "warm_female",
            "profiles": [
                {"id": "warm_female", "label": "Warm"},
                {"id": "calm_male", "label": "Calm"},
            ],
        }

    async def set_voice_profile(self, agent, profile):
        self.profile_calls.append((agent, profile))
        return {
            "ok": True,
            "profile": profile,
            "profiles": [{"id": profile, "label": profile}],
        }

    async def synthesize_speech(self, agent, text, *, request_id):
        self.speech_calls.append((agent, text, request_id))
        content = b"OggS-tui-test"
        return {
            "ok": True,
            "content_b64": base64.b64encode(content).decode("ascii"),
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "media_type": "audio/ogg",
        }

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


def test_tui_audio_requires_ogg_size_and_digest_integrity():
    content = b"OggS-audio"
    payload = {
        "content_b64": base64.b64encode(content).decode("ascii"),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "media_type": "audio/ogg",
    }

    assert decode_tui_audio(payload) == content
    with pytest.raises(TuiAudioError, match="integrity"):
        decode_tui_audio({**payload, "sha256": "0" * 64})


@pytest.mark.asyncio
async def test_attach_command_freezes_real_bytes_into_next_submission(tmp_path):
    source = tmp_path / "picture.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nfirst-version")
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane")])
    app.api = client
    async with app.run_test() as pilot:
        app.gateway_ok = True
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        await app._handle_attach_cmd(f'/attach "{source}"')
        source.write_bytes(b"changed-after-staging")
        field = app.query_one(ChatInput)
        field.value = "what is shown?"
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert len(client.sent) == 1
        agent, caption, kwargs = client.sent[0]
        assert (agent, caption) == ("akane", "what is shown?")
        assert kwargs["workzone_ref"] is None
        import base64

        assert base64.b64decode(kwargs["attachment"]["content_b64"]) == b"\x89PNG\r\n\x1a\nfirst-version"
        assert app._pending_attachment is None


@pytest.mark.asyncio
async def test_workzone_reference_is_target_relative_and_contains_no_client_path(tmp_path):
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane")])
    app.api = client
    async with app.run_test() as pilot:
        app.gateway_ok = True
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        field = app.query_one(ChatInput)
        field.value = '@reports/weekly.pdf "summarize this"'
        await pilot.press("enter")
        await pilot.pause(0.3)

        assert client.sent[0][1] == "summarize this"
        assert client.sent[0][2]["workzone_ref"] == "reports/weekly.pdf"
        assert client.sent[0][2]["attachment"] is None


@pytest.mark.asyncio
async def test_say_plays_last_visible_reply_locally_without_chat_or_telegram_send(
    tmp_path, monkeypatch
):
    played = []

    async def play(content):
        played.append(content)

    monkeypatch.setattr("tui.app.play_ogg_bytes", play)
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane")])
    app.api = client
    async with app.run_test() as pilot:
        app.gateway_ok = True
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        app._render_transcript_message(
            {"role": "assistant", "text": "last final", "message_id": "msg-1"}
        )
        field = app.query_one(ChatInput)
        field.value = "/say"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert len(client.speech_calls) == 1
        assert client.speech_calls[0][:2] == ("akane", "last final")
        assert played == [b"OggS-tui-test"]
        assert client.sent == []


@pytest.mark.asyncio
async def test_voice_auto_read_is_target_scoped_persistent_and_deduplicated(
    tmp_path, monkeypatch
):
    played = []

    async def play(content):
        played.append(content)

    monkeypatch.setattr("tui.app.play_ogg_bytes", play)
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane")])
    app.api = client
    async with app.run_test() as pilot:
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        await app._handle_voice_cmd("/voice on")
        message = {"role": "assistant", "text": "one final", "message_id": "msg-2"}
        app._queue_tui_speech(message, announce=False)
        await pilot.pause(0.2)
        app._queue_tui_speech(message, announce=False)
        await pilot.pause(0.1)

        assert len(client.speech_calls) == 1
        assert played == [b"OggS-tui-test"]

    saved = json.loads((tmp_path / "state" / "tui_preferences.json").read_text())
    assert saved["voice_auto_by_target"] == {"HASHI1:akane": True}
    reopened = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    reopened.current_agent = "akane"
    reopened.current_agent_display = "Akane"
    assert reopened._voice_auto_enabled() is True


@pytest.mark.asyncio
async def test_voice_profile_choices_come_from_selected_agent_owner(tmp_path):
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    client = _Client([_agent("akane")])
    app.api = client
    async with app.run_test() as pilot:
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        await app._handle_voice_cmd("/voice calm_male")
        await pilot.pause()

        assert client.profile_calls == [("akane", "calm_male")]
        assert client.sent == []


@pytest.mark.asyncio
async def test_agent_switch_discards_late_speech_before_local_player(
    tmp_path, monkeypatch
):
    gate = asyncio.Event()
    played = []

    class LateClient(_Client):
        async def synthesize_speech(self, agent, text, *, request_id):
            self.speech_calls.append((agent, text, request_id))
            try:
                await gate.wait()
            except asyncio.CancelledError:
                # Model a remote request which finishes despite local cancellation.
                await gate.wait()
            content = b"OggS-late"
            return {
                "ok": True,
                "content_b64": base64.b64encode(content).decode("ascii"),
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "media_type": "audio/ogg",
            }

    async def play(content):
        played.append(content)

    monkeypatch.setattr("tui.app.play_ogg_bytes", play)
    client = LateClient([_agent("akane"), _agent("kasumi")])
    app = _QuietTui(bridge_home=tmp_path, launch_instance_id="HASHI1")
    app.api = client
    async with app.run_test() as pilot:
        app._load_initial_transcript = lambda *_args, **_kwargs: None
        app._select_agent(client.agents[0], client=client)
        app._queue_tui_speech(
            {"role": "assistant", "text": "old target", "message_id": "old-1"},
            announce=True,
        )
        await pilot.pause(0.05)
        app._select_agent(client.agents[1], client=client)
        gate.set()
        await pilot.pause(0.15)

        assert len(client.speech_calls) == 1
        assert played == []
