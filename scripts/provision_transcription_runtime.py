#!/usr/bin/env python3
"""Provision HASHI speech dependencies outside the active Core environment."""

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

from orchestrator.voice_transcription_worker import (  # noqa: E402
    REQUIRED_DISTRIBUTIONS,
    RESULT_PREFIX as TRANSCRIPTION_RESULT_PREFIX,
)

CONFIG_SCHEMA_VERSION = 1
APPROVED_PYTHON = "3.12.13"
DEFAULT_LOCK = PROJECT_ROOT / "constraints" / "transcription-py312.lock"
WORKER_PATH = PROJECT_ROOT / "orchestrator" / "voice_transcription_worker.py"


class ProvisioningError(RuntimeError):
    """The isolated runtime could not be prepared and was not published."""


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    """Return an absolute path without dereferencing a venv Python symlink."""

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


def validate_runtime_target(
    runtime_dir: Path,
    *,
    active_prefix: Path | None = None,
) -> Path:
    """Reject every target that could mutate or contain the Core environment."""

    target = Path(runtime_dir).expanduser().resolve()
    active = Path(active_prefix or sys.prefix).expanduser().resolve()
    if target == active or _inside(target, active) or _inside(active, target):
        raise ProvisioningError(
            "Transcription runtime must not overlap the active HASHI Core environment"
        )
    return target


def default_runtime_dir(bridge_home: Path, lock_path: Path = DEFAULT_LOCK) -> Path:
    lock_digest = sha256_file(Path(lock_path).resolve())[:16]
    return (
        Path(bridge_home)
        / "state"
        / "runtimes"
        / "transcription"
        / f"py312-{lock_digest}"
    )


def _run_command(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
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
            f"Isolated transcription runtime command failed to start: {type(exc).__name__}"
        ) from exc


def _require_success(
    completed: subprocess.CompletedProcess[str],
    *,
    action: str,
) -> None:
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
    raise ProvisioningError(
        f"Transcription runtime {action} failed (exit={completed.returncode}): "
        f"{detail[-1000:]}"
    )


