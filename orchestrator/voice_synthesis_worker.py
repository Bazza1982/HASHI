"""Short-lived TTS probe, model cache and Edge worker for a helper.

Only the standard library is imported at module scope. Optional speech
packages load after this module starts under the isolated interpreter, keeping
them out of the long-lived HASHI Core and Function Worker runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

RESULT_PREFIX = "HASHI_TTS_RESULT="
EDGE_REQUIRED_DISTRIBUTIONS = (
    "edge-tts",
    "aiohttp",
    "imageio-ffmpeg",
)
KOKORO_REQUIRED_DISTRIBUTIONS = (
    "kokoro",
    "misaki",
    "pyopenjtalk-plus",
    "soundfile",
)
REQUIRED_DISTRIBUTIONS = EDGE_REQUIRED_DISTRIBUTIONS + KOKORO_REQUIRED_DISTRIBUTIONS
KOKORO_REPO_ID = "hexgrad/Kokoro-82M"
KOKORO_REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"
KOKORO_BASE_FILES = ("config.json", "kokoro-v1_0.pth")


def _safe_error_payload(exc: Exception) -> dict[str, Any]:
    """Return actionable worker evidence without leaking signed service URLs."""

    name = type(exc).__name__
    raw = str(exc or "")
    status = getattr(exc, "status", None)
    if status == 403 or (name == "WSServerHandshakeError" and re.search(r"\b403\b", raw)):
        return {
            "status": "error",
            "error_code": "tts_protocol_rejected",
            "error": (
                "Edge TTS rejected the speech request (HTTP 403). "
                "The installed Edge speech protocol may be out of date."
            ),
            "retryable": False,
        }
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return {
            "status": "error",
            "error_code": "tts_timeout",
            "error": "The TTS operation did not complete before the request timed out.",
            "retryable": True,
        }
    detail = re.sub(
        r"\b(?:https?|wss?)://[^\s'\"<>]+",
        "[service URL omitted]",
        raw,
        flags=re.IGNORECASE,
    )
    detail = re.sub(
        r"(?i)\b(token|key|secret|signature|sig|authorization|sec-ms-gec)\s*[=:]\s*[^\s,;'\" ]+",
        r"\1=[redacted]",
        detail,
    )
    detail = re.sub(r"\s+", " ", detail).strip()
    return {
        "status": "error",
        "error_code": "tts_provider_failed",
        "error": f"{name}: {detail or 'TTS provider failed.'}"[:500],
        "retryable": True,
    }


def _package_versions(*, edge_only: bool = False) -> dict[str, str]:
    import edge_tts  # noqa: F401

    required = EDGE_REQUIRED_DISTRIBUTIONS if edge_only else REQUIRED_DISTRIBUTIONS
    if not edge_only:
        import kokoro  # noqa: F401
        import misaki  # noqa: F401
        import soundfile  # noqa: F401

    return {
        name: importlib.metadata.version(name)
        for name in required
    }


def probe_runtime(*, edge_only: bool = False) -> dict[str, Any]:
    import imageio_ffmpeg
    import subprocess

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ffmpeg, "-version"], check=True, timeout=30,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {
        "status": "ok",
        "python": ".".join(str(item) for item in sys.version_info[:3]),
        "packages": _package_versions(edge_only=edge_only),
        "ffmpeg": ffmpeg,
    }


def prepare_kokoro_assets(
    model_cache: Path,
    voice_names: tuple[str, ...],
    *,
    local_files_only: bool,
) -> dict[str, Any]:
    """Download or verify only the Kokoro model and configured free voices."""

    voices = tuple(dict.fromkeys(str(value).strip() for value in voice_names))
    if not voices or any(not re.fullmatch(r"[a-z]{2}_[a-z0-9_]+", voice) for voice in voices):
        raise ValueError("at least one valid Kokoro voice is required")
    cache_root = Path(model_cache).expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HOME"] = str(cache_root)
    os.environ["HF_HUB_CACHE"] = str(cache_root / "hub")

    from huggingface_hub import snapshot_download

    patterns = [
        *KOKORO_BASE_FILES,
        *(f"voices/{voice}.pt" for voice in voices),
    ]
    snapshot = Path(
        snapshot_download(
            repo_id=KOKORO_REPO_ID,
            revision=KOKORO_REVISION,
            allow_patterns=patterns,
            local_files_only=local_files_only,
        )
    )
    missing = [relative for relative in patterns if not (snapshot / relative).is_file()]
    if missing:
        raise RuntimeError("Kokoro cache is incomplete: " + ", ".join(missing))
    return {
        "status": "ok",
        "repo_id": KOKORO_REPO_ID,
        "revision": KOKORO_REVISION,
        "model_cache": str(cache_root),
        "voices": list(voices),
    }


async def synthesize(
    text: str,
    *,
    output_path: Path,
    voice: str,
    rate: str,
) -> dict[str, Any]:
    import edge_tts

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
    )
    await communicate.save(str(output_path))
    if not output_path.is_file() or output_path.stat().st_size < 100:
        raise RuntimeError("Edge TTS did not produce a valid audio file")
    return {
        "status": "ok",
        "size_bytes": output_path.stat().st_size,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edge-only", action="store_true")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--probe", action="store_true")
    action.add_argument("--prepare-kokoro", action="store_true")
    action.add_argument("--check-kokoro", action="store_true")
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--kokoro-voice", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--voice")
    parser.add_argument("--rate", default="+0%")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # The parent writes UTF-8. Isolated mode ignores Python encoding environment
    # variables, so Windows pipes otherwise use the machine's legacy code page.
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    try:
        if args.probe:
            payload = probe_runtime(edge_only=args.edge_only)
        elif args.prepare_kokoro or args.check_kokoro:
            if args.model_cache is None:
                raise ValueError("--model-cache is required for Kokoro preparation")
            payload = prepare_kokoro_assets(
                args.model_cache,
                tuple(args.kokoro_voice),
                local_files_only=bool(args.check_kokoro),
            )
        else:
            if args.output is None or not str(args.voice or "").strip():
                raise ValueError("--output and --voice are required")
            text = sys.stdin.read(20_001)
            if not text.strip():
                raise ValueError("speech text is required")
            if len(text) > 20_000:
                raise ValueError("speech text is too long")
            payload = asyncio.run(
                synthesize(
                    text,
                    output_path=args.output.expanduser().resolve(),
                    voice=str(args.voice),
                    rate=str(args.rate),
                )
            )
        return_code = 0
    except Exception as exc:
        payload = _safe_error_payload(exc)
        return_code = 1
    print(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False), flush=True)
    return return_code


if __name__ == "__main__":
    os.environ.setdefault("PYTHONNOUSERSITE", "1")
    raise SystemExit(main())
