"""Call's optional external AV presentation; independent of microphone/camera."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import replace

from .tts import CatalogCallTTS

from .contract import CallError
from .diagnostics import emit, error_facts, receipt_facts

OUTPUT_PROTOCOL = "call-output/1"
VIDEO_TTS_SUBMISSION_CONCURRENCY = 2


class _VideoTTSLifecycle:
    """One group from local admission through provider terminal state."""

    def __init__(
        self,
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
        self.call = call
        self.turn = turn
        self.epoch = epoch
        self.segment = segment
        self.utterance = utterance
        self.completion = completion
        self.predecessor = predecessor
        self.wait_seconds = wait_seconds
        self.text_chars = text_chars
        self.created_at = time.monotonic()
        self.slot_acquired_at = None
        self.submit_started_at = None
        self.accepted_at = None
        self.finished_at = None
        self.state = "waiting_slot"
        self.submission_started = asyncio.Event()
        self.accepted = asyncio.Event()
        self.provider_task = None
        self.unsubmitted_cancelled = False

    @property
    def submitted(self):
        return self.submit_started_at is not None

    @property
    def accepted_job(self):
        return self.accepted_at is not None


class VideoTTSDeferred(CallError):
    """An accepted VideoTTS job that outlived its adaptive foreground wait."""

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
        lifecycle,
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
        self.lifecycle = lifecycle
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
        self._video_jobs = {}
        self._provider_tasks = set()
        self._late_observers = set()
        # Bound only the short async-job admission handshake. Once the provider
        # accepts a job, its render continues concurrently and the next submits.
        self._submission_slots = asyncio.Semaphore(VIDEO_TTS_SUBMISSION_CONCURRENCY)
        # All logical groups are scheduled immediately. This provider window
        # bounds actual work; waiting here never consumes the adaptive render
        # budget, which starts only after the provider accepts a job.
        self._provider_slots = asyncio.Semaphore(
            selection.max_inflight_groups if selection is not None else 1
        )
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

    def client_wait_remaining_seconds(self, segment):
        """Project a rolling queue wait, then the accepted job's real deadline."""
        job = self._video_jobs.get((self.connector.epoch, int(segment)))
        if job is None:
            return None
        now = time.monotonic()
        if job.accepted_at is not None:
            return max(
                0.0,
                job.accepted_at + job.wait_seconds + 30.0 - now,
            )
        # A local waiter or uncertain submission has no render deadline yet.
        # Keep the client polling while the separately bounded lifecycle resolves.
        return 30.0

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

    def _register_video_job(self, job):
        self._video_jobs[(job.epoch, job.segment)] = job
        if len(self._video_jobs) > 64:
            for key, old_job in list(self._video_jobs.items()):
                task = old_job.provider_task
                if key[0] != job.epoch and (task is None or task.done()):
                    self._video_jobs.pop(key, None)

    def _own_provider_task(self, coroutine, lifecycle):
        task = asyncio.create_task(coroutine)
        lifecycle.provider_task = task
        self._provider_tasks.add(task)

        def finished(completed):
            lifecycle.finished_at = time.monotonic()
            if completed.cancelled():
                lifecycle.state = "cancelled"
            elif completed.exception() is not None:
                lifecycle.state = "failed"
            elif lifecycle.state not in {"completed", "failed", "cancelled"}:
                lifecycle.state = "completed"
            self._provider_tasks.discard(completed)

        task.add_done_callback(finished)
        return task

    @staticmethod
    def _lifecycle_facts(job):
        now = time.monotonic()
        slot_at = job.slot_acquired_at
        submit_at = job.submit_started_at
        accepted_at = job.accepted_at
        return {
            "call_id": job.call.binding.get("call_id"),
            "turn_id": job.turn.get("turn_id"),
            "segment": job.segment,
            "text_chars": job.text_chars,
            "run_state": job.state,
            "wait_budget_ms": int(job.wait_seconds * 1000),
            "queue_wait_ms": int(((slot_at or now) - job.created_at) * 1000),
            "total_elapsed_ms": int((now - job.created_at) * 1000),
            **(
                {
                    "pre_submit_elapsed_ms": int((submit_at - slot_at) * 1000),
                }
                if submit_at is not None and slot_at is not None
                else {}
            ),
            **(
                {
                    "submission_elapsed_ms": int((accepted_at - submit_at) * 1000),
                    "job_elapsed_ms": int((now - accepted_at) * 1000),
                }
                if accepted_at is not None and submit_at is not None
                else {}
            ),
        }

    def _deferred_facts(self, deferred):
        return self._lifecycle_facts(deferred.lifecycle)

    def _cancel_unsubmitted(self, job, reason_code):
        if job.submitted:
            return False
        if job.unsubmitted_cancelled:
            return True
        job.unsubmitted_cancelled = True
        job.state = "cancelled_unsubmitted"
        if not job.completion.done():
            job.completion.set_result(False)
        task = job.provider_task
        if task is not None and not task.done():
            task.cancel()
        emit(
            "video_tts_unsubmitted_cancelled",
            **self._lifecycle_facts(job),
            reason_code=reason_code,
        )
        return True

    def _cancel_unsubmitted_for_turn(self, turn, reason_code):
        for job in tuple(self._video_jobs.values()):
            if job.turn is turn:
                self._cancel_unsubmitted(job, reason_code)

    async def _observe_abandoned(self, deferred, *, update_health):
        try:
            result = await deferred.wait()
            accepted = deferred.lifecycle.accepted_job
            if update_health and self.video_enabled:
                self.transient_reason_code = ""
            emit(
                (
                    "video_tts_late_completed"
                    if accepted
                    else "video_tts_submission_outcome_completed"
                ),
                **self._deferred_facts(deferred),
                **receipt_facts(result.get("provider_receipt")),
            )
        except asyncio.CancelledError:
            emit(
                "video_tts_abandoned_observer_cancelled",
                **self._deferred_facts(deferred),
            )
        except Exception as exc:
            if update_health and self.video_enabled:
                self.note_transient_fallback(
                    exc.code
                    if isinstance(exc, CallError)
                    else "call_video_generation_failed"
                )
            emit(
                (
                    "video_tts_late_failed"
                    if deferred.lifecycle.accepted_job
                    else "video_tts_submission_outcome_failed"
                ),
                **self._deferred_facts(deferred),
                **error_facts(exc, "call_video_generation_failed"),
            )

    def _abandon_deferred(self, deferred, reason_code, *, update_health):
        if not deferred.completion.done():
            deferred.completion.set_result(False)
        if update_health and self.video_enabled:
            self.note_transient_fallback(reason_code)
        if self._cancel_unsubmitted(deferred.lifecycle, reason_code):
            return
        task = deferred.provider_task
        if task.done() and task.cancelled():
            return
        observer = asyncio.create_task(
            self._observe_abandoned(deferred, update_health=update_health)
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

        selection = self.selection
        if selection.voice_source == "call_tts":
            route = CatalogCallTTS.voice_route(call, text)
            selection = replace(selection, voice=route.voice_name)
        wait_seconds = selection.foreground_wait_seconds(text)
        lifecycle = _VideoTTSLifecycle(
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
        self._register_video_job(lifecycle)

        submission_acquired = False
        submission_released = False

        def release_submission():
            nonlocal submission_released
            if submission_acquired and not submission_released:
                submission_released = True
                self._submission_slots.release()

        def start_submission():
            if lifecycle.submit_started_at is not None:
                return
            lifecycle.submit_started_at = time.monotonic()
            lifecycle.state = "submitting"
            lifecycle.submission_started.set()
            emit(
                "video_tts_submission_started",
                **self._lifecycle_facts(lifecycle),
            )

        def on_job(receipt):
            turn.setdefault("provider_receipts", {}).setdefault("video_tts", {})[
                utterance
            ] = receipt
            status = receipt.get("status")
            if status == "accepted" and lifecycle.accepted_at is None:
                start_submission()
                lifecycle.accepted_at = time.monotonic()
                lifecycle.state = "accepted"
                lifecycle.accepted.set()
                release_submission()
                emit(
                    "video_tts_foreground_wait_started",
                    **self._lifecycle_facts(lifecycle),
                )
            elif status in {"completed", "failed", "cancelled"}:
                lifecycle.state = status

        async def generate():
            nonlocal submission_acquired
            provider_acquired = False
            try:
                await self._provider_slots.acquire()
                provider_acquired = True
                lifecycle.slot_acquired_at = time.monotonic()
                lifecycle.state = "waiting_submission"
                emit(
                    "video_tts_slot_acquired",
                    **self._lifecycle_facts(lifecycle),
                )
                await self._submission_slots.acquire()
                submission_acquired = True
                lifecycle.state = "preparing_submission"
                self.connector.require_active(epoch)
                if call.phase != "active" or call.turn is not turn:
                    raise CallError("call_video_interrupted", 409)
                return await self.synthesizer.synthesize(
                    selection,
                    self.connector.reference,
                    text,
                    privacy_level=self.privacy(),
                    request_id=request_id,
                    expected_origin=self.origin,
                    on_submit=start_submission,
                    on_job=on_job,
                )
            finally:
                release_submission()
                if provider_acquired:
                    self._provider_slots.release()

        provider_task = self._own_provider_task(generate(), lifecycle)
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
            lifecycle=lifecycle,
        )

        async def wait_for_event(event, *, timeout=None):
            waiter = asyncio.create_task(event.wait())
            try:
                done, _pending = await asyncio.wait(
                    {provider_task, waiter},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                return done
            finally:
                if not waiter.done():
                    waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)

        try:
            done = await wait_for_event(
                lifecycle.submission_started,
                timeout=selection.queue_timeout_seconds,
            )
            if not done and provider_task.done():
                done = {provider_task}
            if not done and not lifecycle.submitted:
                self.note_transient_fallback("call_video_wait_timeout")
                self._cancel_unsubmitted(lifecycle, "call_video_wait_timeout")
                emit(
                    "video_tts_queue_wait_timeout",
                    **self._lifecycle_facts(lifecycle),
                )
                raise CallError("call_video_wait_timeout", 202)
            if provider_task in done:
                return await deferred.prepare(provider_task.result())

            done = await wait_for_event(lifecycle.accepted)
            if provider_task in done:
                return await deferred.prepare(provider_task.result())

            remaining = max(
                0.0,
                wait_seconds - (time.monotonic() - lifecycle.accepted_at),
            )
            if remaining <= 0:
                self.note_transient_fallback("call_video_wait_timeout")
                emit("video_tts_wait_deferred", **self._deferred_facts(deferred))
                raise deferred
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(provider_task), timeout=remaining
                )
            except TimeoutError:
                self.note_transient_fallback("call_video_wait_timeout")
                emit("video_tts_wait_deferred", **self._deferred_facts(deferred))
                raise deferred
        except asyncio.CancelledError:
            deferred.abandon("call_video_interrupted", update_health=False)
            raise
        except VideoTTSDeferred:
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
        self._cancel_unsubmitted_for_turn(turn, "call_video_interrupted")
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
        for job in tuple(self._video_jobs.values()):
            self._cancel_unsubmitted(job, "call_ended")
        for task in tuple(self._provider_tasks):
            if not task.done():
                task.cancel()
        self.connector.closed = True
        self.connector.reference = b""
        self.connector.epoch += 1

    async def end(self):
        tasks = tuple(self._provider_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        await self.connector.end()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        observers = tuple(self._late_observers)
        if observers:
            await asyncio.gather(*observers, return_exceptions=True)


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
