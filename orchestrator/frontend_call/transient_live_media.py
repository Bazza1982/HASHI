"""Current-turn live camera frames that never enter Session persistence."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from orchestrator.multimodal_contract import validate_inline_image_data_url


TRANSIENT_LIVE_FRAMES_METADATA_KEY = "hashi_transient_live_frames_v1"
TRANSIENT_LIVE_FRAMES_TYPE = "hashi.transient-live-frames"
LIVE_FRAME_WINDOW_SECONDS = 10.0
LIVE_FRAME_LIMIT = 10
LIVE_FRAME_BUFFER_LIMIT = 32
_RUNTIME_REGISTRY = "_transient_live_frames_by_request"


def _request_key(value: object) -> str:
    return str(value or "").strip().removeprefix("hashi-request:")


def normalize_capture_clock(value: object) -> dict[str, Any]:
    """Validate timing metadata; it conveys no identity or authorization."""
    if not isinstance(value, Mapping) or set(value) != {
        "basis", "client_captured_at", "uncertainty_ms"
    }:
        raise ValueError("invalid camera capture clock")
    original = value.get("client_captured_at")
    uncertainty = value.get("uncertainty_ms")
    if (value.get("basis") != "server_roundtrip_lower_bound"
        or not isinstance(original, str) or len(original) > 40
        or type(uncertainty) is not int or not 0 <= uncertainty <= 2**53 - 1):
        raise ValueError("invalid camera capture clock")
    try:
        stamp = datetime.fromisoformat(original.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid camera capture clock") from exc
    return {
        "basis": "server_roundtrip_lower_bound",
        "client_captured_at": original,
        "uncertainty_ms": uncertainty,
    }


def _selected_frame_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise ValueError("invalid selected live frame limit")
    return value


def encode_live_frames(
    frames: Sequence[Mapping[str, Any]], *, max_frames: int = LIVE_FRAME_LIMIT
) -> dict[str, Any]:
    """Encode an already-selected turn snapshot for one Function IPC hop."""

    limit = _selected_frame_limit(max_frames)
    if len(frames) > limit:
        raise ValueError(f"live frame batch accepts at most {limit} frames")
    encoded: list[dict[str, Any]] = []
    for frame in frames:
        image = frame.get("image")
        if not isinstance(image, bytes) or not image:
            raise ValueError("live frame image must be non-empty bytes")
        sequence = frame.get("frame_sequence")
        if type(sequence) is not int or sequence < 1:
            raise ValueError("live frame sequence must be a positive integer")
        captured_at = str(frame.get("captured_at") or "")
        stamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("live frame capture time must be timezone-aware")
        encoded.append(
            {
                "data_url": "data:image/jpeg;base64,"
                + base64.b64encode(image).decode("ascii"),
                "frame_sequence": sequence,
                "captured_at": captured_at,
                **({"capture_clock": normalize_capture_clock(frame["capture_clock"])}
                   if frame.get("capture_clock") is not None else {}),
            }
        )
    return {
        "type": TRANSIENT_LIVE_FRAMES_TYPE,
        "version": 1,
        "scope": "current_input_only",
        "frames": encoded,
    }


def pop_live_frames_metadata(
    metadata: dict[str, Any], *, max_frames: int = LIVE_FRAME_LIMIT
) -> tuple[dict[str, Any], ...]:
    """Remove and validate raw frames before PAO persistence or canonical audit."""

    raw = metadata.pop(TRANSIENT_LIVE_FRAMES_METADATA_KEY, None)
    if raw is None:
        return ()
    if not isinstance(raw, Mapping) or (
        raw.get("type") != TRANSIENT_LIVE_FRAMES_TYPE
        or raw.get("version") != 1
        or raw.get("scope") != "current_input_only"
    ):
        raise ValueError("invalid transient live frame envelope")
    rows = raw.get("frames")
    if not isinstance(rows, list):
        raise ValueError("transient live frames must be a list")
    limit = _selected_frame_limit(max_frames)
    if len(rows) > limit:
        raise ValueError(f"live frame batch accepts at most {limit} frames")
    frames: list[dict[str, Any]] = []
    previous_sequence = 0
    previous_stamp: datetime | None = None
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("invalid transient live frame")
        sequence = row.get("frame_sequence")
        if type(sequence) is not int or sequence <= previous_sequence:
            raise ValueError("live frame sequences must be strictly increasing")
        captured_at = str(row.get("captured_at") or "")
        stamp = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if stamp.tzinfo is None or (
            previous_stamp is not None and stamp < previous_stamp
        ):
            raise ValueError(
                "live frame capture times must be ordered and timezone-aware"
            )
        data_url = str(row.get("data_url") or "")
        mime_type, _ = validate_inline_image_data_url(data_url)
        if mime_type != "image/jpeg":
            raise ValueError("live camera frames must be JPEG images")
        frames.append(
            {
                "data_url": data_url,
                "frame_sequence": sequence,
                "captured_at": captured_at,
                **({"capture_clock": normalize_capture_clock(row["capture_clock"])}
                   if row.get("capture_clock") is not None else {}),
            }
        )
        previous_sequence = sequence
        previous_stamp = stamp
    return tuple(frames)


def bind_live_frames(
    runtime: Any,
    request_id: str,
    frames: Sequence[Mapping[str, Any]],
    *,
    freshness_seconds: int,
) -> None:
    key = _request_key(request_id)
    if not key or not frames:
        return
    if type(freshness_seconds) is not int or not 1 <= freshness_seconds <= 2**31 - 1:
        raise ValueError("live frame freshness policy is invalid")
    registry = getattr(runtime, _RUNTIME_REGISTRY, None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(runtime, _RUNTIME_REGISTRY, registry)
    registry[key] = {
        "frames": tuple(dict(frame) for frame in frames),
        "freshness_seconds": freshness_seconds,
    }


class _TimedLiveFrame(dict):
    """In-memory timing metadata that is never serialized into a model request."""

    def __init__(self, value: Mapping[str, Any], captured: datetime, freshness: int):
        super().__init__(value)
        self.captured = captured
        self.freshness = freshness


class _TransientLivePart(dict):
    """Mark only camera-origin content, leaving ordinary attachments untouched."""

    def __init__(self, value: Mapping[str, Any], frame: Mapping[str, Any], *, image: bool):
        super().__init__(value)
        self.captured = getattr(frame, "captured", None)
        self.freshness = getattr(frame, "freshness", None)
        self.image = image

    def expired(self, now: datetime) -> bool:
        if self.captured is None or self.freshness is None:
            return False
        age = (now - self.captured).total_seconds()
        return age < -3 or age > self.freshness


def mark_live_frame_part(
    value: Mapping[str, Any], frame: Mapping[str, Any], *, image: bool
) -> dict[str, Any]:
    return _TransientLivePart(value, frame, image=image)


def translate_live_frame_part(
    original: Mapping[str, Any], value: Mapping[str, Any]
) -> dict[str, Any]:
    """Carry in-memory camera provenance through a provider-specific wire shape."""
    if isinstance(original, _TransientLivePart):
        return _TransientLivePart(value, original, image=original.image)
    return dict(value)


def redact_transient_camera_evidence(value: Any) -> tuple[Any, int]:
    """Preserve ordinary content while omitting explicitly tagged live image bytes."""
    if isinstance(value, _TransientLivePart):
        if value.image:
            return {
                "type": "text",
                "text": "[Transient live camera image omitted from retained evidence.]",
            }, 1
        return dict(value), 0
    if isinstance(value, Mapping):
        result = {}
        omitted = 0
        for key, item in value.items():
            result[key], count = redact_transient_camera_evidence(item)
            omitted += count
        return result, omitted
    if isinstance(value, (list, tuple)):
        result = []
        omitted = 0
        for item in value:
            projected, count = redact_transient_camera_evidence(item)
            result.append(projected)
            omitted += count
        return result, omitted
    return value, 0


def project_live_frame_messages(
    messages: Sequence[Mapping[str, Any]], *,
    for_checkpoint: bool = False,
    retain_tags: bool = False,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Omit transient camera content at checkpoints or when it is no longer fresh."""
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("current time must be timezone-aware")
    result = []
    total_omitted = 0
    for original in messages:
        message = dict(original)
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            omitted = 0
            for part in content:
                if isinstance(part, _TransientLivePart):
                    if for_checkpoint or part.expired(current):
                        omitted += int(part.image)
                        continue
                    parts.append(part if retain_tags else dict(part))
                else:
                    parts.append(part)
            if omitted:
                text_type = (
                    "input_text"
                    if any(isinstance(part, Mapping) and part.get("type") in {"input_text", "input_image"}
                           for part in content)
                    else "text"
                )
                parts.append({
                    "type": text_type,
                    "text": (
                        "Live camera snapshots were transient and are unavailable "
                        "in this saved continuation."
                        if for_checkpoint else
                        "Some live camera snapshots expired before this provider "
                        "request and are no longer included."
                    ),
                })
                total_omitted += omitted
            message["content"] = parts
        result.append(message)
    return result, total_omitted


