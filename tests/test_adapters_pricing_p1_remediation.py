from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from adapters.antigravity_cli import AntigravityCLIAdapter
from adapters.grok_cli import GrokCLIAdapter
from adapters.hashi_api import HashiApiAdapter
from adapters.her_v2_provider import _EvidenceRecordingToolRegistry
from adapters.openrouter_api import (
    _PARALLEL_SAFE_READONLY_TOOLS,
    _ProviderCallBudgetExhausted,
)
from adapters.xai_api import XaiApiAdapter
from orchestrator.config import AgentConfig, GlobalConfig
from orchestrator.her_v2.config import HERv2Config
from orchestrator.her_v2.models import Effort, Stage, StageRequest
from orchestrator.her_v2.v3_config import HERv3ModelTarget, apply_v3_target, normalise_v3_config
from orchestrator.permissions.remote_registry import BrokeredToolRegistry
from tools.registry import ToolResult


def _agent_config(tmp_path: Path, *, engine: str, model: str, extra: dict | None = None) -> AgentConfig:
    sys_md = tmp_path / "agent.md"
    sys_md.write_text("# test\n", encoding="utf-8")
    return AgentConfig(
        name="p1-test-agent",
        engine=engine,
        workspace_dir=tmp_path,
        system_md=sys_md,
        model=model,
        is_active=True,
        extra=extra or {},
    )


def test_herv3_uses_lightweight_profile_for_auxiliary_and_emits_migration_warnings():
    raw = {
        "profiles": {
            "lightweight": {"engine": "deepseek-api", "model": "deepseek-flash", "reasoning": "high"},
            "premium": {"engine": "deepseek-api", "model": "deepseek-v4-pro", "reasoning": "high"},
        },
        "fallback": {"level1": {"engine": "openrouter-api", "model": "fallback-model"}},
        "voice_routes": {"direct_target": {"engine": "google-api", "model": "voice-model"}},
        "planning_tools_enabled": True,
        "review_limits": {"high": 2},
    }
    normalized = normalise_v3_config(raw)
    assert normalized["profiles"]["main"]["model"] == "deepseek-v4-pro"
    assert normalized["profiles"]["auxiliary"]["model"] == "deepseek-flash"
    assert normalized["profiles"]["auxiliary"]["reasoning"] == "off"

    warned_fields = {item["field"] for item in normalized["migration_warnings"]}
    assert {"fallback", "voice_routes", "planning_tools_enabled", "review_limits"} <= warned_fields

    cfg = HERv2Config.from_mapping(raw)
    assert cfg.profile_for(Stage.MEDITATION).model == "deepseek-flash"
    assert {item["field"] for item in cfg.migration_warnings} >= warned_fields


def test_renormalized_config_retains_auxiliary_and_migration_warnings():
    raw = {
        "profiles": {
            "premium": {"engine": "deepseek-api", "model": "deepseek-v4-pro"},
            "lightweight": {"engine": "deepseek-api", "model": "deepseek-flash"},
        },
        "fallback": {"legacy": "ignored"},
    }
    normalized = normalise_v3_config(raw)
    # This is the real config-consumer boundary, which normalizes again.
    config = HERv2Config.from_mapping(normalized)
    assert config.profile_for(Stage.MEDITATION).model == "deepseek-flash"
    assert any(warning["field"] == "fallback" for warning in config.migration_warnings)


def test_model_selection_keeps_configured_lightweight_auxiliary():
    raw = {
        "profiles": {
            "premium": {"engine": "deepseek-api", "model": "deepseek-v4-pro", "reasoning": "high"},
            "lightweight": {"engine": "deepseek-api", "model": "deepseek-flash"},
        }
    }
    selected = apply_v3_target(raw, HERv3ModelTarget("hashi-api", "main-model"))
    config = HERv2Config.from_mapping(selected)
    assert config.profile_for(Stage.EXECUTION).model == "main-model"
    assert config.profile_for(Stage.EXECUTION).reasoning == "high"
    assert config.profile_for(Stage.MEDITATION).model == "deepseek-flash"


def test_model_selection_can_replace_legacy_target_outside_current_allowlist():
    raw = {
        "profiles": {
            "premium": {"engine": "deepseek-api", "model": "old-main", "reasoning": "high"},
        },
        "v3_provider_allowlist": ["hashi-api"],
    }
    selected = apply_v3_target(raw, HERv3ModelTarget("hashi-api", "new-main"))
    config = HERv2Config.from_mapping(selected)
    assert config.profile_for(Stage.EXECUTION).engine == "hashi-api"
    assert config.profile_for(Stage.EXECUTION).model == "new-main"
    assert config.profile_for(Stage.EXECUTION).reasoning == "high"
    with pytest.raises(ValueError, match="allowlist"):
        apply_v3_target(raw, HERv3ModelTarget("xai-api", "not-allowed"))
    with pytest.raises(ValueError, match="allowlist"):
        apply_v3_target(
            {**raw, "auxiliary": {"provider": "xai-api", "model": "not-allowed"}},
            HERv3ModelTarget("hashi-api", "new-main"),
        )


def test_default_auxiliary_follows_main_after_repeated_model_selection():
    raw = {"main": {"provider": "hashi-api", "model": "old-main"}}
    normalized = normalise_v3_config(raw)
    switched = apply_v3_target(normalized, HERv3ModelTarget("hashi-api", "new-main"))
    switched_again = apply_v3_target(switched, HERv3ModelTarget("hashi-api", "later-main"))
    assert HERv2Config.from_mapping(switched_again).profile_for(Stage.MEDITATION).model == "later-main"


