"""Validation for immutable voice-preview assets shipped with HASHI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any


VOICE_PREVIEW_BUNDLE_SCHEMA_VERSION = 1
VOICE_PREVIEW_VERSION = "v1"
VOICE_PREVIEW_LOCALES = ("en", "zh-CN")
VOICE_PREVIEW_PROFILES = (
    "warm_female",
    "clear_female",
    "warm_male",
    "calm_male",
)
VOICE_PREVIEW_RENDERERS = ("native", "tts")
DEFAULT_VOICE_PREVIEW_ASSET_ROOT = (
    Path(__file__).resolve().parent / "voice_preview_assets"
)


class VoicePreviewBundleError(RuntimeError):
    """Raised when a packaged voice-preview bundle is absent or invalid."""


def _manifest_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VoicePreviewBundleError("voice preview manifest is unreadable") from exc
    if not isinstance(payload, dict):
        raise VoicePreviewBundleError("voice preview manifest must be an object")
    return payload


def validate_voice_preview_bundle(
    *,
    root: Path | None = None,
    version: str = VOICE_PREVIEW_VERSION,
    require_complete: bool = True,
) -> dict[tuple[str, str, str], Path]:
    """Validate one complete bundle and return its indexed asset paths."""

    asset_root = Path(root or DEFAULT_VOICE_PREVIEW_ASSET_ROOT).resolve()
    version_root = (asset_root / version).resolve()
    manifest = _manifest_object(version_root / "manifest.json")
    if manifest.get("schema_version") != VOICE_PREVIEW_BUNDLE_SCHEMA_VERSION:
        raise VoicePreviewBundleError("voice preview manifest schema is unsupported")
    if manifest.get("version") != version:
        raise VoicePreviewBundleError("voice preview manifest version does not match")
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise VoicePreviewBundleError("voice preview manifest entries are invalid")

    indexed: dict[tuple[str, str, str], Path] = {}
    seen_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise VoicePreviewBundleError("voice preview manifest entry is invalid")
        locale = str(entry.get("locale") or "").strip()
        profile = str(entry.get("profile") or "").strip()
        renderer = str(entry.get("renderer") or "").strip()
        relative_text = str(entry.get("path") or "").strip()
        relative = PurePosixPath(relative_text)
        expected_relative = PurePosixPath(locale, profile, f"{renderer}.ogg")
        if (
            not locale
            or not profile
            or not renderer
            or relative.is_absolute()
            or relative != expected_relative
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise VoicePreviewBundleError("voice preview manifest path is invalid")
        key = (locale, profile, renderer)
        if key in indexed or relative_text in seen_paths:
            raise VoicePreviewBundleError("voice preview manifest has duplicate entries")
        candidate = (version_root / Path(*relative.parts)).resolve()
        try:
            candidate.relative_to(version_root)
        except ValueError as exc:
            raise VoicePreviewBundleError(
                "voice preview asset escapes its bundle"
            ) from exc
        try:
            payload = candidate.read_bytes()
        except OSError as exc:
            raise VoicePreviewBundleError("voice preview asset is missing") from exc
        if len(payload) <= 4 or not payload.startswith(b"OggS"):
            raise VoicePreviewBundleError("voice preview asset is not a valid OGG file")
        if entry.get("size_bytes") != len(payload):
            raise VoicePreviewBundleError("voice preview asset size does not match")
        if str(entry.get("sha256") or "").casefold() != hashlib.sha256(
            payload
        ).hexdigest():
            raise VoicePreviewBundleError("voice preview asset digest does not match")
        indexed[key] = candidate
        seen_paths.add(relative_text)

    expected = {
        (locale, profile, renderer)
        for locale in VOICE_PREVIEW_LOCALES
        for profile in VOICE_PREVIEW_PROFILES
        for renderer in VOICE_PREVIEW_RENDERERS
    }
    if require_complete and set(indexed) != expected:
        raise VoicePreviewBundleError("voice preview bundle coverage is incomplete")
    return indexed


__all__ = [
    "DEFAULT_VOICE_PREVIEW_ASSET_ROOT",
    "VOICE_PREVIEW_BUNDLE_SCHEMA_VERSION",
    "VOICE_PREVIEW_VERSION",
    "VoicePreviewBundleError",
    "validate_voice_preview_bundle",
]
