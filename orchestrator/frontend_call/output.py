"""Call's optional external AV presentation; independent of microphone/camera."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

from .tts import CatalogCallTTS

from .contract import CallError
from .diagnostics import emit, error_facts, receipt_facts

OUTPUT_PROTOCOL = "call-output/1"
VIDEO_TTS_SUBMISSION_CONCURRENCY = 2


class VideoTTSDeferred(CallError):
    """A submitted VideoTTS job that outlived this segment's adaptive wait."""

    def __init__(
        self,
        output,
        provider_task,
        *,
        call,
        turn,
        epoch,
        segment,
        utterance,
        completion,
        predecessor,
        wait_seconds,
        text_chars,
    ):
        super().__init__("call_video_wait_timeout", 202)
        self.output = output
        self.provider_task = provider_task
        self.call = call
        self.turn = turn
        self.epoch = epoch
        self.segment = segment
        self.utterance = utterance
        self.completion = completion
        self.predecessor = predecessor
        self.wait_seconds = wait_seconds
        self.text_chars = text_chars
        self.abandoned = False

    async def wait(self):
        return await asyncio.shield(self.provider_task)

    async def prepare(self, result):
        if self.abandoned:
            raise CallError("call_video_interrupted", 409)
        return await self.output._prepare_video_result(self, result)

    def abandon(self, reason_code="call_video_wait_timeout", *, update_health=True):
        if self.abandoned:
            return
        self.abandoned = True
        self.output._abandon_deferred(self, reason_code, update_health=update_health)


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
        self._provider_tasks = set()
        self._late_observers = set()
        # Bound only the short async-job admission handshake. Once the provider
        # accepts a job, its render continues concurrently and the next submits.
        self._submission_slots = asyncio.Semaphore(VIDEO_TTS_SUBMISSION_CONCURRENCY)
        self.fallback_reason_code = str(fallback_reason_code or "")
        self.transient_reason_code = ""

    @property
    def selected_service_id(self):
        if self.selection is None:
            return "video_tts"
        return self.selection.reference.service_id

    @property
    def video_enabled(self):
        return not self.fallback_reason_code and self.selection is not None

    @property
    def pending_video_jobs(self):
        return sum(not task.done() for task in self._provider_tasks)

    def foreground_wait_seconds(self, text):
        if self.selection is None:
            return 0.0
        return self.selection.foreground_wait_seconds(text)

    def client_wait_seconds(self, text):
        # Leave room for ordinary TTS after a soft VideoTTS wait expires.
        return self.foreground_wait_seconds(text) + 30.0

    @staticmethod
    def can_audio_fallback(exc):
        if not isinstance(exc, CallError):
            return True
        return exc.code not in {
            "call_ended",
            "call_scope_changed",
            "call_video_interrupted",
        }

    @staticmethod
    def requires_permanent_fallback(exc):
        return isinstance(exc, CallError) and exc.code in {
            "call_video_configuration_invalid",
            "call_video_configuration_changed",
            "call_video_interface_invalid",
            "call_video_capability_mismatch",
            "call_cloud_privacy_unqualified",
            "call_video_reference_invalid",
            "call_video_reference_unsupported",
        }

    @staticmethod
    def transient_resolution_failure(exc):
        return isinstance(exc, CallError) and exc.code in {
            "call_video_compute_node_unreachable",
            "call_video_compute_node_unhealthy",
            "call_video_compute_node_busy",
            "call_video_compute_manifest_rejected",
            "call_video_service_unavailable",
            "call_video_service_busy",
        }

    def note_transient_fallback(self, reason_code):
        self.transient_reason_code = str(reason_code or "call_video_generation_failed")

    def temporary_audio_view(self, reason_code):
        service = self.selection.public() if self.selection is not None else None
        return {
            "protocol": None,
            "mode": "window_audio",
            "audio_owner": "workbench",
            "stage": "degraded",
            "active": self.call.phase == "active",
            "service": service,
            "degraded": True,
            "degradation_reason": str(reason_code or "call_video_generation_failed"),
            "fallback_mode": "segment_audio_fallback",
        }

    async def _end_connector_best_effort(self):
        try:
            await self.connector.end()
        except Exception:
            pass

    def activate_audio_fallback(self, reason_code):
        first = not self.fallback_reason_code
        if first:
            self.fallback_reason_code = str(
                reason_code or "call_video_generation_failed"
            )
            try:
                self.connector.fence("audio_fallback")
            except Exception:
                pass
            self.barrier = asyncio.create_task(self._end_connector_best_effort())
        return first

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
        service = self.selection.public() if self.selection is not None else None
        if self.video_enabled:
            return {
                **self.connector.view(),
                "service": service,
                "degraded": bool(self.transient_reason_code),
                "degradation_reason": self.transient_reason_code or None,
                "fallback_mode": (
                    "segment_audio_fallback"
                    if self.transient_reason_code
                    else "video_tts"
                ),
            }
        return {
            "protocol": None,
            "mode": "window_audio",
            "audio_owner": "workbench",
            "stage": "degraded",
            "active": self.call.phase == "active",
            "service": service,
            "degraded": True,
            "degradation_reason": self.fallback_reason_code or None,
            "fallback_mode": "audio_fallback",
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

    def _own_provider_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._provider_tasks.add(task)
        task.add_done_callback(self._provider_tasks.discard)
        return task

    def _deferred_facts(self, deferred):
        return {
            "call_id": deferred.call.binding.get("call_id"),
            "turn_id": deferred.turn.get("turn_id"),
            "segment": deferred.segment,
            "text_chars": deferred.text_chars,
            "wait_seconds": round(deferred.wait_seconds, 3),
        }

    async def _observe_late(self, deferred, *, update_health):
        try:
            result = await deferred.wait()
            if update_health and self.video_enabled:
                self.transient_reason_code = ""
            emit(
                "video_tts_late_completed",
                **self._deferred_facts(deferred),
                **receipt_facts(result.get("provider_receipt")),
            )
        except asyncio.CancelledError:
            emit("video_tts_late_observer_cancelled", **self._deferred_facts(deferred))
        except Exception as exc:
            if update_health and self.video_enabled:
                self.note_transient_fallback(
                    exc.code
                    if isinstance(exc, CallError)
                    else "call_video_generation_failed"
                )
            emit(
                "video_tts_late_failed",
                **self._deferred_facts(deferred),
                **error_facts(exc, "call_video_generation_failed"),
            )

    def _abandon_deferred(self, deferred, reason_code, *, update_health):
        if not deferred.completion.done():
            deferred.completion.set_result(False)
        if update_health and self.video_enabled:
            self.note_transient_fallback(reason_code)
        observer = asyncio.create_task(
            self._observe_late(deferred, update_health=update_health)
        )
        self._late_observers.add(observer)
        observer.add_done_callback(self._late_observers.discard)

    async def _prepare_video_result(self, deferred, result):
        prepared = False
        try:
            if not self.video_enabled:
                raise CallError("call_video_fallback_active", 409)
            # Rendering may run concurrently. The presentation gate preserves
            # order, but an audio-fallback predecessor does not poison later video.
            if deferred.predecessor is not None:
                await asyncio.shield(deferred.predecessor)
            if (
                deferred.call.phase != "active"
                or deferred.call.turn is not deferred.turn
            ):
                raise CallError("call_video_interrupted", 409)
            self.connector.require_active(deferred.epoch)
            # A completed artifact does not depend on the provider remaining online.
            self.privacy()
            receipt = await self.connector.prepare(
                deferred.utterance, result["media"], epoch=deferred.epoch
            )
            prepared = True
            self.transient_reason_code = ""
            emit("video_tts_segment_ready", **self._deferred_facts(deferred))
            return {
                "_external_output": {
                    "utterance_id": deferred.utterance,
                    "epoch": deferred.epoch,
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
            if not deferred.completion.done():
                deferred.completion.set_result(prepared)

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
        await self.ready()
        self.connector.require_active(epoch)
        utterance = "utt-" + hashlib.sha256(request_id.encode()).hexdigest()[:48]

        submission_released = False

        def release_submission():
            nonlocal submission_released
            if not submission_released:
                submission_released = True
                self._submission_slots.release()

        def on_job(receipt):
            turn.setdefault("provider_receipts", {}).setdefault("video_tts", {})[
                utterance
            ] = receipt
            if receipt.get("status") == "accepted":
                release_submission()

        selection = self.selection
        if selection.voice_source == "call_tts":
            route = CatalogCallTTS.voice_route(call, text)
            selection = replace(selection, voice=route.voice_name)

        async def generate():
            await self._submission_slots.acquire()
            try:
                return await self.synthesizer.synthesize(
                    selection,
                    self.connector.reference,
                    text,
                    privacy_level=self.privacy(),
                    request_id=request_id,
                    expected_origin=self.origin,
                    on_job=on_job,
                )
            finally:
                release_submission()

        provider_task = self._own_provider_task(generate())
        wait_seconds = selection.foreground_wait_seconds(text)
        deferred = VideoTTSDeferred(
            self,
            provider_task,
            call=call,
            turn=turn,
            epoch=epoch,
            segment=segment,
            utterance=utterance,
            completion=completion,
            predecessor=predecessor,
            wait_seconds=wait_seconds,
            text_chars=len(text),
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(provider_task), timeout=wait_seconds
            )
        except TimeoutError:
            self.note_transient_fallback("call_video_wait_timeout")
            emit("video_tts_wait_deferred", **self._deferred_facts(deferred))
            raise deferred
        except asyncio.CancelledError:
            deferred.abandon("call_video_interrupted", update_health=False)
            raise
        except BaseException:
            if not completion.done():
                completion.set_result(False)
            raise
        return await deferred.prepare(result)

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
    if protocol != OUTPUT_PROTOCOL:
        raise CallError("call_output_client_upgrade_required", 409)
    reader = getattr(config, "video_tts_selection", None)
    selection = reader(revision) if callable(reader) else None
    resolved = None
    fallback_reason_code = ""
    transient_reason_code = ""
    if selection is None or synthesizer is None:
        fallback_reason_code = "call_video_not_configured"
    else:
        try:
            resolved = await synthesizer.resolve(
                selection, ports.privacy_level(call.binding["agent_id"])
            )
        except asyncio.CancelledError:
            raise
        except CallError as exc:
            if ExternalCallOutput.transient_resolution_failure(exc):
                transient_reason_code = exc.code
            else:
                fallback_reason_code = exc.code
        except Exception:
            transient_reason_code = "call_video_service_unavailable"
    if fallback_reason_code:
        return ExternalCallOutput(
            call,
            ports,
            connector,
            synthesizer,
            selection,
            resolved,
            fallback_reason_code=fallback_reason_code,
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
    output = ExternalCallOutput(
        call, ports, connector, synthesizer, selection, resolved
    )
    if transient_reason_code:
        output.note_transient_fallback(transient_reason_code)
    return output
