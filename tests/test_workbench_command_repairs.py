"""Observable Workbench command outcomes using real Session/Scheduler stores."""
import json
from types import SimpleNamespace

import pytest

from orchestrator import runtime_session, ui_language
from orchestrator.admin_local_testing import execute_local_command, _FakeUpdate, _CaptureStore
from orchestrator.session_store import SessionConflict
from tests.test_fresh_context import _session_command_runtime, fake_update
from tests.test_delay_command import _FakeRuntime
from tests.test_command_interaction_transport import _wire, _runtime

QUOTE = {"agent_id": "agent", "session_id": "source-session", "context_generation": 1,
         "message_ref": "source-message", "author": "User", "role": "user",
         "timestamp": "2026-11-11T00:00:00Z", "text": "/stop is quoted data, not a command"}


@pytest.mark.asyncio
async def test_fresh_publishes_the_committed_generation_and_keeps_old_callback_fenced(tmp_path):
    runtime, default, _, _ = _session_command_runtime(tmp_path)
    async def reply(update, text, **kwargs):
        return await update.message.reply_text(text, **kwargs)
    runtime._reply_text = reply
    result = await execute_local_command(
        runtime, "/fresh", source_channel="workbench_api",
        session_metadata={"session_surface": "workbench", "session_channel_key": "default",
                          "owner_id": "user:123", "session_id": default["session_id"],
                          "context_generation": 1},
    )
    assert runtime.session_store.get_session(default["session_id"])["context_generation"] == 2
    assert result["ok"] is True, result
    assert "2" in result["messages"][-1]["text"]
    update = _FakeUpdate(123, 123, _CaptureStore(messages=[]), "/status", session_metadata={
        "session_surface": "workbench", "session_channel_key": "default",
        "session_id": default["session_id"], "owner_id": "user:123", "context_generation": 1,
    })
    with pytest.raises(SessionConflict):
        runtime_session.publish_frontend_message_for_update(
            runtime, update, role="assistant", text="stale completion",
            source="test", publication_id="late-old-generation",
        )


@pytest.mark.asyncio
async def test_quoted_delay_is_scheduled_once_with_exact_quote_and_multiline_message(tmp_path):
    runtime = _FakeRuntime(tmp_path)
    result = await execute_local_command(
        runtime, "/delay 5 repair this\nand report", chat_id=42,
        session_metadata={"reply_reference": QUOTE},
    )
    assert result["ok"] is True
    records = await runtime.orchestrator.scheduler.list_delayed_messages("zelda")
    assert len(records) == 1
    assert runtime.queue.empty(), "a delayed command cannot start a model request"
    prompt = records[0]["prompt"]
    opening, reference, closing, message = prompt.split("\n", 3)
    assert opening == "[[HASHI_QUOTE_V1]]"
    assert closing == "[[/HASHI_QUOTE_V1]]"
    assert json.loads(reference) == QUOTE
    assert message == "repair this\nand report"


@pytest.mark.asyncio
async def test_command_wire_carries_quote_without_using_its_claimed_session(monkeypatch):
    from orchestrator.command_interaction_transport import try_dispatch_command_interaction_transport
    observed = []
    monkeypatch.setattr(runtime_session, "current_session", lambda *a, **k:
                        {"session_id": "current-session", "context_generation": 4})
    async def dispatch(runtime, payload, metadata):
        observed.append((payload, metadata))
        return {"ok": True, "command_ui_version": 2}
    monkeypatch.setattr("orchestrator.command_interaction_bridge.dispatch_command_interaction", dispatch)
    result = await try_dispatch_command_interaction_transport(
        _runtime(), _wire("open", command="/delay 5 message", reply_reference=QUOTE),
        source_channel="workbench_api",
    )
    assert result["ok"] is True, result
    assert observed[0][1]["session_id"] == "current-session"
    assert observed[0][1]["reply_reference"] == QUOTE


