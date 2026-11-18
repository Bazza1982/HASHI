"""Bounded PAO media derivation; reuses configured OCR/STT, never an Engine Run."""

from __future__ import annotations
import asyncio
import base64
import io
import json
import logging
import os
from functools import partial
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path


logger = logging.getLogger("BridgeU.FileContent")

# The current authenticated JSON transport has a 64 MiB body budget.
CONTENT_WIRE_MAX_BYTES = 64 * 1024 * 1024
CONTENT_WIRE_MAX_INPUT_BYTES = ((CONTENT_WIRE_MAX_BYTES - 4096) // 4) * 3
_CAPACITY_DEFAULTS = {
    "max_input_bytes": 32 * 1024 * 1024,
    "max_image_pixels": 64_000_000,
    "max_decoded_bytes": 256 * 1024 * 1024,
    "receiving_concurrency": 2,
    "processing_concurrency": 2,
    "queue_limit": 8,
    "ocr_timeout_seconds": 120,
}


def _content_capacity(overrides=None):
    if overrides is not None and (
        not isinstance(overrides, dict) or set(overrides) - set(_CAPACITY_DEFAULTS)
    ):
        raise ValueError("invalid file content capacity configuration")
    values = {}
    for key, default in _CAPACITY_DEFAULTS.items():
        raw = (overrides or {}).get(key, os.environ.get("HASHI_FILE_CONTENT_" + key.upper(), default))
        if type(raw) not in (int, str) or (isinstance(raw, str) and not raw.isdecimal()):
            raise ValueError(f"invalid file content capacity: {key}")
        try:
            number = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid file content capacity: {key}") from exc
        if type(raw) is bool or number < (0 if key == "queue_limit" else 1):
            raise ValueError(f"invalid file content capacity: {key}")
        values[key] = number
    if values["max_input_bytes"] > CONTENT_WIRE_MAX_INPUT_BYTES:
        raise ValueError("file content capacity exceeds the current authenticated JSON transport")
    return values


def content_request_timeout_seconds():
    """Budget for the authenticated proxy hop, including queued work."""
    raw = os.environ.get("HASHI_FILE_CONTENT_REQUEST_TIMEOUT_SECONDS", "7200")
    if not raw.isdecimal() or int(raw) < 1 or int(raw) > 2147483:
        raise ValueError("invalid file content request timeout")
    return int(raw)


async def _bounded_thread(callable_, *args):
    """Cancellation cannot release admission while native work is still running."""
    task = asyncio.create_task(asyncio.to_thread(callable_, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve a native failure without replacing the caller's cancellation.
        if task.done() and not task.cancelled():
            task.exception()
        raise


class FileContentError(ValueError):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status
        super().__init__(code)


async def read_content_request(request, *, max_bytes):
    """Strict JSON binds policy and bytes without unsigned MIME/header framing."""
    if getattr(request, "content_type", "") != "application/json":
        raise FileContentError("fileshare_content_invalid", 415)
    encoded_limit = ((max_bytes + 2) // 3) * 4
    try:
        raw = await request.read()
        if len(raw) > encoded_limit + 4096:
            raise FileContentError("fileshare_content_limit", 413)
        value = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or not {"filename", "operation", "payload"} <= set(value)
            or set(value) - {"filename", "operation", "allow_compute", "payload"}
            or not isinstance(value["filename"], str)
            or not isinstance(value["operation"], str)
            or ("allow_compute" in value and not isinstance(value["allow_compute"], bool))
            or not isinstance(value["payload"], str)
        ):
            raise FileContentError("fileshare_content_invalid")
        if len(value["filename"]) > 240:
            raise FileContentError("fileshare_content_invalid")
        if len(value["payload"]) > encoded_limit:
            raise FileContentError("fileshare_content_limit", 413)
        payload = base64.b64decode(value["payload"], validate=True)
        if len(payload) > max_bytes:
            raise FileContentError("fileshare_content_limit", 413)
        if base64.b64encode(payload).decode("ascii") != value["payload"]:
            raise FileContentError("fileshare_content_invalid")
        return payload, {
            "filename": value["filename"],
            "operation": value["operation"],
            "allow_compute": value.get("allow_compute"),
        }
    except FileContentError:
        raise
    except (ValueError, TypeError, UnicodeError) as error:
        raise FileContentError("fileshare_content_invalid") from error


class FileContentService:
    MAX_BYTES = 32 * 1024 * 1024

    def __init__(self, *, transcriber=None, ocr=None, configuration=None, capacity=None):
        self.transcriber = transcriber
        self.ocr = ocr
        self.configuration = configuration
        self.capacity = _content_capacity(capacity)
        self.MAX_BYTES = self.capacity["max_input_bytes"]
        self._receiving_slots = asyncio.Semaphore(self.capacity["receiving_concurrency"])
        self._processing_slots = asyncio.Semaphore(self.capacity["processing_concurrency"])
        self.active = 0
        self.receiving = 0
        self.receiving_waiters = 0
        self.processing_waiters = 0

    @asynccontextmanager
    async def _admit(self, semaphore, count_name, waiting_name):
        if semaphore.locked() and getattr(self, waiting_name) >= self.capacity["queue_limit"]:
            raise FileContentError("fileshare_content_busy", 429)
        setattr(self, waiting_name, getattr(self, waiting_name) + 1)
        try:
            await semaphore.acquire()
        finally:
            setattr(self, waiting_name, getattr(self, waiting_name) - 1)
        setattr(self, count_name, getattr(self, count_name) + 1)
        try:
            yield
        finally:
            setattr(self, count_name, getattr(self, count_name) - 1)
            semaphore.release()

    async def process_request(self, request):
        async with self._admit(self._receiving_slots, "receiving", "receiving_waiters"):
            payload, metadata = await read_content_request(
                request, max_bytes=self.MAX_BYTES
            )
            return await self.process(payload, **metadata)

    def config(self):
        if self.configuration:
            return self.configuration()
        from orchestrator.voice_transcriber import _transcription_configuration

        return _transcription_configuration()[1]

    def capabilities(self):
        from tools.ocr import resolve_tesseract_binary, resolve_paddle_model_root

        configured = self.config()
        transcription_max = self.MAX_BYTES
        if configured.get("provider") == "compute":
            from orchestrator.voice_transcriber import compute_audio_input_limit
            transcription_max = min(transcription_max, compute_audio_input_limit(configured))
        return {
            "version": 1,
            "ocr": {
                "supported": True,
                "local": True,
                "available": bool(
                    resolve_tesseract_binary() or resolve_paddle_model_root()
                ),
            },
            "transcription": {
                "supported": True,
                "configured_service": configured.get("provider") == "compute",
                "service_opt_in_required": False,
                "max_bytes": transcription_max,
            },
            "max_bytes": self.MAX_BYTES,
            "capacity": {**self.capacity, "max_input_bytes": self.MAX_BYTES,
                         "transport_max_input_bytes": CONTENT_WIRE_MAX_INPUT_BYTES,
                         "request_timeout_seconds": content_request_timeout_seconds()},
        }

    def normalize_image(self, payload):
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            pixels = image.width * image.height
            if (
                pixels > self.capacity["max_image_pixels"]
                or pixels * 8 > self.capacity["max_decoded_bytes"]
            ):
                raise FileContentError("fileshare_content_limit", 413)
            # Downscale before RGB conversion so JPEG decoders can reduce their
            # decoded working set; malformed/bomb images still fail in Pillow.
            image.thumbnail((2400, 2400))
            image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90)
            return output.getvalue()

    async def process(self, payload, *, filename, operation, allow_compute=None):
        if not isinstance(payload, bytes) or not 0 < len(payload) <= self.MAX_BYTES:
            raise FileContentError("fileshare_content_limit", 413)
        if (
            not isinstance(filename, str)
            or not filename
            or len(filename) > 240
            or any(char in filename for char in "/\\\x00\r\n")
            or operation not in {"ocr", "transcribe"}
        ):
            raise FileContentError("fileshare_content_invalid")
        async with self._admit(self._processing_slots, "active", "processing_waiters"):
            if operation == "ocr":
                try:
                    normalized = await _bounded_thread(self.normalize_image, payload)
                except FileContentError:
                    raise
                except Exception as error:
                    raise FileContentError("fileshare_image_invalid", 422) from error
                if self.ocr is None:
                    from tools.ocr import extract_image_bytes

                    ocr = partial(
                        extract_image_bytes, timeout_seconds=self.capacity["ocr_timeout_seconds"]
                    )
                else:
                    ocr = self.ocr
                result = await _bounded_thread(ocr, normalized)
                if result.status not in {"ok", "partial", "empty"}:
                    logger.error(
                        "OCR extraction failed: engine=%s status=%s reason=%s",
                        getattr(result, "engine", "unknown"),
                        result.status,
                        getattr(result, "error", "not supplied"),
                    )
                    raise FileContentError(
                        "fileshare_ocr_"
                        + (
                            result.status
                            if result.status in {"unavailable", "timeout", "error"}
                            else "failed"
                        ),
                        422,
                    )
                response = {
                    "status": "ready" if result.status == "ok" else "partial",
                    "error_code": None
                    if result.status == "ok"
                    else "fileshare_ocr_partial",
                    "segments": [{"text": result.text, "locator": {"kind": "image"}}]
                    if result.text
                    else [],
                }
            else:
                config = self.config()
                if config.get("provider") == "compute":
                    if allow_compute is False:
                        raise FileContentError("fileshare_compute_opt_in_required", 403)
                    from orchestrator.voice_transcriber import compute_audio_input_limit
                    if len(payload) > compute_audio_input_limit(config):
                        raise FileContentError("fileshare_content_limit", 413)
                if Path(filename).suffix.lower() not in {
                    ".wav",
                    ".ogg",
                    ".mp3",
                    ".m4a",
                    ".webm",
                    ".flac",
                    ".mp4",
                    ".aac",
                    ".mkv",
                    ".mov",
                    ".avi",
                    ".wma",
                    ".aiff",
                    ".opus",
                }:
                    raise FileContentError("fileshare_media_unsupported", 415)
                from orchestrator.voice_transcriber import (
                    VoiceTranscriber,
                    get_transcriber,
                    _external_python,
                )

                if (
                    self.transcriber is None
                    and config.get("provider", "local") == "local"
                    and not _external_python()
                ):
                    raise FileContentError(
                        "fileshare_isolated_transcription_unavailable", 503
                    )
                transcriber = self.transcriber or get_transcriber()
                with tempfile.TemporaryDirectory(
                    prefix="hashi-file-content-"
                ) as temporary:
                    pathname = Path(temporary) / (
                        "media" + Path(filename).suffix.lower()
                    )
                    pathname.write_bytes(payload)
                    try:
                        options = {"include_segments": True}
                        if isinstance(transcriber, VoiceTranscriber):
                            # The owner can change the selected provider between
                            # admission and dispatch; carry an explicit local-only
                            # choice to the actual transcriber as well.
                            options["allow_compute"] = allow_compute is not False
                        result = await asyncio.wait_for(
                            transcriber.transcribe_result(pathname, **options),
                            timeout=90,
                        )
                    except asyncio.TimeoutError as error:
                        raise FileContentError(
                            "fileshare_transcription_timeout", 504
                        ) from error
                if not result.ok:
                    raise FileContentError(result.failure.code, 422)
                timed = [
                    {
                        "text": segment.text,
                        "locator": {
                            "kind": "time",
                            "start": segment.start,
                            "end": segment.end,
                        },
                    }
                    for segment in result.segments
                ]
                response = {
                    "status": "ready" if timed else "partial",
                    "error_code": None if timed else "fileshare_timestamps_unavailable",
                    "segments": timed
                    or [{"text": result.text, "locator": {"kind": "text"}}],
                }
            if (
                len(json.dumps(response, ensure_ascii=False).encode("utf-8"))
                > 2 * 1024 * 1024
            ):
                raise FileContentError("fileshare_content_limit", 413)
            return response
