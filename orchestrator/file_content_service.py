"""Bounded PAO media derivation; reuses configured OCR/STT, never an Engine Run."""

from __future__ import annotations
import asyncio
import base64
import io
import json
import tempfile
from pathlib import Path


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
            or set(value) != {"filename", "operation", "allow_compute", "payload"}
            or not isinstance(value["filename"], str)
            or not isinstance(value["operation"], str)
            or not isinstance(value["allow_compute"], bool)
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
            key: value[key] for key in ("filename", "operation", "allow_compute")
        }
    except FileContentError:
        raise
    except (ValueError, TypeError, UnicodeError) as error:
        raise FileContentError("fileshare_content_invalid") from error


class FileContentService:
    MAX_BYTES = 32 * 1024 * 1024

    def __init__(self, *, transcriber=None, ocr=None, configuration=None):
        self.transcriber = transcriber
        self.ocr = ocr
        self.configuration = configuration
        self.active = 0
        self.receiving = 0

    async def process_request(self, request):
        if self.receiving >= 2:
            raise FileContentError("fileshare_content_busy", 429)
        self.receiving += 1
        try:
            payload, metadata = await read_content_request(
                request, max_bytes=self.MAX_BYTES
            )
            return await self.process(payload, **metadata)
        finally:
            self.receiving -= 1

    def config(self):
        if self.configuration:
            return self.configuration()
        from orchestrator.voice_transcriber import _transcription_configuration

        return _transcription_configuration()[1]

    def capabilities(self):
        from tools.ocr import resolve_tesseract_binary, resolve_paddle_model_root

        configured = self.config()
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
                "service_opt_in_required": configured.get("provider") == "compute",
            },
            "max_bytes": self.MAX_BYTES,
        }

    @staticmethod
    def normalize_image(payload):
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            if image.width * image.height > 20_000_000:
                raise FileContentError("fileshare_content_limit", 413)
            image = image.convert("RGB")
            image.thumbnail((2400, 2400))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90)
            return output.getvalue()

    async def process(self, payload, *, filename, operation, allow_compute=False):
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
        if self.active >= 2:
            raise FileContentError("fileshare_content_busy", 429)
        self.active += 1
        try:
            if operation == "ocr":
                try:
                    normalized = await _bounded_thread(self.normalize_image, payload)
                except FileContentError:
                    raise
                except Exception as error:
                    raise FileContentError("fileshare_image_invalid", 422) from error
                if self.ocr is None:
                    from tools.ocr import extract_image_bytes

                    ocr = extract_image_bytes
                else:
                    ocr = self.ocr
                result = await _bounded_thread(ocr, normalized)
                if result.status not in {"ok", "partial", "empty"}:
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
                if config.get("provider") == "compute" and not allow_compute:
                    raise FileContentError("fileshare_compute_opt_in_required", 403)
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
                        result = await asyncio.wait_for(
                            transcriber.transcribe_result(
                                pathname, include_segments=True
                            ),
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
        finally:
            self.active -= 1
