"""Resolve immutable previews for concrete /voice TTS presets.

Selecting a voice only reads a verified product asset.  It never invokes a
provider, and it never substitutes another voice when the exact sample is
missing.
"""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.voice_preview_bundle import (
    VOICE_PREVIEW_VERSION,
    VoicePreviewBundleError,
    validate_voice_preview_bundle,
)


DEFAULT_TTS_VOICE_PREVIEW_ASSET_ROOT = (
    Path(__file__).resolve().parent / "tts_voice_preview_assets"
)
TTS_VOICE_PREVIEW_KIND = "tts_voice_presets"
TTS_VOICE_PREVIEW_TEXT = {
    "en-US": "Hello, this is my voice. I can read your messages clearly and naturally.",
    "en-GB": "Hello, this is my voice. I can read your messages clearly and naturally.",
    "en-IE": "Hello, this is my voice. I can read your messages clearly and naturally.",
    "zh-CN": "您好，这是我的声音。我会清楚自然地为您朗读文字。",
    "zh-TW": "您好，這是我的聲音。我會清楚自然地為您朗讀文字。",
    "zh-HK": "你好，呢個係我嘅聲音。我會清楚自然噉為你朗讀文字。",
    "ja-JP": "こんにちは、これは私の声です。文章を自然にはっきり読み上げます。",
}


def _stable_voice_id(provider: object, voice_name: object) -> str:
    value = str(voice_name or "").strip()
    if str(provider or "").strip().casefold() == "piper":
        return Path(value).name
    return value


def get_tts_voice_preview_assets(
    alias: str,
    preset: dict,
    *,
    telegram: bool,
    root: Path | None = None,
) -> tuple[tuple[str, Path], ...]:
    """Return one exact prerecorded sample after digest/provenance validation."""

    preset_id = str(alias or "").strip().casefold()
    locale = str(preset.get("locale") or "").strip()
    provider = str(preset.get("provider") or "").strip().casefold()
    voice = _stable_voice_id(provider, preset.get("voice_name"))
    if not preset_id or not locale or not provider or not voice:
        return ()

    asset_root = Path(root or DEFAULT_TTS_VOICE_PREVIEW_ASSET_ROOT).resolve()
    manifest_path = asset_root / VOICE_PREVIEW_VERSION / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("kind") != TTS_VOICE_PREVIEW_KIND
            or manifest.get("text") != TTS_VOICE_PREVIEW_TEXT
            or not isinstance(manifest.get("entries"), list)
        ):
            return ()
        renderer = "tts" if telegram else "tts_mp3"
        entry = next(
            (
                item
                for item in manifest["entries"]
                if isinstance(item, dict)
                and item.get("locale") == locale
                and item.get("profile") == preset_id
                and item.get("renderer") == renderer
            ),
            None,
        )
        if (
            entry is None
            or str(entry.get("provider") or "").strip().casefold() != provider
            or _stable_voice_id(provider, entry.get("voice")).casefold()
            != voice.casefold()
            or entry.get("script") != TTS_VOICE_PREVIEW_TEXT.get(locale)
        ):
            return ()
        assets = validate_voice_preview_bundle(
            root=asset_root,
            version=VOICE_PREVIEW_VERSION,
            require_complete=False,
            allowed_formats=("ogg", "mp3"),
        )
        path = assets.get((locale, preset_id, renderer))
    except (OSError, UnicodeError, ValueError, TypeError, VoicePreviewBundleError):
        return ()
    return ((renderer, path),) if path else ()


__all__ = [
    "DEFAULT_TTS_VOICE_PREVIEW_ASSET_ROOT",
    "TTS_VOICE_PREVIEW_KIND",
    "TTS_VOICE_PREVIEW_TEXT",
    "get_tts_voice_preview_assets",
]
