"""Fixed output-audio publication into the original Worker asset owner."""
from __future__ import annotations

import asyncio
import base64
from hashlib import sha256
import re
from uuid import uuid4

from orchestrator.audio_assets import AudioAssetStore, AudioAssetNotFound, asset_root_from_global_config
from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.media_bridge import CHUNK, MAX_MEDIA


async def invoke_audio(turn, operation, args, kwargs):
    if args or getattr(turn, "media", None) is None:
        raise PermissionDenied("audio publication requires the accepted Turn")
    owner = turn.media
    pending = getattr(owner, "_audio_transfers", None)
    if pending is None:
        pending = owner._audio_transfers = {}
    if operation == "audio_begin":
        if set(kwargs) != {"engine", "model", "metadata", "size", "digest"}:
            raise PermissionDenied("invalid native output declaration")
        size, digest = kwargs["size"], kwargs["digest"]
        if type(size) is not int or not 0 < size <= MAX_MEDIA or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise PermissionDenied("native output exceeds its bound")
        provider = turn.executor._stage_provider
        backend = provider.backend_manager.create_herv3_provider_backend(kwargs["engine"], kwargs["model"])
        resolver = getattr(backend, "_native_audio_output_profile", None)
        policy = resolver(owner.content) if callable(resolver) else None
        metadata = kwargs["metadata"]
        if not isinstance(policy, dict) or not isinstance(metadata, dict) or metadata != {
            "mime_type": policy["mime_type"], "audio_format": policy["asset_format"],
            "retention_seconds": policy["retention_seconds"], "retention_indefinite": policy["retention_indefinite"],
        }:
            raise PermissionDenied("native output differs from the selected audio profile")
        identity = sha256((kwargs["engine"] + ":" + kwargs["model"] + ":" + digest).encode()).hexdigest()
        for token, record in pending.items():
            if record["identity"] == identity:
                if record["size"] != size or record["metadata"] != metadata:
                    raise PermissionDenied("native output declaration changed")
                return token
        if len(pending) >= 16 or any("result" not in record for record in pending.values()):
            raise PermissionDenied("native output publication is already pending")
        token = uuid4().hex
        pending[token] = {"identity": identity, "size": size, "digest": digest, "metadata": metadata,
                          "data": bytearray(), "engine": kwargs["engine"], "model": kwargs["model"]}
        return token
    if set(kwargs) - {"token", "offset", "data"} or kwargs.get("token") not in pending:
        raise PermissionDenied("no native output belongs to this token")
    record = pending[kwargs["token"]]
    if operation == "audio_chunk" and set(kwargs) == {"token", "offset", "data"}:
        offset = kwargs["offset"]
        if type(offset) is not int or offset < 0 or offset % CHUNK:
            raise PermissionDenied("invalid native output offset")
        data = base64.b64decode(kwargs["data"], validate=True)
        if len(data) != min(CHUNK, record["size"] - offset) or not data:
            raise PermissionDenied("invalid native output chunk size")
        if "result" in record:
            return True
        target = record["data"]
        if offset < len(target):
            if target[offset:offset + len(data)] != data:
                raise PermissionDenied("native output retry changed")
        elif offset == len(target):
            target.extend(data)
        else:
            raise PermissionDenied("native output chunk skipped bytes")
        return True
    if operation != "audio_complete" or set(kwargs) != {"token"}:
        raise PermissionDenied("unknown native output operation")
    if "result" in record:
        return record["result"]
    payload = bytes(record["data"])
    if len(payload) != record["size"] or sha256(payload).hexdigest() != record["digest"]:
        raise PermissionDenied("native output bytes differ from the declaration")
    provider = turn.executor._stage_provider
    global_config = provider.runtime_context.global_config
    store = AudioAssetStore(asset_root_from_global_config(global_config))
    context = turn.binding.context
    asset_id = "media_" + sha256((turn.binding.request_id + ":" + record["identity"]).encode()).hexdigest()[:40]
    def publish():
        try:
            result = store.describe(asset_id, owner_id=context.owner_id, session_id=context.session_id)
            if result["sha256"] != record["digest"] or result["size_bytes"] != len(payload):
                raise PermissionDenied("the original native output changed")
            return result
        except AudioAssetNotFound:
            return store.create(payload, owner_id=context.owner_id, session_id=context.session_id,
                                direction="output", asset_id=asset_id, **record["metadata"],
                                correlation={"request_id": turn.binding.request_id, "provider": record["engine"], "model": record["model"]})
    if "publishing" not in record:
        record["publishing"] = asyncio.create_task(asyncio.to_thread(publish))
    result = await asyncio.shield(record["publishing"])
    record["result"] = result
    record.pop("data", None)
    return result


class HostAudioStore:
    def __init__(self, ports, engine, model):
        self.ports, self.engine, self.model = ports, engine, model

    def create(self, payload, **kwargs):
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_MEDIA:
            raise PermissionDenied("native output payload exceeds its bound")
        metadata = {key: kwargs[key] for key in ("mime_type", "audio_format", "retention_seconds", "retention_indefinite")}
        token = self.ports._sync("audio_begin", engine=self.engine, model=self.model, metadata=metadata,
                                 size=len(payload), digest=sha256(payload).hexdigest())
        for offset in range(0, len(payload), CHUNK):
            self.ports._sync("audio_chunk", token=token, offset=offset,
                             data=base64.b64encode(payload[offset:offset + CHUNK]).decode("ascii"))
        return self.ports._sync("audio_complete", token=token)
