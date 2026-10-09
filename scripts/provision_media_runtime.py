#!/usr/bin/env python3
"""Full media installation: isolated STT, model weights, TTS and converter."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import provision_transcription_runtime as stt
from scripts import provision_tts_runtime as tts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-home", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--runtime-root", type=Path,
                        help="Optional relocatable build/image helper root, outside Core.")
    args = parser.parse_args(argv)
    common = ["--bridge-home", str(args.bridge_home)] + (["--check"] if args.check else [])
    # A failed required stage ends the full installer; no successful completion
    # receipt or runtime task is published by its caller.
    stt_dir = ["--runtime-dir", str(args.runtime_root / "transcription")] if args.runtime_root else []
    tts_dir = ["--runtime-dir", str(args.runtime_root / "tts")] if args.runtime_root else []
    if stt.main(common + stt_dir + ["--prepare-model"]) != 0:
        return 1
    if tts.main(common + tts_dir) != 0:
        return 1
    print("HASHI full media runtime is ready (API credentials and device permissions are instance/user settings).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
