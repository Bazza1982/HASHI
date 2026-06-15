from types import SimpleNamespace

import pytest

from adapters import deepseek_api
from adapters.deepseek_api import DeepSeekAdapter
from adapters.openrouter_api import _APIResult
from orchestrator.enterprise import IdentityService, PolicyEvaluator
from tools.registry import ToolResult


class _DummyToolRegistry:
    max_loops = 2

    def __init__(self):
        self.calls = []

    def get_tool_definitions(self, tiers=None):
        if tiers == []:
            return []
        return [
            {
                "type": "function",
                "function": {
                    "name": "file_list",
                    "description": "List files.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]

    async def execute(self, tool_name, arguments, tool_call_id=""):
        self.calls.append((tool_name, arguments, tool_call_id))
        return ToolResult(tool_call_id=tool_call_id, output="tool output")


def _adapter(tmp_path, *, global_config=None):
    cfg = SimpleNamespace(
        name="ying",
        engine="deepseek-api",
        model="deepseek-v4-pro",
        workspace_dir=tmp_path,
        system_md=None,
        extra={},
    )
    adapter = DeepSeekAdapter(cfg, global_config or SimpleNamespace(), api_key="test-key")
    adapter.tool_registry = _DummyToolRegistry()
    return adapter


def _init_org(tmp_path, org_id: str = "ORG-001") -> None:
    identity = IdentityService.from_path(tmp_path / "state" / "enterprise.sqlite")
    identity.create_organization(org_id=org_id, name="Acme")


def _enterprise_global_config(tmp_path):
    return SimpleNamespace(
        deployment_profile="enterprise",
        organization_id="ORG-001",
        bridge_home=tmp_path,
    )


def test_deepseek_reasoning_helper_supports_old_api_result_shape():
    class OldAPIResult:
        def __init__(self, text, tool_calls, finish_reason):
            self.text = text
            self.tool_calls = tool_calls
            self.finish_reason = finish_reason

    result = deepseek_api._with_reasoning_content(
        OldAPIResult(text="", tool_calls=[], finish_reason="tool_calls"),
        "legacy-safe reasoning",
    )

    assert result.reasoning_content == "legacy-safe reasoning"


@pytest.mark.asyncio
async def test_deepseek_tool_loop_preserves_reasoning_content_non_stream(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    seen_messages = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path": "/tmp"}'},
        }
    ]

    async def fake_call(payload, headers, on_stream_event):
        seen_messages.append(payload["messages"])
        if len(seen_messages) == 1:
            return _APIResult(
                text="",
                tool_calls=tool_calls,
                finish_reason="tool_calls",
                reasoning_content="Need to inspect the directory.",
            )
        assistant_msg = payload["messages"][2]
        assert assistant_msg["reasoning_content"] == "Need to inspect the directory."
        return _APIResult(text="done", tool_calls=None, finish_reason="stop")

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("check files", "req-test")

    assert response.is_success is True
    assert response.text == "done"
    assert response.tool_call_count == 1
    assert response.tool_loop_count == 1


@pytest.mark.asyncio
async def test_openrouter_tool_execution_blocks_shell_policy(tmp_path):
    _init_org(tmp_path)
    policy = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    policy.add_rule(action="shell.execute", resource="shell:bash", effect="deny")
    adapter = _adapter(tmp_path, global_config=_enterprise_global_config(tmp_path))
    messages = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command": "rm -rf /tmp/example"}'},
        }
    ]

    await adapter._run_tool_calls(tool_calls, messages, on_stream_event=None)

    assert adapter.tool_registry.calls == []
    assert messages == [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "Error: tool call blocked by enterprise policy: bash",
        }
    ]


@pytest.mark.asyncio
async def test_openrouter_tool_execution_blocks_file_write_approval_required(tmp_path):
    _init_org(tmp_path)
    policy = PolicyEvaluator.from_path(tmp_path / "state" / "enterprise.sqlite", org_id="ORG-001")
    policy.add_rule(action="file.write", resource="file:/tmp/report.md", effect="approval_required")
    adapter = _adapter(tmp_path, global_config=_enterprise_global_config(tmp_path))
    messages = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_write", "arguments": '{"path": "/tmp/report.md", "content": "x"}'},
        }
    ]

    await adapter._run_tool_calls(tool_calls, messages, on_stream_event=None)

    assert adapter.tool_registry.calls == []
    assert messages[0]["content"] == "Error: tool call requires approval by enterprise policy: file_write"


@pytest.mark.asyncio
async def test_deepseek_tool_loop_limit_requests_final_no_tools_answer(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    adapter.tool_registry.max_loops = 1
    seen_payloads = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path": "/tmp"}'},
        }
    ]

    async def fake_call(payload, headers, on_stream_event):
        seen_payloads.append(payload)
        if len(seen_payloads) == 1:
            assert "tools" in payload
            return _APIResult(text="", tool_calls=tool_calls, finish_reason="tool_calls")
        assert "tools" not in payload
        assert payload["messages"][-1]["content"].startswith("Tool loop limit reached.")
        return _APIResult(text="final answer", tool_calls=None, finish_reason="stop")

    monkeypatch.setattr(adapter, "_call_api_once", fake_call)

    response = await adapter.generate_response("check files", "req-test")

    assert response.is_success is True
    assert response.text == "final answer"
    assert response.tool_call_count == 1
    assert response.tool_loop_count == 1
    assert len(seen_payloads) == 2


@pytest.mark.asyncio
async def test_deepseek_tool_loop_preserves_reasoning_content_stream(monkeypatch, tmp_path):
    adapter = _adapter(tmp_path)
    seen_messages = []
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "file_list", "arguments": '{"path": "/tmp"}'},
        }
    ]

    async def fake_stream(payload, headers, on_stream_event):
        seen_messages.append(payload["messages"])
        if len(seen_messages) == 1:
            return _APIResult(
                text="",
                tool_calls=tool_calls,
                finish_reason="tool_calls",
                reasoning_content="Streaming reasoning chunk.",
            )
        assistant_msg = payload["messages"][2]
        assert assistant_msg["reasoning_content"] == "Streaming reasoning chunk."
        return _APIResult(text="done", tool_calls=None, finish_reason="stop")

    async def on_stream_event(_event):
        return None

    monkeypatch.setattr(adapter, "_stream_api_once", fake_stream)

    response = await adapter.generate_response(
        "check files",
        "req-test",
        on_stream_event=on_stream_event,
    )

    assert response.is_success is True
    assert response.text == "done"
    assert response.tool_call_count == 1
    assert response.tool_loop_count == 1
