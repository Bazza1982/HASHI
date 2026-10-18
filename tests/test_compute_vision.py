from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from PIL import Image

from orchestrator.compute_vision import ComputeVisionError, LanComputeVisionProjector
from orchestrator.multimodal_contract import canonical_request_content


def _content(path):
    data = path.read_bytes()
    return canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "What is in this image?"},
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "image-1",
                "modality": "image",
                "kind": "photo",
                "mime_type": "image/png",
                "filename": path.name,
                "caption": "",
                "local_ref": str(path),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "transport": {"message_id": 1},
            },
        ]
    )


class _Catalogue:
    def __init__(self, matches):
        self.matches = tuple(matches)
        self.requirements = None

    def match(self, requirements):
        self.requirements = requirements
        return self.matches


class _Directory:
    def __init__(self, catalogue, errors=None):
        self.catalogue = catalogue
        self.errors = errors or {}

    async def refresh(self):
        return SimpleNamespace(catalogue=self.catalogue, errors=self.errors)


class _Client:
    def __init__(self):
        self.calls = []

    async def invoke_json(self, resolved, protocol, payload):
        self.calls.append((resolved, protocol, payload))
        return {
            "choices": [
                {"message": {"content": "A small red square on a white background."}}
            ]
        }


@pytest.mark.asyncio
async def test_lan_projector_uses_resolved_service_and_returns_bounded_text(tmp_path):
    image = tmp_path / "sample.png"
    Image.new("RGB", (32, 32), "red").save(image)
    offering = SimpleNamespace(
        model_id="minicpm-v-4.6",
        service_id="vision",
        image=SimpleNamespace(max_bytes=2 * 1024 * 1024),
    )
    resolved = SimpleNamespace(
        offering=offering,
        snapshot=SimpleNamespace(node=SimpleNamespace(id="hashi-compute")),
    )
    catalogue = _Catalogue([resolved])
    client = _Client()
    projector = LanComputeVisionProjector(
        tmp_path / "unused.json",
        client=client,
        directory=_Directory(catalogue),
    )

    result = await projector.project(
        _content(image),
        question="What is in this image?",
    )

    assert result.text.endswith("A small red square on a white background.")
    assert result.node_id == "hashi-compute"
    assert len(client.calls) == 1
    assert client.calls[0][1] == "openai.chat.completions"
    payload = client.calls[0][2]
    assert payload["model"] == "minicpm-v-4.6"
    image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/jpeg;base64,")
    assert catalogue.requirements.locations == frozenset({"lan"})


@pytest.mark.asyncio
async def test_lan_projector_fails_closed_when_no_lan_visual_service_matches(tmp_path):
    projector = LanComputeVisionProjector(
        tmp_path / "unused.json",
        client=_Client(),
        directory=_Directory(
            _Catalogue([]),
            errors={"hashi-compute": "compute_node_unreachable"},
        ),
    )
    image = tmp_path / "sample.png"
    Image.new("RGB", (32, 32), "red").save(image)

    with pytest.raises(ComputeVisionError, match="compute_node_unreachable"):
        await projector.project(_content(image), question="Inspect it")
