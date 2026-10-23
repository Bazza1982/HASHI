"""Call-owned VideoTTS consumer of the PAO Compute Service Directory.

Both protocols are vendor-neutral: a synchronous H.264/AAC MP4 response, or a
JSON async job returning the same complete MP4. AP owns the reference and final
AV validation. No visual library, fallback provider or audio synthesis lives here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import struct
import time
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit

import aiohttp

from orchestrator.compute_services import (
    ComputeReference,
    ComputeRequirements,
    ComputeServiceClient,
    ComputeServiceDirectory,
    ComputeServiceError,
    ComputeServicesConfig,
)
from .contract import CallError
from .diagnostics import emit

ASYNC_PROTOCOL = "video.speech.async.v1"
SYNC_PROTOCOL = "video.speech.mp4.v1"
CAPABILITIES_PROTOCOL = "video.speech.capabilities.v1"
MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_REFERENCE_BYTES = 4 * 1024 * 1024
_JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")


@dataclass(frozen=True)
class VideoTTSSelection:
    reference: ComputeReference
    protocol: str
    voice: str
    speed: float
    max_input_chars: int
    job_path: str
    cancel_path: str | None
    request_timeout_seconds: float
    job_timeout_seconds: float
    voice_source: str = "configured"

    @classmethod
    def parse(cls, raw):
        if raw is None or raw == {"enabled": False}:
            return None
        if not isinstance(raw, dict):
            raise CallError("call_video_configuration_invalid", 503)
        if raw.get("enabled") is False:
            return None
        allowed = {
            "enabled",
            "reference",
            "protocol",
            "voice",
            "voice_source",
            "speed",
            "max_input_chars",
            "job_path",
            "cancel_path",
            "request_timeout_seconds",
            "job_timeout_seconds",
        }
        if set(raw) - allowed or raw.get("enabled") is not True:
            raise CallError("call_video_configuration_invalid", 503)
        ref = raw.get("reference")
        if not isinstance(ref, dict) or set(ref) - {
            "node_id",
            "service_id",
            "model_id",
        }:
            raise CallError("call_video_configuration_invalid", 503)
        try:
            reference = ComputeReference(**ref)
        except (TypeError, ValueError, ComputeServiceError) as exc:
            raise CallError("call_video_configuration_invalid", 503) from exc
        protocol = raw.get("protocol", ASYNC_PROTOCOL)
        voice = raw.get("voice")
        if protocol not in {ASYNC_PROTOCOL, SYNC_PROTOCOL} or not isinstance(
            voice, str
        ):
            raise CallError("call_video_configuration_invalid", 503)
        if not voice or len(voice) > 128 or any(ord(c) < 32 for c in voice):
            raise CallError("call_video_configuration_invalid", 503)
        voice_source = raw.get("voice_source", "configured")
        if not isinstance(voice_source, str) or voice_source not in {"configured", "call_tts"}:
            raise CallError("call_video_configuration_invalid", 503)
        speed = raw.get("speed", 1.0)
        maximum = raw.get("max_input_chars", 400)
        request_timeout = raw.get("request_timeout_seconds", 120)
        job_timeout = raw.get("job_timeout_seconds", 900)
        for value, low, high in (
            (speed, 0.5, 2),
            (request_timeout, 1, 900),
            (job_timeout, 1, 1800),
        ):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not low <= value <= high
            ):
                raise CallError("call_video_configuration_invalid", 503)
        if type(maximum) is not int or not 1 <= maximum <= 4000:
            raise CallError("call_video_configuration_invalid", 503)
        job_path = raw.get("job_path", "/v1/video/jobs/{job_id}")
        cancel_path = raw.get("cancel_path")
        for path in (job_path, cancel_path):
            if path is None:
                continue
            if not isinstance(path, str) or path.count("{job_id}") != 1:
                raise CallError("call_video_configuration_invalid", 503)
            parsed = urlsplit(path)
            if (
                not path.startswith("/")
                or path.startswith("//")
                or parsed.netloc
                or parsed.scheme
                or parsed.query
                or parsed.fragment
                or "\\" in path
                or any(ord(c) < 32 for c in path)
                or any(p in {".", ".."} for p in path.split("/"))
                or "{" in path.replace("{job_id}", "")
                or "}" in path.replace("{job_id}", "")
            ):
                raise CallError("call_video_configuration_invalid", 503)
        return cls(
            reference,
            protocol,
            voice,
            float(speed),
            maximum,
            job_path,
            cancel_path,
            float(request_timeout),
            float(job_timeout),
            voice_source,
        )

    def public(self):
        return {
            "node_id": self.reference.node_id,
            "service_id": self.reference.service_id,
            "model_id": self.reference.model_id,
            "protocol": self.protocol,
            "voice": self.voice,
            **({"voice_source": self.voice_source} if self.voice_source != "configured" else {}),
        }


def same_origin_url(node, value):
    """A task result cannot move the selected node's trust boundary."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2000
        or "\\" in value
        or any(ord(c) < 33 for c in value)
    ):
        raise CallError("call_video_result_url_invalid", 502)
    origin = urlsplit(node.base_url)
    result = urlsplit(urljoin(node.base_url + "/", value))
    if (
        (result.scheme, result.hostname, result.port)
        != (origin.scheme, origin.hostname, origin.port)
        or result.username
        or result.password
        or result.fragment
    ):
        raise CallError("call_video_result_url_invalid", 502)
    return result.geturl()


