"""Read-only loader for HASHI EXP directories.

EXP is intentionally outside HASHI core runtime wiring. This module gives tests,
agents, and future commands a stable way to discover and read context-specific
experience without registering anything globally.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExpEntry:
    """A resolved EXP domain on disk."""

    exp_id: str
    root: Path
    manifest: dict[str, Any]

    @property
    def overview_path(self) -> Path:
        return self.root / "EXP.md"

    def read_overview(self) -> str:
        return self.overview_path.read_text(encoding="utf-8")

    def playbook_path(self, name: str) -> Path:
        playbooks = self.manifest.get("playbooks", {})
        try:
            relative = playbooks[name]
        except KeyError as exc:
            available = ", ".join(sorted(playbooks)) or "none"
            raise KeyError(f"Unknown EXP playbook {name!r}; available: {available}") from exc
        return self.root / relative

    def read_playbook(self, name: str) -> str:
        return self.playbook_path(name).read_text(encoding="utf-8")


class ExpStore:
    """Discover and read EXP domains under a HASHI repository."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else Path(__file__).resolve().parent

    def list_ids(self) -> list[str]:
        ids: list[str] = []
        for manifest_path in sorted(self.root.glob("*/*/manifest.json")):
            manifest = self._read_json(manifest_path)
            exp_id = manifest.get("id")
            if isinstance(exp_id, str):
                ids.append(exp_id)
        return ids

    def get(self, exp_id: str) -> ExpEntry:
        manifest_path = self._manifest_path(exp_id)
        manifest = self._read_json(manifest_path)
        if manifest.get("type") != "exp":
            raise ValueError(f"{exp_id!r} is not an EXP manifest")
        actual_id = manifest.get("id")
        if actual_id != exp_id:
            raise ValueError(f"Manifest id mismatch: expected {exp_id!r}, found {actual_id!r}")
        return ExpEntry(exp_id=exp_id, root=manifest_path.parent, manifest=manifest)

    def get_manifest(self, exp_id: str) -> dict[str, Any]:
        return dict(self.get(exp_id).manifest)

    def get_playbook(self, exp_id: str, name: str) -> str:
        return self.get(exp_id).read_playbook(name)

    def _manifest_path(self, exp_id: str) -> Path:
        parts = exp_id.split("/")
        if len(parts) != 2 or any(not part for part in parts):
            raise ValueError("EXP id must have the shape '<owner>/<domain>'")
        manifest_path = self.root.joinpath(*parts, "manifest.json")
        if not manifest_path.exists():
            raise FileNotFoundError(f"EXP manifest not found: {manifest_path}")
        return manifest_path

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"Expected JSON object in {path}")
        return data
