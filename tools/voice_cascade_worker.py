"""Standalone local Voice Cascade Worker for HASHI Phone.

Provides WebRTC audio streaming via aiortc, session authentication,
generation-based barge-in gate closing, and sideband WebSocket events.
"""
from __future__ import annotations

import asyncio
import fractions
import json
import logging
import math
import os
import time
from collections import deque
from typing import Any
from uuid import uuid4

from aiohttp import WSMsgType, web

_LOGGER = logging.getLogger("voice_cascade_worker")

# Check aiortc availability
try:
    import aiortc
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
    from av import AudioFrame, AudioResampler
    AIORTC_AVAILABLE = True
except ImportError:
    aiortc = None
    MediaStreamTrack = object  # type: ignore
    AudioFrame = None  # type: ignore
    AudioResampler = None  # type: ignore
    AIORTC_AVAILABLE = False

MAX_SIDEBAND_BACKLOG = 512
MAX_CONTEXT_UPDATES = 128
SPEECH_RMS_THRESHOLD = 650
SPEECH_START_FRAMES = 2
SPEECH_END_FRAMES = 15


class CascadeAudioStreamTrack(MediaStreamTrack if AIORTC_AVAILABLE else object):
    """Audio track transmitting fixed test audio or synthesized speech chunks."""

    kind = "audio"

    def __init__(self, session: CascadeSession):
        if AIORTC_AVAILABLE:
            super().__init__()
        self.session = session
        self._sample_rate = 48000
        self._samples_per_frame = 960  # 20ms at 48kHz
        self._timestamp = 0
        self._start_time: float | None = None
        self.delivered_chunks: list[int] = []  # Record delivered generation IDs

    async def recv(self) -> Any:
        if not AIORTC_AVAILABLE:
            raise RuntimeError("aiortc unavailable")
        if self.session.closed:
            raise aiortc.mediastreams.MediaStreamError("session_closed")

        # Pace frames at their media timestamp, then inspect the gate. This
        # prevents a queued reply from being drained faster than playback.
        loop = asyncio.get_running_loop()
        if self._start_time is None:
            self._start_time = loop.time()
        await asyncio.sleep(max(0.0, self._start_time + self._timestamp / self._sample_rate - loop.time()))
        pts = self._timestamp
        self._timestamp += self._samples_per_frame

        # Check if gate is closed or output generation cancelled
        chunk_data = b"\x00" * (self._samples_per_frame * 2)  # 20ms s16 mono silence
        if self.session.output_gate_open and self.session.pending_audio_chunks:
            # Drain chunks matching the current generation
            while self.session.pending_audio_chunks:
                gen, data = self.session.pending_audio_chunks.pop(0)
                if gen == self.session.output_generation:
                    chunk_data = data
                    self.delivered_chunks.append(gen)
                    break
                # Older generation chunks are discarded immediately!

        frame = AudioFrame(format="s16", layout="mono", samples=self._samples_per_frame)
        frame.sample_rate = self._sample_rate
        frame.pts = pts
        frame.time_base = fractions.Fraction(1, self._sample_rate)
        frame.planes[0].update(chunk_data)
        return frame


