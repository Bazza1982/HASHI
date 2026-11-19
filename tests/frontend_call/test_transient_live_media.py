from types import SimpleNamespace
from datetime import datetime, timedelta

import pytest

from orchestrator.frontend_call.transient_live_media import (
    TRANSIENT_LIVE_FRAMES_METADATA_KEY,
    bind_live_frames,
    discard_live_frames,
    encode_live_frames,
    live_frames_for,
    pop_live_frames_metadata,
)


JPEG = bytes.fromhex("ffd8ffc00008080010001000ffd9")


def _frames():
    return (
        {
            "image": JPEG,
            "frame_sequence": 7,
            "captured_at": "2026-10-17T06:20:00+11:00",
            "captured_unix": 1792182000.0,
        },
        {
            "image": JPEG,
            "frame_sequence": 9,
            "captured_at": "2026-10-17T06:20:01+11:00",
            "captured_unix": 1792182001.0,
        },
    )


def test_live_frames_are_removed_from_metadata_before_persistence_and_bound_in_memory():
    metadata = {"ordinary": "safe"}
    metadata[TRANSIENT_LIVE_FRAMES_METADATA_KEY] = encode_live_frames(_frames())

    frames = pop_live_frames_metadata(metadata)

    assert TRANSIENT_LIVE_FRAMES_METADATA_KEY not in metadata
    assert [frame["frame_sequence"] for frame in frames] == [7, 9]
    assert all(
        frame["data_url"].startswith("data:image/jpeg;base64,") for frame in frames
    )

    runtime = SimpleNamespace()
    bind_live_frames(runtime, "req-live", frames, freshness_seconds=20)
    at_capture = datetime.fromisoformat("2026-10-17T06:20:02+11:00")
    assert live_frames_for(runtime, "hashi-request:req-live", now=at_capture) == frames
    discard_live_frames(runtime, "req-live")
    assert live_frames_for(runtime, "req-live") == ()


def test_live_frame_metadata_rejects_more_than_the_turn_contract():
    encoded = encode_live_frames(_frames())
    metadata = {
        TRANSIENT_LIVE_FRAMES_METADATA_KEY: {
            "type": "hashi.transient-live-frames",
            "version": 1,
            "scope": "current_input_only",
            "frames": [encoded["frames"][0]] * 11,
        }
    }

    with pytest.raises(ValueError, match="at most 10"):
        pop_live_frames_metadata(metadata)


def test_frames_expire_at_provider_lookup_using_frozen_budget():
    frames = pop_live_frames_metadata(
        {TRANSIENT_LIVE_FRAMES_METADATA_KEY: encode_live_frames(_frames())}
    )
    runtime = SimpleNamespace()
    bind_live_frames(runtime, "req-live", frames, freshness_seconds=2)
    start = datetime.fromisoformat("2026-10-17T06:20:02+11:00")
    assert [
        frame["frame_sequence"]
        for frame in live_frames_for(runtime, "req-live", now=start)
    ] == [7, 9]
    assert [
        frame["frame_sequence"]
        for frame in live_frames_for(
            runtime, "req-live", now=start + timedelta(seconds=1)
        )
    ] == [9]
    assert live_frames_for(runtime, "req-live", now=start + timedelta(seconds=2)) == ()
    assert live_frames_for(runtime, "other-request", now=start) == ()


def test_normalized_frame_preserves_original_clock_and_uncertainty():
    frame = dict(_frames()[0])
    frame["capture_clock"] = {
        "basis": "server_roundtrip_lower_bound",
        "client_captured_at": "2026-10-17T06:22:00+11:00",
        "uncertainty_ms": 400,
    }
    result = pop_live_frames_metadata(
        {TRANSIENT_LIVE_FRAMES_METADATA_KEY: encode_live_frames((frame,))}
    )
    assert result[0]["capture_clock"] == frame["capture_clock"]
    with pytest.raises(ValueError, match="capture clock"):
        encode_live_frames(
            (
                {
                    **frame,
                    "capture_clock": {**frame["capture_clock"], "uncertainty_ms": -1},
                },
            )
        )


def test_selected_live_frame_capacity_can_exceed_legacy_ten():
    first = _frames()[0]
    frames = tuple(
        {
            **first,
            "frame_sequence": index,
            "captured_at": (
                datetime.fromisoformat(first["captured_at"]) + timedelta(seconds=index)
            ).isoformat(),
        }
        for index in range(1, 16)
    )
    encoded = encode_live_frames(frames, max_frames=20)
    with pytest.raises(ValueError, match="at most 10"):
        pop_live_frames_metadata({TRANSIENT_LIVE_FRAMES_METADATA_KEY: encoded})
    decoded = pop_live_frames_metadata(
        {TRANSIENT_LIVE_FRAMES_METADATA_KEY: encoded}, max_frames=20
    )
    assert len(decoded) == 15 and [row["frame_sequence"] for row in decoded] == list(
        range(1, 16)
    )