def test_configured_equal_lightweight_stays_selected_after_main_changes():
    raw = {"profiles": {
        "premium": {"engine": "hashi-api", "model": "old-main"},
        "lightweight": {"engine": "hashi-api", "model": "old-main"},
    }}
    normalized = normalise_v3_config(raw)
    switched = apply_v3_target(normalized, HERv3ModelTarget("hashi-api", "new-main"))
    assert HERv2Config.from_mapping(switched).profile_for(Stage.MEDITATION).model == "old-main"


def test_migration_warning_identifies_the_rejected_triage_profile():
    normalized = normalise_v3_config({
        "main": {"provider": "hashi-api", "model": "main-model"},
        "profiles": {"triage": {"engine": "xai-api", "model": "triage-model"}},
        "v3_provider_allowlist": ["hashi-api"],
    })
    assert normalized["profiles"]["auxiliary"]["model"] == "main-model"
    assert any(warning["field"] == "profiles.triage" for warning in normalized["migration_warnings"])


@pytest.mark.asyncio
async def test_hashi_api_enforces_provider_call_limit_and_before_hook(tmp_path: Path):
    cfg = _agent_config(tmp_path, engine="hashi-api", model="gpt-5.6-luna")
    gcfg = GlobalConfig(authorized_id=0)
    adapter = HashiApiAdapter(cfg, gcfg)

    with pytest.raises(_ProviderCallBudgetExhausted):
        await adapter.generate_response("hi", "req-0", provider_call_limit=0)

    calls = 0

    def before_call():
        nonlocal calls
        calls += 1

    async def handler(_request):
        return httpx.Response(
            200,
            json={
                "id": "resp-1",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    res = await adapter.generate_response(
        "hi",
        "req-1",
        provider_call_limit=1,
        before_provider_call=before_call,
    )
    assert res.is_success is True
    assert calls == 1
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_xai_stream_eof_without_done_or_finish_reason_does_not_forge_stop(tmp_path: Path):
    cfg = _agent_config(tmp_path, engine="xai-api", model="grok-4.2")
    gcfg = GlobalConfig(authorized_id=0)
    adapter = XaiApiAdapter(cfg, gcfg, api_key="xai-test")

    async def handler(_request):
        body = 'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    adapter.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.RemoteProtocolError):
        await adapter._stream_api_once(
            {"model": "grok-4.2", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            {"Authorization": "Bearer xai-test"},
            None,
        )
    await adapter.shutdown()


@pytest.mark.asyncio
async def test_grok_cli_cancellation_kills_process_tree(tmp_path: Path, monkeypatch):
    cfg = _agent_config(tmp_path, engine="grok-cli", model="grok-4")
    gcfg = GlobalConfig(authorized_id=0)
    adapter = GrokCLIAdapter(cfg, gcfg)

    class _FakeProc:
        pid = 424242
        returncode = None

        class _Stream:
            async def readline(self):
                await asyncio.sleep(60)
                return b""

            async def read(self, _n):
                await asyncio.sleep(60)
                return b""

        def __init__(self):
            self.stdout = self._Stream()
            self.stderr = self._Stream()

        async def wait(self):
            await asyncio.sleep(60)
            return 0

    killed = []

    async def fake_spawn(*_args, **_kwargs):
        return _FakeProc()

    async def fake_kill(proc, logger=None, reason=""):
        killed.append((proc.pid, reason))
        proc.returncode = -9

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(adapter, "force_kill_process_tree", fake_kill)

    task = asyncio.create_task(adapter.generate_response("hello", "req-cancel"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed and killed[0][0] == 424242


def test_antigravity_cli_uses_configured_idle_timeout(tmp_path: Path):
    cfg = _agent_config(tmp_path, engine="antigravity-cli", model="gemini-3.8-flash-high", extra={"idle_timeout_sec": 600})
    gcfg = GlobalConfig(authorized_id=0)
    adapter = AntigravityCLIAdapter(cfg, gcfg)
    assert adapter.IDLE_TIMEOUT_SEC == 600
    cmd = adapter._build_cmd(None)
    idx = cmd.index("--print-timeout")
    assert cmd[idx + 1] == "600s"


@pytest.mark.asyncio
async def test_brokered_registry_denial_is_error_and_evidence_registry_serializes_concurrent_calls(tmp_path: Path):
    defn = {"type": "function", "function": {"name": "wiki_search", "description": "s", "parameters": {"type": "object", "properties": {}}}}
    broker = BrokeredToolRegistry(SimpleNamespace(), definitions=[defn], scratch_root=tmp_path)
    assert broker.supports_parallel_readonly is True
    assert {"wiki_search", "documentation_read", "workspace_inspect", "tool_find"} <= _PARALLEL_SAFE_READONLY_TOOLS

    denied = await broker.execute("shell", {"command": "id"}, tool_call_id="c1")
    assert denied.is_error is True
    assert denied.details.get("control_disposition") == "denied"

    class _SlowBase:
        workspace_dir = tmp_path

        async def execute(self, name, arguments, call_id=""):
            await asyncio.sleep(0.01)
            return ToolResult(tool_call_id=call_id, output=f"ok:{call_id}", is_error=False)

        def is_read_only(self, _name):
            return True

    req = StageRequest(
        turn_id="t1",
        request_ref="r1",
        stage=Stage.EXECUTION,
        role="main",
        attempt=1,
        goal="test",
        classification=None,
        effort=Effort.MEDIUM,
    )
    rec_reg = _EvidenceRecordingToolRegistry(_SlowBase(), req)
    await asyncio.gather(*(rec_reg.execute("wiki_search", {}, f"id-{i}") for i in range(10)))
    serials = [r.details["receipt_serial"] for r in rec_reg.receipts]
    assert sorted(serials) == list(range(1, 11))
