from types import SimpleNamespace

from orchestrator.flexible_agent_runtime import FlexibleAgentRuntime
from orchestrator.legacy.bridge_agent_runtime import BridgeAgentRuntime
from orchestrator.source_policy import (
    is_human_hchat_source,
    source_requires_manual_remote_api_permission,
)


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
    assert runtime._remote_backend_block_reason(" HCHAT-REPLY:AKANE ") is None


def test_remote_api_policy_still_blocks_automated_sources_for_flexible_runtime():
    runtime = _FlexiblePolicyRuntime("deepseek-api")

    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("scheduler")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("scheduler-retry")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("scheduler-skill")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("loop_skill")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("startup")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("bridge-transfer:agent")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("bridge:mailbox")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("cos-query:status")
    assert "Blocked deepseek-api" in runtime._remote_backend_block_reason("ticket:123")


def test_remote_api_policy_allows_hchat_sources_for_legacy_runtime():
    runtime = _LegacyPolicyRuntime("openrouter-api")

    assert runtime._remote_backend_block_reason("bridge:hchat") is None
    assert runtime._remote_backend_block_reason("hchat-reply:zelda") is None


def test_remote_api_policy_does_not_affect_cli_backends():
    runtime = _FlexiblePolicyRuntime("claude-cli")

    assert runtime._remote_backend_block_reason("scheduler") is None


def test_shared_source_policy_separates_human_hchat_from_automation():
    for source in ["bridge:hchat", "bridge:hchat-draft", "hchat-reply:akane", " HCHAT-REPLY:AKANE "]:
        assert is_human_hchat_source(source)
        assert not source_requires_manual_remote_api_permission(source)

    for source in ["bridge:mailbox", "bridge-transfer:agent", "scheduler", "scheduler-skill", "loop_skill", "startup"]:
        assert not is_human_hchat_source(source)
        assert source_requires_manual_remote_api_permission(source)
