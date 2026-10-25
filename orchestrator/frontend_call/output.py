"""Call's optional external AV presentation; independent of microphone/camera."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

from .tts import CatalogCallTTS

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
        self._prepare_done = {}

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

    def _window_receipt(self, receipt):
        """Project the logical Call ID without mutating connector-owned scope."""
        if receipt is None:
            return None
        if not isinstance(receipt, dict):
            raise CallError("call_video_connector_response_invalid", 502)
        return {**receipt, "call_id": self.call.binding["call_id"]}

    def _prepare_completion(self, epoch, segment, *, reset_failed=False):
        key = (int(epoch), int(segment))
        completion = self._prepare_done.get(key)
        if (
            completion is not None
            and reset_failed
            and completion.done()
            and completion.result() is False
        ):
            completion = None
        if completion is None:
            completion = asyncio.get_running_loop().create_future()
            self._prepare_done[key] = completion
        if len(self._prepare_done) > 32:
            for old_key, old_completion in list(self._prepare_done.items()):
                if old_key[0] != epoch and old_completion.done():
                    self._prepare_done.pop(old_key, None)
        return completion

    async def synthesize(self, text, *, request_id, segment=0):
        call, turn = self.call, self.call.turn
        epoch = self.connector.epoch
        segment = max(0, int(segment or 0))
        completion = self._prepare_completion(epoch, segment, reset_failed=True)
        predecessor = (
            self._prepare_completion(epoch, segment - 1) if segment > 0 else None
        )
        prepared = False
        await self.ready()
        self.connector.require_active(epoch)
        utterance = "utt-" + hashlib.sha256(request_id.encode()).hexdigest()[:48]

        def on_job(receipt):
            turn.setdefault("provider_receipts", {}).setdefault("video_tts", {})[
                utterance
            ] = receipt

        selection = self.selection
        if selection.voice_source == "call_tts":
            route = CatalogCallTTS.voice_route(call, text)
            selection = replace(selection, voice=route.voice_name)
        try:
            result = await self.synthesizer.synthesize(
                selection,
                self.connector.reference,
                text,
                privacy_level=self.privacy(),
                request_id=request_id,
                expected_origin=self.origin,
                on_job=on_job,
            )
            # Rendering may run concurrently, but AP accepts one ordered item.
            # A later segment therefore waits only at the presentation gate,
            # after its expensive Compute work has already finished.
            if predecessor is not None and not await asyncio.shield(predecessor):
                raise CallError("call_video_prior_segment_failed", 502)
            if call.phase != "active" or call.turn is not turn:
                raise CallError("call_video_interrupted", 409)
            self.connector.require_active(epoch)
            # A completed artifact does not depend on the provider remaining online.
            self.privacy()
            receipt = await self.connector.prepare(
                utterance, result["media"], epoch=epoch
            )
            prepared = True
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
        finally:
            if not completion.done():
                completion.set_result(prepared)

    async def commit(self, asset):
        presentation = asset.get("_external_output")
        if not isinstance(presentation, dict):
            raise CallError("call_video_not_prepared", 409)
        await self.ready()
        self.privacy()
        return self._window_receipt(
            await self.connector.commit(
                presentation["utterance_id"], epoch=presentation["epoch"]
            )
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
            "presentation": self._window_receipt(receipt),
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
    resolver = getattr(ports, "call_output", None)
    if not callable(resolver):
        return None
    connector = await resolver(call.owner, call.binding, generation=generation)
    if connector is None:
        return None
    # An opted-in AP connection makes /call a video call. Audio remains /phone.
    reader = getattr(config, "video_tts_selection", None)
    selection = reader(revision) if callable(reader) else None
    if selection is None:
        raise CallError("call_video_not_configured", 503)
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
