"""Bounded sequential media coordination; PAO alone owns Messages and Runs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4
from .contract import (
    CallError,
    PROTOCOL,
    LEASE_SECONDS,
    MAX_CALL_SECONDS,
    identifier,
    integer,
    validate_body,
    digest,
    decode_wav,
    decode_jpeg,
    speech_segments,
)


@dataclass
class Call:
    owner: str
    binding: dict
    profile: dict
    targets: dict
    fingerprint: str
    expires: float
    started: float
    allow_cloud: bool
    phase: str = "active"
    sequence: int = 0
    turn: dict = field(default_factory=dict)
    task: asyncio.Task | None = None
    speech_task: asyncio.Task | None = None
    speech_index: int = -1
    speech_cache: dict = field(default_factory=dict)
    speech_errors: dict = field(default_factory=dict)
    speech_attempts: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)


class CallService:
    def __init__(
        self, config, ports, adapters, *, clock=time.monotonic, poll_seconds=0.7
    ):
        self.config, self.ports, self.adapters = config, ports, adapters
        self.clock, self.poll_seconds = clock, poll_seconds
        self.generation = uuid4().hex
        self.calls = {}

    def busy(self, owner):
        self.expire()
        return any(
            c.owner == owner and c.phase == "active" for c in self.calls.values()
        )

    def expire(self):
        now = self.clock()
        for key, call in list(self.calls.items()):
            if call.phase == "active" and (
                call.expires <= now or now - call.started >= MAX_CALL_SECONDS
            ):
                self._end(call)
            if call.phase == "ended" and now - call.expires > 60:
                self.calls.pop(key, None)

    def _end(self, call):
        call.phase = "ended"
        call.expires = self.clock()
        # Once PAO admission starts, do not cancel an uncertain accepted request.
        if call.task and call.turn.get("phase") != "submitting":
            call.task.cancel()
        if call.speech_task:
            call.speech_task.cancel()
        call.speech_cache.clear()

    async def close(self):
        tasks = []
        for call in self.calls.values():
            self._end(call)
            tasks += [t for t in (call.task, call.speech_task) if t]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.calls.clear()

    def _view(self, call):
        turn = {
            k: v
            for k, v in call.turn.items()
            if k
            in {
                "turn_id",
                "sequence",
                "phase",
                "text",
                "answer",
                "run_id",
                "message_id",
                "request_id",
                "error",
                "vision_error",
                "captured_at",
                "speech_segments",
                "speech_truncated",
                "latency_ms",
                "run_state",
                "provider_receipts",
            }
        }
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "generation": self.generation,
            "call_id": call.binding["call_id"],
            "binding": call.binding,
            "phase": call.phase,
            "sequence": call.sequence,
            "turn": turn,
            "rows": list(call.rows),
            "duration_seconds": int(self.clock() - call.started),
        }

    def _get(self, owner, body):
        if body.get("generation") != self.generation:
            raise CallError("call_generation_changed", 409)
        call = self.calls.get(identifier(body.get("call_id")))
        if not call or call.owner != owner:
            raise CallError("call_not_found", 410)
        if any(
            body.get(k) != call.binding[k]
            for k in ("client_id", "agent_id", "session_id", "context_generation")
        ):
            raise CallError("call_scope_changed", 409)
        return call

    def _privacy(self, call, kind):
        self.ports.validate(call.owner, call.binding)
        target = call.targets[kind]
        if target and target["location"] == "cloud":
            if not call.allow_cloud:
                raise CallError("call_cloud_consent_required", 403)
            if self.ports.privacy_level(call.binding["agent_id"]) not in (0, 1):
                raise CallError("call_cloud_privacy_unqualified", 403)

    async def invoke(self, owner, body):
        body = validate_body(body)
        op = body["operation"]
        self.expire()
        if op in ("context", "save_profile", "start"):
            self.ports.validate(owner, body)
        if op == "context":
            return {
                "ok": True,
                "protocol": PROTOCOL,
                "generation": self.generation,
                "busy": self.busy(owner) or self.ports.phone_busy(owner),
                "batch_stt": True,
                "native_image": False,
                **self.config.context(owner, body["agent_id"]),
            }
        if op == "save_profile":
            if self.busy(owner):
                raise CallError("call_end_before_configuring", 409)
            return {
                "ok": True,
                **self.config.save(
                    owner, body["agent_id"], body.get("revision"), body.get("profile")
                ),
            }
        if op == "start":
            if body.get("generation") != self.generation:
                raise CallError("call_generation_changed", 409)
            call_id = identifier(body.get("call_id"))
            if type(body.get("allow_cloud")) is not bool:
                raise CallError("call_consent_invalid")
            fingerprint = digest(body)
            existing = self.calls.get(call_id)
            if existing:
                if existing.owner != owner or existing.fingerprint != fingerprint:
                    raise CallError("call_idempotency_conflict", 409)
                return self._view(existing)
            if self.busy(owner) or self.ports.phone_busy(owner):
                raise CallError("call_already_active", 409)
            if len(self.calls) >= 16:
                raise CallError("call_capacity", 429)
            profile, targets = self.config.freeze(
                owner, body["agent_id"], body.get("revision")
            )
            binding = {
                k: body[k]
                for k in (
                    "client_id",
                    "agent_id",
                    "session_id",
                    "context_generation",
                    "call_id",
                )
            }
            call = Call(
                owner,
                binding,
                profile,
                targets,
                fingerprint,
                self.clock() + LEASE_SECONDS,
                self.clock(),
                body["allow_cloud"],
            )
            for kind in ("stt", "tts", "vision"):
                self._privacy(call, kind)
            # No awaits between busy check and reservation (one event-loop owner).
            self.calls[call_id] = call
            return self._view(call)
        call = self._get(owner, body)
        if op == "end":
            self._end(call)
            return self._view(call)
        if call.phase != "active":
            raise CallError("call_ended", 410)
        self.ports.validate(owner, call.binding)
        call.expires = self.clock() + LEASE_SECONDS
        if op == "snapshot":
            return self._view(call)
        if op == "turn":
            sequence = integer(body.get("sequence"), 1, 10000)
            turn_id = identifier(body.get("turn_id"))
            fingerprint = digest(body)
            if sequence == call.sequence:
                if call.turn.get("digest") != fingerprint:
                    raise CallError("call_idempotency_conflict", 409)
                return self._view(call)
            if sequence != call.sequence + 1:
                raise CallError("call_sequence_invalid", 409)
            if call.task and not call.task.done():
                raise CallError("call_turn_busy", 409)
            audio = decode_wav(body.get("audio_b64"))
            image = None
            if body.get("image_b64"):
                if not call.targets["vision"]:
                    raise CallError("call_vision_not_configured", 409)
                image = decode_jpeg(body["image_b64"])
                if (
                    not isinstance(body.get("captured_at"), str)
                    or len(body["captured_at"]) > 40
                ):
                    raise CallError("call_capture_time_invalid")
            if call.speech_task:
                call.speech_task.cancel()
            call.speech_cache.clear()
            call.speech_errors.clear()
            call.speech_attempts.clear()
            call.speech_index = -1
            call.sequence = sequence
            call.turn = {
                "turn_id": turn_id,
                "sequence": sequence,
                "digest": fingerprint,
                "phase": "transcribing",
                "captured_at": body.get("captured_at") if image else None,
            }
            call.task = asyncio.create_task(self._run(call, audio, image))
            return self._view(call)
        if op == "speech":
            index = integer(body.get("segment"), 0, 11)
            if (
                call.turn.get("turn_id") != body.get("turn_id")
                or call.turn.get("phase") != "complete"
            ):
                raise CallError("call_speech_not_ready", 409)
            parts = call.turn.get("speech_segments", [])
            if index >= len(parts):
                raise CallError("call_speech_index_invalid")
            if index in call.speech_cache:
                return {"ok": True, "ready": True, **call.speech_cache[index]}
            if call.speech_task and not call.speech_task.done():
                if index != call.speech_index:
                    raise CallError("call_speech_busy", 409)
                return {"ok": True, "ready": False}
            if index in call.speech_errors:
                if body.get("retry") is not True or call.speech_attempts[index] >= 2:
                    raise CallError(call.speech_errors[index], 502)
                call.speech_errors.pop(index, None)
            elif index <= call.speech_index:
                raise CallError("call_speech_evicted", 409)
            elif index != call.speech_index + 1:
                raise CallError("call_speech_order_invalid", 409)
            self._privacy(call, "tts")
            call.speech_index = index
            call.speech_attempts[index] = call.speech_attempts.get(index, 0) + 1
            call.speech_task = asyncio.create_task(
                self._speak(call, index, parts[index])
            )
            return {"ok": True, "ready": False}
        raise CallError("call_invalid_operation")

    async def _run(self, call, audio, image):
        turn = call.turn
        start = self.clock()
        try:
            self._privacy(call, "stt")
            transcription = await self.adapters.transcribe(
                call.targets["stt"], call.profile["stt"], audio
            )
            text = transcription["text"]
            if transcription.get("provider_receipt"):
                turn.setdefault("provider_receipts", {})["stt"] = transcription[
                    "provider_receipt"
                ]
            audio = b""
            turn["text"] = text
            observation = ""
            if image:
                turn["phase"] = "observing"
                try:
                    self._privacy(call, "vision")
                    observation = await self.adapters.observe(
                        call.targets["vision"], call.profile["vision"], image, text
                    )
                except CallError as exc:
                    turn["vision_error"] = exc.code
                    observation = "No image observation is available for this turn. Do not claim to have seen the image."
                image = None
            if call.phase != "active":
                return
            turn["phase"] = "submitting"
            self.ports.validate(call.owner, call.binding)
            result = await self.ports.admit(
                call.owner,
                call.binding,
                turn["turn_id"],
                text,
                observation,
                turn.get("captured_at"),
            )
            turn.update(result)
            turn["phase"] = "thinking"
            call.rows.append(
                {"speaker": "user", "text": text, "turn_id": turn["turn_id"]}
            )
            call.rows = call.rows[-6:]
            while call.phase == "active":
                self.ports.validate(call.owner, call.binding)
                run = self.ports.result(call.owner, call.binding, turn["run_id"])
                turn["run_state"] = run["state"]
                if run["terminal"]:
                    if run["state"] != "completed":
                        raise CallError("call_agent_" + run["state"], 409)
                    answer = run["text"]
                    if not answer:
                        raise CallError("call_answer_empty", 502)
                    turn["answer"] = answer[:64000]
                    turn["speech_segments"], turn["speech_truncated"] = speech_segments(
                        answer
                    )
                    turn["phase"] = "complete"
                    turn["latency_ms"] = int((self.clock() - start) * 1000)
                    call.rows.append(
                        {
                            "speaker": "assistant",
                            "text": answer[:64000],
                            "turn_id": turn["turn_id"],
                        }
                    )
                    call.rows = call.rows[-6:]
                    return
                if self.clock() - start > 600:
                    raise CallError("call_agent_timeout_continues_in_chat", 504)
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            turn["phase"] = "failed"
            turn["error"] = (
                exc.code if isinstance(exc, CallError) else "call_turn_failed"
            )
        finally:
            audio = b""
            image = None

    async def _speak(self, call, index, text):
        turn = call.turn
        try:
            result = await self.adapters.synthesize(
                call.targets["tts"], call.profile["tts"], text
            )
            if call.phase == "active" and call.turn is turn:
                if result.get("provider_receipt"):
                    turn.setdefault("provider_receipts", {}).setdefault("tts", {})[
                        str(index)
                    ] = result["provider_receipt"]
                call.speech_cache[index] = result
                for old in sorted(call.speech_cache)[:-2]:
                    call.speech_cache.pop(old, None)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if call.turn is turn:
                call.speech_errors[index] = (
                    exc.code if isinstance(exc, CallError) else "call_speech_failed"
                )
