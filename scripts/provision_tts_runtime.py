#!/usr/bin/env python3
"""Provision free Edge and Kokoro TTS outside the active HASHI runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.voice_synthesis_worker import (  # noqa: E402
    REQUIRED_DISTRIBUTIONS,
    RESULT_PREFIX as TTS_RESULT_PREFIX,
)
from orchestrator.config_json import (  # noqa: E402
    new_config_json,
    read_config_json,
    write_config_json,
)

CONFIG_SCHEMA_VERSION = 1
APPROVED_PYTHON = "3.12.13"
DEFAULT_LOCK = PROJECT_ROOT / "constraints" / "tts-py312.lock"
WORKER_PATH = PROJECT_ROOT / "orchestrator" / "voice_synthesis_worker.py"


class ProvisioningError(RuntimeError):
    """The isolated runtime could not be prepared and published."""


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_python_path(runtime_dir: Path) -> Path:
    return (
        Path(runtime_dir) / "Scripts" / "python.exe"
        if os.name == "nt"
        else Path(runtime_dir) / "bin" / "python"
    )


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_runtime_target(runtime_dir: Path, *, active_prefix: Path | None = None) -> Path:
    target = Path(runtime_dir).expanduser().resolve()
    active = Path(active_prefix or sys.prefix).expanduser().resolve()
    if target == active or _inside(target, active) or _inside(active, target):
        raise ProvisioningError(
            "TTS runtime must not overlap the active HASHI Core environment"
        )
    return target


def default_runtime_dir(bridge_home: Path, lock_path: Path = DEFAULT_LOCK) -> Path:
    return (
        Path(bridge_home)
        / "state"
        / "runtimes"
        / "tts"
        / f"py312-{sha256_file(Path(lock_path).resolve())[:16]}"
    )


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProvisioningError(
            f"Isolated TTS runtime command failed to start: {type(exc).__name__}"
        ) from exc


def _require_success(completed: subprocess.CompletedProcess[str], *, action: str) -> None:
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
    raise ProvisioningError(
        f"TTS runtime {action} failed (exit={completed.returncode}): {detail[-1000:]}"
    )


def _worker_payload(
    completed: subprocess.CompletedProcess[str],
    *,
    action: str,
) -> dict[str, Any]:
    result_line = next(
        (
            line[len(TTS_RESULT_PREFIX) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(TTS_RESULT_PREFIX)
        ),
        None,
    )
    if result_line is None:
        _require_success(completed, action=action)
        raise ProvisioningError(f"TTS runtime {action} returned no result")
    try:
        payload = json.loads(result_line)
    except json.JSONDecodeError as exc:
        raise ProvisioningError(f"TTS runtime {action} returned invalid JSON") from exc
    if completed.returncode != 0 or not isinstance(payload, dict) or payload.get("status") != "ok":
        detail = str(payload.get("error") if isinstance(payload, dict) else f"{action} failed")
        raise ProvisioningError(f"TTS runtime {action} failed: {detail[-1000:]}")
    return payload


def _locked_versions(lock_path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")) or "==" not in line:
            continue
        name, version = line.split("==", 1)
        versions[name.strip().casefold().replace("_", "-")] = version.split(";", 1)[0].strip()
    for required in REQUIRED_DISTRIBUTIONS:
        if required not in versions:
            raise ProvisioningError(f"TTS lock does not pin {required}")
    return versions


def probe_runtime(python: Path, *, lock_path: Path) -> dict[str, Any]:
    # Importing the local Kokoro/Torch stack for the first time can exceed one
    # minute on Windows while antivirus scans native wheels. It remains a
    # bounded, short-lived Function probe rather than a Core startup action.
    completed = _run([str(python), "-I", str(WORKER_PATH), "--probe"], timeout=180.0)
    payload = _worker_payload(completed, action="probe")
    if str(payload.get("python") or "") != APPROVED_PYTHON:
        raise ProvisioningError(
            f"TTS runtime uses unsupported Python {payload.get('python') or 'unknown'}"
        )
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise ProvisioningError("TTS runtime probe returned no package versions")
    locked = _locked_versions(lock_path)
    for name in REQUIRED_DISTRIBUTIONS:
        if str(packages.get(name) or "") != locked[name]:
            raise ProvisioningError(
                f"TTS runtime found {name} {packages.get(name) or 'missing'}; expected {locked[name]}"
            )
    return payload


def configured_kokoro_voices() -> tuple[str, ...]:
    """Derive model preparation from the authoritative voice catalogue."""

    from orchestrator.voice_manager import VoiceManager

    return tuple(
        sorted(
            {
                str(preset["voice_name"])
                for preset in VoiceManager.VOICE_PRESETS.values()
                if preset.get("provider") == "kokoro"
            }
        )
    )


def prepare_kokoro_model(
    python: Path,
    *,
    runtime_dir: Path,
    check_only: bool,
) -> dict[str, Any]:
    voices = configured_kokoro_voices()
    if not voices:
        raise ProvisioningError("No configured Kokoro voices were found")
    command = [
        str(python),
        "-I",
        str(WORKER_PATH),
        "--check-kokoro" if check_only else "--prepare-kokoro",
        "--model-cache",
        str(runtime_dir / "model-cache"),
    ]
    for voice in voices:
        command.extend(["--kokoro-voice", voice])
    completed = _run(command, timeout=180.0 if check_only else 1500.0)
    return _worker_payload(
        completed,
        action="Kokoro cache check" if check_only else "Kokoro model preparation",
    )


def _write_platform_config(
    bridge_home: Path,
    *,
    python: Path,
    runtime_dir: Path,
    lock_path: Path,
    probe: dict[str, Any],
    kokoro: dict[str, Any],
) -> Path:
    config_path = bridge_home / "state" / "platform" / "tts.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "python": str(python),
        "runtime_dir": str(runtime_dir),
        "lock_sha256": sha256_file(lock_path),
        "python_version": str(probe["python"]),
        "packages": dict(probe["packages"]),
        "ffmpeg": str(probe["ffmpeg"]),
        "model_cache": str(kokoro["model_cache"]),
        "kokoro_repo": str(kokoro["repo_id"]),
        "kokoro_revision": str(kokoro["revision"]),
        "kokoro_voices": list(kokoro["voices"]),
    }
    try:
        document = read_config_json(config_path)
    except FileNotFoundError:
        document = new_config_json(config_path)
    document.update(payload)
    write_config_json(config_path, document)
    return config_path


def provision_runtime(
    *,
    bridge_home: Path,
    runtime_dir: Path,
    base_python: Path,
    lock_path: Path,
) -> dict[str, Any]:
    bridge_home = Path(bridge_home).expanduser().resolve()
    runtime_dir = validate_runtime_target(runtime_dir)
    base_python = _absolute_path(base_python)
    lock_path = Path(lock_path).expanduser().resolve()
    if not base_python.is_file():
        raise ProvisioningError("Base Python executable does not exist")
    if not lock_path.is_file():
        raise ProvisioningError("TTS dependency lock does not exist")
    _locked_versions(lock_path)
    python = runtime_python_path(runtime_dir)
    if not python.is_file():
        runtime_dir.parent.mkdir(parents=True, exist_ok=True)
        completed = _run([str(base_python), "-m", "venv", str(runtime_dir)], timeout=180.0)
        _require_success(completed, action="environment creation")
    if not python.is_file():
        raise ProvisioningError("TTS environment has no Python executable")
    completed = _run(
        [str(_absolute_path(python)), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(lock_path)],
        timeout=1500.0,
    )
    _require_success(completed, action="dependency installation")
    python = _absolute_path(python)
    probe = probe_runtime(python, lock_path=lock_path)
    prepare_kokoro_model(python, runtime_dir=runtime_dir, check_only=False)
    kokoro = prepare_kokoro_model(python, runtime_dir=runtime_dir, check_only=True)
    config_path = _write_platform_config(
        bridge_home,
        python=python,
        runtime_dir=runtime_dir,
        lock_path=lock_path,
        probe=probe,
        kokoro=kokoro,
    )
    return {
        "status": "ready",
        "python": str(python),
        "runtime_dir": str(runtime_dir),
        "config_path": str(config_path),
        "lock_sha256": sha256_file(lock_path),
        "probe": probe,
        "kokoro": kokoro,
    }


def check_runtime(*, bridge_home: Path, runtime_dir: Path, lock_path: Path) -> dict[str, Any]:
    bridge_home = Path(bridge_home).expanduser().resolve()
    runtime_dir = validate_runtime_target(runtime_dir)
    lock_path = Path(lock_path).expanduser().resolve()
    python = _absolute_path(runtime_python_path(runtime_dir))
    if not python.is_file():
        raise ProvisioningError("Isolated TTS runtime is not installed")
    probe = probe_runtime(python, lock_path=lock_path)
    kokoro = prepare_kokoro_model(python, runtime_dir=runtime_dir, check_only=True)
    config_path = bridge_home / "state" / "platform" / "tts.json"
    if not config_path.is_file():
        raise ProvisioningError("TTS platform configuration is not published")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if config.get("python") != str(python):
        raise ProvisioningError("TTS platform configuration selects another runtime")
    if config.get("lock_sha256") != sha256_file(lock_path):
        raise ProvisioningError("TTS platform configuration uses another lock")
    if config.get("model_cache") != str(runtime_dir / "model-cache"):
        raise ProvisioningError("TTS platform configuration selects another model cache")
    if config.get("kokoro_repo") != kokoro["repo_id"]:
        raise ProvisioningError("TTS platform configuration selects another Kokoro repository")
    if config.get("kokoro_revision") != kokoro["revision"]:
        raise ProvisioningError("TTS platform configuration selects another Kokoro revision")
    if config.get("kokoro_voices") != list(configured_kokoro_voices()):
        raise ProvisioningError("TTS platform configuration does not cover configured Kokoro voices")
    return {
        "status": "ready",
        "python": str(python),
        "probe": probe,
        "kokoro": kokoro,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-home", type=Path, default=Path(os.environ["BRIDGE_HOME"]) if os.environ.get("BRIDGE_HOME") else None)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--base-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.bridge_home is None:
        print("FAIL: --bridge-home or BRIDGE_HOME is required", file=sys.stderr)
        return 2
    lock_path = args.lock.expanduser().resolve()
    runtime_dir = args.runtime_dir or default_runtime_dir(args.bridge_home, lock_path)
    try:
        receipt = (
            check_runtime(bridge_home=args.bridge_home, runtime_dir=runtime_dir, lock_path=lock_path)
            if args.check
            else provision_runtime(
                bridge_home=args.bridge_home,
                runtime_dir=runtime_dir,
                base_python=args.base_python,
                lock_path=lock_path,
            )
        )
    except (OSError, ValueError, ProvisioningError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
