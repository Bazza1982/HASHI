"""Short-lived faster-whisper worker for HASHI voice transcription.

This module deliberately imports only the standard library at module scope so
Function-generation qualification remains independent of native speech
packages. Heavy packages are imported only after this file starts under the
instance's isolated transcription interpreter.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

RESULT_PREFIX = "HASHI_TRANSCRIPTION_RESULT="
REQUIRED_DISTRIBUTIONS = ("faster-whisper", "ctranslate2", "av")


def _package_versions() -> dict[str, str]:
    # Importing all three modules verifies that their native extensions load,
    # while metadata supplies stable distribution-version names.
    import av  # noqa: F401
    import ctranslate2  # noqa: F401
    import faster_whisper  # noqa: F401

    return {
        name: importlib.metadata.version(name)
        for name in REQUIRED_DISTRIBUTIONS
    }


def probe_runtime() -> dict[str, Any]:
    return {
        "status": "ok",
        "python": ".".join(str(item) for item in sys.version_info[:3]),
        "packages": _package_versions(),
    }


def _detect_device() -> tuple[str, str]:
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", "float16"
    except (ImportError, RuntimeError):
        pass

    try:
        import ctranslate2

        supported = ctranslate2.get_supported_compute_types("cuda")
        if "float16" in supported:
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def transcribe(
    audio_path: Path,
    *,
    model_size: str,
    language: str | None,
) -> dict[str, Any]:
    from faster_whisper import WhisperModel

    device, compute_type = _detect_device()
    model = WhisperModel(
        model_size,
        device=device,
        compute_type=compute_type,
    )
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        vad_filter=True,
    )
    text = " ".join(
        part
        for segment in segments
        if (part := str(segment.text or "").strip())
    ).strip()
    return {
        "status": "ok" if text else "empty",
        "text": text,
        "device": device,
        "compute_type": compute_type,
        "detected_language": str(getattr(info, "language", "") or ""),
        "language_probability": float(
            getattr(info, "language_probability", 0.0) or 0.0
        ),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--model-size", default="small")
    parser.add_argument("--language")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.probe:
            payload = probe_runtime()
        else:
            if args.audio is None:
                raise ValueError("--audio is required")
            audio_path = args.audio.expanduser().resolve()
            if not audio_path.is_file():
                raise FileNotFoundError("audio file is unavailable")
            payload = transcribe(
                audio_path,
                model_size=str(args.model_size),
                language=str(args.language) if args.language else None,
            )
        return_code = 0
    except Exception as exc:
        payload = {
            "status": "error",
            "error": f"{type(exc).__name__}: {str(exc)[:400]}",
        }
        return_code = 1
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)
    return return_code


if __name__ == "__main__":
    os.environ.setdefault("PYTHONNOUSERSITE", "1")
    raise SystemExit(main())