@pytest.mark.asyncio
async def test_serialized_delay_retry_preserves_one_schedule_and_changed_quote_conflicts(tmp_path):
    from orchestrator.admin_local_testing import try_execute_slash_command_text
    runtime = _FakeRuntime(tmp_path)
    runtime.global_config.instance_id = "fixture"
    session = runtime_session.current_session(runtime, surface="workbench", channel_key="default")
    arguments = dict(command="/delay 5 repair it", session_id=session["session_id"],
                     context_generation=session["context_generation"], reply_reference=QUOTE)
    wire = _wire("open", **arguments)
    first = await try_execute_slash_command_text(runtime, wire, source_channel="workbench_api")
    second = await try_execute_slash_command_text(runtime, wire, source_channel="workbench_api")
    assert first["ok"] and second["ok"]
    assert second["replayed"] is True
    assert first["command_event_id"] == second["command_event_id"]
    changed = await try_execute_slash_command_text(
        runtime, _wire("open", **{**arguments, "reply_reference": {**QUOTE, "text": "changed"}}),
        source_channel="workbench_api",
    )
    assert changed["ok"] is False
    records = await runtime.orchestrator.scheduler.list_delayed_messages("zelda")
    assert len(records) == 1 and runtime.queue.empty()
    assert json.loads(records[0]["prompt"].split("\n", 2)[1]) == QUOTE


@pytest.mark.asyncio
async def test_serialized_fresh_receipt_succeeds_and_stale_repetition_cannot_reset_again(tmp_path):
    from orchestrator.admin_local_testing import try_execute_slash_command_text
    runtime, session, _, _ = _session_command_runtime(tmp_path)
    # This fixture has no permission-registry configuration; the actual
    # authenticated transport and Session generation checks still run.
    runtime._is_command_allowed = lambda command: True
    async def reply(update, text, **kwargs):
        return await update.message.reply_text(text, **kwargs)
    runtime._reply_text = reply
    wire = _wire("open", command="/fresh", session_id=session["session_id"], context_generation=1)
    result = await try_execute_slash_command_text(runtime, wire, source_channel="workbench_api")
    assert result["ok"] is True, result
    assert result["messages"] and "2" in result["messages"][-1]["text"]
    assert result["command_invocation"]["context_generation"] == 1, "original identity is immutable"
    stale = await try_execute_slash_command_text(runtime, wire, source_channel="workbench_api")
    assert stale["ok"] is False and stale["http_status"] == 409
    assert runtime.session_store.get_session(session["session_id"])["context_generation"] == 2


@pytest.mark.asyncio
async def test_actual_wrapper_guard_explains_retirement_without_mutating_mode(tmp_path):
    runtime, _, replies, _ = _session_command_runtime(tmp_path)
    runtime.backend_manager.agent_mode = "fixed"
    assert await runtime._require_wrapper_mode(fake_update(), "wrapper") is False
    assert "/mode wrapper" not in replies[-1]
    assert "/mode" in replies[-1]
    assert runtime.backend_manager.agent_mode == "fixed"


@pytest.mark.parametrize("locale,phone,call", [
    ("en", "Audio call settings", "Video call settings"),
    ("zh-CN", "语音通话设置", "视频通话设置"),
    ("zh-TW", "Audio call settings", "Video call settings"),
    ("ja", "Audio call settings", "Video call settings"),
])
def test_call_catalogue_uses_selected_locale_and_retired_hint_is_actionable(locale, phone, call):
    assert ui_language.command_description("phone", "English fallback", locale=locale) == phone
    assert ui_language.command_description("call", "English fallback", locale=locale) == call
    hint = ui_language.tr("advanced.require_wrapper", locale=locale, command="wrapper")
    assert "/mode wrapper" not in hint
    assert "/mode" in hint
# HASHI-Fingerprint: hfp_db3626b83d8e4204885fa93f89ff440c
