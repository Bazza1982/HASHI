from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def normalize_windows_like_path(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").lower()


def find_loaded_extension_id(secure_preferences_path: Path, extension_path: str) -> str | None:
    data = json.loads(secure_preferences_path.read_text(encoding="utf-8"))
    settings = ((data.get("extensions") or {}).get("settings") or {})
    expected = normalize_windows_like_path(extension_path)
    for extension_id, info in settings.items():
        candidate = str(info.get("path") or "")
        if candidate and normalize_windows_like_path(candidate) == expected:
            return extension_id
    return None


def update_native_host_allowed_origins(
    manifest_path: Path,
    required_origins: list[str],
) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = list(manifest.get("allowed_origins") or [])
    merged: list[str] = []
    for origin in [*existing, *required_origins]:
        if origin not in merged:
            merged.append(origin)
    manifest["allowed_origins"] = merged
    manifest_path.write_text(json.dumps(manifest, indent=4) + "\n", encoding="utf-8")
    return manifest