class CascadeSession:
    def __init__(
        self,
        session_id: str,
        sdp_offer: str,
        instructions: str,
        model: str,
        voice: str,
        input_messages: list[dict[str, Any]] | None = None,
    ):
        self.session_id = session_id
        self.sdp_offer = sdp_offer
        self.instructions = instructions
        self.model = model
        self.voice = voice
        self.input_messages = list(input_messages or [])
        self.context_updates: list[dict[str, str]] = []
        self.created_at = time.time()
        self.call_epoch = 1
        self.turn_id = 0
        self.output_generation = 0
        self.output_gate_open = True
        self.user_speaking = False
        self.deferred_speech: deque[str] = deque()
        self.sequence = 0
        self.muted = False
        self.closed = False
        self.ws: web.WebSocketResponse | None = None
        self._sideband_lock = asyncio.Lock()
        self._sideband_backlog: deque[dict[str, Any]] = deque()
        self.sideband_overflow = False
        self.peer_connection: Any = None
        self.data_channel: Any = None
        self.output_track: CascadeAudioStreamTrack | None = None
        self.incoming_track_task: asyncio.Task[Any] | None = None
        # Pending chunks stored as tuples: (generation, pcm_bytes)
        self.pending_audio_chunks: list[tuple[int, bytes]] = []

    def next_seq(self) -> int:
        self.sequence += 1
        return self.sequence

    async def emit_sideband(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        payload = {
            "type": event_type,
            "event_id": f"evt-{uuid4().hex[:12]}",
            "session_id": self.session_id,
            "output_generation": self.output_generation,
            **(data or {}),
        }
        async with self._sideband_lock:
            if self.ws is not None and not self.ws.closed:
                try:
                    payload["seq"] = self.next_seq()
                    await self.ws.send_json(payload)
                    return
                except Exception as exc:
                    _LOGGER.warning("Failed to emit sideband event %s: %s", event_type, exc)
                    payload.pop("seq", None)
            if len(self._sideband_backlog) >= MAX_SIDEBAND_BACKLOG:
                self.sideband_overflow = True
                self.output_gate_open = False
                return
            self._sideband_backlog.append(payload)

    async def attach_sideband(self, ws: web.WebSocketResponse) -> None:
        async with self._sideband_lock:
            self.ws = ws
            started = {
                "type": "session.started",
                "event_id": f"evt-{uuid4().hex[:12]}",
                "seq": self.next_seq(),
                "session_id": self.session_id,
                "output_generation": self.output_generation,
                "session": {"id": self.session_id, "model": self.model, "voice": self.voice},
            }
            await ws.send_json(started)
            while self._sideband_backlog:
                event = self._sideband_backlog[0]
                event["seq"] = self.next_seq()
                try:
                    await ws.send_json(event)
                except Exception:
                    event.pop("seq", None)
                    break
                self._sideband_backlog.popleft()

    async def emit_datachannel(self, message: dict[str, Any]) -> None:
        if self.data_channel and hasattr(self.data_channel, "send"):
            try:
                self.data_channel.send(json.dumps(message))
            except Exception as exc:
                _LOGGER.warning("Failed to emit datachannel message: %s", exc)

    async def on_user_speech_started(self) -> int:
        """Triggered when incoming user audio/speech starts (barge-in).

        Increments generation, closes the output gate, purges all pending audio chunks,
        and dispatches gate-close events across sideband and WebRTC DataChannel.
        """
        if self.closed or self.user_speaking:
            return self.output_generation
        self.user_speaking = True
        self.output_generation += 1
        self.output_gate_open = False
        gen = self.output_generation
        _LOGGER.info("User speech started: session %s barge-in to generation %d", self.session_id, gen)

        # 1. Purge all pending audio chunks of previous generations
        self.pending_audio_chunks.clear()

        # 2. Emit output.gate.closed on browser DataChannel
        await self.emit_datachannel({
            "type": "output.gate.closed",
            "generation": gen,
            "timestamp": time.time(),
        })

        # 3. Emit session.output_gate.closed on HASHI sideband
        await self.emit_sideband("session.output_gate.closed", {
            "event_id": f"evt_gate_{uuid4().hex[:12]}",
            "generation": gen,
            "reason": "user_speech_interrupted",
        })
        return gen

    async def on_user_speech_ended(self) -> None:
        if not self.user_speaking:
            return
        self.user_speaking = False
        while self.deferred_speech and not self.user_speaking and not self.closed:
            await self.speak_text(self.deferred_speech.popleft())

    async def speak_text(self, text: str) -> None:
        """Enqueue assistant speech text and audio chunks under the current generation."""
        if self.closed:
            return
        if self.user_speaking:
            self.deferred_speech.append(text)
            return
        current_gen = self.output_generation
        self.turn_id += 1
        await self.emit_datachannel({"type": "output.gate.opened", "generation": current_gen})
        if self.user_speaking or current_gen != self.output_generation:
            self.deferred_speech.appendleft(text)
            return
        self.output_gate_open = True

        now_ms = int((time.time() - self.created_at) * 1000)
        duration_ms = max(50, int(len(text) * 60))
        start_ms = max(0, now_ms - duration_ms)
        end_ms = now_ms

        # Emit output transcript delta with complete timestamp and event_id
        await self.emit_sideband("session.output_transcript.delta", {
            "event_id": f"evt_out_{uuid4().hex[:12]}",
            "delta": text,
            "turn_id": self.turn_id,
            "generation": current_gen,
            "start_ms": start_ms,
            "end_ms": end_ms,
        })
        if self.user_speaking or current_gen != self.output_generation:
            return

        # Queue test audio chunks (20ms frames of test audio tagged with generation)
        test_frame_bytes = b"\x01\x00" * 960  # Non-zero audio pattern
        for _ in range(3):
            self.pending_audio_chunks.append((current_gen, test_frame_bytes))

    async def simulate_user_input(self, text: str) -> None:
        """Simulate user voice transcription (STT final) with complete timestamps."""
        if self.closed:
            return
        now_ms = int((time.time() - self.created_at) * 1000)
        duration_ms = max(50, int(len(text) * 60))
        start_ms = max(0, now_ms - duration_ms)
        end_ms = now_ms

        await self.emit_sideband("session.input_transcript.delta", {
            "event_id": f"evt_in_{uuid4().hex[:12]}",
            "delta": text,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "final": True,
        })

    def handle_incoming_track(self, track: Any) -> None:
        """Use sustained audio energy to detect speech starts and ends."""
        async def _read_track() -> None:
            resampler = AudioResampler(format="s16", layout="mono", rate=16000)
            active_frames = 0
            quiet_frames = 0
            try:
                while not self.closed:
                    frame = await track.recv()
                    if self.muted:
                        active_frames = 0
                        quiet_frames = 0
                        continue
                    for mono in resampler.resample(frame):
                        pcm = bytes(mono.planes[0])[: mono.samples * 2]
                        samples = memoryview(pcm).cast("h") if pcm else ()
                        rms = math.isqrt(sum(sample * sample for sample in samples) // len(samples)) if samples else 0
                        if rms >= SPEECH_RMS_THRESHOLD:
                            active_frames += 1
                            quiet_frames = 0
                            if active_frames >= SPEECH_START_FRAMES and not self.user_speaking:
                                await self.on_user_speech_started()
                        else:
                            active_frames = 0
                            if self.user_speaking:
                                quiet_frames += 1
                                if quiet_frames >= SPEECH_END_FRAMES:
                                    quiet_frames = 0
                                    await self.on_user_speech_ended()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("Incoming audio track ended: %s", type(exc).__name__)

        self.incoming_track_task = asyncio.create_task(_read_track())

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.emit_sideband("session.closed", {"reason": "session_terminated"})
        if self.incoming_track_task and not self.incoming_track_task.done():
            self.incoming_track_task.cancel()
        if self.ws and not self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self.peer_connection:
            try:
                await self.peer_connection.close()
            except Exception:
                pass


class VoiceCascadeWorker:
    def __init__(
        self,
        token: str,
        host: str = "127.0.0.1",
        port: int = 8775,
    ):
        if not isinstance(token, str) or not token.strip():
            raise ValueError("CASCADE_WORKER_TOKEN must be configured")
        self.token = token.strip()
        self.host = host
        self.port = port
        self.sessions: dict[str, CascadeSession] = {}
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_post("/v1/sessions", self.handle_create_session)
        self.app.router.add_get("/v1/sessions/{session_id}/attach", self.handle_attach)
        self.app.router.add_post("/v1/sessions/{session_id}/interrupt", self.handle_interrupt)
        self.app.router.add_post("/v1/sessions/{session_id}/speech_started", self.handle_speech_started)
        self.app.router.add_post("/v1/sessions/{session_id}/speak", self.handle_speak)
        self.app.router.add_post("/v1/sessions/{session_id}/simulate_speech", self.handle_simulate_speech)
        self.app.router.add_delete("/v1/sessions/{session_id}", self.handle_delete_session)

    def _verify_auth(self, request: web.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and auth[7:].strip() == self.token

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "service": "voice-cascade-worker",
            "version": "1.1.0",
            "aiortc_available": AIORTC_AVAILABLE,
            "active_sessions": len(self.sessions),
        })

    async def handle_create_session(self, request: web.Request) -> web.Response:
        if not self._verify_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid_json"}, status=400)

        sdp_offer = body.get("sdp", "")
        if not sdp_offer or not sdp_offer.startswith("v=0"):
            return web.json_response({"error": "invalid_sdp"}, status=400)

        # Explicit failure when aiortc is not available: no synthetic SDP answer fallback!
        if not AIORTC_AVAILABLE:
            return web.json_response(
                {
                    "error": "aiortc_unavailable",
                    "detail": "aiortc is required for real WebRTC media connection",
                },
                status=503,
            )

        input_messages = body.get("input_messages", [])
        if not isinstance(input_messages, list) or len(input_messages) > 128:
            return web.json_response({"error": "invalid_input_messages"}, status=400)

        session_id = f"cascade-{uuid4().hex}"
        session = CascadeSession(
            session_id=session_id,
            sdp_offer=sdp_offer,
            instructions=body.get("instructions", ""),
            model=body.get("model", "cascade-v1"),
            voice=body.get("voice", "default"),
            input_messages=input_messages,
        )

        try:
            pc = RTCPeerConnection()
            session.peer_connection = pc

            # Bidirectional media tracks
            output_track = CascadeAudioStreamTrack(session)
            session.output_track = output_track
            pc.addTrack(output_track)

            @pc.on("datachannel")
            def on_datachannel(channel: Any) -> None:
                session.data_channel = channel

                @channel.on("open")
                def on_open() -> None:
                    asyncio.create_task(session.emit_datachannel({
                        "type": "session.started",
                        "session": {"id": session.session_id, "model": session.model, "voice": session.voice},
                    }))

                # aiortc may deliver the server-side channel after it has
                # already entered the open state, so the callback alone is
                # insufficient for the browser's start handshake.
                if channel.readyState == "open":
                    on_open()

            @pc.on("track")
            def on_track(track: Any) -> None:
                if track.kind == "audio":
                    session.handle_incoming_track(track)

            offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
            await pc.setRemoteDescription(offer)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            answer_sdp = pc.localDescription.sdp
        except Exception as exc:
            _LOGGER.error("SDP negotiation failed: %s", exc)
            if session.peer_connection is not None:
                await session.peer_connection.close()
            return web.json_response(
                {"error": "sdp_negotiation_failed", "detail": str(exc)},
                status=400,
            )

        self.sessions[session_id] = session
        return web.json_response({
            "provider_session_id": session_id,
            "sdp_answer": answer_sdp,
        })

    async def handle_attach(self, request: web.Request) -> web.WebSocketResponse:
        if not self._verify_auth(request):
            return web.Response(status=401, text="unauthorized")

        session_id = request.match_info["session_id"]
        _LOGGER.info("Worker handle_attach entered for session_id=%s", session_id)
        session = self.sessions.get(session_id)
        if not session:
            return web.Response(status=404, text="session_not_found")
        if session.closed or session.sideband_overflow:
            return web.Response(status=410, text="session_replay_unavailable")

        ws = web.WebSocketResponse(heartbeat=20.0)
        await ws.prepare(request)

        # Sideband reconnect: replace old websocket without destroying the session
        if session.ws and not session.ws.closed:
            try:
                await session.ws.close(code=1000, message=b"replaced_by_new_attach")
            except Exception:
                pass
        await session.attach_sideband(ws)

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        kind = data.get("type", "")
                        event_id = data.get("event_id")

                        if kind == "session.close":
                            await session.close()
                            self.sessions.pop(session_id, None)
                            break
                        elif kind == "session.input_audio.mute":
                            session.muted = True
                            await session.on_user_speech_ended()
                            await session.emit_sideband(
                                "session.input_audio.muted", {"client_event_id": event_id}
                            )
                        elif kind == "session.input_audio.unmute":
                            session.muted = False
                            await session.emit_sideband(
                                "session.input_audio.unmuted", {"client_event_id": event_id}
                            )
                        elif kind in {
                            "session.instructions.append",
                            "session.commentary.append",
                            "session.thinking.append",
                        }:
                            content = data.get("content", "")
                            if not isinstance(content, str) or not content:
                                await session.emit_sideband("error", {
                                    "client_event_id": event_id,
                                    "error": {"code": "invalid_context_update"},
                                })
                                continue
                            if len(session.context_updates) >= MAX_CONTEXT_UPDATES:
                                await session.emit_sideband("error", {
                                    "client_event_id": event_id,
                                    "error": {"code": "context_update_limit"},
                                })
                                continue
                            # All three update types are context for the
                            # future local reasoning engine. Speaking their
                            # raw content would expose instructions and
                            # background task data to the caller.
                            session.context_updates.append({"kind": kind, "content": content})
                            evt_type = kind.replace(".append", ".appended")
                            await session.emit_sideband(
                                evt_type,
                                {
                                    "client_event_id": event_id,
                                },
                            )
                    except Exception as exc:
                        _LOGGER.warning("Error processing websocket message: %s", exc)
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            # On WS disconnect, decouple ws reference, but PRESERVE the session for reconnect!
            if session.ws is ws:
                session.ws = None

        return ws

    async def handle_speech_started(self, request: web.Request) -> web.Response:
        """Endpoint to signal user speech started / barge-in event."""
        if not self._verify_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        session_id = request.match_info["session_id"]
        session = self.sessions.get(session_id)
        if not session:
            return web.json_response({"error": "not_found"}, status=404)
        new_gen = await session.on_user_speech_started()
        return web.json_response({"ok": True, "output_generation": new_gen, "gate_closed": True})

    async def handle_interrupt(self, request: web.Request) -> web.Response:
        if not self._verify_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        session_id = request.match_info["session_id"]
        session = self.sessions.get(session_id)
        if not session:
            return web.json_response({"error": "not_found"}, status=404)
        new_gen = await session.on_user_speech_started()
        return web.json_response({"ok": True, "output_generation": new_gen})

    async def handle_speak(self, request: web.Request) -> web.Response:
        if not self._verify_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        session_id = request.match_info["session_id"]
        session = self.sessions.get(session_id)
        if not session:
            return web.json_response({"error": "not_found"}, status=404)
        body = await request.json()
        text = body.get("text", "")
        await session.speak_text(text)
        return web.json_response({
            "ok": True,
            "turn_id": session.turn_id,
            "generation": session.output_generation,
        })

    async def handle_simulate_speech(self, request: web.Request) -> web.Response:
        if not self._verify_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        session_id = request.match_info["session_id"]
        session = self.sessions.get(session_id)
        if not session:
            return web.json_response({"error": "not_found"}, status=404)
        body = await request.json()
        text = body.get("text", "")
        await session.simulate_user_input(text)
        return web.json_response({"ok": True, "simulated": text})

    async def handle_delete_session(self, request: web.Request) -> web.Response:
        if not self._verify_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        session_id = request.match_info["session_id"]
        session = self.sessions.pop(session_id, None)
        if session:
            await session.close()
        return web.json_response({"ok": True})


def create_worker_app(token: str) -> web.Application:
    worker = VoiceCascadeWorker(token=token)
    return worker.app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    host = os.environ.get("CASCADE_WORKER_HOST", "127.0.0.1")
    port = int(os.environ.get("CASCADE_WORKER_PORT", "8775"))
    token = os.environ.get("CASCADE_WORKER_TOKEN", "")
    worker = VoiceCascadeWorker(token=token, host=host, port=port)
    print(f"Starting Voice Cascade Worker on http://{host}:{port}...", flush=True)
    web.run_app(worker.app, host=host, port=port)
