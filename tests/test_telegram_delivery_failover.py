from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

from orchestrator import telegram_delivery_failover as failover
from orchestrator import runtime_status, telegram_stream_policy
from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime


class _Bot:
    def __init__(self, *, send_error=None):
        self.messages = []
        self.send_error = send_error

    async def send_message(self, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.messages))


def _runtime(tmp_path: Path, name: str, *, preview_default: bool = True):
    workspace = tmp_path / "workspaces" / name
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = SimpleNamespace(
        name=name,
        workspace_dir=workspace,
        config=SimpleNamespace(
            extra={
                "telegram_stream_enabled": True,
                "answer_stream_preview": preview_default,
            },
            telegram_token_key=name,
        ),
        global_config=SimpleNamespace(project_root=tmp_path),
        app=SimpleNamespace(bot=_Bot()),
        telegram_connected=True,
        startup_success=True,
        token=f"token-{name}",
    )
    return runtime


@pytest.mark.asyncio
async def test_handle_retry_after_persists_incident_and_warns_failover(tmp_path):
    source = _runtime(tmp_path, "kasumi", preview_default=False)
    fail = _runtime(tmp_path, "lin_yueru")
    orchestrator = SimpleNamespace(runtimes=[source, fail], raw_config={})
    source.orchestrator = orchestrator
    fail.orchestrator = orchestrator

    await failover.handle_retry_after(
        source,
        exc=RetryAfter(123),
        chat_id=321,
        request_id="req-1",
        purpose="response",
        text="final answer",
    )

    state_path = tmp_path / "state" / "telegram_delivery_health.json"
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    record = saved["agents"]["kasumi"]
    assert record["status"] == "blocked"
    assert record["retry_after_s"] == 123
    assert fail.app.bot.messages
    assert "Delivery warning from HASHI2" in fail.app.bot.messages[0]["text"]
    assert (tmp_path / "workspaces" / "kasumi" / "undelivered" / "req-1.md").exists()
    assert (tmp_path / "workspaces" / "kasumi" / "undelivered" / "req-1.json").exists()


@pytest.mark.asyncio
async def test_handle_blocked_send_dedupes_warning(tmp_path):
    source = _runtime(tmp_path, "kasumi")
    fail = _runtime(tmp_path, "lin_yueru")
    orchestrator = SimpleNamespace(runtimes=[source, fail], raw_config={})
    source.orchestrator = orchestrator
    fail.orchestrator = orchestrator

    await failover.handle_retry_after(
        source,
        exc=RetryAfter(60),
        chat_id=321,
        request_id="req-1",
        purpose="response",
        text="first answer",
    )
    count_after_first = len(fail.app.bot.messages)
    blocked = await failover.handle_blocked_send(
        source,
        chat_id=321,
        request_id="req-2",
        purpose="response",
        text="second answer",
    )

    assert blocked is True
    assert len(fail.app.bot.messages) == count_after_first


@pytest.mark.asyncio
async def test_failover_warning_generic_error_is_recorded_not_raised(tmp_path):
    source = _runtime(tmp_path, "kasumi")
    fail = _runtime(tmp_path, "lin_yueru")
    fail.app.bot = _Bot(send_error=RuntimeError("telegram network down"))
    orchestrator = SimpleNamespace(runtimes=[source, fail], raw_config={})
    source.orchestrator = orchestrator
    fail.orchestrator = orchestrator

    await failover.handle_retry_after(
        source,
        exc=RetryAfter(60),
        chat_id=321,
        request_id="req-1",
        purpose="response",
        text="answer",
    )

    saved = json.loads((tmp_path / "state" / "telegram_delivery_health.json").read_text(encoding="utf-8"))
    record = saved["agents"]["kasumi"]
    assert record["status"] == "blocked"
    assert record["failover_failed"] is True
    assert "RuntimeError: telegram network down" in record["last_failover_error"]


