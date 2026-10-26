"""AgentPresence's scoped call-output port (ap-call-video/1).

Only this connector knows AP's HTTP paths. Its caller owns synthesis and Call
state; this port owns the negotiated display context and playback receipts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from urllib.parse import urlencode, urlsplit

import aiohttp

from orchestrator.frontend_call.contract import CallError

OUTPUT_PROTOCOL = "call-output/1"
AP_PROTOCOL = "ap-call-video/1"
MAX_JSON = 1024 * 1024
MAX_IMAGE = 4 * 1024 * 1024
MAX_MEDIA = 64 * 1024 * 1024


class AgentPresenceCallVideo:
    audio_owner = "agentpresence"

    def __init__(
        self,
        base_url,
        token,
        instance_id,
        call_id,
        *,
        session_factory=aiohttp.ClientSession,
        poll_seconds=0.15,
    ):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise CallError("call_video_connector_configuration_invalid", 503)
        self.base_url, self.token = base_url.rstrip("/"), token
        self.instance_id, self.call_id = instance_id, call_id
        self.session_factory, self.poll_seconds = session_factory, poll_seconds
        self.context_id = ""
        self.generation = 0
        self.reference = b""
        self.state = {}
        self.closed = False
        self.epoch = 0
        self.lock = asyncio.Lock()
        self.interrupted = set()
        self.error_code = None

    async def request(
        self,
        method,
        path,
        *,
        payload=None,
        data=None,
        mime=None,
        maximum=MAX_JSON,
        binary=False,
        timeout=10,
    ):
        if not path.startswith("/v1/") or path.startswith("//") or "\\" in path:
            raise CallError("call_video_connector_response_invalid", 502)
        headers = {
            "X-AP-Token": self.token,
            "Accept": "image/png" if binary else "application/json",
        }
        if mime:
            headers["Content-Type"] = mime
        try:
            async with self.session_factory(
                timeout=aiohttp.ClientTimeout(total=timeout), trust_env=False
            ) as session:
                async with session.request(
                    method,
                    self.base_url + path,
                    json=payload,
                    data=data,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status not in {200, 202}:
                        raise CallError("call_video_connector_rejected", 502)
                    expected = "image/png" if binary else "application/json"
                    if (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .casefold()
                        != expected
                    ):
                        raise CallError("call_video_connector_response_invalid", 502)
                    chunks, total = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > maximum:
                            raise CallError(
                                "call_video_connector_response_invalid", 502
                            )
                        chunks.append(chunk)
            body = b"".join(chunks)
            if binary:
                return body
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError("object required")
            return result
        except asyncio.CancelledError:
            raise
        except CallError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise CallError("call_video_output_lost", 502) from exc
        except (ValueError, UnicodeError) as exc:
            raise CallError("call_video_connector_response_invalid", 502) from exc

    async def probe(self):
        """No side effects. Unconnected/old AP preserves the legacy Call path."""
        try:
            caps = await self.request("GET", "/v1/capabilities", timeout=2)
            binding = await self.request("GET", "/v1/hashi", timeout=2)
        except CallError:
            return False
        contract = caps.get("call_video_contract") or {}
        role_video_tts = contract.get("video_tts_supported", True)
        return bool(
            caps.get("features", {}).get("call_video") is True
            and contract.get("version") == AP_PROTOCOL
            and contract.get("requires_commit") is True
            and role_video_tts is True
            and binding.get("configured") is True
            and binding.get("instance_id") == self.instance_id
        )

    def scope(self):
        return {"call_id": self.call_id, "context_id": self.context_id}

    def remember(self, state):
        if (
            state.get("call_id") != self.call_id
            or state.get("context_id") != self.context_id
            or state.get("instance_id") != self.instance_id
            or state.get("audio_owner") != self.audio_owner
            or type(state.get("generation")) is not int
        ):
            raise CallError("call_video_connector_scope_changed", 409)
        # An older in-flight GET must not roll back an interrupt's generation.
        if state["generation"] < self.generation:
            return self.state
        self.state = state
        self.generation = state["generation"]
        return state

    async def start(self):
        payload = {"call_id": self.call_id, "instance_id": self.instance_id}
        try:
            state = await self.request("POST", "/v1/call/video/start", payload=payload)
        except (CallError, asyncio.CancelledError):
            # Start is idempotent for this exact ID. Reconcile an uncertain
            # response solely to close its context, never to start another call.
            try:
                recovered = await asyncio.shield(
                    self.request(
                        "POST", "/v1/call/video/start", payload=payload, timeout=3
                    )
                )
                context = recovered.get("context_id")
                if isinstance(context, str) and len(context) == 32:
                    self.context_id = context
                    self.remember(recovered)
                    await asyncio.shield(self.end())
            except Exception:
                self.error_code = "call_video_start_unconfirmed"
            raise
        context = state.get("context_id")
        if (
            not isinstance(context, str)
            or len(context) != 32
            or any(c not in "0123456789abcdef" for c in context)
        ):
            raise CallError("call_video_connector_response_invalid", 502)
        self.context_id = context
        self.remember(state)
        try:
            if state.get("call_active") is not True:
                raise CallError("call_video_output_ended", 410)
            reference = state.get("reference") or {}
            # Construct the endpoint ourselves; a response cannot redirect token/image transport.
            data = await self.request(
                "GET",
                "/v1/call/video/reference?" + urlencode(self.scope()),
                maximum=MAX_IMAGE,
                binary=True,
            )
            if (
                reference.get("mime") != "image/png"
                or not data.startswith(b"\x89PNG\r\n\x1a\n")
                or hashlib.sha256(data).hexdigest() != reference.get("sha256")
            ):
                raise CallError("call_video_reference_invalid", 502)
            self.reference = data
        except BaseException:
            await asyncio.shield(self.end())
            raise
        return self

    async def refresh(self):
        state = self.remember(
            await self.request(
                "GET", "/v1/call/video/status?" + urlencode(self.scope())
            )
        )
        if state.get("call_active") is not True:
            self.closed = True
        return state

    def view(self):
        raw = self.state.get("playback")
        playback = None
        if isinstance(raw, dict):
            playback = {
                k: raw[k]
                for k in (
                    "event",
                    "stage",
                    "utterance_id",
                    "generation",
                    "reason",
                    "media_time",
                    "video_width",
                    "video_height",
                    "audio_paused",
                    "audio_muted",
                )
                if k in raw
            }
            audio = raw.get("audio")
            if isinstance(audio, dict):
                playback["audio"] = {
                    k: audio[k] for k in ("peak", "rms", "ctx") if k in audio
                }
        return {
            "protocol": OUTPUT_PROTOCOL,
            "mode": "external_av",
            "audio_owner": self.audio_owner,
            "stage": self.state.get("stage", "connecting"),
            "phase": self.state.get("phase"),
            "active": not self.closed,
            "playback": playback,
            "error_code": self.error_code,
        }

    def require_active(self, epoch=None):
        if self.closed:
            raise CallError("call_video_output_ended", 410)
        if epoch is not None and epoch != self.epoch:
            raise CallError("call_video_interrupted", 409)

    async def prepare(self, utterance_id, media, *, epoch):
        self.require_active(epoch)
        if not isinstance(media, bytes) or not 0 < len(media) <= MAX_MEDIA:
            raise CallError("call_video_av_required", 502)
        # Only one pending/playing utterance is owned by AP. Wait without
        # holding the control lock so interruption/end always has priority.
        deadline = asyncio.get_running_loop().time() + 105
        submitted = False
        while True:
            self.require_active(epoch)
            state = await self.refresh()
            self.require_active(epoch)
            replies = state.get("replies") or []
            own = next(
                (r for r in replies if r.get("utterance_id") == utterance_id), None
            )
            if own:
                status = own.get("state")
                if status in {"prepared", "committed", "playing", "completed"}:
                    return dict(own)
                if status in {"failed", "interrupted", "cancelled"}:
                    raise CallError("call_video_playback_failed", 502)
            elif not submitted and not any(
                r.get("state") in {"accepted", "prepared", "committed", "playing"}
                for r in replies
            ):
                async with self.lock:
                    self.require_active(epoch)
                    await self.request(
                        "POST",
                        "/v1/call/video/media?"
                        + urlencode(
                            {
                                **self.scope(),
                                "utterance_id": utterance_id,
                                "generation": self.generation,
                            }
                        ),
                        data=media,
                        mime="video/mp4",
                    )
                    submitted = True
            if asyncio.get_running_loop().time() > deadline:
                raise CallError("call_video_playback_timeout", 502)
            await asyncio.sleep(self.poll_seconds)

    async def commit(self, utterance_id, *, epoch):
        async with self.lock:
            self.require_active(epoch)
            return await self.request(
                "POST",
                "/v1/call/video/commit",
                payload={
                    **self.scope(),
                    "utterance_id": utterance_id,
                    "generation": self.generation,
                },
            )

    def fence(self, key):
        """Synchronous local fence before cancelling generation or awaiting I/O."""
        if key not in self.interrupted:
            self.interrupted.add(key)
            self.epoch += 1
            return True
        return False

    async def interrupt(self):
        async with self.lock:
            if self.closed:
                return
            state = await self.request(
                "POST",
                "/v1/call/video/interrupt",
                payload={**self.scope(), "generation": self.generation},
            )
            self.remember(state)

    async def end(self):
        self.closed = True
        self.reference = b""
        if not self.context_id:
            return
        async with self.lock:
            try:
                state = await self.request(
                    "POST", "/v1/call/video/end", payload=self.scope()
                )
                self.remember(state)
            except CallError as exc:
                self.error_code = exc.code
