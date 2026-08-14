#!/usr/bin/env python3
"""Provision and verify HASHI's pinned multilingual Tesseract model pack."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.ocr import default_ocr_model_root, ocr_manifest_path  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported OCR manifest schema")
    revision = str(payload.get("revision") or "").strip()
    if len(revision) != 40:
        raise ValueError("OCR manifest revision must be a full Git commit")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("OCR manifest has no language files")
    return payload


def _entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    entries = list(manifest["files"])
    license_file = dict(manifest.get("license_file") or {})
    if license_file:
        entries.append(
            {
                "name": str(license_file["path"]),
                "size_bytes": int(license_file["size_bytes"]),
                "sha256": str(license_file["sha256"]),
            }
        )
    return entries


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
    for entry in _entries(manifest):
        name = str(entry["name"])
        failure = _verify_file(destination / name, entry)
        if failure:
            failures.append(f"{name}: {failure}")
    return failures


def _download(url: str, destination: Path, entry: dict[str, Any]) -> None:
    expected_size = int(entry["size_bytes"])
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
    digest = hashlib.sha256()
    received = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "HASHI-OCR-Provisioner/1"})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > expected_size:
                    raise ValueError(f"download exceeded pinned size for {destination.name}")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if received != expected_size:
            raise ValueError(
                f"download size mismatch for {destination.name}: {received} != {expected_size}"
            )
        expected_digest = str(entry["sha256"]).casefold()
        if digest.hexdigest() != expected_digest:
            raise ValueError(f"download SHA-256 mismatch for {destination.name}")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def provision(destination: Path, manifest: dict[str, Any]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    source = str(manifest["source"]).rstrip("/")
    revision = str(manifest["revision"])
    for entry in _entries(manifest):
        name = str(entry["name"])
        target = destination / name
        if _verify_file(target, entry) is None:
            continue
        url = f"{source}/raw/{revision}/{name}"
        _download(url, target, entry)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ocr_manifest_path())
    parser.add_argument("--destination", type=Path, default=default_ocr_model_root())
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
    if not args.check:
        provision(destination, manifest)
    failures = verify(destination, manifest)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    languages = ",".join(str(item["language"]) for item in manifest["files"])
    print(
        f"OCR model pack verified: revision={manifest['revision']} "
        f"languages={languages} destination={destination}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