def project_live_frame_payload(
    payload: Mapping[str, Any], *, retain_tags: bool = True, now: datetime | None = None
) -> tuple[dict[str, Any], int]:
    result = dict(payload)
    omitted = 0
    for key in ("messages", "input"):
        rows = result.get(key)
        if isinstance(rows, list) and all(isinstance(item, Mapping) for item in rows):
            result[key], count = project_live_frame_messages(
                rows, retain_tags=retain_tags, now=now
            )
            omitted += count
    return result, omitted


def live_frames_for(
    runtime: Any, request_ref: str, *, now: datetime | None = None
) -> tuple[dict[str, Any], ...]:
    # Runs may wait after admission. Filter again at each provider invocation;
    # the frozen policy comes from the verified Call context, never a model hint.
    registry = getattr(runtime, _RUNTIME_REGISTRY, None)
    if not isinstance(registry, Mapping):
        return ()
    entry = registry.get(_request_key(request_ref))
    if not isinstance(entry, Mapping):
        return ()
    frames = entry.get("frames")
    freshness = entry.get("freshness_seconds")
    if not isinstance(frames, tuple) or type(freshness) is not int or freshness < 1:
        return ()
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("current time must be timezone-aware")
    result = []
    for frame in frames:
        try:
            captured = datetime.fromisoformat(str(frame["captured_at"]).replace("Z", "+00:00"))
            if captured.tzinfo is None:
                continue
            age = (current - captured).total_seconds()
            if -3 <= age <= freshness:
                result.append(_TimedLiveFrame(frame, captured, freshness))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return tuple(result)


def discard_live_frames(runtime: Any, request_id: str) -> None:
    registry = getattr(runtime, _RUNTIME_REGISTRY, None)
    if isinstance(registry, dict):
        registry.pop(_request_key(request_id), None)


def clear_live_frames(runtime: Any) -> None:
    registry = getattr(runtime, _RUNTIME_REGISTRY, None)
    if isinstance(registry, dict):
        registry.clear()


def live_frame_facts(
    frames: Sequence[Mapping[str, Any]], *, window_seconds: float = LIVE_FRAME_WINDOW_SECONDS
) -> dict[str, Any]:
    if not frames:
        return {"count": 0, "window_seconds": window_seconds}
    return {
        "count": len(frames),
        "window_seconds": window_seconds,
        "oldest_captured_at": str(frames[0].get("captured_at") or ""),
        "newest_captured_at": str(frames[-1].get("captured_at") or ""),
        "oldest_sequence": frames[0].get("frame_sequence"),
        "newest_sequence": frames[-1].get("frame_sequence"),
        "ordering": "oldest_to_newest",
    }
