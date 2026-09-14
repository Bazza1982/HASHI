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

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from orchestrator import voice_transcription_worker

logger = logging.getLogger("VoiceTranscriber")

# Defaults — can be overridden via GlobalConfig / agents.json
DEFAULT_MODEL_SIZE = "small"
DEFAULT_LANGUAGE = None  # None = auto-detect
EXTERNAL_TRANSCRIPTION_TIMEOUT_SECONDS = 900.0


def _external_python() -> str:
    """Resolve an optional isolated transcription interpreter.

    Native transcription packages are Function dependencies.  Instances that
    keep them outside the stable HASHI runtime point at that environment here;
    no package from it is imported into the Function Worker.
    """

    override = str(os.environ.get("HASHI_TRANSCRIPTION_PYTHON") or "").strip()
    if override:
        return override
    bridge_home = str(os.environ.get("BRIDGE_HOME") or "").strip()
    if not bridge_home:
        return ""
    config_path = Path(bridge_home) / "state" / "platform" / "transcription.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("transcription platform configuration is unreadable") from exc
    if not isinstance(config, dict):
        raise ValueError("transcription platform configuration must be an object")
    python = config.get("python", "")
    if not isinstance(python, str):
        raise ValueError("transcription platform python must be a string")
    return python.strip()


class VoiceTranscriber:
    """Singleton-style local Whisper transcriber with lazy model loading."""

    def __init__(self, model_size: str = DEFAULT_MODEL_SIZE, language: str | None = DEFAULT_LANGUAGE):
        self.model_size = model_size
        self.language = language
        self._model = None
        self._device = None
        self._compute_type = None
        self._lock = asyncio.Lock()
        self._external_process = None
        self._external_process_python = ""
        self._external_request_sequence = 0

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

        try:
            external_python = _external_python()
            if external_python:
                return await self._transcribe_external(audio_path, external_python)
            loop = asyncio.get_running_loop()
            async with self._lock:
                # Lazy load on first call
                if self._model is None:
                    await loop.run_in_executor(None, self._load_model)
            text = await loop.run_in_executor(None, self._transcribe_sync, str(audio_path))
            return text
        except Exception as e:
            logger.error(f"Transcription failed for {audio_path}: {e}", exc_info=True)
            return f"[Transcription error] {e}"

    def _detach_external_process(self):
        process = self._external_process
        self._external_process = None
        self._external_process_python = ""
        return process

    async def aclose(self) -> None:
        """Close the isolated helper and wait for its stdio transports."""

        process = self._detach_external_process()
        if process is None:
            return
        stdin = process.stdin
        if stdin is not None:
            stdin.close()
            wait_closed = getattr(stdin, "wait_closed", None)
            if callable(wait_closed):
                try:
                    await wait_closed()
                except (BrokenPipeError, ConnectionError):
                    pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.terminate()
                await process.wait()

    async def _ensure_external_process(self, python: str):
        process = self._external_process
        if (
            process is not None
            and process.returncode is None
            and self._external_process_python == python
        ):
            return process
        await self.aclose()
        worker_path = Path(voice_transcription_worker.__file__).resolve()
        process = await asyncio.create_subprocess_exec(
            python,
            "-I",
            str(worker_path),
            "--serve",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(worker_path.parent.parent),
        )
        if process.stdin is None or process.stdout is None:
            process.terminate()
            raise RuntimeError("isolated transcription worker has no JSON channel")
        self._external_process = process
        self._external_process_python = python
        return process

    async def _read_external_result(self, process, request_id: str) -> dict:
        total_bytes = 0
        for _ in range(1000):
            raw = await process.stdout.readline()
            if not raw:
                raise RuntimeError("isolated transcription worker exited without a result")
            total_bytes += len(raw)
            if total_bytes > voice_transcription_worker.MAX_OUTPUT_BYTES:
                raise RuntimeError("isolated transcription worker output exceeded its limit")
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith(voice_transcription_worker.RESULT_PREFIX):
                continue
            try:
                payload = json.loads(line[len(voice_transcription_worker.RESULT_PREFIX) :])
            except json.JSONDecodeError as exc:
                raise RuntimeError("isolated transcription worker returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("isolated transcription worker returned an invalid result")
            if payload.get("version") != voice_transcription_worker.PROTOCOL_VERSION:
                raise RuntimeError("isolated transcription worker protocol does not match")
            if str(payload.get("id") or "") != request_id:
                raise RuntimeError("isolated transcription worker response ID does not match")
            return payload
        raise RuntimeError("isolated transcription worker returned too many log lines")

    async def _transcribe_external(self, audio_path: Path, python: str) -> str:
        async with self._lock:
            process = await self._ensure_external_process(python)
            self._external_request_sequence += 1
            request_id = str(self._external_request_sequence)
            request = {
                "version": voice_transcription_worker.PROTOCOL_VERSION,
                "id": request_id,
                "audio_path": str(audio_path.resolve()),
                "model_size": self.model_size,
                "language": self.language,
            }
            try:
                process.stdin.write(
                    (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
                )
                await process.stdin.drain()
                payload = await asyncio.wait_for(
                    self._read_external_result(process, request_id),
                    timeout=EXTERNAL_TRANSCRIPTION_TIMEOUT_SECONDS,
                )
            except Exception:
                await self.aclose()
                raise
        if not payload.get("ok"):
            raise RuntimeError(
                str(payload.get("error") or "isolated transcription worker failed")[:500]
            )
        self._device = str(payload.get("device") or "") or None
        self._compute_type = str(payload.get("compute_type") or "") or None
        logger.info(
            "Isolated transcription complete: device=%s language=%s duration=%.1fs",
            self._device or "unknown",
            str(payload.get("language") or "unknown"),
            float(payload.get("duration") or 0.0),
        )
        return str(payload.get("text") or "").strip()

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
