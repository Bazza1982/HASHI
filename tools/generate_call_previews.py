#!/usr/bin/env python3
"""Offline generation of immutable Call voice samples using a configured target.

Credentials and instance paths are supplied locally, never written to the bundle.
Re-running resumes verified entries. A provider/model change needs a new bundle.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.config_json import read_config_json
from orchestrator.enterprise.secret_refs import ConnectorSecretResolver
from orchestrator.frontend_call.adapters import MediaAdapters
from orchestrator.frontend_call.config import CallConfig, OPENROUTER_GEMINI_TTS_MODELS
from orchestrator.frontend_call.voice_catalog import GEMINI_VOICES
from orchestrator.frontend_call.voice_previews import PREVIEW_ROOT, PREVIEW_TEXT, PREVIEW_VERSION
from orchestrator.media_runtime import configured_media_executable
from orchestrator.voice_preview_bundle import validate_voice_preview_bundle
from orchestrator.voice_synthesizer import convert_audio_to_ogg


async def generate(args):
    os.environ["BRIDGE_HOME"] = str(args.bridge_home.resolve())
    _, targets = CallConfig(args.bridge_home / "call_profiles.json").read()
    target = targets[args.target]
    if target["kind"] != "tts" or target["model"] not in OPENROUTER_GEMINI_TTS_MODELS:
        raise ValueError("Select a qualified Gemini TTS target")
    ffmpeg = configured_media_executable("ffmpeg", bridge_home=args.bridge_home)
    if not ffmpeg:
        raise ValueError("Configure the instance media executable before generation")
    adapter = MediaAdapters(secret_resolver=ConnectorSecretResolver(
        secrets=read_config_json(args.bridge_home / "secrets.json")))
    version_root = args.output / PREVIEW_VERSION
    manifest_path = version_root / "manifest.json"
    identity = {key: target[key] for key in ("adapter", "base_url", "model")}
    manifest = {"schema_version": 1, "version": PREVIEW_VERSION, **identity,
        "text": PREVIEW_TEXT, "options": {}, "entries": []}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(manifest.get(key) != value for key, value in identity.items()) or manifest.get("text") != PREVIEW_TEXT:
            raise ValueError("Existing bundle has different synthesis provenance")
        validate_voice_preview_bundle(root=args.output, version=PREVIEW_VERSION,
                                      require_complete=False, allowed_formats=("ogg", "mp3"))
    completed = {(entry["locale"], entry["profile"]) for entry in manifest["entries"] if entry["renderer"] == "call"}
    voices = args.voices or list(GEMINI_VOICES)
    if any(voice not in target["voices"] or voice not in GEMINI_VOICES for voice in voices):
        raise ValueError("Voice is not registered in this target")
    semaphore, lock = asyncio.Semaphore(2), asyncio.Lock()
    failures = []

    async def one(locale, voice):
        if (locale, voice) in completed:
            return
        async with semaphore:
            started = time.monotonic()
            try:
                result = await adapter.synthesize(target, {"voice_id": voice, "options": {}}, PREVIEW_TEXT[locale])
                with tempfile.TemporaryDirectory(prefix="call-preview-") as temporary:
                    source = Path(temporary) / "source.wav"
                    source.write_bytes(base64.b64decode(result["content_b64"], validate=True))
                    encoded = Path(temporary) / "call.ogg"
                    await convert_audio_to_ogg(ffmpeg, source, encoded)
                    payload = encoded.read_bytes()
                if not payload.startswith(b"OggS"):
                    raise ValueError("Invalid converted sample")
                relative = f"{locale}/{voice}/call.ogg"
                path = version_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
                async with lock:
                    manifest["entries"].append({"locale": locale, "profile": voice, "renderer": "call",
                        "path": relative, "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
                    manifest["entries"].sort(key=lambda entry: (entry["locale"], entry["profile"]))
                    temporary_manifest = manifest_path.with_suffix(".tmp")
                    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    temporary_manifest.replace(manifest_path)
                print(json.dumps({"voice": voice, "locale": locale, "bytes": len(payload),
                    "ms": round((time.monotonic()-started)*1000)}), flush=True)
            except Exception as exc:
                failure = {"voice": voice, "locale": locale, "error": type(exc).__name__}
                failures.append(failure)
                print(json.dumps(failure), flush=True)

    await asyncio.gather(*(one(locale, voice) for locale in PREVIEW_TEXT for voice in voices))
    # Browser previews use MP3 for Safari/WebKit; Telegram keeps the existing OGG contract.
    # Transcode verified samples offline, so selecting a voice never performs synthesis.
    for entry in list(manifest["entries"]):
        if entry["renderer"] != "call":
            continue
        relative = f"{entry['locale']}/{entry['profile']}/call_mp3.mp3"
        if any(item["path"] == relative for item in manifest["entries"]):
            continue
        source, destination = version_root / entry["path"], version_root / relative
        process = await asyncio.create_subprocess_exec(ffmpeg, "-y", "-v", "error", "-i", str(source),
            "-ac", "1", "-ar", "24000", "-c:a", "libmp3lame", "-b:a", "96k", str(destination),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await process.communicate()
        if process.returncode:
            raise RuntimeError("MP3 preview conversion failed")
        payload = destination.read_bytes()
        manifest["entries"].append({"locale": entry["locale"], "profile": entry["profile"],
            "renderer": "call_mp3", "format": "mp3", "path": relative,
            "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
    manifest["entries"].sort(key=lambda entry: (entry["locale"], entry["profile"], entry["renderer"]))
    temporary_manifest = manifest_path.with_suffix(".tmp")
    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_manifest.replace(manifest_path)
    indexed = validate_voice_preview_bundle(root=args.output, version=PREVIEW_VERSION,
                                           require_complete=False, allowed_formats=("ogg", "mp3"))
    expected = {(locale, voice, renderer) for locale in PREVIEW_TEXT for voice in voices for renderer in ("call", "call_mp3")}
    if failures or not expected.issubset(indexed):
        raise RuntimeError("Preview generation incomplete; rerun to resume missing samples")
    print(json.dumps({"verified_samples": len(indexed), "model": target["model"]}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-home", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", type=Path, default=PREVIEW_ROOT)
    parser.add_argument("--voices", nargs="+")
    args = parser.parse_args()
    asyncio.run(asyncio.wait_for(generate(args), timeout=600))