class VideoSpeechClient(ComputeServiceClient):
    """Bounded media/job transport retaining the directory's credential owner."""

    async def request(
        self,
        node,
        method,
        url,
        *,
        payload=None,
        timeout=None,
        maximum=1024 * 1024,
        media=False,
        request_id=None,
    ):
        url = same_origin_url(node, url)
        headers = self._headers(node)
        headers["Accept"] = "video/mp4" if media else "application/json"
        if request_id:
            headers["Idempotency-Key"] = hashlib.sha256(request_id.encode()).hexdigest()
        try:
            async with self.session_factory(
                timeout=aiohttp.ClientTimeout(total=timeout or node.timeout_seconds),
                trust_env=False,
            ) as session:
                async with session.request(
                    method, url, headers=headers, json=payload, allow_redirects=False
                ) as response:
                    if response.status not in {200, 201, 202}:
                        raise CallError("call_video_service_rejected", 502)
                    mime = (
                        response.headers.get("Content-Type", "")
                        .split(";", 1)[0]
                        .casefold()
                    )
                    expected = (
                        {"video/mp4", "application/octet-stream"}
                        if media
                        else {"application/json", "application/problem+json"}
                    )
                    if mime not in expected:
                        raise CallError("call_video_response_invalid", 502)
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > maximum:
                            raise CallError("call_video_response_too_large", 502)
                        chunks.append(chunk)
            data = b"".join(chunks)
            if media:
                if not data or b"ftyp" not in data[:64]:
                    raise CallError("call_video_av_required", 502)
                return data
            result = json.loads(data.decode("utf-8-sig"))
            if not isinstance(result, dict):
                raise ValueError("object required")
            return result
        except asyncio.CancelledError:
            raise
        except CallError:
            raise
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            # A POST timeout is an uncertain effect, never a reason to resubmit.
            code = (
                "call_video_outcome_unknown"
                if method == "POST"
                else "call_video_service_unavailable"
            )
            raise CallError(code, 502) from exc
        except (ValueError, UnicodeError) as exc:
            raise CallError("call_video_response_invalid", 502) from exc