def _parse_lock_versions(lock_path: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    required = {name.casefold() for name in REQUIRED_DISTRIBUTIONS}
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-")) or "==" not in line:
            continue
        name, version = line.split("==", 1)
        normalized = name.strip().casefold().replace("_", "-")
        version = version.split(";", 1)[0].strip()
        if normalized in required:
            versions[normalized] = version
    for name in REQUIRED_DISTRIBUTIONS:
        if name.casefold() not in versions:
            raise ProvisioningError(f"Transcription lock does not pin {name}")
    return versions


def probe_runtime(python: Path, *, lock_path: Path) -> dict[str, Any]:
    completed = _run_command(
        [str(python), "-I", str(WORKER_PATH), "--probe"],
        timeout=60.0,
    )
    result_line = next(
        (
            line[len(TRANSCRIPTION_RESULT_PREFIX) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(TRANSCRIPTION_RESULT_PREFIX)
        ),
        None,
    )
    if result_line is None:
        _require_success(completed, action="probe")
        raise ProvisioningError("Transcription runtime probe returned no result")
    try:
        payload = json.loads(result_line)
    except json.JSONDecodeError as exc:
        raise ProvisioningError(
            "Transcription runtime probe returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProvisioningError("Transcription runtime probe returned an invalid result")
    if completed.returncode != 0 or payload.get("status") != "ok":
        detail = str(payload.get("error") or completed.stderr or "probe failed")
        raise ProvisioningError(f"Transcription runtime probe failed: {detail[-1000:]}")
    if str(payload.get("python") or "") != APPROVED_PYTHON:
        raise ProvisioningError(
            "Transcription runtime probe used an unsupported Python version: "
            f"{payload.get('python') or 'unknown'}"
        )
    packages = payload.get("packages")
    if not isinstance(packages, dict):
        raise ProvisioningError("Transcription runtime probe returned no package versions")
    for name, expected in _parse_lock_versions(lock_path).items():
        actual = str(packages.get(name) or "")
        if actual != expected:
            raise ProvisioningError(
                f"Transcription runtime probe found {name} {actual or 'missing'}; "
                f"expected {expected}"
            )
    return payload


def _write_platform_config(
    bridge_home: Path,
    *,
    python: Path,
    runtime_dir: Path,
    lock_path: Path,
    probe: dict[str, Any],
) -> Path:
    config_path = bridge_home / "state" / "platform" / "transcription.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "python": str(python),
        "runtime_dir": str(runtime_dir),
        "lock_sha256": sha256_file(lock_path),
        "python_version": str(probe["python"]),
        "packages": dict(probe["packages"]),
    }
    temporary = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return config_path


def provision_runtime(
    *,
    bridge_home: Path,
    runtime_dir: Path,
    base_python: Path,
    lock_path: Path,
) -> dict[str, Any]:
    bridge_home = Path(bridge_home).expanduser().resolve()
    runtime_dir = validate_runtime_target(Path(runtime_dir))
    base_python = _absolute_path(base_python)
    lock_path = Path(lock_path).expanduser().resolve()
    if not base_python.is_file():
        raise ProvisioningError("Base Python executable does not exist")
    if not lock_path.is_file():
        raise ProvisioningError("Transcription dependency lock does not exist")
    _parse_lock_versions(lock_path)

    python = runtime_python_path(runtime_dir)
    if not python.is_file():
        runtime_dir.parent.mkdir(parents=True, exist_ok=True)
        completed = _run_command(
            [str(base_python), "-m", "venv", str(runtime_dir)],
            timeout=180.0,
        )
        _require_success(completed, action="environment creation")
    if not python.is_file():
        raise ProvisioningError(
            "Transcription environment was created without a Python executable"
        )

    completed = _run_command(
        [
            str(_absolute_path(python)),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--requirement",
            str(lock_path),
        ],
        timeout=900.0,
    )
    _require_success(completed, action="dependency installation")
    python = _absolute_path(python)
    probe = probe_runtime(python, lock_path=lock_path)
    _write_platform_config(
        bridge_home,
        python=python,
        runtime_dir=runtime_dir,
        lock_path=lock_path,
        probe=probe,
    )
    return {
        "status": "ready",
        "python": str(python),
        "runtime_dir": str(runtime_dir),
        "lock_sha256": sha256_file(lock_path),
        "probe": probe,
    }


def check_runtime(
    *,
    bridge_home: Path,
    runtime_dir: Path,
    lock_path: Path,
) -> dict[str, Any]:
    bridge_home = Path(bridge_home).expanduser().resolve()
    runtime_dir = validate_runtime_target(Path(runtime_dir))
    lock_path = Path(lock_path).expanduser().resolve()
    python = _absolute_path(runtime_python_path(runtime_dir))
    if not python.is_file():
        raise ProvisioningError("Isolated transcription runtime is not installed")
    probe = probe_runtime(python, lock_path=lock_path)
    config_path = bridge_home / "state" / "platform" / "transcription.json"
    if not config_path.is_file():
        raise ProvisioningError("Transcription platform configuration is not published")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if config.get("python") != str(python):
        raise ProvisioningError("Transcription platform configuration selects another runtime")
    if config.get("lock_sha256") != sha256_file(lock_path):
        raise ProvisioningError("Transcription platform configuration uses another lock")
    return {"status": "ready", "python": str(python), "probe": probe}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bridge-home",
        type=Path,
        default=Path(os.environ["BRIDGE_HOME"]) if os.environ.get("BRIDGE_HOME") else None,
    )
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--base-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the isolated runtime and published config without installing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.bridge_home is None:
        print("FAIL: --bridge-home or BRIDGE_HOME is required", file=sys.stderr)
        return 2
    lock_path = args.lock.expanduser().resolve()
    runtime_dir = args.runtime_dir or default_runtime_dir(args.bridge_home, lock_path)
    try:
        if args.check:
            receipt = check_runtime(
                bridge_home=args.bridge_home,
                runtime_dir=runtime_dir,
                lock_path=lock_path,
            )
        else:
            receipt = provision_runtime(
                bridge_home=args.bridge_home,
                runtime_dir=runtime_dir,
                base_python=args.base_python,
                lock_path=lock_path,
            )
    except (OSError, ValueError, ProvisioningError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