@pytest.mark.asyncio
async def test_tick_recovery_clears_block_and_sends_notice(tmp_path):
    source = _runtime(tmp_path, "kasumi")
    orchestrator = SimpleNamespace(runtimes=[source], raw_config={}, global_cfg=SimpleNamespace(project_root=tmp_path))
    source.orchestrator = orchestrator
    path = tmp_path / "state" / "telegram_delivery_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {
                    "kasumi": {
                        "token_key": "telegram:kasumi",
                        "status": "blocked",
                        "blocked_until": "2000-01-01T00:00:00+00:00",
                        "retry_after_s": 60,
                        "incident_id": "tg-kasumi-test",
                        "per_chat": {"321": {"first_warned_at": "2000-01-01T00:00:00+00:00"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    await failover._tick_recovery(orchestrator)

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["agents"]["kasumi"]["status"] == "healthy"
    assert source.app.bot.messages
    assert "Delivery recovered" in source.app.bot.messages[0]["text"]


@pytest.mark.asyncio
async def test_tick_recovery_retry_after_reextends_block(tmp_path):
    source = _runtime(tmp_path, "kasumi")
    source.app.bot = _Bot(send_error=RetryAfter(90))
    orchestrator = SimpleNamespace(runtimes=[source], raw_config={}, global_cfg=SimpleNamespace(project_root=tmp_path))
    source.orchestrator = orchestrator
    path = tmp_path / "state" / "telegram_delivery_health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {
                    "kasumi": {
                        "token_key": "telegram:kasumi",
                        "status": "blocked",
                        "blocked_until": "2000-01-01T00:00:00+00:00",
                        "retry_after_s": 60,
                        "incident_id": "tg-kasumi-test",
                        "per_chat": {"321": {"first_warned_at": "2000-01-01T00:00:00+00:00"}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    await failover._tick_recovery(orchestrator)

    saved = json.loads(path.read_text(encoding="utf-8"))
    record = saved["agents"]["kasumi"]
    assert record["status"] == "blocked"
    assert record["retry_after_s"] == 90
    assert record["blocked_until"] is not None
    assert not source.app.bot.messages


def test_persisted_typing_only_defaults_override_legacy_preview_config(tmp_path):
    runtime = _runtime(tmp_path, "kasumi", preview_default=True)
    telegram_stream_policy.set_policy_value(runtime, "placeholder", True)

    enabled, source = failover.preview_status(runtime)
    assert enabled is False
    assert source == "persisted override"

    failover.set_preview_enabled(runtime, True)

    enabled, source = failover.preview_status(runtime)
    assert enabled is True
    assert source == "persisted override"

    failover.set_preview_enabled(runtime, False)

    enabled, source = failover.preview_status(runtime)
    assert enabled is False
    assert source == "persisted override"
    assert failover.effective_preview_enabled(runtime) is False


@pytest.mark.asyncio
async def test_cmd_preview_updates_persisted_preference_and_reports_status(tmp_path):
    replies: list[str] = []

    async def _reply_text(_update, text, **_kwargs):
        replies.append(text)

    runtime = SimpleNamespace(
        workspace_dir=tmp_path / "workspaces" / "kasumi",
        config=SimpleNamespace(
            extra={
                "telegram_stream_enabled": True,
                "answer_stream_preview": True,
            }
        ),
        _reply_text=_reply_text,
        _is_authorized_user=lambda _user_id: True,
    )
    runtime.workspace_dir.mkdir(parents=True, exist_ok=True)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1))

    await FlexibleAgentRuntime.cmd_preview(runtime, update, SimpleNamespace(args=["off"]))
    assert "OFF" in replies[-1]
    assert failover.effective_preview_enabled(runtime) is False

    await FlexibleAgentRuntime.cmd_preview(runtime, update, SimpleNamespace(args=["status"]))
    assert "persisted override" in replies[-1]
    assert "OFF" in replies[-1]


def test_status_summary_reports_delivery_block_and_preview(tmp_path):
    runtime = _runtime(tmp_path, "kasumi", preview_default=False)
    runtime.backend_ready = True
    runtime.telegram_connected = True
    runtime._get_whatsapp_connected = lambda: False
    runtime.skill_manager = None
    runtime._job_counts = lambda: (0, 0)
    runtime.current_request_meta = None
    runtime.last_error_summary = None
    runtime.last_error_at = None
    runtime.backend_manager = SimpleNamespace(agent_mode="flex", get_state_snapshot=lambda: {})
    runtime._process_info = lambda: "pid 123"
    runtime.queue = SimpleNamespace(qsize=lambda: 0)
    runtime.is_generating = False
    runtime._pending_session_primer = None
    runtime.last_success_at = None
    runtime.last_activity_at = None
    runtime._format_age = lambda _value: "never"
    runtime.config.active_backend = "grok"
    runtime.config.allowed_backends = [{"engine": "grok"}]
    runtime.get_current_model = lambda: "grok-4"
    runtime._get_current_effort = lambda: "medium"
    runtime._format_status_mode_block = lambda mode, state, detailed: []
    runtime._verbose = False
    runtime._think = False
    runtime.last_prompt = None
    runtime.last_response = None
    runtime.recent_context_path = tmp_path / "recent_context.jsonl"
    runtime.handoff_path = tmp_path / "handoff.md"
    runtime.memory_store = SimpleNamespace(get_stats=lambda: {"turns": 0, "memories": 0})
    runtime.workspace_dir = tmp_path / "workspaces" / "kasumi"
    runtime.session_started_at = failover._now()
    runtime.transcript_log_path = tmp_path / "transcript.jsonl"
    runtime._pending_auto_recall_context = None
    runtime.last_backend_switch_at = None

    state_path = tmp_path / "state" / "telegram_delivery_health.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "agents": {
                    "kasumi": {
                        "token_key": "telegram:kasumi",
                        "status": "blocked",
                        "blocked_until": "2030-01-01T00:00:00+10:00",
                        "retry_after_s": 60,
                        "incident_id": "tg-kasumi-test",
                        "active_failover_agent": "lin_yueru",
                        "per_chat": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    text = runtime_status.build_status_text(runtime, detailed=True)
    assert "delivery-blocked" == runtime_status.compute_status_string(runtime)
    assert "📨 Delivery: blocked" in text
    assert "remaining" in text
    assert "until 2030-01-01T00:00:00+10:00" in text
    assert "via lin_yueru" in text
    assert "👁 Preview: OFF (persisted override)" in text
