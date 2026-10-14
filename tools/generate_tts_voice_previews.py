#!/usr/bin/env python3
"""Offline generation of exact prerecorded samples for /voice TTS presets.

The generator uses each preset's real provider and voice.  It never substitutes
another provider.  Credentials, local model paths, and runtime locations are
supplied by the instance and are not copied into the product bundle.
"""

from __future__ import annotations

# ruff: noqa: E402 - direct execution bootstraps the repository import path.

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestrator.media_runtime import configured_media_executable
from orchestrator.tts_providers import build_provider
from orchestrator.tts_voice_previews import (
    DEFAULT_TTS_VOICE_PREVIEW_ASSET_ROOT,
    TTS_VOICE_PREVIEW_KIND,
    TTS_VOICE_PREVIEW_TEXT,
)
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_preview_bundle import (
    VOICE_PREVIEW_VERSION,
    validate_voice_preview_bundle,
)


def _stable_voice_id(provider: str, voice_name: object) -> str:
    value = str(voice_name or "").strip()
    return Path(value).name if provider == "piper" else value


def _entry(
    path: Path,
    *,
    version_root: Path,
    alias: str,
    locale: str,
    renderer: str,
    audio_format: str,
    provider: str,
    voice: str,
    script: str,
) -> dict:
    payload = path.read_bytes()
    return {
        "locale": locale,
        "profile": alias,
        "renderer": renderer,
        "format": audio_format,
        "path": path.relative_to(version_root).as_posix(),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "provider": provider,
        "voice": voice,
        "script": script,
    }


def _write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


async def _transcode_mp3(ffmpeg: str, source: Path, destination: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "24000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "96k",
        str(destination),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode or not destination.is_file():
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"MP3 preview conversion failed: {detail}")


async def generate(args: argparse.Namespace) -> Path:
    bridge_home = args.bridge_home.resolve()
    os.environ["BRIDGE_HOME"] = str(bridge_home)
    ffmpeg = configured_media_executable("ffmpeg", bridge_home=bridge_home)
    if not ffmpeg:
        raise ValueError("Configure the instance media executable before generation")

    output_root = args.output.resolve()
    version_root = output_root / VOICE_PREVIEW_VERSION
    manifest_path = version_root / "manifest.json"
    manifest = {
        "schema_version": 1,
        "version": VOICE_PREVIEW_VERSION,
        "kind": TTS_VOICE_PREVIEW_KIND,
        "text": TTS_VOICE_PREVIEW_TEXT,
        "entries": [],
    }
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("kind") != TTS_VOICE_PREVIEW_KIND
            or manifest.get("text") != TTS_VOICE_PREVIEW_TEXT
        ):
            raise ValueError("Existing bundle has different synthesis provenance")
        validate_voice_preview_bundle(
            root=output_root,
            version=VOICE_PREVIEW_VERSION,
            require_complete=False,
            allowed_formats=("ogg", "mp3"),
        )

    aliases = args.aliases or list(VoiceManager.VOICE_PRESETS)
    unknown = [alias for alias in aliases if alias not in VoiceManager.VOICE_PRESETS]
    if unknown:
        raise ValueError(f"Unknown voice preset aliases: {', '.join(unknown)}")
    completed = {
        (str(entry.get("profile")), str(entry.get("renderer")))
        for entry in manifest["entries"]
        if isinstance(entry, dict)
    }
    failures: list[dict] = []
    manager = VoiceManager(
        bridge_home / "state" / "preview-generator",
        bridge_home / "media",
        ffmpeg_cmd=ffmpeg,
    )

    for alias in aliases:
        if {(alias, "tts"), (alias, "tts_mp3")} <= completed and not args.force:
            continue
        preset = manager._preset_payload(alias) or VoiceManager.VOICE_PRESETS[alias]
        provider_name = str(preset["provider"]).strip().casefold()
        locale = str(preset["locale"])
        script = TTS_VOICE_PREVIEW_TEXT[locale]
        voice_name = str(preset["voice_name"])
        voice_id = _stable_voice_id(provider_name, voice_name)
        started = time.monotonic()
        try:
            provider = build_provider(provider_name, ffmpeg_cmd=ffmpeg)
            provider_options = dict(preset.get("provider_options") or {})
            if provider_name in {"kokoro", "coqui"}:
                provider_options.setdefault("python_exe", sys.executable)
            with tempfile.TemporaryDirectory(prefix=f"tts-preview-{alias}-") as raw:
                temporary = Path(raw)
                asset = await provider.synthesize(
                    text=script,
                    output_dir=temporary,
                    stem="preview",
                    voice_name=voice_name,
                    rate=0,
                    max_chars=240,
                    provider_options=provider_options,
                )
                ogg_path = version_root / locale / alias / "tts.ogg"
                mp3_path = version_root / locale / alias / "tts_mp3.mp3"
                ogg_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(asset.ogg_path, ogg_path)
                await _transcode_mp3(ffmpeg, ogg_path, mp3_path)
            manifest["entries"] = [
                entry
                for entry in manifest["entries"]
                if not (
                    entry.get("profile") == alias
                    and entry.get("renderer") in {"tts", "tts_mp3"}
                )
            ]
            manifest["entries"].extend(
                [
                    _entry(
                        ogg_path,
                        version_root=version_root,
                        alias=alias,
                        locale=locale,
                        renderer="tts",
                        audio_format="ogg",
                        provider=provider_name,
                        voice=voice_id,
                        script=script,
                    ),
                    _entry(
                        mp3_path,
                        version_root=version_root,
                        alias=alias,
                        locale=locale,
                        renderer="tts_mp3",
                        audio_format="mp3",
                        provider=provider_name,
                        voice=voice_id,
                        script=script,
                    ),
                ]
            )
            manifest["entries"].sort(
                key=lambda item: (
                    str(item.get("locale")),
                    str(item.get("profile")),
                    str(item.get("renderer")),
                )
            )
            _write_manifest(manifest_path, manifest)
            print(
                json.dumps(
                    {
                        "alias": alias,
                        "provider": provider_name,
                        "voice": voice_id,
                        "locale": locale,
                        "ms": round((time.monotonic() - started) * 1000),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:
            failure = {
                "alias": alias,
                "provider": provider_name,
                "error": type(exc).__name__,
            }
            failures.append(failure)
            print(json.dumps(failure), flush=True)

    indexed = validate_voice_preview_bundle(
        root=output_root,
        version=VOICE_PREVIEW_VERSION,
        require_complete=False,
        allowed_formats=("ogg", "mp3"),
    )
    expected = {
        (str(VoiceManager.VOICE_PRESETS[alias]["locale"]), alias, renderer)
        for alias in aliases
        for renderer in ("tts", "tts_mp3")
    }
    if failures or not expected.issubset(indexed):
        raise RuntimeError("Preview generation incomplete; rerun to resume missing samples")
    print(json.dumps({"verified_samples": len(indexed)}), flush=True)
    return manifest_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bridge-home",
        type=Path,
        default=ROOT,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_TTS_VOICE_PREVIEW_ASSET_ROOT,
    )
    parser.add_argument("--aliases", nargs="+")
    parser.add_argument("--force", action="store_true")
    return parser


if __name__ == "__main__":
    parsed = _parser().parse_args()
    asyncio.run(asyncio.wait_for(generate(parsed), timeout=900))
