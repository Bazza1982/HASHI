from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from adapters.openrouter_api import OpenRouterAdapter
from orchestrator.config import AgentConfig, FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from tools.registry import ToolRegistry
from tools.vision_inspect import (
    LlamaCppVisionProvider,
    VisionAnswer,
    VisionInspectError,
    execute_vision_inspect,
)


class _FakeProvider:
    def __init__(self):
        self.image_bytes = b""

    async def inspect(self, *, image_bytes: bytes, question: str, detail: str) -> VisionAnswer:
        self.image_bytes = image_bytes
        assert question == "What is happening?"
        assert detail == "standard"
        return VisionAnswer(
            "A person is repairing a bicycle.",
            ("A person is crouched beside the rear wheel.",),
            ("The exact tool is unclear.",),
        )


def _write_attachment(root: Path, *, message_id: str = "msg-1", attachment_id: str = "att-1") -> str:
    message_dir = root / "messages" / message_id
    message_dir.mkdir(parents=True)
    image_path = message_dir / "photo.png"
    Image.new("RGB", (32, 24), "blue").save(image_path)
    digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    (message_dir / "manifest.json").write_text(
        json.dumps(
            {
                "message_id": message_id,
                "attachments": [
                    {
                        "attachment_id": attachment_id,
                        "filename": image_path.name,
                        "stored_path": str(image_path),
                        "sha256": digest,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return f"attachment:{message_id}:{attachment_id}"


def test_vision_tool_is_explicitly_opt_in(tmp_path):
    wildcard = ToolRegistry(["*"], tmp_path, tmp_path, {})
    explicit = ToolRegistry(["vision_inspect"], tmp_path, tmp_path, {})

    assert wildcard.is_allowed("vision_inspect") is False
    assert explicit.is_allowed("vision_inspect") is True
    assert explicit.get_tool_definitions()[0]["function"]["name"] == "vision_inspect"


@pytest.mark.asyncio
async def test_vision_inspect_resolves_attachment_and_returns_bounded_json(tmp_path):
    media_root = tmp_path / "remote"
    image_ref = _write_attachment(media_root)
    provider = _FakeProvider()

    output = await execute_vision_inspect(
        {"image_ref": image_ref, "question": "What is happening?"},
        access_root=tmp_path / "workspace",
        workspace_dir=tmp_path / "workspace",
        media_roots=[media_root],
        options={"model": "test-vlm"},
        provider=provider,
    )

    payload = json.loads(output)
    assert payload["answer"] == "A person is repairing a bicycle."
    assert payload["observations"] == ["A person is crouched beside the rear wheel."]
    assert payload["uncertainties"] == ["The exact tool is unclear."]
    assert payload["normalized_size"] == [32, 24]
    assert provider.image_bytes.startswith(b"\xff\xd8\xff")
    assert "stored_path" not in output


@pytest.mark.asyncio
async def test_vision_inspect_rejects_tampered_attachment(tmp_path):
    media_root = tmp_path / "remote"
    image_ref = _write_attachment(media_root)
    image_path = media_root / "messages" / "msg-1" / "photo.png"
    image_path.write_bytes(image_path.read_bytes() + b"tampered")

    output = await execute_vision_inspect(
        {"image_ref": image_ref, "question": "What is happening?"},
        access_root=tmp_path,
        workspace_dir=tmp_path,
        media_roots=[media_root],
        options={},
        provider=_FakeProvider(),
    )

    assert output.startswith("Error: vision_inspect failed:")
    assert "checksum" in output


@pytest.mark.asyncio
async def test_llama_cpp_provider_sends_openai_multimodal_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "answer": "A red square.",
                                    "observations": ["The center is red."],
                                    "uncertainties": [],
                                }
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        provider = LlamaCppVisionProvider(
            {"endpoint": "http://127.0.0.1:8081/v1", "model": "qwen-test"},
            client=client,
        )
        answer = await provider.inspect(
            image_bytes=b"jpeg",
            question="What is shown?",
            detail="brief",
        )
    finally:
        await client.aclose()

    assert answer.answer == "A red square."
    assert captured["model"] == "qwen-test"
    assert captured["messages"][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/jpeg;base64,"
    )
    assert captured["response_format"] == {"type": "json_object"}


def test_llama_cpp_provider_blocks_unlisted_remote_host():
    with pytest.raises(VisionInspectError, match="allowed_hosts"):
        LlamaCppVisionProvider({"endpoint": "http://example.com:8081/v1"})


def test_backend_declares_configured_native_vision(tmp_path):
    config = AgentConfig(
        name="vision",
        engine="openrouter-api",
        workspace_dir=tmp_path,
        system_md=tmp_path / "agent.md",
        model="native-vlm",
        is_active=True,
        extra={"image_input": "native"},
    )

    adapter = OpenRouterAdapter(config, None, "test")

    assert adapter.image_input_mode == "native"
    assert adapter.capabilities.supports_native_vision is True


def _manager(tmp_path: Path) -> FlexibleBackendManager:
    config = FlexibleAgentConfig(
        name="text-agent",
        workspace_dir=tmp_path / "workspace",
        system_md=tmp_path / "agent.md",
        telegram_token_key="text-agent",
        allowed_backends=[],
        active_backend="openrouter-api",
        project_root=tmp_path,
    )
    global_config = GlobalConfig(
        authorized_id=0,
        base_media_dir=tmp_path / "media",
        project_root=tmp_path,
        instance_id="HASHI1",
    )
    return FlexibleBackendManager(config, global_config, {})


def test_backend_manager_exposes_vision_only_in_tool_mode(tmp_path):
    manager = _manager(tmp_path)
    tools = {
        "allowed": ["file_read", "vision_inspect"],
        "vision_inspect": {"endpoint": "http://127.0.0.1:8081/v1"},
    }

    native = manager._resolve_tools_config(
        {"engine": "openrouter-api", "image_input": "native", "tools": tools}
    )
    tool = manager._resolve_tools_config(
        {"engine": "openrouter-api", "image_input": "tool", "tools": tools}
    )

    assert native is not None and native["allowed"] == ["file_read"]
    assert tool is not None and set(tool["allowed"]) == {"file_read", "vision_inspect"}


def test_backend_manager_builds_scoped_vision_media_roots(tmp_path):
    manager = _manager(tmp_path)
    adapter_config = AgentConfig(
        name="text-agent",
        engine="openrouter-api",
        workspace_dir=tmp_path / "workspace",
        system_md=tmp_path / "agent.md",
        model="text-model",
        is_active=True,
    )

    roots = manager._vision_media_roots(adapter_config)

    assert roots == [
        tmp_path / "media" / "text-agent",
        tmp_path / "state" / "remote_attachments" / "hashi1",
    ]
