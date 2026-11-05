#!/usr/bin/env python3
"""Regenerate derived wheel hash locks from canonical Functions version pins.

This resolves/downloads metadata only. It never installs packages, touches an
existing interpreter, selects a runtime, or starts a service.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.runtime_contract import load_runtime_policy  # noqa: E402


def pinned_versions(text):
    result = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "--")):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^ ;\\]+)(?:\s*;[^\\]+)?\s*\\?", line)
        if match is None:
            raise ValueError("Derived lock contains a non-exact requirement: " + line)
        name = re.sub(r"[-_.]+", "-", match[1]).lower()
        if name in result:
            raise ValueError("Duplicate requirement: " + name)
        result[name] = match[2]
    return result


def compile_locks(output_root, *, uv="uv", cache=None):
    output_root = Path(output_root).resolve()
    if output_root.exists():
        raise ValueError("Use a new output directory; existing locks are never overwritten")
    policy = load_runtime_policy(ROOT)
    canonical = ROOT / policy.standard_lock
    expected = pinned_versions(canonical.read_text(encoding="utf-8"))
    executable = shutil.which(uv)
    if executable is None:
        raise ValueError("uv is required as an external build tool")
    output_root.mkdir(parents=True)
    env = dict(os.environ)
    if cache is not None:
        env["UV_CACHE_DIR"] = str(Path(cache).resolve())
    with tempfile.TemporaryDirectory(prefix="hashi-functions-locks-") as temporary:
        work = Path(temporary)
        definitions = (
            ("functions-py312.hashes.lock", canonical, ["--universal"]),
            ("portable-windows-py312.hashes.lock",
             ROOT / "packaging/portable_windows/requirements.in",
             ["--python-platform", "x86_64-pc-windows-msvc"]),
        )
        results = {}
        for name, source, platform in definitions:
            target = work / name
            subprocess.run(
                [executable, "pip", "compile", str(source),
                 "--python-version", policy.python_text, *platform,
                 "--generate-hashes", "--no-header", "--no-annotate",
                 "--no-build", "--output-file", str(target)],
                cwd=ROOT, env=env, check=True, timeout=600,
                capture_output=True, text=True,
            )
            text = target.read_text(encoding="utf-8")
            actual = pinned_versions(text)
            if name.startswith("functions-") and actual != expected:
                raise ValueError("Canonical Functions dependency closure changed during hash resolution")
            if name.startswith("portable-") and any(actual.get(k) != v for k, v in expected.items()):
                raise ValueError("Portable dependencies diverge from canonical Functions")
            if "--hash=sha256:" not in text:
                raise ValueError("Derived lock lacks wheel artifact hashes")
            destination = output_root / name
            destination.write_text(
                "# Derived from canonical Functions version pins; regenerate with\n"
                "# python scripts/compile_functions_locks.py --output-root NEW_DIRECTORY\n"
                + text, encoding="utf-8", newline="\n")
            results[name] = destination
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args(argv)
    for name, target in compile_locks(args.output_root, uv=args.uv, cache=args.cache).items():
        print(f"{name}: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# HASHI-Fingerprint: hfp_a98ac87945714a1f8582d3ee45491b16
