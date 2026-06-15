from __future__ import annotations

import json
import shutil
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class BackupItem:
    name: str
    path: Path
    required: bool = False


@dataclass(frozen=True)
class BackupResult:
    archive_path: Path
    manifest: dict


class EnterpriseBackup:
    MANIFEST_NAME = "manifest.json"

    def create_archive(self, output_path: Path | str, items: list[BackupItem]) -> BackupResult:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "backup_id": f"backup-{uuid4().hex}",
            "created_at": _utc_now_iso(),
            "items": [],
        }
        with tarfile.open(output, "w:gz") as archive:
            for item in items:
                name = _safe_archive_name(item.name)
                path = Path(item.path)
                exists = path.exists()
                if item.required and not exists:
                    raise FileNotFoundError(f"required backup item missing: {path}")
                entry = {
                    "name": name,
                    "source_path": str(path),
                    "required": item.required,
                    "included": exists,
                    "kind": "missing",
                }
                if exists:
                    entry["kind"] = "directory" if path.is_dir() else "file"
                    archive.add(path, arcname=name)
                manifest["items"].append(entry)
            encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
            info = tarfile.TarInfo(self.MANIFEST_NAME)
            info.size = len(encoded)
            archive.addfile(info, _BytesReader(encoded))
        return BackupResult(archive_path=output, manifest=manifest)

    def read_manifest(self, archive_path: Path | str) -> dict:
        with tarfile.open(Path(archive_path), "r:gz") as archive:
            member = archive.getmember(self.MANIFEST_NAME)
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError("backup manifest missing")
            return json.loads(extracted.read().decode("utf-8"))

    def restore_archive(self, archive_path: Path | str, destination: Path | str, *, overwrite: bool = False) -> dict:
        destination_path = Path(destination)
        destination_path.mkdir(parents=True, exist_ok=True)
        restored: list[str] = []
        with tarfile.open(Path(archive_path), "r:gz") as archive:
            for member in archive.getmembers():
                if member.name == self.MANIFEST_NAME:
                    continue
                try:
                    target = _safe_restore_target(destination_path, member.name)
                except ValueError as exc:
                    raise ValueError(f"unsafe restore member: {member.name!r}") from exc
                if target.exists() and not overwrite:
                    raise FileExistsError(f"restore target exists: {target}")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError(f"restore member has no file content: {member.name!r}")
                    with target.open("wb") as handle:
                        shutil.copyfileobj(source, handle)
                else:
                    raise ValueError(f"unsupported restore member type: {member.name!r}")
                restored.append(member.name)
        return {"destination": str(destination_path), "restored": restored}


class _BytesReader:
    def __init__(self, data: bytes):
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._data) - self._offset
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _safe_archive_name(name: str) -> str:
    normalized = str(name or "").strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
        raise ValueError(f"unsafe backup item name: {name!r}")
    return normalized


def _safe_restore_target(destination: Path, member_name: str) -> Path:
    normalized = _safe_archive_name(member_name)
    target = (destination / normalized).resolve()
    destination_resolved = destination.resolve()
    if target != destination_resolved and destination_resolved not in target.parents:
        raise ValueError(f"unsafe restore member: {member_name!r}")
    return target
