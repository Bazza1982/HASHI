from types import SimpleNamespace

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.legacy.bridge_agent_runtime import BridgeAgentRuntime


class _FlexiblePolicyRuntime:
    _source_requires_manual_permission = FlexibleAgentRuntime._source_requires_manual_permission
    _remote_backend_block_reason = FlexibleAgentRuntime._remote_backend_block_reason

    def __init__(self, engine: str):
        self.config = SimpleNamespace(active_backend=engine)


class _LegacyPolicyRuntime:
    _source_requires_manual_permission = BridgeAgentRuntime._source_requires_manual_permission
    _remote_backend_block_reason = BridgeAgentRuntime._remote_backend_block_reason

    def __init__(self, engine: str):
        self.config = SimpleNamespace(engine=engine)


def test_remote_api_policy_allows_hchat_sources_for_flexible_runtime():
    runtime = _FlexiblePolicyRuntime("deepseek-api")

    assert runtime._remote_backend_block_reason("bridge:hchat") is None
    assert runtime._remote_backend_block_reason("bridge:hchat-draft") is None
    assert runtime._remote_backend_block_reason("hchat-reply:akane") is None


def test_remote_api_policy_still_blocks_automated_sources_for_flexible_runtime():
    runtime = _FlexiblePolicyRuntime("deepseek-api")

    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("scheduler")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("loop_skill")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("bridge-transfer:agent")


def test_remote_api_policy_allows_hchat_sources_for_legacy_runtime():
    runtime = _LegacyPolicyRuntime("openrouter-api")

    assert runtime._remote_backend_block_reason("bridge:hchat") is None
    assert runtime._remote_backend_block_reason("hchat-reply:zelda") is None


def test_remote_api_policy_does_not_affect_cli_backends():
    runtime = _FlexiblePolicyRuntime("claude-cli")

    assert runtime._remote_backend_block_reason("scheduler") is None
