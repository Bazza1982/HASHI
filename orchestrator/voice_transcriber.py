from __future__ import annotations
"""
Local voice-to-text transcription using faster-whisper.

Provides GPU-accelerated (CUDA) or CPU-based speech-to-text for all agents.
Voice/audio messages from Telegram are transcribed locally before dispatch
to any backend, so every backend gets plain text regardless of whether it
supports audio files natively.

Device selection priority:
  1. CUDA GPU  (if available — requires nvidia GPU + CUDA toolkit)
  2. CPU       (automatic fallback — fast on modern AMD/Intel chips)

Model is loaded lazily on first transcription and kept in memory for
subsequent calls.
"""

import logging
import asyncio
from pathlib import Path
from functools import lru_cache

logger = logging.getLogger("VoiceTranscriber")

# Defaults — can be overridden via GlobalConfig / agents.json
DEFAULT_MODEL_SIZE = "small"
DEFAULT_LANGUAGE = None  # None = auto-detect


class VoiceTranscriber:
    """Singleton-style local Whisper transcriber with lazy model loading."""

    def __init__(self, model_size: str = DEFAULT_MODEL_SIZE, language: str | None = DEFAULT_LANGUAGE):
        self.model_size = model_size
        self.language = language
        self._model = None
        self._device = None
        self._compute_type = None
        self._lock = asyncio.Lock()

    def _load_model(self):
        """Load the faster-whisper model. Called once on first use."""
        if self._model is not None:
            return

        from faster_whisper import WhisperModel

        # Detect best available device
        device, compute_type = self._detect_device()
        self._device = device
        self._compute_type = compute_type

        logger.info(
            f"Loading Whisper model '{self.model_size}' on {device} "
            f"(compute_type={compute_type})..."
        )
        self._model = WhisperModel(
            self.model_size,
            device=device,
            compute_type=compute_type,
        )
        logger.info(f"Whisper model loaded successfully on {device}.")

    @staticmethod
    def _detect_device() -> tuple[str, str]:
        """Detect best available compute device."""
        try:
            import torch
            if torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                logger.info(f"CUDA GPU detected: {gpu_name}")
                return "cuda", "float16"
        except ImportError:
            pass

        # CTranslate2 (used by faster-whisper) can also check CUDA directly
        try:
            import ctranslate2
            if "cuda" in ctranslate2.get_supported_compute_types("cuda"):
                logger.info("CUDA available via CTranslate2")
                return "cuda", "float16"
        except Exception:
            pass

        logger.info("No CUDA GPU found, using CPU for Whisper inference.")
        return "cpu", "int8"

    async def transcribe(self, audio_path: str | Path) -> str:
        """
        Transcribe an audio file to text.

        Runs the model in a thread executor to avoid blocking the event loop.
        Returns the transcribed text, or an error message string prefixed
        with [Transcription error] on failure.
        """
        audio_path = Path(audio_path)
        if not audio_path.exists():
            return f"[Transcription error] File not found: {audio_path}"

        async with self._lock:
            # Lazy load on first call
            if self._model is None:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._load_model)

        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, self._transcribe_sync, str(audio_path))
            return text
        except Exception as e:
            logger.error(f"Transcription failed for {audio_path}: {e}", exc_info=True)
            return f"[Transcription error] {e}"

    def _transcribe_sync(self, audio_path: str) -> str:
        """Synchronous transcription (runs in executor thread)."""
        segments, info = self._model.transcribe(
            audio_path,
            language=self.language,
            beam_size=5,
            vad_filter=True,  # skip silence for faster processing
        )
        detected_lang = info.language
        lang_prob = info.language_probability
        logger.info(
            f"Transcribing {audio_path}: detected_language={detected_lang} "
            f"(probability={lang_prob:.2f}), duration={info.duration:.1f}s"
        )

        parts = []
        for segment in segments:
            parts.append(segment.text.strip())

        text = " ".join(parts).strip()
        logger.info(f"Transcription complete: {len(text)} chars")
        return text


# Module-level singleton — shared by all agents
_instance: VoiceTranscriber | None = None


def get_transcriber(model_size: str = DEFAULT_MODEL_SIZE, language: str | None = DEFAULT_LANGUAGE) -> VoiceTranscriber:
    """Get or create the shared VoiceTranscriber instance."""
    global _instance
    if _instance is None:
        _instance = VoiceTranscriber(model_size=model_size, language=language)
    return _instance
