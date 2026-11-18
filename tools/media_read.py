"""Bounded, path-scoped media ingestion for the HER MCP gateway."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import shutil
import subprocess
import tempfile
import threading
import warnings
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from tools.registry import StructuredToolOutput
from tools.ocr import (
    OCR_DEFAULT_TIMEOUT_SECONDS, OCRCleanupUnconfirmed, OCRResult, extract_image_bytes,
    format_ocr_block, normalize_ocr_languages,
)
from tools.office.limits import _available_memory as _available_media_memory


IMAGE_EXTENSIONS = {
    ".bmp": "bmp",
    ".gif": "gif",
    ".jpeg": "jpeg",
    ".jpg": "jpeg",
    ".png": "png",
    ".tif": "tiff",
    ".tiff": "tiff",
    ".webp": "webp",
}
PDF_EXTENSIONS = {".pdf"}
VIDEO_EXTENSIONS = {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".ogv", ".webm"}
AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".webm"}

IMAGE_MAX_BYTES = 15 * 1024 * 1024
PDF_MAX_BYTES = 50 * 1024 * 1024
AV_MAX_BYTES = 100 * 1024 * 1024
MAX_SOURCE_PIXELS = 64_000_000
MAX_OUTPUT_PIXELS = 16_000_000
MAX_OUTPUT_DIMENSION = 4_000
MAX_IMAGE_BYTES = 2_500_000
# Keep base64 plus prompt/history under the smallest certified provider body
# limit (DashScope: 6 MiB) with useful headroom for non-media request data.
MAX_TOTAL_IMAGE_BYTES = 4_000_000
MAX_PDF_PAGES = 30
MAX_PDF_IMAGES = 12
MAX_PDF_TEXT_CHARS = 100_000
MAX_VIDEO_SECONDS = 600.0


class MediaReadError(ValueError):
    """A safe error suitable for returning to the model."""


@dataclass(frozen=True)
class MediaProbe:
    kind: str
    format_name: str
    duration: float = 0.0
    has_audio: bool = False
    has_video: bool = False


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    original_format: str
    original_size: tuple[int, int]
    normalized_size: tuple[int, int]
    region: tuple[int, int, int, int] | None = None


def _media_source_budgets(options: dict[str, Any] | None) -> dict[str, int]:
    values = (options or {}).get("resource_budgets", {})
    defaults = {"image_source_bytes": IMAGE_MAX_BYTES, "pdf_source_bytes": PDF_MAX_BYTES,
                "av_source_bytes": AV_MAX_BYTES}
    available = _available_media_memory()
    if available and available > 0:
        defaults.update(image_source_bytes=max(1, available // 32),
                        pdf_source_bytes=max(1, available // 16),
                        av_source_bytes=max(1, available // 4))
    if not isinstance(values, dict) or set(values) - set(defaults):
        raise MediaReadError("media_read resource_budgets contains unsupported fields")
    for key, value in values.items():
        if type(value) is not int or value <= 0:
            raise MediaReadError(f"media_read resource budget {key} must be a positive integer")
        defaults[key] = value
    return defaults


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_media_path(
    raw_path: str,
    *,
    access_root: Path,
    workspace_dir: Path,
    media_roots: list[Path],
) -> Path:
    if not str(raw_path or "").strip():
        raise MediaReadError("no media path provided")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise MediaReadError("media file does not exist") from exc
    if not resolved.is_file():
        raise MediaReadError("media path is not a regular file")

    roots = [Path(access_root).expanduser().resolve()]
    roots.extend(Path(root).expanduser().resolve() for root in media_roots)
    if not any(_within(resolved, root) for root in roots):
        raise MediaReadError("media path is outside the allowed access and media roots")
    return resolved


def _signature(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if header.startswith(b"BM"):
        return "bmp"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio"
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return "video"
    if header.startswith((b"fLaC", b"ID3", b"OggS", b"\x1aE\xdf\xa3")):
        return "av"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "av"
    if len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE0) == 0xE0:
        return "audio"
    return None


def _validate_signature(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(32)
    detected = _signature(header)
    suffix = path.suffix.casefold()
    expected = IMAGE_EXTENSIONS.get(suffix)
    if expected:
        if detected != expected:
            raise MediaReadError(
                f"file extension {suffix!r} does not match detected media signature {detected!r}"
            )
        return "image"
    if suffix in PDF_EXTENSIONS:
        if detected != "pdf":
            raise MediaReadError("PDF extension does not match the file signature")
        return "pdf"
    if suffix not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
        raise MediaReadError(f"unsupported media extension {suffix or '<none>'!r}")
    if detected not in {"audio", "video", "av"}:
        raise MediaReadError(
            f"file extension {suffix!r} does not match a supported audio/video signature"
        )
    return "av"


def _check_size(path: Path, limit: int, label: str) -> None:
    size = path.stat().st_size
    if size > limit:
        raise MediaReadError(f"{label} size {size} bytes exceeds the configured {limit} byte limit")
    if size <= 0:
        raise MediaReadError(f"{label} is empty")


def _jpeg_bytes(image: Any, *, max_dimension: int = MAX_OUTPUT_DIMENSION) -> tuple[bytes, tuple[int, int]]:
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
        raise MediaReadError("image dimensions exceed the decode safety limit")
    scale = min(
        1.0,
        max_dimension / max(width, height),
        math.sqrt(MAX_OUTPUT_PIXELS / (width * height)),
    )
    if scale < 1.0:
        image = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.LANCZOS,
        )
    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")

    quality = 88
    while True:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        encoded = buffer.getvalue()
        if len(encoded) <= MAX_IMAGE_BYTES:
            return encoded, image.size
        if quality > 62:
            quality -= 8
            continue
        width, height = image.size
        if max(width, height) <= 900:
            raise MediaReadError("normalized image cannot fit within the output safety limit")
        image.thumbnail((int(width * 0.8), int(height * 0.8)), Image.Resampling.LANCZOS)


def _image_block(encoded: bytes) -> dict[str, str]:
    return {
        "type": "image",
        "mimeType": "image/jpeg",
        "data": base64.b64encode(encoded).decode("ascii"),
    }


def normalize_image(path: Path, *, source_byte_limit: int = IMAGE_MAX_BYTES,
                    region: dict[str, int] | None = None) -> NormalizedImage:
    """Decode one image or an explicitly selected region for a vision model."""
    from PIL import Image, ImageOps

    _check_size(path, source_byte_limit, "image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                source.seek(0)
                original_format = source.format or "unknown"
                original_size = source.size
                if (
                    original_size[0] <= 0
                    or original_size[1] <= 0
                    or original_size[0] * original_size[1] > MAX_SOURCE_PIXELS
                ):
                    raise MediaReadError("image dimensions exceed the decode safety limit")
                oriented = ImageOps.exif_transpose(source)
                original_size = oriented.size
                selected_region = None
                if region is not None:
                    if not isinstance(region, dict) or set(region) != {"x", "y", "width", "height"}:
                        raise MediaReadError("image_region requires x, y, width, and height")
                    x, y, width, height = (region[key] for key in ("x", "y", "width", "height"))
                    if (any(type(value) is not int for value in (x, y, width, height))
                            or x < 0 or y < 0 or width < 1 or height < 1
                            or x + width > original_size[0] or y + height > original_size[1]):
                        raise MediaReadError("image_region is outside the oriented source image")
                    selected_region = (x, y, width, height)
                    oriented = oriented.crop((x, y, x + width, y + height))
                encoded, normalized_size = _jpeg_bytes(oriented.copy())
    except MediaReadError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaReadError("image dimensions exceed Pillow's decode safety limit") from exc
    except Exception as exc:
        raise MediaReadError(f"image decode failed: {exc}") from exc
    return NormalizedImage(
        data=encoded,
        original_format=original_format,
        original_size=original_size,
        normalized_size=normalized_size,
        region=selected_region,
    )


def _read_image(
    path: Path,
    *,
    ocr_mode: str = "auto",
    ocr_languages: tuple[str, ...] | None = None,
    ocr_timeout_seconds: float = OCR_DEFAULT_TIMEOUT_SECONDS,
    cancellation_event: threading.Event | None = None,
    source_byte_limit: int = IMAGE_MAX_BYTES,
    image_region: dict[str, int] | None = None,
) -> StructuredToolOutput:
    normalized = normalize_image(path, source_byte_limit=source_byte_limit, region=image_region)
    encoded = normalized.data
    original_format = normalized.original_format
    original_size = normalized.original_size
    normalized_size = normalized.normalized_size

    ocr_result: OCRResult | None = None
    if ocr_mode != "off":
        ocr_result = extract_image_bytes(encoded, languages=ocr_languages, timeout_seconds=ocr_timeout_seconds, cancellation_event=cancellation_event)
        if ocr_mode == "required" and (
            ocr_result.status not in {"ok", "empty"} or ocr_result.missing_languages
        ):
            detail = ocr_result.error or ocr_result.status
            raise MediaReadError(f"required OCR is unavailable or incomplete: {detail}")

    ocr_status = ocr_result.status if ocr_result is not None else "off"
    used_languages = "+".join(ocr_result.used_languages) if ocr_result is not None else "none"
    summary = (
        f"Media image ready: name={path.name}; format={original_format}; "
        f"source={original_size[0]}x{original_size[1]}; "
        f"normalized={normalized_size[0]}x{normalized_size[1]}; "
        f"region={normalized.region or (0, 0, *original_size)}; frame=1; "
        f"ocr_status={ocr_status}; ocr_languages={used_languages}."
    )
    content: list[dict[str, str]] = [{"type": "text", "text": summary}]
    if ocr_result is not None:
        content.append({"type": "text", "text": format_ocr_block(ocr_result)})
    content.append(_image_block(encoded))
    return StructuredToolOutput(
        output=summary,
        content=content,
    )


def _sample_indices(indices: list[int], maximum: int) -> list[int]:
    if len(indices) <= maximum:
        return indices
    if maximum == 1:
        return [indices[0]]
    chosen = {
        indices[round(position * (len(indices) - 1) / (maximum - 1))]
        for position in range(maximum)
    }
    return sorted(chosen)


def _source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _encode_media_cursor(state: dict[str, Any]) -> str:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_pdf_cursor(value: str) -> dict[str, Any]:
    try:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError()
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        state = json.loads(raw)
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError()
        return state
    except (ValueError, TypeError, UnicodeError):
        raise MediaReadError("invalid PDF continuation") from None


def _decode_audio_cursor(value: str) -> dict[str, Any]:
    try:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError()
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        state = json.loads(raw)
        if (not isinstance(state, dict) or state.get("version") != 1
                or state.get("kind") not in {"audio", "video"}
                or not isinstance(state.get("sha256"), str) or len(state["sha256"]) != 64
                or any(type(state.get(key)) not in (int, float) or not math.isfinite(state[key])
                       for key in ("next", "end"))
                or state["next"] < 0 or state["end"] <= state["next"]):
            raise ValueError()
        return state
    except (ValueError, TypeError, OverflowError, UnicodeError):
        raise MediaReadError("invalid audio continuation") from None


def _ffmpeg_seconds(value: float | Decimal) -> str:
    return format(Decimal(str(value)), "f")


def _pdf_text_prefix(text: str, budget: int) -> int:
    low, high = 0, min(len(text), budget)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(text[:middle], ensure_ascii=False)) <= budget:
            low = middle
        else:
            high = middle - 1
    return low


def _read_pdf(path: Path, mode: str | None, page_start: int | None = None,
              page_end: int | None = None, cursor: str | None = None, *,
              source_byte_limit: int = PDF_MAX_BYTES) -> StructuredToolOutput:
    _check_size(path, source_byte_limit, "PDF")
    try:
        import fitz
    except ImportError as exc:
        raise MediaReadError("PyMuPDF is unavailable; install the declared PyMuPDF dependency") from exc

    initial_stat = path.stat()
    source_hash = _source_digest(path)
    try:
        document = fitz.open(path)
    except Exception as exc:
        raise MediaReadError(f"PDF decode failed: {exc}") from exc
    try:
        if document.needs_pass:
            raise MediaReadError("encrypted PDF files are not supported")
        page_count = document.page_count
        if cursor:
            state = _decode_pdf_cursor(cursor)
            if state.get("sha256") != source_hash:
                raise MediaReadError("PDF source changed since the continuation was issued")
            if ((mode is not None and mode != state.get("mode"))
                    or (page_start is not None and page_start != state.get("start"))
                    or (page_end is not None and page_end != state.get("end"))):
                raise MediaReadError("PDF continuation does not match the original range or mode")
            mode, start, requested_end = state.get("mode"), state.get("start"), state.get("end")
            page_no, offset, image_done = state.get("page"), state.get("offset"), state.get("image_done")
        else:
            mode = mode or "auto"
            start = 1 if page_start is None else page_start
            requested_end = page_count if page_end is None else page_end
            page_no, offset, image_done = start, 0, False
        if (not isinstance(mode, str) or mode not in {"auto", "all", "none"}
                or any(type(value) is not int for value in (start, requested_end, page_no, offset))
                or type(image_done) is not bool or offset < 0
                or start < 1 or requested_end < start or requested_end > page_count
                or page_no < start or page_no > requested_end):
            raise MediaReadError(f"requested PDF page range or continuation is outside 1-{page_count}")

        first_page = page_no
        last_completed = page_no - 1
        rendered_pages, text_segments, blocks = [], [], []
        image_bytes, text_budget, visited = 0, 12000, 0
        while page_no <= requested_end and visited < MAX_PDF_PAGES:
            page = document.load_page(page_no - 1)
            text = page.get_text("text")
            if offset > len(text):
                raise MediaReadError("PDF continuation text offset is invalid")
            render = mode == "all" or (mode == "auto" and len(text.strip()) < 20)
            if render and not image_done:
                if len(rendered_pages) >= MAX_PDF_IMAGES:
                    break
                bounds = page.rect
                scale = min(2.0, 2200 / max(bounds.width, bounds.height, 1))
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                from PIL import Image
                with Image.open(io.BytesIO(pixmap.tobytes("png"))) as image:
                    encoded, _ = _jpeg_bytes(image, max_dimension=2200)
                if image_bytes + len(encoded) > MAX_TOTAL_IMAGE_BYTES:
                    if not blocks:
                        raise MediaReadError("one PDF page cannot fit the selected image budget")
                    break
                image_bytes += len(encoded)
                rendered_pages.append(page_no)
                blocks.append({"type": "text", "text": f"Rendered PDF page {page_no} follows."})
                blocks.append(_image_block(encoded))
                image_done = True
            if text and offset < len(text):
                header = f"[PDF page {page_no}; offset {offset}]\n"
                available = text_budget - len(json.dumps(header, ensure_ascii=False)) - 100
                count = _pdf_text_prefix(text[offset:], max(0, available))
                if count == 0:
                    break
                segment = text[offset:offset + count]
                text_segments.append({"page": page_no, "offset": offset, "length": count,
                                      "page_length": len(text), "content_index": len(blocks) + 1})
                blocks.append({"type": "text", "text": header + segment})
                text_budget -= len(json.dumps(header + segment, ensure_ascii=False)) + 10
                offset += count
                if offset < len(text):
                    break
            last_completed = page_no
            page_no += 1
            offset, image_done = 0, False
            visited += 1
        if (path.stat().st_size != initial_stat.st_size or path.stat().st_mtime_ns != initial_stat.st_mtime_ns
                or _source_digest(path) != source_hash):
            raise MediaReadError("PDF source changed while reading; start again")
        next_state = ({"version": 1, "sha256": source_hash, "mode": mode, "start": start,
                       "end": requested_end, "page": page_no, "offset": offset, "image_done": image_done}
                      if page_no <= requested_end else None)
        next_cursor = _encode_media_cursor(next_state) if next_state else None
        processed_end = max(first_page, last_completed)
        summary = (f"Media PDF ready: name={path.name}; pages={page_count}; "
                   f"processed_pages={first_page}-{processed_end}; requested_end={requested_end}; "
                   f"next_page={page_no if next_cursor else None}; rendered_pages={rendered_pages}; "
                   f"mode={mode}; pdf_next_cursor={next_cursor}.")
        content = [{"type": "text", "text": summary}, *blocks]
        return StructuredToolOutput(output=summary, content=content, details={
            "pdf_next_cursor": next_cursor, "pdf_complete": next_cursor is None,
            "pdf_source_sha256": source_hash, "pdf_text_segments": text_segments,
            "pdf_rendered_pages": rendered_pages,
        })
    finally:
        document.close()


def _run_process(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaReadError(f"media helper timed out after {timeout} seconds") from exc
    except OSError as exc:
        raise MediaReadError(f"media helper could not start: {exc}") from exc


def _probe_av(path: Path) -> MediaProbe:
    if shutil.which("ffprobe") is None:
        raise MediaReadError("ffprobe is unavailable")
    completed = _run_process(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        timeout=20,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()[-500:] or "unknown ffprobe error"
        raise MediaReadError(f"audio/video probe failed: {detail}")
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams") or []
        has_video = any(stream.get("codec_type") == "video" for stream in streams)
        has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        duration = float((payload.get("format") or {}).get("duration") or 0)
        format_name = str((payload.get("format") or {}).get("format_name") or "unknown")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaReadError("ffprobe returned malformed metadata") from exc
    if not has_audio and not has_video:
        raise MediaReadError("file contains no supported audio or video stream")
    if not math.isfinite(duration) or duration <= 0:
        raise MediaReadError("audio/video duration is missing or invalid")
    return MediaProbe(
        kind="video" if has_video else "audio",
        format_name=format_name,
        duration=duration,
        has_audio=has_audio,
        has_video=has_video,
    )


def _validate_av_extension(path: Path, probe: MediaProbe) -> None:
    suffix = path.suffix.casefold()
    if probe.kind == "video" and suffix not in VIDEO_EXTENSIONS:
        raise MediaReadError(f"extension {suffix!r} does not match the detected video stream")
    if probe.kind == "audio" and suffix not in AUDIO_EXTENSIONS:
        raise MediaReadError(f"extension {suffix!r} does not match the detected audio stream")


def _video_frames(path: Path, probe: MediaProbe, count: int, *,
                  start: float = 0.0, end: float | None = None) -> list[tuple[float, bytes]]:
    if shutil.which("ffmpeg") is None:
        raise MediaReadError("ffmpeg is unavailable")
    fractions = [0.5] if count == 1 else [0.1 + (0.8 * index / (count - 1)) for index in range(count)]
    frames: list[tuple[float, bytes]] = []
    total_bytes = 0
    with tempfile.TemporaryDirectory(prefix="hashi-media-video-") as temporary:
        for index, fraction in enumerate(fractions):
            window_end = probe.duration if end is None else end
            timestamp = min(start + (window_end - start) * fraction,
                            max(start, window_end - 0.05))
            destination = Path(temporary) / f"frame-{index:02d}.jpg"
            completed = _run_process(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    _ffmpeg_seconds(timestamp),
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(1920,iw)':-2",
                    "-q:v",
                    "3",
                    "-y",
                    str(destination),
                ],
                timeout=30,
            )
            if completed.returncode != 0 or not destination.is_file():
                detail = completed.stderr.strip()[-500:] or "frame output missing"
                raise MediaReadError(f"video frame extraction failed: {detail}")
            from PIL import Image

            with Image.open(destination) as image:
                encoded, _ = _jpeg_bytes(image.copy(), max_dimension=1920)
            if total_bytes + len(encoded) > MAX_TOTAL_IMAGE_BYTES:
                break
            total_bytes += len(encoded)
            frames.append((timestamp, encoded))
    if not frames:
        raise MediaReadError("video produced no usable frames")
    return frames


async def _transcribe_normalized(path: Path, *, start: float = 0.0, end: float | None = None) -> str:
    if shutil.which("ffmpeg") is None:
        raise MediaReadError("ffmpeg is unavailable for audio normalization")
    with tempfile.TemporaryDirectory(prefix="hashi-media-audio-") as temporary:
        normalized = Path(temporary) / "normalized.wav"
        completed = await asyncio.to_thread(
            _run_process,
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                *(["-ss", _ffmpeg_seconds(start)] if start else []),
                "-i",
                str(path),
                *(["-t", _ffmpeg_seconds(Decimal(str(end)) - Decimal(str(start)))] if end is not None else []),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(normalized),
            ],
            timeout=90,
        )
        if completed.returncode != 0 or not normalized.is_file():
            detail = completed.stderr.strip()[-500:] or "normalized audio output missing"
            raise MediaReadError(f"audio normalization failed: {detail}")
        from orchestrator.voice_transcriber import get_transcriber

        transcript = await get_transcriber().transcribe(normalized)
    if not transcript or transcript.startswith("[Transcription error]"):
        raise MediaReadError(f"normalized audio transcription failed: {transcript or 'empty result'}")
    return transcript


async def _read_av(
    path: Path,
    *,
    frame_count: int,
    transcribe_audio: bool,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    audio_cursor: str | None = None,
    source_byte_limit: int = AV_MAX_BYTES,
) -> StructuredToolOutput:
    _check_size(path, source_byte_limit, "audio/video")
    probe = await asyncio.to_thread(_probe_av, path)
    _validate_av_extension(path, probe)
    initial_stat = path.stat()
    saved_hash = None
    if audio_cursor is not None:
        state = _decode_audio_cursor(audio_cursor)
        if state["kind"] != probe.kind or (probe.kind == "video" and (not transcribe_audio or not probe.has_audio)):
            raise MediaReadError("audio continuation does not match the requested media operation")
        if ((start_seconds is not None and start_seconds != state["next"])
                or (end_seconds is not None and end_seconds != state["end"])):
            raise MediaReadError("audio continuation does not match the requested time range")
        start_seconds, end_seconds = state["next"], state["end"]
        saved_hash = state["sha256"]
    selected = start_seconds is not None or end_seconds is not None
    for value in (start_seconds, end_seconds):
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise MediaReadError("audio/video time range must use finite numbers")
    start = float(start_seconds) if start_seconds is not None else 0.0
    end = float(end_seconds) if end_seconds is not None else probe.duration
    if start < 0 or end <= start or end > probe.duration:
        raise MediaReadError(f"requested time range is outside 0-{probe.duration:.3f} seconds")
    audio_end = min(end, start + MAX_VIDEO_SECONDS)
    audio_next = audio_end if audio_end < end else None
    wants_audio = probe.kind == "audio" or (transcribe_audio and probe.has_audio)
    source_hash = None
    if wants_audio and (audio_next is not None or saved_hash is not None):
        source_hash = await asyncio.to_thread(_source_digest, path)
        if saved_hash is not None and source_hash != saved_hash:
            raise MediaReadError("audio source changed since the continuation was issued")
    async def continuation():
        if source_hash is None:
            return None
        current = path.stat()
        if (current.st_size != initial_stat.st_size or current.st_mtime_ns != initial_stat.st_mtime_ns
                or await asyncio.to_thread(_source_digest, path) != source_hash):
            raise MediaReadError("audio source changed while reading; start again")
        if audio_next is None:
            return None
        return _encode_media_cursor({"version": 1, "kind": probe.kind, "sha256": source_hash,
                                     "next": audio_next, "end": end})
    async def transcribe():
        if not selected and audio_next is None:
            return await _transcribe_normalized(path)
        return await _transcribe_normalized(path, start=start, end=audio_end)
    if probe.kind == "audio":
        transcript = await transcribe()
        next_cursor = await continuation()
        summary = (
            f"Media audio ready: name={path.name}; format={probe.format_name}; "
            f"duration={probe.duration:.2f}s; audio_seconds={start:.3f}-{audio_end:.3f}; "
            f"audio_next_start={audio_next}; audio_next_cursor={next_cursor}; normalized=wav/mono/16kHz."
        )
        return StructuredToolOutput(
            output=summary,
            content=[
                {"type": "text", "text": summary},
                {"type": "text", "text": f"[Audio transcription]\n{transcript}"},
            ],
            details={"audio_next_cursor": next_cursor, "audio_range": {"start": start, "end": audio_end, "requested_end": end}},
        )

    if selected:
        frames = await asyncio.to_thread(_video_frames, path, probe, frame_count, start=start, end=end)
    else:
        frames = await asyncio.to_thread(_video_frames, path, probe, frame_count)
    transcript = None
    if transcribe_audio and probe.has_audio:
        transcript = await transcribe()
    next_cursor = await continuation() if transcript is not None else None
    timestamps = [round(timestamp, 3) for timestamp, _ in frames]
    summary = (
        f"Media video ready: name={path.name}; format={probe.format_name}; "
        f"duration={probe.duration:.2f}s; selected_seconds={start:.3f}-{end:.3f}; "
        f"frame_timestamps={timestamps}; audio_transcribed={transcript is not None}; "
        f"audio_next_start={audio_next if transcript is not None else None}; audio_next_cursor={next_cursor}."
    )
    content: list[dict[str, str]] = [{"type": "text", "text": summary}]
    for timestamp, encoded in frames:
        content.append({"type": "text", "text": f"Video frame at {timestamp:.3f}s follows."})
        content.append(_image_block(encoded))
    if transcript is not None:
        content.append({"type": "text", "text": f"[Video audio transcription]\n{transcript}"})
    return StructuredToolOutput(output=summary, content=content,
                                details={"audio_next_cursor": next_cursor,
                                         "audio_range": {"start": start, "end": audio_end, "requested_end": end}}
                                if transcript is not None else None)


async def execute_media_read(
    args: dict[str, Any],
    *,
    access_root: Path,
    workspace_dir: Path,
    media_roots: list[Path],
    options: dict[str, Any] | None = None,
) -> str | StructuredToolOutput:
    """Read one supported media file without exposing bytes in audit output."""
    try:
        budgets = _media_source_budgets(options)
        path = _resolve_media_path(
            str(args.get("path") or ""),
            access_root=access_root,
            workspace_dir=workspace_dir,
            media_roots=media_roots,
        )
        kind = await asyncio.to_thread(_validate_signature, path)
        if kind == "image":
            ocr_mode = str(args.get("ocr_mode") or "auto").strip().casefold()
            if ocr_mode not in {"auto", "required", "off"}:
                raise MediaReadError("ocr_mode must be auto, required, or off")
            raw_languages = args.get("ocr_languages")
            if raw_languages is not None and not isinstance(raw_languages, list):
                raise MediaReadError("ocr_languages must be an array of language codes")
            ocr_languages = (
                normalize_ocr_languages(raw_languages)
                if raw_languages is not None
                else None
            )
            raw_timeout = args.get("ocr_timeout_seconds", (options or {}).get("ocr_timeout_seconds", OCR_DEFAULT_TIMEOUT_SECONDS))
            if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)) or not math.isfinite(raw_timeout) or raw_timeout <= 0:
                raise MediaReadError("ocr_timeout_seconds must be a positive finite number")
            cancellation_event = threading.Event()
            worker = asyncio.create_task(asyncio.to_thread(
                _read_image,
                path,
                ocr_mode=ocr_mode,
                ocr_languages=ocr_languages,
                ocr_timeout_seconds=float(raw_timeout),
                cancellation_event=cancellation_event,
                source_byte_limit=budgets["image_source_bytes"],
                image_region=args.get("image_region"),
            ))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancellation_event.set()
                try:
                    await asyncio.shield(worker)
                except OCRCleanupUnconfirmed as exc:
                    raise asyncio.CancelledError("OCR cancellation cleanup unconfirmed") from exc
                except Exception:
                    pass
                raise
        if kind == "pdf":
            mode = str(args["pdf_pages"]) if args.get("pdf_pages") is not None else None
            if mode is not None and mode not in {"auto", "all", "none"}:
                raise MediaReadError("pdf_pages must be auto, all, or none")
            return await asyncio.to_thread(_read_pdf, path, mode, args.get("page_start"),
                                           args.get("page_end"), args.get("pdf_cursor"),
                                           source_byte_limit=budgets["pdf_source_bytes"])
        frame_count = int(args.get("video_frames", 3))
        if not 1 <= frame_count <= 6:
            raise MediaReadError("video_frames must be between 1 and 6")
        return await _read_av(
            path,
            frame_count=frame_count,
            transcribe_audio=bool(args.get("transcribe_audio", False)),
            start_seconds=args.get("start_seconds"),
            end_seconds=args.get("end_seconds"),
            audio_cursor=args.get("audio_cursor"),
            source_byte_limit=budgets["av_source_bytes"],
        )
    except (MediaReadError, OSError, ValueError) as exc:
        return f"Error: media_read failed: {exc}"
