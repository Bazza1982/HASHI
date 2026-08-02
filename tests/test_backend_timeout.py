from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.claude_cli import ClaudeCLIAdapter
from adapters.claw_cli import ClawCLIAdapter
from adapters.codex_cli import CodexCLIAdapter
from adapters.gemini_cli import GeminiCLIAdapter
from adapters.grok_cli import GrokCLIAdapter
from adapters.timeout_policy import (
    HARD_TIMEOUT_KEY,
    IDLE_TIMEOUT_KEY,
    apply_timeout_layers,
    refresh_timeout_extra,
    timeout_policy_snapshot,
)
from orchestrator.backend_timeout import (
    clear_timeout_override,
    read_timeout_override,
    set_timeout_override,
)
from orchestrator.workspace_state import WorkspaceStateStore


class _Backend(BaseBackend):
    DEFAULT_IDLE_TIMEOUT_SEC = 60
    DEFAULT_HARD_TIMEOUT_SEC = 600

    def _define_capabilities(self):
        return BackendCapabilities(False, False, False, False, True)

    async def initialize(self):
        return True

    async def generate_response(
        self,
        prompt,
        request_id,
        is_retry=False,
        silent=False,
        on_stream_event=None,
    ):
        return BackendResponse(text=prompt, duration_ms=0)

    async def shutdown(self):
        return None

    async def handle_new_session(self):
        return True


def _backend(tmp_path, *, extra=None):
    config = SimpleNamespace(
        name="agent",
        engine="codex-cli",
        workspace_dir=tmp_path,
        extra=extra or {},
    )
    return _Backend(config, SimpleNamespace())


def test_long_running_cli_defaults_are_one_hour_and_twenty_four_hours():
    for adapter_class in (
        CodexCLIAdapter,
        ClaudeCLIAdapter,
        GeminiCLIAdapter,
        GrokCLIAdapter,
        ClawCLIAdapter,
    ):
        assert adapter_class.DEFAULT_IDLE_TIMEOUT_SEC == 60 * 60
        assert adapter_class.DEFAULT_HARD_TIMEOUT_SEC == 24 * 60 * 60


def test_backend_rejects_hard_timeout_below_idle_timeout(tmp_path):
    with pytest.raises(ValueError, match="greater than or equal"):
        _backend(
            tmp_path,
            extra={
                IDLE_TIMEOUT_KEY: 120,
                HARD_TIMEOUT_KEY: 60,
            },
        )


def test_timeout_state_preserves_other_workspace_fields_and_resets_one_backend(tmp_path):
    store = WorkspaceStateStore(tmp_path)
    store.replace(
        {
            "active_backend": "codex-cli",
            "unrelated": {"keep": True},
        }
    )

    saved = set_timeout_override(
        store,
        "codex-cli",
        idle_seconds=3600,
        hard_seconds=360000,
    )
    set_timeout_override(
        store,
        "claude-cli",
        idle_seconds=7200,
        hard_seconds=172800,
    )

    assert saved == {
        IDLE_TIMEOUT_KEY: 3600,
        HARD_TIMEOUT_KEY: 360000,
    }
    assert read_timeout_override(store, "codex-cli") == saved
    clear_timeout_override(store, "codex-cli")

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["active_backend"] == "codex-cli"
    assert state["unrelated"] == {"keep": True}
    assert "codex-cli" not in state["backend_timeouts"]
    assert "claude-cli" in state["backend_timeouts"]


def test_refresh_timeout_extra_restores_configured_values_after_reset(tmp_path):
    extra = apply_timeout_layers(
        {IDLE_TIMEOUT_KEY: 600, HARD_TIMEOUT_KEY: 7200},
        engine="codex-cli",
        agent_extra={IDLE_TIMEOUT_KEY: 600, HARD_TIMEOUT_KEY: 7200},
        persisted_override={IDLE_TIMEOUT_KEY: 3600, HARD_TIMEOUT_KEY: 360000},
    )
    backend = _backend(tmp_path, extra=extra)
    assert timeout_policy_snapshot(backend).hard_seconds == 360000

    refresh_timeout_extra(
        backend.config.extra,
        engine="codex-cli",
        persisted_override={},
    )

    policy = timeout_policy_snapshot(backend)
    assert policy.idle_seconds == 600
    assert policy.hard_seconds == 7200


@pytest.mark.asyncio
async def test_timeout_monitor_reports_kind_values_sources_and_activity_age(tmp_path):
    extra = apply_timeout_layers(
        {},
        engine="codex-cli",
        persisted_override={IDLE_TIMEOUT_KEY: 1, HARD_TIMEOUT_KEY: 30},
    )
    backend = _backend(tmp_path, extra=extra)
    backend.last_activity_at = time.time() - 2
    task = asyncio.create_task(asyncio.sleep(30))

    timeout_kind = await backend._wait_for_task_with_timeouts(
        task,
        started_monotonic=time.perf_counter(),
    )
    diagnostic = backend._timeout_diagnostic(
        timeout_kind or "unknown",
        started_monotonic=time.perf_counter(),
    )
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert timeout_kind == "idle"
    assert "kind=idle" in diagnostic
    assert "idle_timeout_s=1" in diagnostic
    assert "hard_timeout_s=30" in diagnostic
    assert "idle_source=user_override" in diagnostic
    assert "hard_source=user_override" in diagnostic
    assert "last_output_age_s=" in diagnostic
    assert "total_runtime_s=" in diagnostic
