#!/usr/bin/env python3
"""Launch Core only through the installer-qualified runtime selection."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.hashi_instance_cli import _select_core_runtime  # noqa: E402
from tools.instance_registry import InstanceRegistryError  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    _args, forwarded = parser.parse_known_args(argv)
    try:
        command = _select_core_runtime(ROOT)
        result = subprocess.run(
            [*command, str(ROOT / "main.py"), *forwarded], cwd=ROOT, check=False)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except (OSError, InstanceRegistryError) as exc:
        print(f"HASHI qualified Core launch failed: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_9508de756bc14ca38c2c78a5ec694fb6
