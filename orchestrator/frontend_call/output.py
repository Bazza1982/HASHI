"""Call's optional external AV presentation; independent of microphone/camera."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

from .fallback_av import mux_audio_to_static_mp4
from .tts import CatalogCallTTS

from .contract import CallError

OUTPUT_PROTOCOL = "call-output/1"


class ExternalCallOutput:
    def __init__(
        self,
        call,
        ports,
        connector,
        synthesizer,
        selection,
        resolved=None,
        *,
        fallback_reason_code=None,
        fallback_muxer=None,
    ):
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
        self.fallback_reason_code = str(fallback_reason_code or "")
        self.fallback_mode = (
            "audio_fallback" if self.fallback_reason_code else "video_tts"
        )
        self.fallback_error_code = ""
        self.fallback_muxer = fallback_muxer or mux_audio_to_static_mp4

    @property
    def selected_service_id(self):
        if self.selection is None:
            return "video_tts"
        return self.selection.reference.service_id

    @property
    def video_enabled(self):
        return self.selection is not None and self.fallback_mode == "video_tts"

    @property
    def captions_only(self):
        return self.fallback_mode == "captions_only"

    def activate_audio_fallback(self, reason_code):
        if not self.fallback_reason_code:
            self.fallback_reason_code = str(reason_code or "call_video_generation_failed")
        if not self.captions_only:
            self.fallback_mode = "audio_fallback"

    def activate_captions_only(self, error_code):
        self.fallback_error_code = str(error_code or "call_speech_unavailable")
        self.fallback_mode = "captions_only"

    @staticmethod
    def can_audio_fallback(exc):
        if not isinstance(exc, CallError):
            return False
        return bool(getattr(exc, "call_audio_fallback_allowed", False)) or exc.code in {
            "call_video_fallback_active",
            "call_video_prior_segment_failed",
        }

    def privacy(self):
        self.ports.validate(self.call.owner, self.call.binding)
        level = self.ports.privacy_level(self.call.binding["agent_id"])
        if self.video_enabled and self.location == "internet" and level not in (0, 1):
            raise CallError("call_cloud_privacy_unqualified", 403)
        return level

    async def ready(self):
        if self.barrier is not None:
            await asyncio.shield(self.barrier)

    def view(self):
        return {
            **self.connector.view(),
            "service": self.selection.public() if self.selection is not None else None,
            "degraded": not self.video_enabled,
            "degradation_reason": self.fallback_reason_code or None,
            "fallback_mode": self.fallback_mode,
            "error_code": self.fallback_error_code or None,
        }

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
        if not self.video_enabled:
            raise CallError("call_video_fallback_active", 409)
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

        handoff_to_audio = False
        try:
            try:
                selection = self.selection
                if selection.voice_source == "call_tts":
                    route = CatalogCallTTS.voice_route(call, text)
                    selection = replace(selection, voice=route.voice_name)
                result = await self.synthesizer.synthesize(
                    selection,
                    self.connector.reference,
                    text,
                    privacy_level=self.privacy(),
                    request_id=request_id,
                    expected_origin=self.origin,
                    on_job=on_job,
                )
            except asyncio.CancelledError:
                raise
            except CallError as exc:
                if exc.code not in {
                    "call_video_interrupted",
                    "call_ended",
                    "call_scope_changed",
                    "call_cloud_privacy_unqualified",
                }:
                    exc.call_audio_fallback_allowed = True
                    handoff_to_audio = True
                raise
            except Exception as exc:
                error = CallError("call_video_generation_failed", 502)
                error.call_audio_fallback_allowed = True
                handoff_to_audio = True
                raise error from exc
            # Rendering may run concurrently, but AP accepts one ordered item.
            # A later segment therefore waits only at the presentation gate,
            # after its expensive Compute work has already finished.
            if predecessor is not None and not await asyncio.shield(predecessor):
                raise CallError("call_video_prior_segment_failed", 502)
            if not self.video_enabled:
                raise CallError("call_video_fallback_active", 409)
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
            if not completion.done() and not handoff_to_audio:
                completion.set_result(prepared)

    async def prepare_audio_fallback(
        self,
        audio,
        media_type,
        *,
        request_id,
        segment=0,
        provider_receipt=None,
        billing_target=None,
        billing_usage=None,
        tts_route=None,
    ):
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
        try:
            media = await self.fallback_muxer(
                self.connector.reference, audio, media_type
            )
            if predecessor is not None and not await asyncio.shield(predecessor):
                raise CallError("call_video_prior_segment_failed", 502)
            if call.phase != "active" or call.turn is not turn:
                raise CallError("call_video_interrupted", 409)
            self.connector.require_active(epoch)
            self.privacy()
            receipt = await self.connector.prepare(utterance, media, epoch=epoch)
            prepared = True
            return {
                "_external_output": {
                    "utterance_id": utterance,
                    "epoch": epoch,
                    "receipt": receipt,
                },
                "provider_receipt": provider_receipt,
                "_billing_target": billing_target,
                "_billing_usage": billing_usage,
                "_tts_route": tts_route,
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
    config,
    ports,
    synthesizer,
    call,
    *,
    revision,
    generation,
    protocol,
    audio_fallback_available=False,
    fallback_muxer=None,
):
    resolver = getattr(ports, "call_output", None)
    if not callable(resolver):
        return None
    connector = await resolver(call.owner, call.binding, generation=generation)
    if connector is None:
        return None
    # AP remains the sole presenter. If VideoTTS is unavailable, ordinary TTS
    # is packaged as a static MP4 so the same scoped output contract survives.
    if protocol != OUTPUT_PROTOCOL:
        raise CallError("call_output_client_upgrade_required", 409)
    reader = getattr(config, "video_tts_selection", None)
    selection = reader(revision) if callable(reader) else None
    resolved = None
    fallback_reason_code = ""
    if selection is None or synthesizer is None:
        fallback_reason_code = "call_video_not_configured"
    else:
        try:
            resolved = await synthesizer.resolve(
                selection, ports.privacy_level(call.binding["agent_id"])
            )
        except CallError as exc:
            fallback_reason_code = exc.code
        except Exception:
            fallback_reason_code = "call_video_service_unavailable"
    if fallback_reason_code and not audio_fallback_available:
        raise CallError(fallback_reason_code, 503)
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
    return ExternalCallOutput(
        call,
        ports,
        connector,
        synthesizer,
        selection,
        resolved,
        fallback_reason_code=fallback_reason_code,
        fallback_muxer=fallback_muxer,
    )
