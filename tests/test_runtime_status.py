from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orchestrator import runtime_status


class _SkillManager:
    def __init__(self):
        self._hb = [{"enabled": True}, {"enabled": False}]
        self._cron = [{"enabled": True}]
        self._nudge = [{"enabled": True}, {"enabled": False}]

    def list_jobs(self, kind, agent_name=None):
        if kind == "heartbeat":
            return self._hb
        if kind == "cron":
            return self._cron
        return self._nudge

    def get_active_toggle_ids(self, workspace_dir):
        return {"recall", "anatta"}

    def get_active_heartbeat_job(self, agent_name):
        return {"enabled": True, "interval_seconds": 1200}


def _runtime():
    runtime = SimpleNamespace()
    runtime.backend_ready = True
    runtime.telegram_connected = True
    runtime.skill_manager = _SkillManager()
    runtime.name = "lin_yueru"
    runtime.workspace_dir = Path("/tmp/hashi-workspace")
    runtime.config = SimpleNamespace(active_backend="codex-cli", allowed_backends=[{"engine": "codex-cli"}])
    runtime.backend_manager = SimpleNamespace(agent_mode="flex", current_backend=SimpleNamespace(_session_id=None))
    runtime.current_request_meta = {"request_id": "req-1", "source": "text", "summary": "Ping"}
    runtime.last_error_summary = None
    runtime.last_error_at = None
    runtime.last_success_at = None
    runtime.last_activity_at = None
    runtime._pending_session_primer = None
    runtime._pending_auto_recall_context = None
    runtime.is_generating = False
    runtime.queue = SimpleNamespace(qsize=lambda: 2)
    runtime.transcript_log_path = Path("/tmp/hashi-workspace/transcript.jsonl")
    runtime.session_started_at = __import__("datetime").datetime(2026, 5, 9, 23, 0, 0)
    runtime.last_prompt = None
    runtime.last_response = None
    runtime.memory_store = SimpleNamespace(get_stats=lambda: {"turns": 3, "memories": 4})
    runtime.recent_context_path = Path("/tmp/hashi-workspace/recent_context.jsonl")
    runtime.handoff_path = Path("/tmp/hashi-workspace/handoff.md")
    runtime._verbose = True
    runtime._think = False
    runtime.last_backend_switch_at = None
    runtime.session_id_dt = "session-1"
    runtime.get_current_model = lambda: "gpt-test"
    runtime._get_current_effort = lambda: "medium"
    runtime._get_whatsapp_connected = lambda: False
    runtime._format_age = lambda dt: "never" if dt is None else "now"
    runtime._process_info = lambda: "idle"
    runtime._job_counts = lambda: runtime_status.job_counts(runtime)
    return runtime


def test_compute_status_string_prefers_online_then_local_then_offline():
    runtime = _runtime()
    assert runtime_status.compute_status_string(runtime) == "online"
    runtime.telegram_connected = False
    assert runtime_status.compute_status_string(runtime) == "local"
    runtime.backend_ready = False
    assert runtime_status.compute_status_string(runtime) == "offline"


def test_job_counts_only_include_enabled_jobs():
    heartbeat_count, cron_count, nudge_count = runtime_status.job_counts(_runtime())
    assert heartbeat_count == 1
    assert cron_count == 1
    assert nudge_count == 1


def test_build_status_text_contains_core_runtime_summary():
    text = runtime_status.build_status_text(_runtime(), detailed=False)
    assert "🧠 lin_yueru" in text
    assert "🔔 Proactive: ON • every 20 min • hb 1 • cron 1 • nudge 1" in text
    assert "Use /status full for more detail." in text


def test_build_status_text_full_includes_workspace_details():
    text = runtime_status.build_status_text(_runtime(), detailed=True)
    assert "📁 Workspace: /tmp/hashi-workspace" in text
    assert "🧩 Allowed Backends: codex-cli" in text
    assert "📚 Bridge Memory: 3 turns • 4 memories" in text
