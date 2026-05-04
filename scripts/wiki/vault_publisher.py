"""Versioned Obsidian vault publisher for generated wiki pages."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import WikiConfig
from .fetcher import has_private_content, has_sensitive_content
from .page_generator import PageDraft


GENERATED_TOPIC_DIR = "10_GENERATED_TOPICS"
GENERATED_INDEX_DIR = "30_GENERATED_INDEXES"
SYSTEM_DIR = "00_SYSTEM"
HISTORY_DIR = "wiki_publish_history"
BACKUP_DIR = "wiki_publish_backups"
STAGING_DIR = "wiki_publish_staging"
LATEST_MANIFEST = "wiki_publish_manifest_latest.json"


@dataclass(frozen=True)
class PublishedFile:
    source: str
    destination: str
    action: str
    previous_sha256: str | None
    new_sha256: str
    backup: str | None = None


@dataclass(frozen=True)
class VaultPublishResult:
    publish_id: str
    manifest_path: Path
    latest_manifest_path: Path
    files: list[PublishedFile]

    @property
    def created(self) -> int:
        return sum(1 for item in self.files if item.action == "created")

    @property
    def updated(self) -> int:
        return sum(1 for item in self.files if item.action == "updated")

    @property
    def unchanged(self) -> int:
        return sum(1 for item in self.files if item.action == "unchanged")


@dataclass(frozen=True)
class VaultRollbackResult:
    publish_id: str
    restored: int
    removed: int
    skipped: int


def publish_vault(
    config: WikiConfig,
    drafts: list[PageDraft],
    *,
    now: datetime | None = None,
) -> VaultPublishResult:
    """Publish generated dry-run drafts into the generated-only vault zone."""
    timestamp = now or datetime.now(ZoneInfo(config.timezone))
    publish_id = timestamp.strftime("%Y%m%dT%H%M%S%z")
    system_dir = config.vault_root / SYSTEM_DIR
    history_dir = system_dir / HISTORY_DIR
    backup_root = system_dir / BACKUP_DIR / publish_id
    staging_root = system_dir / STAGING_DIR / publish_id
    history_dir.mkdir(parents=True, exist_ok=True)
    backup_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)

    published: list[PublishedFile] = []
    staged: list[tuple[PageDraft, Path, Path]] = []
    for draft in drafts:
        relative_source = _relative_draft_path(config, draft.path)
        destination = _destination_for(config, relative_source)
        content = _prepare_vault_content(draft.path.read_text(encoding="utf-8"))
        _validate_staged_content(content, draft.path)
        staging_path = staging_root / relative_source
        staging_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(staging_path, content)
        staged.append((draft, destination, staging_path))

    staging_manifest = {
        "publish_id": publish_id,
        "generated_at": timestamp.isoformat(),
        "staging_root": str(staging_root),
        "files": [
            {
                "source": str(draft.path),
                "staging": str(staging_path),
                "destination": str(destination),
                "sha256": _file_sha256(staging_path),
            }
            for draft, destination, staging_path in staged
        ],
    }
    _atomic_write(
        staging_root / "manifest.json",
        json.dumps(staging_manifest, ensure_ascii=False, indent=2) + "\n",
    )

    for draft, destination, staging_path in staged:
        content = staging_path.read_text(encoding="utf-8")
        new_hash = _sha256(content)
        previous_hash = _file_sha256(destination) if destination.exists() else None
        if previous_hash == new_hash:
            published.append(
                PublishedFile(
                    source=str(draft.path),
                    destination=str(destination),
                    action="unchanged",
                    previous_sha256=previous_hash,
                    new_sha256=new_hash,
                )
            )
            continue

        backup_path: Path | None = None
        if destination.exists():
            backup_path = backup_root / destination.relative_to(config.vault_root)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, backup_path)
            action = "updated"
        else:
            action = "created"

        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(destination, content)
        published.append(
            PublishedFile(
                source=str(draft.path),
                destination=str(destination),
                action=action,
                previous_sha256=previous_hash,
                new_sha256=new_hash,
                backup=str(backup_path) if backup_path else None,
            )
        )

    manifest_path = history_dir / f"{publish_id}.json"
    latest_manifest_path = system_dir / LATEST_MANIFEST
    manifest = {
        "publish_id": publish_id,
        "generated_at": timestamp.isoformat(),
        "vault_root": str(config.vault_root),
        "generated_zones": [GENERATED_TOPIC_DIR, GENERATED_INDEX_DIR],
        "staging_root": str(staging_root),
        "backup_root": str(backup_root),
        "files": [asdict(item) for item in published],
        "summary": {
            "created": sum(1 for item in published if item.action == "created"),
            "updated": sum(1 for item in published if item.action == "updated"),
            "unchanged": sum(1 for item in published if item.action == "unchanged"),
        },
    }
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(manifest_path, payload)
    _atomic_write(latest_manifest_path, payload)
    return VaultPublishResult(
        publish_id=publish_id,
        manifest_path=manifest_path,
        latest_manifest_path=latest_manifest_path,
        files=published,
    )


def rollback_latest_publish(config: WikiConfig, *, manifest_path: Path | None = None) -> VaultRollbackResult:
    """Rollback the latest generated-zone vault publish."""
    selected_manifest = manifest_path or config.vault_root / SYSTEM_DIR / LATEST_MANIFEST
    manifest = json.loads(selected_manifest.read_text(encoding="utf-8"))
    restored = 0
    removed = 0
    skipped = 0
    for item in manifest.get("files", []):
        destination = Path(item["destination"])
        _assert_generated_destination(config, destination)
        backup = item.get("backup")
        if item.get("action") == "created":
            if destination.exists():
                destination.unlink()
                removed += 1
            else:
                skipped += 1
        elif item.get("action") == "updated" and backup:
            backup_path = Path(backup)
            if backup_path.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup_path, destination)
                restored += 1
            else:
                skipped += 1
        else:
            skipped += 1
    return VaultRollbackResult(
        publish_id=manifest["publish_id"],
        restored=restored,
        removed=removed,
        skipped=skipped,
    )


def _relative_draft_path(config: WikiConfig, path: Path) -> Path:
    try:
        relative = path.relative_to(config.dry_run_pages_dir)
    except ValueError as exc:
        raise ValueError(f"Draft path is outside dry-run pages dir: {path}") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"Unsafe draft path: {path}")
    return relative


def _destination_for(config: WikiConfig, relative_source: Path) -> Path:
    if relative_source.parts and relative_source.parts[0] == "Topics":
        destination = config.vault_root / GENERATED_TOPIC_DIR / Path(*relative_source.parts[1:])
    else:
        destination = config.vault_root / GENERATED_INDEX_DIR / relative_source
    _assert_generated_destination(config, destination)
    return destination


def _assert_generated_destination(config: WikiConfig, destination: Path) -> None:
    relative = destination.resolve().relative_to(config.vault_root.resolve())
    if not relative.parts or relative.parts[0] not in {GENERATED_TOPIC_DIR, GENERATED_INDEX_DIR}:
        raise ValueError(f"Refusing to write outside generated vault zones: {destination}")


def _prepare_vault_content(content: str) -> str:
    text = content.replace("status: dry-run", "status: auto-generated", 1)
    text = text.replace(
        "<!-- WIKI-GENERATED: dry-run draft; do not treat as final synthesis. -->",
        "<!-- WIKI-GENERATED: auto-published generated page; edit source pipeline, not this file. -->",
        1,
    )
    return text


def _validate_staged_content(content: str, source: Path) -> None:
    if not content.strip():
        raise ValueError(f"Refusing to publish empty generated page: {source}")
    if "status: auto-generated" not in content:
        raise ValueError(f"Generated page did not pass auto-generated marker check: {source}")
    if has_private_content(content) or has_sensitive_content(content):
        raise ValueError(f"Generated page failed privacy scan: {source}")


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
