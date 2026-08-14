"""Bounded, path-scoped media ingestion for the HER MCP gateway."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import shutil
import subprocess
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.registry import StructuredToolOutput
from tools.ocr import OCRResult, extract_image_bytes, format_ocr_block, normalize_ocr_languages


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
        raise MediaReadError(f"{label} exceeds the {limit // (1024 * 1024)} MB safety limit")
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


def _read_image(
    path: Path,
    *,
    ocr_mode: str = "auto",
    ocr_languages: tuple[str, ...] | None = None,
) -> StructuredToolOutput:
    from PIL import Image

    _check_size(path, IMAGE_MAX_BYTES, "image")
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
                encoded, normalized_size = _jpeg_bytes(source.copy())
    except MediaReadError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaReadError("image dimensions exceed Pillow's decode safety limit") from exc
    except Exception as exc:
        raise MediaReadError(f"image decode failed: {exc}") from exc

    ocr_result: OCRResult | None = None
    if ocr_mode != "off":
        ocr_result = extract_image_bytes(encoded, languages=ocr_languages)
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
        f"normalized={normalized_size[0]}x{normalized_size[1]}; frame=1; "
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


def _read_pdf(path: Path, mode: str) -> StructuredToolOutput:
    _check_size(path, PDF_MAX_BYTES, "PDF")
    try:
        import fitz
    except ImportError as exc:
        raise MediaReadError("PyMuPDF is unavailable; install the declared PyMuPDF dependency") from exc

    try:
        document = fitz.open(path)
    except Exception as exc:
        raise MediaReadError(f"PDF decode failed: {exc}") from exc
    try:
        if document.needs_pass:
            raise MediaReadError("encrypted PDF files are not supported")
        page_count = document.page_count
        if page_count > MAX_PDF_PAGES:
            raise MediaReadError(f"PDF has {page_count} pages; maximum is {MAX_PDF_PAGES}")

        page_texts: list[str] = []
        render_candidates: list[int] = []
        total_chars = 0
        for index in range(page_count):
            page = document.load_page(index)
            text = page.get_text("text").strip()
            remaining = MAX_PDF_TEXT_CHARS - total_chars
            if remaining > 0 and text:
                clipped = text[:remaining]
                page_texts.append(f"[PDF page {index + 1}]\n{clipped}")
                total_chars += len(clipped)
            if mode == "all" or (mode == "auto" and len(text) < 20):
                render_candidates.append(index)

        selected = _sample_indices(render_candidates, MAX_PDF_IMAGES)
        blocks: list[dict[str, str]] = []
        image_bytes = 0
        rendered_pages: list[int] = []
        for index in selected:
            page = document.load_page(index)
            bounds = page.rect
            scale = min(2.0, 2200 / max(bounds.width, bounds.height, 1))
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            from PIL import Image

            image = Image.open(io.BytesIO(pixmap.tobytes("png")))
            encoded, _ = _jpeg_bytes(image, max_dimension=2200)
            if image_bytes + len(encoded) > MAX_TOTAL_IMAGE_BYTES:
                break
            image_bytes += len(encoded)
            rendered_pages.append(index + 1)
            blocks.append(
                {"type": "text", "text": f"Rendered PDF page {index + 1} follows."}
            )
            blocks.append(_image_block(encoded))

        text_payload = "\n\n".join(page_texts)
        if total_chars >= MAX_PDF_TEXT_CHARS:
            text_payload += "\n\n[PDF text truncated at the safety limit]"
        summary = (
            f"Media PDF ready: name={path.name}; pages={page_count}; "
            f"text_chars={total_chars}; rendered_pages={rendered_pages}; mode={mode}."
        )
        content: list[dict[str, str]] = [{"type": "text", "text": summary}]
        if text_payload:
            content.append({"type": "text", "text": text_payload})
        content.extend(blocks)
        if not text_payload and not rendered_pages:
            raise MediaReadError("PDF produced neither readable text nor renderable pages")
        return StructuredToolOutput(output=summary, content=content)
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
    if duration > MAX_VIDEO_SECONDS:
        raise MediaReadError(
            f"audio/video duration {duration:.1f}s exceeds the {MAX_VIDEO_SECONDS:.0f}s safety limit"
        )
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


def _video_frames(path: Path, probe: MediaProbe, count: int) -> list[tuple[float, bytes]]:
    if shutil.which("ffmpeg") is None:
        raise MediaReadError("ffmpeg is unavailable")
    fractions = [0.5] if count == 1 else [0.1 + (0.8 * index / (count - 1)) for index in range(count)]
    frames: list[tuple[float, bytes]] = []
    total_bytes = 0
    with tempfile.TemporaryDirectory(prefix="hashi-media-video-") as temporary:
        for index, fraction in enumerate(fractions):
            timestamp = min(max(0.0, probe.duration * fraction), max(0.0, probe.duration - 0.05))
            destination = Path(temporary) / f"frame-{index:02d}.jpg"
            completed = _run_process(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-ss",
                    f"{timestamp:.3f}",
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


async def _transcribe_normalized(path: Path) -> str:
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
                "-i",
                str(path),
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
) -> StructuredToolOutput:
    _check_size(path, AV_MAX_BYTES, "audio/video")
    probe = await asyncio.to_thread(_probe_av, path)
    _validate_av_extension(path, probe)
    if probe.kind == "audio":
        transcript = await _transcribe_normalized(path)
        summary = (
            f"Media audio ready: name={path.name}; format={probe.format_name}; "
            f"duration={probe.duration:.2f}s; normalized=wav/mono/16kHz."
        )
        return StructuredToolOutput(
            output=summary,
            content=[
                {"type": "text", "text": summary},
                {"type": "text", "text": f"[Audio transcription]\n{transcript}"},
            ],
        )

    frames = await asyncio.to_thread(_video_frames, path, probe, frame_count)
    transcript = None
    if transcribe_audio and probe.has_audio:
        transcript = await _transcribe_normalized(path)
    timestamps = [round(timestamp, 3) for timestamp, _ in frames]
    summary = (
        f"Media video ready: name={path.name}; format={probe.format_name}; "
        f"duration={probe.duration:.2f}s; frame_timestamps={timestamps}; "
        f"audio_transcribed={transcript is not None}."
    )
    content: list[dict[str, str]] = [{"type": "text", "text": summary}]
    for timestamp, encoded in frames:
        content.append({"type": "text", "text": f"Video frame at {timestamp:.3f}s follows."})
        content.append(_image_block(encoded))
    if transcript is not None:
        content.append({"type": "text", "text": f"[Video audio transcription]\n{transcript}"})
    return StructuredToolOutput(output=summary, content=content)


async def execute_media_read(
    args: dict[str, Any],
    *,
    access_root: Path,
    workspace_dir: Path,
    media_roots: list[Path],
) -> str | StructuredToolOutput:
    """Read one supported media file without exposing bytes in audit output."""
    try:
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
            return await asyncio.to_thread(
                _read_image,
                path,
                ocr_mode=ocr_mode,
                ocr_languages=ocr_languages,
            )
        if kind == "pdf":
            mode = str(args.get("pdf_pages") or "auto")
            if mode not in {"auto", "all", "none"}:
                raise MediaReadError("pdf_pages must be auto, all, or none")
            return await asyncio.to_thread(_read_pdf, path, mode)
        frame_count = int(args.get("video_frames", 3))
        if not 1 <= frame_count <= 6:
            raise MediaReadError("video_frames must be between 1 and 6")
        return await _read_av(
            path,
            frame_count=frame_count,
            transcribe_audio=bool(args.get("transcribe_audio", False)),
        )
    except (MediaReadError, OSError, ValueError) as exc:
        return f"Error: media_read failed: {exc}"
