"""Call's optional external AV presentation; independent of microphone/camera."""

from __future__ import annotations

import asyncio
import hashlib

from .contract import CallError

OUTPUT_PROTOCOL = "call-output/1"


class ExternalCallOutput:
    def __init__(self, call, ports, connector, synthesizer, selection, resolved=None):
        self.call, self.ports = call, ports
        self.connector, self.synthesizer, self.selection = (
            connector,
            synthesizer,
            selection,
        )
        self.origin = resolved.snapshot.node.base_url if resolved is not None else None
        self.location = (
            resolved.snapshot.node.location if resolved is not None else "local"
        )
        self.barrier = None

    def privacy(self):
        self.ports.validate(self.call.owner, self.call.binding)
        level = self.ports.privacy_level(self.call.binding["agent_id"])
        if self.location == "internet" and level not in (0, 1):
            raise CallError("call_cloud_privacy_unqualified", 403)
        return level

    async def ready(self):
        if self.barrier is not None:
            await asyncio.shield(self.barrier)

    def view(self):
        return {**self.connector.view(), "service": self.selection.public()}

    async def synthesize(self, text, *, request_id):
        call, turn = self.call, self.call.turn
        epoch = self.connector.epoch
        await self.ready()
        self.connector.require_active(epoch)
        utterance = "utt-" + hashlib.sha256(request_id.encode()).hexdigest()[:48]

        def on_job(receipt):
            turn.setdefault("provider_receipts", {}).setdefault("video_tts", {})[
                utterance
            ] = receipt

        result = await self.synthesizer.synthesize(
            self.selection,
            self.connector.reference,
            text,
            privacy_level=self.privacy(),
            request_id=request_id,
            expected_origin=self.origin,
            on_job=on_job,
        )
        if call.phase != "active" or call.turn is not turn:
            raise CallError("call_video_interrupted", 409)
        self.connector.require_active(epoch)
        # A completed artifact does not depend on the provider remaining online.
        self.privacy()
        receipt = await self.connector.prepare(utterance, result["media"], epoch=epoch)
        return {
            "_external_output": {
                "utterance_id": utterance,
                "epoch": epoch,
                "receipt": receipt,
            },
            "provider_receipt": result.get("provider_receipt"),
            "_billing_target": {
                "model": self.selection.reference.model_id,
                "kind": "tts",
                "adapter": "video_tts",
            },
        }

    async def commit(self, asset):
        presentation = asset.get("_external_output")
        if not isinstance(presentation, dict):
            raise CallError("call_video_not_prepared", 409)
        await self.ready()
        self.privacy()
        return await self.connector.commit(
            presentation["utterance_id"], epoch=presentation["epoch"]
        )

    async def status(self, asset):
        await self.connector.refresh()
        presentation = asset.get("_external_output") or {}
        receipt = next(
            (
                r
                for r in self.connector.state.get("replies", ())
                if r.get("utterance_id") == presentation.get("utterance_id")
            ),
            None,
        )
        return {
            "ok": True,
            "output": self.view(),
            "presentation": receipt,
            "ready": receipt is not None,
        }

    def fence(self, turn):
        if turn.get("_speech_interrupted"):
            return False
        turn["_speech_interrupted"] = True
        if self.connector.fence(turn.get("turn_id") or "opening"):
            self.barrier = asyncio.create_task(self.connector.interrupt())
            return True
        return False

    async def interrupt(self):
        await self.ready()

    def ending(self):
        self.connector.closed = True
        self.connector.reference = b""
        self.connector.epoch += 1

    async def end(self):
        await self.connector.end()


async def negotiate_output(
    config, ports, synthesizer, call, *, revision, generation, protocol
):
    reader = getattr(config, "video_tts_selection", None)
    selection = reader(revision) if callable(reader) else None
    if selection is None:
        return None
    resolver = getattr(ports, "call_output", None)
    if not callable(resolver):
        return None
    connector = await resolver(call.owner, call.binding, generation=generation)
    if connector is None:
        return None
    if protocol != OUTPUT_PROTOCOL:
        raise CallError("call_output_client_upgrade_required", 409)
    if synthesizer is None:
        raise CallError("call_video_not_configured", 503)
    resolved = await synthesizer.resolve(
        selection, ports.privacy_level(call.binding["agent_id"])
    )
    try:
        ports.validate(call.owner, call.binding)
        await connector.start()
        if call.phase != "active":
            raise CallError("call_ended", 410)
        ports.validate(call.owner, call.binding)
    except BaseException:
        await asyncio.shield(connector.end())
        raise
    # That sink has negotiated exclusive output and must not also receive the
    # old unscoped leave/return or voice mirror events.
    call.frontend_subscribers = tuple(
        x for x in call.frontend_subscribers if x != connector.audio_owner
    )
    return ExternalCallOutput(call, ports, connector, synthesizer, selection, resolved)