class CallVideoTTS:
    def __init__(self, config_path, *, client=None, directory=None, poll_seconds=0.5):
        self.client = client or VideoSpeechClient()
        self.directory = directory or ComputeServiceDirectory(
            ComputeServicesConfig(config_path), client=self.client
        )
        self.poll_seconds = poll_seconds

    async def resolve(self, selection, privacy_level):
        locations = (
            {"local", "lan", "internet"}
            if privacy_level in (0, 1)
            else {"local", "lan"}
        )
        try:
            refreshed = await self.directory.refresh()
            resolved = refreshed.catalogue.resolve(
                selection.reference,
                ComputeRequirements(
                    input_modalities={"text", "image"},
                    output_modalities={"audio", "video"},
                    protocols={selection.protocol},
                    locations=locations,
                ),
            )
            if resolved.offering.interface(selection.protocol).method != "POST":
                raise CallError("call_video_interface_invalid", 503)
            return resolved
        except ComputeServiceError as exc:
            raise CallError("call_video_" + exc.code, 503) from exc

    async def synthesize(
        self,
        selection,
        reference,
        text,
        *,
        privacy_level,
        request_id,
        expected_origin=None,
        on_job=None,
    ):
        resolved = await self.resolve(selection, privacy_level)
        node, offering = resolved.snapshot.node, resolved.offering
        if expected_origin is not None and node.base_url != expected_origin:
            raise CallError("call_video_configuration_changed", 409)
        if (
            not isinstance(reference, bytes)
            or not reference.startswith(b"\x89PNG\r\n\x1a\n")
            or len(reference) < 24
        ):
            raise CallError("call_video_reference_invalid", 502)
        width, height = struct.unpack(">II", reference[16:24])
        image = offering.image
        if (
            not width
            or not height
            or width * height > 8_000_000
            or len(reference)
            > min(
                MAX_REFERENCE_BYTES,
                image.max_bytes or MAX_REFERENCE_BYTES,
                image.max_total_bytes or MAX_REFERENCE_BYTES,
            )
            or "png" not in image.formats
            or (image.max_count or 0) < 1
        ):
            raise CallError("call_video_reference_unsupported", 409)
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > selection.max_input_chars
        ):
            raise CallError("call_video_text_limit", 409)
        payload = {
            "model": offering.model_id,
            "input": text,
            "voice": selection.voice,
            "speed": selection.speed,
            "image": "data:image/png;base64,"
            + base64.b64encode(reference).decode("ascii"),
        }
        job_id = None
        started = time.monotonic()
        emit("video_tts_submitting", **selection.public(), text_chars=len(text))
        try:
            result = await self.client.request(
                node,
                "POST",
                resolved.interface_url(selection.protocol),
                payload=payload,
                timeout=selection.request_timeout_seconds,
                request_id=request_id,
                media=selection.protocol == SYNC_PROTOCOL,
                maximum=MAX_VIDEO_BYTES
                if selection.protocol == SYNC_PROTOCOL
                else 1024 * 1024,
            )
            if selection.protocol == SYNC_PROTOCOL:
                return {"media": result, "provider_receipt": selection.public()}
            job_id = result.get("id") or result.get("job_id")
            if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
                raise CallError("call_video_job_invalid", 502)
            # This content-free receipt precedes polling and survives provider errors.
            receipt = {**selection.public(), "job_id": job_id}
            emit(
                "video_tts_accepted",
                provider_request_id=job_id,
                requested_model=offering.model_id,
            )
            if on_job is not None:
                on_job({**receipt, "status": "accepted"})
            deadline = started + selection.job_timeout_seconds
            while True:
                status = result.get("status")
                if status == "completed":
                    data = await self.client.request(
                        node,
                        "GET",
                        same_origin_url(node, result.get("video_url")),
                        media=True,
                        maximum=MAX_VIDEO_BYTES,
                        timeout=30,
                    )
                    if on_job is not None:
                        on_job({**receipt, "status": "completed"})
                    return {"media": data, "provider_receipt": receipt}
                if status in {"failed", "cancelled"}:
                    error = result.get("error") or {}
                    code = error.get("code") if isinstance(error, dict) else None
                    safe_code = (
                        code
                        if isinstance(code, str)
                        and re.fullmatch(r"[a-z0-9_]{1,80}", code)
                        else status
                    )
                    emit(
                        "video_tts_failed",
                        provider_request_id=job_id,
                        error_code="call_video_generation_failed",
                        run_state=status,
                    )
                    if on_job is not None:
                        on_job({**receipt, "status": status, "error_code": safe_code})
                    raise CallError(
                        "call_video_generation_failed"
                        if status == "failed"
                        else "call_video_generation_cancelled",
                        502,
                    )
                if status not in {
                    None,
                    "accepted",
                    "pending",
                    "queued",
                    "running",
                    "processing",
                }:
                    raise CallError("call_video_job_invalid", 502)
                if time.monotonic() >= deadline:
                    raise CallError("call_video_outcome_unknown", 502)
                await asyncio.sleep(self.poll_seconds)
                result = await self.client.request(
                    node,
                    "GET",
                    node.url(
                        selection.job_path.replace("{job_id}", quote(job_id, safe=""))
                    ),
                )
        except asyncio.CancelledError:
            if job_id and selection.cancel_path:
                # Exactly one best-effort cancel; cancellation never authorizes replay.
                try:
                    await asyncio.shield(
                        self.client.request(
                            node,
                            "POST",
                            node.url(
                                selection.cancel_path.replace(
                                    "{job_id}", quote(job_id, safe="")
                                )
                            ),
                            payload={},
                            timeout=3,
                        )
                    )
                except Exception:
                    emit("video_tts_cancel_unconfirmed", job_id=job_id)
            raise
