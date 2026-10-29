"""Run-bound media/voice conversion ports; no host-selected file loader."""
from __future__ import annotations

import asyncio
import base64
import copy
from hashlib import sha256
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from orchestrator.multimodal_contract import normalize_request_content
from orchestrator.permissions.authority import PermissionDenied

MAX_MEDIA = 64 * 1024 * 1024
CHUNK = 192 * 1024


class RunMedia:
    def __init__(self, content, prompt):
        self.content = normalize_request_content(content)
        self.prompt = prompt
        self._bytes, self._bundle = {}, None
        self._lock = asyncio.Lock()

    async def bundle(self, content=None):
        async with self._lock:
            if content is None and self._bundle is not None:
                return copy.deepcopy(self._bundle)
            result = normalize_request_content(self.content if content is None else content)
            if result is None:
                return None
            result = copy.deepcopy(result)
            added = {}
            total = sum(map(len, self._bytes.values()))
            for item in result["parts"]:
                if item["type"] != "media":
                    continue
                size = item["size_bytes"]
                if size > MAX_MEDIA - total:
                    raise PermissionDenied("accepted media exceeds the bounded transfer profile")
                path = Path(item["local_ref"])
                def read():
                    with path.open("rb") as stream:
                        return stream.read(size + 1)
                payload = await asyncio.to_thread(read)
                if len(payload) != size or sha256(payload).hexdigest() != item["sha256"]:
                    raise PermissionDenied("accepted media changed before transfer")
                token = uuid4().hex
                item["local_ref"] = "permission-media:" + token
                # Transport references are not authority to open additional files.
                item["transport"] = {}
                added[token] = payload
                total += len(payload)
            self._bytes.update(added)
            if content is None:
                self._bundle = result
            return copy.deepcopy(result)

    def chunk(self, token, offset):
        if (not isinstance(token, str) or token not in self._bytes or type(offset) is not int
                or offset < 0 or offset > len(self._bytes[token]) or offset % CHUNK):
            raise PermissionDenied("media chunk does not belong to the accepted Run")
        return base64.b64encode(self._bytes[token][offset:offset + CHUNK]).decode("ascii")


def _provider(turn):
    provider = getattr(turn.executor, "_stage_provider", None)
    if provider is None:
        raise PermissionDenied("the original media/voice owner is unavailable")
    return provider


async def invoke_media(turn, operation, args, kwargs):
    if operation in {"audio_begin", "audio_chunk", "audio_complete"}:
        from orchestrator.permissions.media_output import invoke_audio
        return await invoke_audio(turn, operation, args, kwargs)
    owner = getattr(turn, "media", None)
    if owner is None:
        raise PermissionDenied("accepted media is unavailable")
    if args:
        raise PermissionDenied("media ports accept only fixed named fields")
    if operation == "media_manifest" and not kwargs:
        return await owner.bundle()
    if operation == "media_chunk" and set(kwargs) == {"token", "offset"}:
        return owner.chunk(**kwargs)
    provider = _provider(turn)
    if operation == "media_cleanup" and not kwargs:
        for record in getattr(owner, "_audio_transfers", {}).values():
            task = record.get("publishing")
            if task is not None:
                await asyncio.shield(task)
        getattr(owner, "_audio_transfers", {}).clear()
        await provider.cleanup_text_audio()
        return None
    request = SimpleNamespace(request_content=owner.content, request_ref="hashi-request:" + turn.binding.request_id)
    if operation == "voice_state" and not kwargs:
        state = provider._native_voice_state(request)
        return {"safe_voice": bool(isinstance(state, dict) and state.get("safe_voice"))} if state else None
    if operation == "voice_transcript" and not kwargs:
        return await provider._released_voice_transcript(request)
    if operation == "text_audio" and set(kwargs) == {"text", "turn_id", "request_ref"}:
        from orchestrator.her_v2.prompts import extract_authoritative_current_request
        expected = extract_authoritative_current_request(owner.prompt)
        if (kwargs["turn_id"] != turn.binding.context.turn_id
                or kwargs["request_ref"] != request.request_ref or kwargs["text"] != expected):
            raise PermissionDenied("audio adaptation differs from the accepted request")
        converted = await provider.materialize_text_audio(**kwargs)
        return await owner.bundle(converted)
    raise PermissionDenied("unknown media/voice operation")


class HostMedia:
    def __init__(self, ports, root):
        self.ports, self.root = ports, Path(root) / "media"
        self.paths = set()

    async def stage(self, content):
        content = normalize_request_content(content)
        if content is None:
            return None
        self.root.mkdir(exist_ok=True)
        result = copy.deepcopy(content)
        total = 0
        for item in result["parts"]:
            if item["type"] != "media":
                continue
            token = item["local_ref"].removeprefix("permission-media:")
            if re.fullmatch(r"[0-9a-f]{32}", token) is None:
                raise PermissionDenied("host media requires an assigned reference")
            size, total = item["size_bytes"], total + item["size_bytes"]
            if total > MAX_MEDIA:
                raise PermissionDenied("host media transfer exceeds its bound")
            suffix = Path(item["filename"]).suffix.lower()
            if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) is None:
                suffix = ".bin"
            target = self.root / (token + suffix)
            if not target.exists():
                digest = sha256()
                with target.open("xb") as stream:
                    self.paths.add(target)
                    for offset in range(0, size, CHUNK):
                        encoded = await self.ports._async("media_chunk", token=token, offset=offset)
                        payload = base64.b64decode(encoded, validate=True)
                        if len(payload) != min(CHUNK, size - offset):
                            raise PermissionDenied("host media chunk size changed")
                        digest.update(payload)
                        stream.write(payload)
                if digest.hexdigest() != item["sha256"]:
                    raise PermissionDenied("host media digest changed")
            elif target.stat().st_size != size or sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                raise PermissionDenied("host staged media changed")
            item["local_ref"] = str(target)
        return result

    async def accepted(self):
        return await self.stage(await self.ports._async("media_manifest"))

    async def text_audio(self, **kwargs):
        return await self.stage(await self.ports._async("text_audio", **kwargs))

    async def close(self):
        for path in tuple(self.paths):
            path.unlink(missing_ok=True)
            self.paths.remove(path)
        if self.root.exists():
            self.root.rmdir()


async def bind_host_media(provider, media):
    from types import MethodType

    state = await media.ports._async("voice_state")

    def native_state(self, request):
        return copy.deepcopy(state)

    async def transcript(self, request):
        return await media.ports._async("voice_transcript")

    async def text_audio(self, **kwargs):
        return await media.text_audio(**kwargs)

    provider._native_voice_state = MethodType(native_state, provider)
    provider._released_voice_transcript = MethodType(transcript, provider)
    provider.materialize_text_audio = MethodType(text_audio, provider)
