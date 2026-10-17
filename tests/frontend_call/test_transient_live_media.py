from types import SimpleNamespace

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
    bind_live_frames(runtime, "req-live", frames)
    assert live_frames_for(runtime, "hashi-request:req-live") == frames
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
