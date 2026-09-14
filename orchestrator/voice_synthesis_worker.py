"""Short-lived Edge TTS worker for an instance-owned helper environment.

Only the standard library is imported at module scope.  ``edge_tts`` is loaded
after this module starts under the isolated interpreter, keeping optional
speech packages out of the long-lived HASHI Core and Function Worker runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

RESULT_PREFIX = "HASHI_TTS_RESULT="
REQUIRED_DISTRIBUTIONS = ("edge-tts", "aiohttp")


def _package_versions() -> dict[str, str]:
    import edge_tts  # noqa: F401

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
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--voice")
    parser.add_argument("--rate", default="+0%")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.probe:
            payload = probe_runtime()
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
