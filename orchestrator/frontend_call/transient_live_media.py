"""Current-turn live camera frames that never enter Session persistence."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from datetime import datetime
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


def encode_live_frames(frames: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Encode an already-selected turn snapshot for one Function IPC hop."""

    if len(frames) > LIVE_FRAME_LIMIT:
        raise ValueError(f"live frame batch accepts at most {LIVE_FRAME_LIMIT} frames")
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
            }
        )
    return {
        "type": TRANSIENT_LIVE_FRAMES_TYPE,
        "version": 1,
        "scope": "current_input_only",
        "frames": encoded,
    }


def pop_live_frames_metadata(metadata: dict[str, Any]) -> tuple[dict[str, Any], ...]:
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
    if len(rows) > LIVE_FRAME_LIMIT:
        raise ValueError(f"live frame batch accepts at most {LIVE_FRAME_LIMIT} frames")
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
            }
        )
        previous_sequence = sequence
        previous_stamp = stamp
    return tuple(frames)


def bind_live_frames(
    runtime: Any, request_id: str, frames: Sequence[Mapping[str, Any]]
) -> None:
    key = _request_key(request_id)
    if not key or not frames:
        return
    registry = getattr(runtime, _RUNTIME_REGISTRY, None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(runtime, _RUNTIME_REGISTRY, registry)
    registry[key] = tuple(dict(frame) for frame in frames)


def live_frames_for(runtime: Any, request_ref: str) -> tuple[dict[str, Any], ...]:
    registry = getattr(runtime, _RUNTIME_REGISTRY, None)
    if not isinstance(registry, Mapping):
        return ()
    frames = registry.get(_request_key(request_ref))
    if not isinstance(frames, tuple):
        return ()
    return tuple(dict(frame) for frame in frames)


def discard_live_frames(runtime: Any, request_id: str) -> None:
    registry = getattr(runtime, _RUNTIME_REGISTRY, None)
    if isinstance(registry, dict):
        registry.pop(_request_key(request_id), None)


def clear_live_frames(runtime: Any) -> None:
    registry = getattr(runtime, _RUNTIME_REGISTRY, None)
    if isinstance(registry, dict):
        registry.clear()


def live_frame_facts(frames: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not frames:
        return {"count": 0, "window_seconds": LIVE_FRAME_WINDOW_SECONDS}
    return {
        "count": len(frames),
        "window_seconds": LIVE_FRAME_WINDOW_SECONDS,
        "oldest_captured_at": str(frames[0].get("captured_at") or ""),
        "newest_captured_at": str(frames[-1].get("captured_at") or ""),
        "oldest_sequence": frames[0].get("frame_sequence"),
        "newest_sequence": frames[-1].get("frame_sequence"),
        "ordering": "oldest_to_newest",
    }
