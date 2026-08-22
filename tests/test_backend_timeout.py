from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from adapters.base import BaseBackend, BackendCapabilities, BackendResponse
from adapters.claude_cli import ClaudeCLIAdapter
from adapters.codex_cli import CodexCLIAdapter
from adapters.gemini_cli import GeminiCLIAdapter
from adapters.grok_cli import GrokCLIAdapter
from adapters.timeout_policy import (
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


def test_active_cli_defaults_use_one_hour_idle_liveness_only():
    for adapter_class in (
        CodexCLIAdapter,
        ClaudeCLIAdapter,
        GeminiCLIAdapter,
        GrokCLIAdapter,
    ):
        assert adapter_class.DEFAULT_IDLE_TIMEOUT_SEC == 60 * 60
        assert adapter_class.USES_LEGACY_HARD_TIMEOUT is False



@pytest.mark.asyncio
async def test_active_backend_does_not_apply_legacy_hard_timeout(tmp_path):
    backend = _backend(
        tmp_path,
        extra={
            IDLE_TIMEOUT_KEY: 60,
            "hard_timeout_sec": 0,
        },
    )
    task = asyncio.create_task(asyncio.sleep(0.01))

    assert await backend._wait_for_task_with_timeouts(
        task,
        started_monotonic=time.perf_counter(),
    ) is None


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
    )
    set_timeout_override(
        store,
        "claude-cli",
        idle_seconds=7200,
    )

    assert saved == {IDLE_TIMEOUT_KEY: 3600}
    assert read_timeout_override(store, "codex-cli") == saved
    clear_timeout_override(store, "codex-cli")

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["active_backend"] == "codex-cli"
    assert state["unrelated"] == {"keep": True}
    assert "codex-cli" not in state["backend_timeouts"]
    assert "claude-cli" in state["backend_timeouts"]


def test_refresh_timeout_extra_restores_configured_values_after_reset(tmp_path):
    extra = apply_timeout_layers(
        {IDLE_TIMEOUT_KEY: 600, "hard_timeout_sec": 7200},
        engine="codex-cli",
        agent_extra={IDLE_TIMEOUT_KEY: 600, "hard_timeout_sec": 7200},
        persisted_override={IDLE_TIMEOUT_KEY: 3600, "hard_timeout_sec": 360000},
    )
    backend = _backend(tmp_path, extra=extra)
    assert timeout_policy_snapshot(backend).idle_seconds == 3600
    assert "hard_timeout_sec" not in backend.config.extra

    refresh_timeout_extra(
        backend.config.extra,
        engine="codex-cli",
        persisted_override={},
    )

    policy = timeout_policy_snapshot(backend)
    assert policy.idle_seconds == 600
    assert "hard_timeout_sec" not in backend.config.extra


@pytest.mark.asyncio
async def test_timeout_monitor_reports_kind_values_sources_and_activity_age(tmp_path):
    extra = apply_timeout_layers(
        {},
        engine="codex-cli",
        persisted_override={IDLE_TIMEOUT_KEY: 1, "hard_timeout_sec": 30},
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
    assert "idle_source=user_override" in diagnostic
    assert "hard_timeout" not in diagnostic
    assert "last_output_age_s=" in diagnostic
    assert "total_runtime_s=" in diagnostic
