"""Edge TTS client for an isolated instance-owned helper interpreter."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestrator import voice_synthesis_worker

logger = logging.getLogger("VoiceSynthesisRuntime")

TTS_RESULT_PREFIX = voice_synthesis_worker.RESULT_PREFIX
TTS_CONFIG_SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 120.0


class TTSRuntimeError(RuntimeError):
    """The isolated speech runtime is unavailable or returned no result."""


def _absolute_path(value: str | os.PathLike[str]) -> Path:
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
        raise TTSRuntimeError("Isolated TTS runtime Python does not exist")
    active_executable = _absolute_path(sys.executable)
    active_prefix = _absolute_path(sys.prefix)
    if candidate == active_executable or _inside(candidate, active_prefix):
        raise TTSRuntimeError(
            "TTS Python must be isolated from the active HASHI runtime"
        )
    return candidate


def _configured_tts_python(bridge_home: Path) -> str:
    config_path = bridge_home / "state" / "platform" / "tts.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return ""
    except (OSError, json.JSONDecodeError) as exc:
        raise TTSRuntimeError("TTS platform configuration is unreadable") from exc
    if not isinstance(payload, dict):
        raise TTSRuntimeError("TTS platform configuration must be an object")
    if payload.get("schema_version") != TTS_CONFIG_SCHEMA_VERSION:
        raise TTSRuntimeError("TTS platform configuration schema is unsupported")
    python = payload.get("python", "")
    if not isinstance(python, str):
        raise TTSRuntimeError("TTS platform Python must be a string")
    return python.strip()


def resolve_tts_python() -> Path | None:
    """Resolve the helper interpreter without falling back to Core."""

    override = str(os.environ.get("HASHI_TTS_PYTHON") or "").strip()
    if override:
        return _validate_isolated_python(override)
    bridge_home_value = str(os.environ.get("BRIDGE_HOME") or "").strip()
    if not bridge_home_value:
        return None
    configured = _configured_tts_python(_absolute_path(bridge_home_value))
    return _validate_isolated_python(configured) if configured else None


def isolated_tts_configured() -> bool:
    try:
        return resolve_tts_python() is not None
    except TTSRuntimeError:
        return False


def _result_payload(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    result_line = next(
        (
            line[len(TTS_RESULT_PREFIX) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(TTS_RESULT_PREFIX)
        ),
        None,
    )
    if result_line is None:
        raise TTSRuntimeError("Isolated TTS worker returned no result")
    try:
        payload = json.loads(result_line)
    except json.JSONDecodeError as exc:
        raise TTSRuntimeError("Isolated TTS worker returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise TTSRuntimeError("Isolated TTS worker returned an invalid result")
    if completed.returncode != 0 or payload.get("status") == "error":
        detail = str(payload.get("error") or "isolated TTS worker failed")
        raise TTSRuntimeError(detail[:500])
    return payload


def synthesize_edge_to_mp3(
    text: str,
    *,
    output_path: Path,
    voice: str,
    rate: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> None:
    python = resolve_tts_python()
    if python is None:
        raise TTSRuntimeError("Isolated TTS runtime is unavailable")
    worker_path = Path(voice_synthesis_worker.__file__).resolve()
    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    for inherited in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(inherited, None)
    environment["PYTHONNOUSERSITE"] = "1"
    try:
        completed = subprocess.run(
            [
                str(python),
                "-I",
                str(worker_path),
                "--output",
                str(output_path),
                "--voice",
                str(voice),
                "--rate",
                str(rate),
            ],
            cwd=str(worker_path.parent.parent),
            input=str(text),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=float(timeout_seconds),
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        output_path.unlink(missing_ok=True)
        raise TTSRuntimeError(
            f"Speech synthesis exceeded the {timeout_seconds:g} second limit"
        ) from exc
    except OSError as exc:
        output_path.unlink(missing_ok=True)
        raise TTSRuntimeError("Isolated TTS worker could not start") from exc
    if completed.stderr.strip():
        logger.debug("Isolated TTS worker stderr: %s", completed.stderr.strip()[-2000:])
    _result_payload(completed)
    if not output_path.is_file() or output_path.stat().st_size < 100:
        output_path.unlink(missing_ok=True)
        raise TTSRuntimeError("Isolated TTS worker produced no valid audio")
