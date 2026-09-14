"""Local speech-to-text through an isolated faster-whisper runtime.

The long-lived HASHI Core and Function Workers must keep the exact dependency
set recorded by the Core runtime contract. Native speech dependencies are
therefore loaded only by a short-lived helper interpreter selected from the
instance platform configuration. A transcription feature install can never
change the active Core/Worker environment or its ``/reboot min`` fingerprint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator import voice_transcription_worker

logger = logging.getLogger("VoiceTranscriber")

DEFAULT_MODEL_SIZE = "small"
DEFAULT_LANGUAGE = None
DEFAULT_TIMEOUT_SECONDS = 900.0
TRANSCRIPTION_RESULT_PREFIX = voice_transcription_worker.RESULT_PREFIX
TRANSCRIPTION_CONFIG_SCHEMA_VERSION = 1


class TranscriptionRuntimeError(RuntimeError):
    """The isolated transcription runtime is unavailable or returned no result."""


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    """Return an absolute path without resolving a venv's Python symlink."""

    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_isolated_python(value: str | os.PathLike[str]) -> Path:
    candidate = _absolute_path(value)
    if not candidate.is_file():
        raise TranscriptionRuntimeError(
            "Isolated transcription runtime Python does not exist"
        )

    active_executable = _absolute_path(sys.executable)
    active_prefix = _absolute_path(sys.prefix)
    if candidate == active_executable or _inside(candidate, active_prefix):
        raise TranscriptionRuntimeError(
            "Transcription Python must be isolated from the active HASHI runtime"
        )
    return candidate


def _configured_transcription_python(bridge_home: Path) -> str:
    config_path = bridge_home / "state" / "platform" / "transcription.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscriptionRuntimeError(
            "Transcription platform configuration is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise TranscriptionRuntimeError(
            "Transcription platform configuration must be an object"
        )
    if payload.get("schema_version") != TRANSCRIPTION_CONFIG_SCHEMA_VERSION:
        raise TranscriptionRuntimeError(
            "Transcription platform configuration schema is unsupported"
        )
    python = payload.get("python", "")
    if not isinstance(python, str):
        raise TranscriptionRuntimeError(
            "Transcription platform Python must be a string"
        )
    return python.strip()


def resolve_transcription_python() -> Path | None:
    """Resolve the configured helper interpreter without falling back to Core."""

    override = str(os.environ.get("HASHI_TRANSCRIPTION_PYTHON") or "").strip()
    if override:
        return _validate_isolated_python(override)

    bridge_home_value = str(os.environ.get("BRIDGE_HOME") or "").strip()
    if not bridge_home_value:
        return None
    configured = _configured_transcription_python(_absolute_path(bridge_home_value))
    return _validate_isolated_python(configured) if configured else None


def _result_payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    result_line = next(
        (
            line[len(TRANSCRIPTION_RESULT_PREFIX) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(TRANSCRIPTION_RESULT_PREFIX)
        ),
        None,
    )
    if result_line is None:
        raise TranscriptionRuntimeError(
            "Isolated transcription worker returned no result"
        )
    try:
        payload = json.loads(result_line)
    except json.JSONDecodeError as exc:
        raise TranscriptionRuntimeError(
            "Isolated transcription worker returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise TranscriptionRuntimeError(
            "Isolated transcription worker returned an invalid result"
        )
    if completed.returncode != 0 or payload.get("status") == "error":
        detail = str(payload.get("error") or "isolated transcription worker failed")
        raise TranscriptionRuntimeError(detail[:500])
    return payload


class VoiceTranscriber:
    """Serialize local transcription calls through an isolated helper process."""

    def __init__(
        self,
        model_size: str = DEFAULT_MODEL_SIZE,
        language: str | None = DEFAULT_LANGUAGE,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.model_size = model_size
        self.language = language
        self.timeout_seconds = timeout_seconds
        # These attributes remain for the existing runtime settings surface.
        # The model itself belongs exclusively to the helper process.
        self._model = None
        self._device = None
        self._compute_type = None
        self._lock = asyncio.Lock()

    def _transcribe_isolated(self, audio_path: Path) -> str:
        python = resolve_transcription_python()
        if python is None:
            raise TranscriptionRuntimeError(
                "Isolated transcription runtime is unavailable"
            )

        worker_path = Path(voice_transcription_worker.__file__).resolve()
        command = [
            str(python),
            "-I",
            str(worker_path),
            "--audio",
            str(audio_path.resolve()),
            "--model-size",
            str(self.model_size),
        ]
        if self.language:
            command.extend(("--language", str(self.language)))

        environment = dict(os.environ)
        for inherited in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
            environment.pop(inherited, None)
        environment["PYTHONNOUSERSITE"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=str(worker_path.parent.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise TranscriptionRuntimeError(
                f"Transcription exceeded the {self.timeout_seconds:g} second limit"
            ) from exc
        except OSError as exc:
            raise TranscriptionRuntimeError(
                "Isolated transcription worker could not start"
            ) from exc

        if completed.stderr.strip():
            logger.debug(
                "Isolated transcription worker stderr: %s",
                completed.stderr.strip()[-2000:],
            )
        payload = _result_payload(completed)
        self._device = str(payload.get("device") or "") or None
        self._compute_type = str(payload.get("compute_type") or "") or None
        return str(payload.get("text") or "").strip()

    async def transcribe(self, audio_path: str | Path) -> str:
        """Transcribe audio without importing speech packages into HASHI."""

        path = Path(audio_path)
        if not path.exists():
            return f"[Transcription error] File not found: {path}"
        try:
            async with self._lock:
                return await asyncio.to_thread(self._transcribe_isolated, path)
        except Exception as exc:
            logger.error("Transcription failed for %s: %s", path, exc, exc_info=True)
            return f"[Transcription error] {exc}"


_instance: VoiceTranscriber | None = None


def get_transcriber(
    model_size: str = DEFAULT_MODEL_SIZE,
    language: str | None = DEFAULT_LANGUAGE,
) -> VoiceTranscriber:
    """Get or create the shared isolated-runtime client."""

    global _instance
    if _instance is None:
        _instance = VoiceTranscriber(model_size=model_size, language=language)
    return _instance
