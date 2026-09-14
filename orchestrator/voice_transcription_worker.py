"""Isolated native-dependency worker for local voice transcription.

The parent Function Worker sends versioned JSON lines over stdio. This helper
runs in a separately prepared Python environment, loads faster-whisper lazily,
and never grants Tool or HASHI runtime authority to that environment.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
RESULT_PREFIX = "HASHI_VOICE_TRANSCRIPTION_RESULT="
MAX_OUTPUT_BYTES = 1_048_576
_MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger = logging.getLogger("VoiceTranscriptionWorker")


def _detect_device() -> tuple[str, str]:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", "float16"
    except ImportError:
        pass
    try:
        import ctranslate2

        if "cuda" in ctranslate2.get_supported_compute_types("cuda"):
            return "cuda", "float16"
    except Exception:
        logger.debug("CTranslate2 CUDA detection failed", exc_info=True)
    return "cpu", "int8"


class _ModelRuntime:
    def __init__(self) -> None:
        self._models: dict[tuple[str, str | None], tuple[Any, str, str]] = {}

    def _model(self, model_size: str, language: str | None):
        key = (model_size, language)
        cached = self._models.get(key)
        if cached is not None:
            return cached
        from faster_whisper import WhisperModel

        device, compute_type = _detect_device()
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        cached = (model, device, compute_type)
        self._models[key] = cached
        return cached

    def transcribe(
        self,
        audio_path: Path,
        *,
        model_size: str,
        language: str | None,
    ) -> dict[str, Any]:
        model, device, compute_type = self._model(model_size, language)
        segments, info = model.transcribe(
            str(audio_path),
            language=language,
            beam_size=5,
            vad_filter=True,
        )
        text = " ".join(
            str(segment.text or "").replace("\x00", "").strip()
            for segment in segments
        ).strip()
        if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES // 2:
            raise ValueError("transcription text exceeded its protocol limit")
        return {
            "text": text,
            "device": device,
            "compute_type": compute_type,
            "language": str(getattr(info, "language", "") or ""),
            "language_probability": float(
                getattr(info, "language_probability", 0.0) or 0.0
            ),
            "duration": float(getattr(info, "duration", 0.0) or 0.0),
        }


def _request_payload(raw: str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    if value.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    request_id = str(value.get("id") or "")
    if not request_id or len(request_id) > 128:
        raise ValueError("invalid request ID")
    model_size = str(value.get("model_size") or "")
    if not _MODEL_NAME.fullmatch(model_size):
        raise ValueError("invalid model size")
    language_value = value.get("language")
    if language_value is not None and not isinstance(language_value, str):
        raise ValueError("language must be a string or null")
    language = str(language_value).strip() if language_value is not None else None
    if language and (len(language) > 32 or not _MODEL_NAME.fullmatch(language)):
        raise ValueError("invalid language")
    audio_path = Path(str(value.get("audio_path") or "")).resolve()
    if not audio_path.is_file():
        raise ValueError("audio file is unavailable")
    return {
        "id": request_id,
        "audio_path": audio_path,
        "model_size": model_size,
        "language": language or None,
    }


def _serve() -> int:
    runtime = _ModelRuntime()
    for raw in sys.stdin:
        request_id = ""
        try:
            request = _request_payload(raw)
            request_id = request["id"]
            result = runtime.transcribe(
                request["audio_path"],
                model_size=request["model_size"],
                language=request["language"],
            )
            payload = {
                "version": PROTOCOL_VERSION,
                "id": request_id,
                "ok": True,
                **result,
            }
        except Exception as exc:
            logger.exception("Isolated transcription request failed")
            payload = {
                "version": PROTOCOL_VERSION,
                "id": request_id,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:500],
            }
        print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args(argv)
    if not args.serve:
        parser.error("--serve is required")
    return _serve()


if __name__ == "__main__":
    raise SystemExit(main())
