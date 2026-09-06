#!/usr/bin/env python3
"""Generate the tiny, original PCM assets used by the TUI Soft Chat profile."""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Callable
from pathlib import Path

SAMPLE_RATE = 44_100
ASSET_ROOT = Path(__file__).resolve().parents[2] / "tui" / "assets" / "sounds"


def _rounded_envelope(position: float, *, attack: float, release_power: float) -> float:
    attack_gain = min(1.0, position / attack)
    return math.sin(attack_gain * math.pi / 2) * (1.0 - position) ** release_power


def _send_sample(time_s: float) -> float:
    duration = 0.085
    position = time_s / duration
    if not 0.0 <= position < 1.0:
        return 0.0
    # A rounded upward chirp: more bubble than bell, with a very quiet octave.
    phase = 2 * math.pi * duration * (
        470 * position + 330 * position**1.75 / 1.75
    )
    envelope = _rounded_envelope(position, attack=0.055, release_power=2.4)
    return envelope * (math.sin(phase) + 0.10 * math.sin(2 * phase + 0.25))


def _bell_tone(
    time_s: float,
    *,
    start_s: float,
    duration_s: float,
    frequency: float,
    amplitude: float,
) -> float:
    position = (time_s - start_s) / duration_s
    if not 0.0 <= position < 1.0:
        return 0.0
    phase = 2 * math.pi * frequency * (time_s - start_s)
    envelope = _rounded_envelope(position, attack=0.06, release_power=1.9)
    colour = (
        math.sin(phase)
        + 0.13 * math.sin(2.01 * phase + 0.4)
        + 0.035 * math.sin(3.98 * phase + 0.8)
    )
    return amplitude * envelope * colour


def _receive_sample(time_s: float) -> float:
    # A soft overlapping major-fifth lift, short enough to feel like a message.
    return _bell_tone(
        time_s,
        start_s=0.0,
        duration_s=0.115,
        frequency=659.255,
        amplitude=0.68,
    ) + _bell_tone(
        time_s,
        start_s=0.062,
        duration_s=0.128,
        frequency=987.767,
        amplitude=1.0,
    )


def _write_wave(
    path: Path,
    *,
    duration_s: float,
    sampler: Callable[[float], float],
    peak: float,
) -> None:
    frame_count = round(duration_s * SAMPLE_RATE)
    values = [sampler(index / SAMPLE_RATE) for index in range(frame_count)]
    maximum = max(abs(value) for value in values)
    scale = peak / maximum
    frames = b"".join(
        struct.pack("<h", round(max(-1.0, min(1.0, value * scale)) * 32_767))
        for value in values
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(frames)


def main() -> None:
    _write_wave(
        ASSET_ROOT / "soft_chat_send.wav",
        duration_s=0.085,
        sampler=_send_sample,
        peak=0.20,
    )
    _write_wave(
        ASSET_ROOT / "soft_chat_receive.wav",
        duration_s=0.190,
        sampler=_receive_sample,
        peak=0.23,
    )


if __name__ == "__main__":
    main()
