#!/usr/bin/env python3
"""Verify and install optional EXP asset packs without trusting archive paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "exp" / "asset-packs.json"


class ExpAssetError(RuntimeError):
    """Raised when an asset pack cannot be safely resolved or installed."""


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ExpAssetError(f"Cannot read EXP asset manifest: {path}") from exc
    if data.get("schema_version") != 1 or not isinstance(data.get("packs"), dict):
        raise ExpAssetError("Unsupported EXP asset manifest")
    return data


def _pack_config(manifest: dict[str, Any], pack_id: str) -> dict[str, Any]:
    raw = manifest["packs"].get(pack_id)
    if not isinstance(raw, dict):
        available = ", ".join(sorted(manifest["packs"])) or "none"
        raise ExpAssetError(f"Unknown EXP asset pack {pack_id!r}; available: {available}")
    return raw


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_source(
    pack: dict[str, Any],
    *,
    project_root: Path,
    source: str | None,
) -> str:
    if source:
        return source
    env_source = os.environ.get("HASHI_EXP_ASSET_SOURCE", "").strip()
    if env_source:
        return env_source
    filename = str(pack.get("filename") or "").strip()
    asset_dir = os.environ.get("HASHI_EXP_ASSET_DIR", "").strip()
    candidates = []
    if asset_dir and filename:
        candidates.append(Path(asset_dir).expanduser() / filename)
    if filename:
        candidates.append(project_root.parent / "hashi-asset-packs" / filename)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    url = str(pack.get("url") or "").strip()
    if url:
        return url
    raise ExpAssetError(
        "No asset source is available; pass --source, set HASHI_EXP_ASSET_SOURCE, "
        "or place the pack in HASHI_EXP_ASSET_DIR"
    )


def _materialize_source(source: str, destination: Path) -> Path:
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme == "https":
        with urllib.request.urlopen(source, timeout=120) as response, destination.open(
            "wb"
        ) as output:
            shutil.copyfileobj(response, output)
        return destination
    if parsed.scheme == "file":
        source_path = Path(urllib.request.url2pathname(parsed.path))
    elif parsed.scheme:
        raise ExpAssetError(f"Unsupported asset source scheme: {parsed.scheme}")
    else:
        source_path = Path(source).expanduser()
    if not source_path.is_file():
        raise ExpAssetError(f"EXP asset pack not found: {source_path}")
    return source_path.resolve()


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    safe: list[tarfile.TarInfo] = []
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or path.parts[0] != "exp"
        ):
            raise ExpAssetError(f"Unsafe EXP asset path: {member.name}")
        if member.issym() or member.islnk() or not member.isfile():
            raise ExpAssetError(f"Unsupported EXP asset member: {member.name}")
        safe.append(member)
    return safe


def install_pack(
    pack_id: str,
    *,
    project_root: Path = PROJECT_ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
    source: str | None = None,
    force: bool = False,
) -> tuple[int, int]:
    manifest = _read_manifest(manifest_path)
    pack = _pack_config(manifest, pack_id)
    resolved_source = _resolve_source(pack, project_root=project_root, source=source)
    expected_hash = str(pack.get("sha256") or "").strip().lower()
    expected_size = int(pack.get("size_bytes") or 0)
    expected_content_size = int(pack.get("content_size_bytes") or 0)
    expected_count = int(pack.get("file_count") or 0)

    with tempfile.TemporaryDirectory(prefix="hashi-exp-assets-") as temp_dir:
        downloaded = Path(temp_dir) / str(pack.get("filename") or "asset-pack.tar.gz")
        archive_path = _materialize_source(resolved_source, downloaded)
        if expected_size and archive_path.stat().st_size != expected_size:
            raise ExpAssetError("EXP asset pack size does not match its manifest")
        if not expected_hash or _sha256(archive_path) != expected_hash:
            raise ExpAssetError("EXP asset pack checksum does not match its manifest")

        installed = 0
        skipped = 0
        member_names: list[str] = []
        with tarfile.open(archive_path, "r:gz") as archive:
            members = _safe_members(archive)
            if expected_count and len(members) != expected_count:
                raise ExpAssetError("EXP asset pack file count does not match its manifest")
            if expected_content_size and sum(member.size for member in members) != expected_content_size:
                raise ExpAssetError("EXP asset content size does not match its manifest")
            allowed_extensions = {
                str(value).lower() for value in pack.get("extensions") or []
            }
            project_root_resolved = project_root.resolve()
            for member in members:
                member_path = PurePosixPath(member.name)
                member_names.append(member.name)
                if (
                    allowed_extensions
                    and member_path.suffix.lower() not in allowed_extensions
                ):
                    raise ExpAssetError(
                        f"Unexpected EXP asset extension: {member.name}"
                    )
                target = project_root.joinpath(*member_path.parts)
                target_parent = target.parent.resolve()
                if not target_parent.is_relative_to(project_root_resolved):
                    raise ExpAssetError(f"Unsafe EXP asset target: {member.name}")
                if target.exists() and not force:
                    skipped += 1
                    continue
                archived_file = archive.extractfile(member)
                if archived_file is None:
                    raise ExpAssetError(f"Cannot read EXP asset: {member.name}")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary_path: Path | None = None
                try:
                    with archived_file, tempfile.NamedTemporaryFile(
                        prefix=f".{target.name}.", dir=target.parent, delete=False
                    ) as temporary:
                        temporary_path = Path(temporary.name)
                        shutil.copyfileobj(archived_file, temporary)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                    temporary_path.replace(target)
                except Exception:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
                    raise
                installed += 1

    marker_root = project_root / "exp" / ".asset-packs"
    marker_root.mkdir(parents=True, exist_ok=True)
    marker = marker_root / f"{pack_id}.json"
    marker_payload = {
        "schema_version": 1,
        "pack_id": pack_id,
        "sha256": expected_hash,
        "installed": installed,
        "skipped_existing": skipped,
        "members": member_names,
    }
    marker.write_text(
        json.dumps(marker_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return installed, skipped


def pack_status(
    pack_id: str,
    *,
    project_root: Path = PROJECT_ROOT,
    manifest_path: Path = DEFAULT_MANIFEST,
) -> tuple[int, int, bool]:
    manifest = _read_manifest(manifest_path)
    pack = _pack_config(manifest, pack_id)
    expected = int(pack.get("file_count") or 0)
    marker = project_root / "exp" / ".asset-packs" / f"{pack_id}.json"
    if not marker.is_file():
        return 0, expected, False
    try:
        marker_data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return 0, expected, False
    members = marker_data.get("members")
    if (
        marker_data.get("schema_version") != 1
        or marker_data.get("pack_id") != pack_id
        or marker_data.get("sha256") != str(pack.get("sha256") or "").lower()
        or not isinstance(members, list)
        or len(members) != expected
        or any(not isinstance(value, str) for value in members)
    ):
        return 0, expected, False
    project_root_resolved = project_root.resolve()
    present = 0
    for member_name in members:
        member_path = PurePosixPath(member_name)
        if (
            member_path.is_absolute()
            or not member_path.parts
            or ".." in member_path.parts
            or member_path.parts[0] != "exp"
        ):
            return 0, expected, False
        candidate = project_root.joinpath(*member_path.parts)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_relative_to(project_root_resolved) and resolved.is_file():
            present += 1
    return present, expected, True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("pack_id", nargs="?", default="exp-assets-v1")
    install = subparsers.add_parser("install")
    install.add_argument("pack_id", nargs="?", default="exp-assets-v1")
    install.add_argument("--source")
    install.add_argument("--force", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            present, expected, marker = pack_status(
                args.pack_id, manifest_path=args.manifest
            )
            print(
                f"{args.pack_id}: {present}/{expected} assets present; "
                f"install marker={'yes' if marker else 'no'}"
            )
            return 0 if present == expected else 1
        installed, skipped = install_pack(
            args.pack_id,
            manifest_path=args.manifest,
            source=args.source,
            force=args.force,
        )
        print(f"{args.pack_id}: installed={installed} skipped_existing={skipped}")
        return 0
    except ExpAssetError as exc:
        print(f"EXP asset error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
