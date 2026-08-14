#!/usr/bin/env python3
"""Provision and verify HASHI's pinned PaddleOCR inference models."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.ocr import (  # noqa: E402
    default_paddle_model_root,
    paddle_ocr_manifest_path,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported PaddleOCR manifest schema")
    source = payload.get("source")
    if not isinstance(source, dict) or not str(source.get("base_url") or "").startswith(
        "https://"
    ):
        raise ValueError("PaddleOCR manifest requires an HTTPS source")
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("PaddleOCR manifest has no models")
    for model in models:
        name = str(model.get("name") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise ValueError(f"unsafe PaddleOCR model name: {name!r}")
        files = model.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError(f"PaddleOCR model {name!r} has no files")
        for entry in files:
            file_path = Path(str(entry.get("path") or ""))
            if len(file_path.parts) != 1 or file_path.name in {"", ".", ".."}:
                raise ValueError(f"unsafe PaddleOCR model file: {file_path}")
    return payload


def _verify_file(path: Path, entry: dict[str, Any]) -> str | None:
    if not path.is_file():
        return "missing"
    expected_size = int(entry["size_bytes"])
    if path.stat().st_size != expected_size:
        return f"size mismatch ({path.stat().st_size} != {expected_size})"
    actual = _sha256(path)
    expected = str(entry["sha256"]).casefold()
    if actual != expected:
        return f"SHA-256 mismatch ({actual})"
    return None


def verify(destination: Path, manifest: dict[str, Any]) -> list[str]:
    failures = []
    for model in manifest["models"]:
        name = str(model["name"])
        for entry in model["files"]:
            relative = str(entry["path"])
            failure = _verify_file(destination / name / relative, entry)
            if failure:
                failures.append(f"{name}/{relative}: {failure}")
    return failures


def _copy_verified_stream(source: BinaryIO, target: Path, entry: dict[str, Any]) -> None:
    temporary = target.with_name(f".{target.name}.{os.getpid()}.partial")
    expected_size = int(entry["size_bytes"])
    expected_digest = str(entry["sha256"]).casefold()
    digest = hashlib.sha256()
    received = 0
    try:
        with temporary.open("wb") as output:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                received += len(chunk)
                if received > expected_size:
                    raise ValueError(f"extracted file exceeded pinned size for {target.name}")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if received != expected_size:
            raise ValueError(
                f"extracted file size mismatch for {target.name}: {received} != {expected_size}"
            )
        if digest.hexdigest() != expected_digest:
            raise ValueError(f"extracted file SHA-256 mismatch for {target.name}")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _download_archive(url: str, target: Path, archive: dict[str, Any]) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "HASHI-OCR-Provisioner/1"})
    expected_size = int(archive["size_bytes"])
    digest = hashlib.sha256()
    received = 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > expected_size:
                raise ValueError(f"download exceeded pinned size for {target.name}")
            digest.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if received != expected_size:
        raise ValueError(f"download size mismatch for {target.name}: {received} != {expected_size}")
    if digest.hexdigest() != str(archive["sha256"]).casefold():
        raise ValueError(f"download SHA-256 mismatch for {target.name}")


def _verify_archive(path: Path, archive: dict[str, Any]) -> None:
    failure = _verify_file(path, archive)
    if failure:
        raise ValueError(f"{path.name}: {failure}")


def _install_model(archive_path: Path, destination: Path, model: dict[str, Any]) -> None:
    name = str(model["name"])
    model_destination = destination / name
    model_destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:") as bundle:
        members = {member.name: member for member in bundle.getmembers()}
        for entry in model["files"]:
            relative = str(entry["path"])
            target = model_destination / relative
            if _verify_file(target, entry) is None:
                continue
            member_name = f"{name}_infer/{relative}"
            member = members.get(member_name)
            if member is None or not member.isfile():
                raise ValueError(f"archive is missing pinned regular file {member_name}")
            source = bundle.extractfile(member)
            if source is None:
                raise ValueError(f"archive member could not be read: {member_name}")
            with source:
                _copy_verified_stream(source, target, entry)


def provision(
    destination: Path,
    manifest: dict[str, Any],
    *,
    archive_directory: Path | None,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    base_url = str(manifest["source"]["base_url"]).rstrip("/")
    with tempfile.TemporaryDirectory(
        prefix="hashi-paddle-ocr-", dir=destination.parent
    ) as temporary:
        staging = Path(temporary)
        for model in manifest["models"]:
            if all(
                _verify_file(destination / str(model["name"]) / str(entry["path"]), entry)
                is None
                for entry in model["files"]
            ):
                continue
            archive = dict(model["archive"])
            archive_name = str(archive["name"])
            supplied = archive_directory / archive_name if archive_directory else None
            if supplied is not None and supplied.is_file():
                archive_path = supplied
            else:
                archive_path = staging / archive_name
                _download_archive(f"{base_url}/{archive_name}", archive_path, archive)
            _verify_archive(archive_path, archive)
            _install_model(archive_path, destination, model)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=paddle_ocr_manifest_path())
    parser.add_argument("--destination", type=Path, default=default_paddle_model_root())
    parser.add_argument(
        "--archive-directory",
        type=Path,
        help="Use already-downloaded pinned archives from this directory when present.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the pinned files without downloading anything.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = _load_manifest(args.manifest.resolve())
    destination = args.destination.expanduser().resolve()
    archive_directory = (
        args.archive_directory.expanduser().resolve()
        if args.archive_directory is not None
        else None
    )
    if not args.check:
        provision(destination, manifest, archive_directory=archive_directory)
    failures = verify(destination, manifest)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    models = ",".join(str(item["name"]) for item in manifest["models"])
    print(
        f"PaddleOCR model pack verified: release={manifest['source']['release']} "
        f"models={models} destination={destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
