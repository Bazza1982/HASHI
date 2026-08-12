from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Iterable


class UnsafeCleanupTarget(RuntimeError):
    """Raised when a requested cleanup can escape the disposable run root."""


class CleanupGuard:
    def __init__(self, lab_root: Path):
        self.lab_root = Path(lab_root).resolve(strict=True)
        self.runs_root = (self.lab_root / "runs").resolve(strict=True)

    def validate_run_root(self, run_root: Path) -> Path:
        raw = Path(run_root)
        if raw.is_symlink():
            raise UnsafeCleanupTarget("run root may not be a symlink")
        resolved = raw.resolve(strict=True)
        if resolved.parent != self.runs_root:
            raise UnsafeCleanupTarget("run root must be one direct child of the lab runs directory")
        if resolved == self.runs_root or resolved == self.lab_root:
            raise UnsafeCleanupTarget("refusing to treat a broad lab directory as a disposable run")
        manifest = resolved / "cleanup_manifest.json"
        if not manifest.is_file() or manifest.is_symlink():
            raise UnsafeCleanupTarget("cleanup manifest is missing or unsafe")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if payload.get("run_root") != str(resolved):
            raise UnsafeCleanupTarget("cleanup manifest does not identify the exact resolved run root")
        return resolved

    def delete_disposable(self, run_root: Path, relative_paths: Iterable[str]) -> list[str]:
        resolved_run = self.validate_run_root(run_root)
        manifest = json.loads((resolved_run / "cleanup_manifest.json").read_text(encoding="utf-8"))
        allowed = {str(item) for item in manifest.get("disposable_paths", [])}
        deleted: list[str] = []
        for raw in relative_paths:
            relative = Path(str(raw))
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise UnsafeCleanupTarget(f"unsafe cleanup path: {raw!r}")
            normalized = relative.as_posix()
            if normalized not in allowed:
                raise UnsafeCleanupTarget(f"cleanup path is not declared disposable: {normalized}")
            candidate = resolved_run / relative
            if candidate.is_symlink():
                raise UnsafeCleanupTarget(f"cleanup target may not be a symlink: {normalized}")
            resolved = candidate.resolve(strict=False)
            if resolved == resolved_run or resolved_run not in resolved.parents:
                raise UnsafeCleanupTarget(f"cleanup target escapes the run root: {normalized}")
            if resolved.exists():
                if resolved.is_dir():
                    shutil.rmtree(resolved)
                else:
                    resolved.unlink()
                deleted.append(normalized)
        return deleted
