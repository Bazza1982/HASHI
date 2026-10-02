"""Offline speech engines for the optional Cascade Phone sidecar.

This module is imported by the isolated sidecar only. Model packages are loaded
lazily, and model weights must already exist on this machine.
"""
from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

MAX_OUTPUT_PCM_BYTES = 48000 * 2 * 60


class LocalCascadeSpeech:
    def __init__(self, *, stt_model: str, tts_model: str, language: str | None = None):
        self.stt_model = stt_model
        self.tts_model = Path(tts_model)
        self.language = language
        self._recognizer = None
        self._synthesizer = None
        self._prepared = False
        self._stt_lock = threading.Lock()
        self._tts_lock = threading.Lock()

    def ready(self) -> bool:
        return self._prepared

    def warmup(self) -> None:
        available = (
            bool(self.stt_model)
            and self.tts_model.is_file()
            and self.tts_model.with_suffix(self.tts_model.suffix + ".json").is_file()
            and all(importlib.util.find_spec(name) is not None for name in ("faster_whisper", "numpy", "piper", "av"))
        )
        if not available:
            raise RuntimeError("Cascade speech dependencies or model files are missing")
        from faster_whisper import WhisperModel
        from piper.voice import PiperVoice

        with self._stt_lock:
            if self._recognizer is None:
                self._recognizer = WhisperModel(
                    self.stt_model, device="cpu", compute_type="int8", local_files_only=True,
                )
        with self._tts_lock:
            if self._synthesizer is None:
                self._synthesizer = PiperVoice.load(self.tts_model)
        self._prepared = True

    def transcribe(self, pcm16: bytes) -> str:
        """Return one final utterance from 16 kHz mono signed PCM."""
        if len(pcm16) < 3200 or len(pcm16) % 2:
            return ""
        import numpy as np

        audio = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        if not self._prepared:
            self.warmup()
        with self._stt_lock:
            segments, _ = self._recognizer.transcribe(
                audio, language=self.language, beam_size=2, vad_filter=False,
                condition_on_previous_text=False,
            )
            return " ".join(segment.text.strip() for segment in segments if segment.text.strip()).strip()

    def synthesize_pcm48(self, text: str) -> bytes:
        """Render text to 48 kHz mono signed PCM for the WebRTC output track."""
        if not text.strip():
            return b""
        from av import AudioFrame, AudioResampler

        if not self._prepared:
            self.warmup()
        with self._tts_lock:
            resampler = AudioResampler(format="s16", layout="mono", rate=48000)
            output = bytearray()
            for chunk in self._synthesizer.synthesize(text):
                raw = chunk.audio_int16_bytes
                frame = AudioFrame(format="s16", layout="mono", samples=len(raw) // 2)
                frame.sample_rate = chunk.sample_rate
                frame.planes[0].update(raw)
                for resampled in resampler.resample(frame):
                    output.extend(bytes(resampled.planes[0])[: resampled.samples * 2])
                    if len(output) > MAX_OUTPUT_PCM_BYTES:
                        raise RuntimeError("Cascade speech exceeds the output duration limit")
            for resampled in resampler.resample(None):
                output.extend(bytes(resampled.planes[0])[: resampled.samples * 2])
                if len(output) > MAX_OUTPUT_PCM_BYTES:
                    raise RuntimeError("Cascade speech exceeds the output duration limit")
            return bytes(output)
