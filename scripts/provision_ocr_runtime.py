#!/usr/bin/env python3
"""Provision pinned, instance-private OCR outside the active HASHI interpreter."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from orchestrator.config_json import (  # noqa: E402 -- standalone checkout root above
    read_config_json,
    new_config_json,
    write_config_json,
)
from scripts import provision_paddle_ocr_models as models  # noqa: E402

LOCK = ROOT / "constraints" / "ocr-py312.lock"
APPROVED_PYTHON = "3.12.13"


class OCRRuntimeError(RuntimeError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def python_at(target):
    return target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def checked_target(value):
    target = Path(value).resolve()
    active = Path(sys.prefix).resolve()
    if target == active or target in active.parents or active in target.parents:
        raise OCRRuntimeError(
            "OCR runtime must not overlap the active HASHI interpreter"
        )
    return target


def run(command, *, timeout, action):
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**os.environ, "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OCRRuntimeError("OCR " + action + " did not finish") from error
    if completed.returncode:
        print(completed.stderr[-4000:], file=sys.stderr)
        raise OCRRuntimeError(
            "OCR " + action + " failed (exit " + str(completed.returncode) + ")"
        )
    return completed.stdout


def probe(python):
    source = (
        "import importlib.metadata as m,json,platform,paddle,paddleocr;"
        "print('HASHI_OCR_PROBE='+json.dumps({'python':platform.python_version(),"
        "'packages':{n:m.version(n) for n in ('paddleocr','paddlepaddle')}}))"
    )
    output = run(
        [str(python), "-I", "-c", source], timeout=90, action="native import check"
    )
    result = next(
        (
            json.loads(line.removeprefix("HASHI_OCR_PROBE="))
            for line in reversed(output.splitlines())
            if line.startswith("HASHI_OCR_PROBE=")
        ),
        None,
    )
    expected = models.load_manifest(
        ROOT / "hashi_assets" / "ocr" / "paddle_manifest.json"
    )["runtime"]
    if (
        not result
        or result["python"] != APPROVED_PYTHON
        or result["packages"] != expected
    ):
        raise OCRRuntimeError(
            "OCR native package versions do not match the qualified profile"
        )
    return result


def prepare(*, bridge_home, runtime_dir, base_python, check=False):
    bridge = Path(bridge_home).resolve()
    target = checked_target(runtime_dir)
    python = python_at(target)
    marker = target / ".hashi-ocr-runtime.json"
    fingerprint = digest(LOCK)
    expected_marker = {"profile": "ocr-py312", "lock_sha256": fingerprint}
    if target.exists():
        if (
            not marker.is_file()
            or json.loads(marker.read_text(encoding="utf-8")) != expected_marker
        ):
            raise OCRRuntimeError("Preserve the existing unrecognised OCR directory")
    elif check:
        raise OCRRuntimeError("OCR runtime is not installed")
    else:
        uv = shutil.which("uv")
        if not uv:
            raise OCRRuntimeError("The existing uv installer is unavailable")
        target.parent.mkdir(parents=True, exist_ok=True)
        run(
            [uv, "venv", "--python", str(base_python), str(target)],
            timeout=90,
            action="environment creation",
        )
        marker.write_text(json.dumps(expected_marker), encoding="utf-8")
    if not check:
        uv = shutil.which("uv")
        if not uv:
            raise OCRRuntimeError("The existing uv installer is unavailable")
        run(
            [uv, "pip", "sync", "--python", str(python), "--require-hashes", str(LOCK)],
            timeout=900,
            action="isolated dependency installation",
        )
    qualified = probe(python)
    model_root = target / "models"
    model_args = ["--destination", str(model_root), "--source", "huggingface"] + (
        ["--check"] if check else []
    )
    if models.main(model_args):
        raise OCRRuntimeError("Pinned OCR model verification failed")
    config_path = bridge / "state" / "platform" / "ocr.json"
    if check:
        config = read_config_json(config_path)
        if config.get("paddle_python") != str(python) or config.get(
            "paddle_model_root"
        ) != str(model_root):
            raise OCRRuntimeError("OCR is prepared but not selected by this instance")
    else:
        try:
            config = read_config_json(config_path)
        except FileNotFoundError:
            config = new_config_json(config_path)
        config.update(
            {
                "schema_version": 1,
                "paddle_python": str(python),
                "paddle_model_root": str(model_root),
                "runtime_dir": str(target),
                "lock_sha256": fingerprint,
                "python_version": qualified["python"],
                "packages": qualified["packages"],
            }
        )
        write_config_json(config_path, config)
    return {
        "status": "ready",
        "python": str(python),
        "models_verified": True,
        "python_version": qualified["python"],
        "packages": qualified["packages"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-home", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--base-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    target = args.runtime_dir or args.bridge_home / "state" / "runtimes" / "ocr" / (
        "py312-" + digest(LOCK)[:16]
    )
    try:
        print(
            json.dumps(
                prepare(
                    bridge_home=args.bridge_home,
                    runtime_dir=target,
                    base_python=args.base_python,
                    check=args.check,
                )
            )
        )
        return 0
    except (OCRRuntimeError, OSError, ValueError) as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
