"""Bound media transfer and original voice/compaction behavior, without services."""
import asyncio
from hashlib import sha256
from types import SimpleNamespace

import pytest

from adapters.her_v2 import _ExecutionStageCompactionProvider, _HChatDraftToolProvider
from adapters.her_v2_provider import HashiStageProvider
from orchestrator.multimodal_contract import canonical_request_content
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.host_protocol import pack
from orchestrator.permissions.media_bridge import RunMedia, HostMedia, bind_host_media
from orchestrator.permissions.runtime_ports import HostRuntimePorts, invoke, manifest
from orchestrator.permissions.models import RuntimeExecutionBinding
from tests.test_permission_pao_fence import setup_run


def content(path, payload):
    path.write_bytes(payload)
    return canonical_request_content([{
        "type": "media", "item_index": 1, "attachment_id": "accepted-audio",
        "modality": "audio", "kind": "voice", "semantic_role": "voice_message",
        "mime_type": "audio/ogg", "filename": "voice.ogg", "caption": "",
        "local_ref": str(path), "size_bytes": len(payload), "sha256": sha256(payload).hexdigest(),
        "transport": {},
    }])


@pytest.mark.asyncio
async def test_accepted_media_stages_only_known_bytes_and_voice_keeps_original_confirmation(tmp_path):
    _, _, accepted, fence, context = setup_run(tmp_path)
    binding = RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent")
    original = content(tmp_path / "voice.ogg", b"accepted audio")
    released, presented = asyncio.Event(), asyncio.Event()
    state = {"safe_voice": True, "status": "ready", "text": "authoritative voice text",
             "release_event": released, "request_confirmation": presented.set}
    provider = HashiStageProvider.__new__(HashiStageProvider)
    provider.runtime_context = SimpleNamespace(_native_voice_transcripts={accepted.request_id: state})
    turn = SimpleNamespace(binding=binding, executor=SimpleNamespace(_stage_provider=provider),
                           media=RunMedia(original, "Accepted request"))
    turn.runtime = SimpleNamespace(provider=provider, delivery=SimpleNamespace(allow_immediate_response=False),
                                   workzone_ref=str(tmp_path))
    class Client:
        def call(self, method, arguments):
            assert method == "runtime_port" and arguments["operation"] == "manifest"
            return pack(manifest(turn.runtime))

        async def call_async(self, method, arguments):
            assert method == "runtime_port"
            return await invoke(turn, **arguments)

    host_root = tmp_path / "host"
    host_root.mkdir()
    ports = HostRuntimePorts(Client())
    media = HostMedia(ports, host_root)
    staged = await media.accepted()
    staged_path = __import__("pathlib").Path(staged["parts"][0]["local_ref"])
    assert staged_path.read_bytes() == b"accepted audio"
    assert staged_path.parent == host_root / "media"
    with pytest.raises(PermissionDenied):
        await ports._async("media_chunk", token=str(tmp_path / "secret.txt"), offset=0)
    with pytest.raises(PermissionDenied):
        await ports._async("media_chunk", token="0" * 32, offset=0)
    host_provider = SimpleNamespace()
    await bind_host_media(host_provider, media)
    assert host_provider._native_voice_state(None) == {"safe_voice": True}
    transcript = asyncio.create_task(host_provider._released_voice_transcript(None))
    await asyncio.wait_for(presented.wait(), 1)
    assert not transcript.done()
    state["status"] = "released"
    released.set()
    assert await transcript == ("authoritative voice text", "released")
    await media.close()
    assert not staged_path.exists()


@pytest.mark.asyncio
async def test_media_change_before_first_transfer_is_rejected(tmp_path):
    path = tmp_path / "voice.ogg"
    owner = RunMedia(content(path, b"original"), "Request")
    path.write_bytes(b"modified")
    with pytest.raises(PermissionDenied, match="changed"):
        await owner.bundle()
    assert not owner._bytes


@pytest.mark.asyncio
async def test_compaction_and_draft_wrappers_are_derived_and_trigger_only_once(tmp_path):
    _, _, accepted, fence, context = setup_run(tmp_path)
    calls = []
    base = SimpleNamespace()
    compact = _ExecutionStageCompactionProvider(base, lambda: calls.append("scheduled"))
    runtime = SimpleNamespace(provider=_HChatDraftToolProvider(compact),
                              delivery=SimpleNamespace(allow_immediate_response=False), workzone_ref=str(tmp_path))
    turn = SimpleNamespace(runtime=runtime, binding=RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent"))
    assert manifest(runtime)["provider_features"] == ["hchat_draft", "execution_compaction"]
    for _ in range(2):
        await invoke(turn, "execution_start", {"args": [], "kwargs": {}})
    assert calls == ["scheduled"] and compact._scheduled
    with pytest.raises(PermissionDenied):
        await invoke(turn, "execution_start", {"args": [], "kwargs": {"path": pack("another")}})


@pytest.mark.asyncio
async def test_text_audio_cannot_send_a_different_request_to_original_converter(tmp_path):
    from orchestrator.permissions.media_bridge import invoke_media

    _, _, accepted, fence, context = setup_run(tmp_path)
    calls = []
    async def convert(**kwargs):
        calls.append(kwargs["text"])
        return content(tmp_path / "derived.ogg", b"derived audio")
    turn = SimpleNamespace(binding=RuntimeExecutionBinding(context, accepted.request_id, fence, "INSTANCE:agent"),
                           media=RunMedia(None, "Accepted request"),
                           executor=SimpleNamespace(_stage_provider=SimpleNamespace(materialize_text_audio=convert)))
    kwargs = {"text": "different data", "turn_id": context.turn_id, "request_ref": "hashi-request:" + accepted.request_id}
    with pytest.raises(PermissionDenied):
        await invoke_media(turn, "text_audio", [], kwargs)
    assert not calls
    kwargs["text"] = "Accepted request"
    result = await invoke_media(turn, "text_audio", [], kwargs)
    assert calls == ["Accepted request"]
    assert result["parts"][0]["local_ref"].startswith("permission-media:")
