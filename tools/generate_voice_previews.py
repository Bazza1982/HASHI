#!/usr/bin/env python3
"""Generate stable native-audio and TTS previews for the /voice menu."""

from __future__ import annotations

# ruff: noqa: E402 - direct execution bootstraps the repository import path.

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sys
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.openrouter_api import OpenRouterAdapter
from orchestrator.multimodal_contract import canonical_request_content
from orchestrator.tts_providers import build_provider
from orchestrator.voice_manager import VoiceManager
from orchestrator.voice_synthesizer import convert_audio_to_ogg

PREVIEW_TEXT = {
    "en": {
        "native": "Native audio model preview. Hello, this is my voice.",
        "tts": "Text model plus text-to-speech preview. Hello, this is my voice.",
        "language": "English",
        "tts_language": "en",
    },
    "zh-CN": {
        "native": "原生音频模型试听。您好，这是我的声音。",
        "tts": "文字模型加文字转语音试听。您好，这是我的声音。",
        "language": "Mandarin Chinese",
        "tts_language": "zh",
    },
}

SUPPORTED_NATIVE_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
)


def _read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise TypeError(f"Expected a JSON object: {path}")
    return payload


def _native_voice(profile_id: str) -> str:
    candidates = VoiceManager.VOICE_PROFILES[profile_id]["native_voices"]
    supported = {voice.casefold(): voice for voice in SUPPORTED_NATIVE_VOICES}
    for candidate in candidates:
        if str(candidate).casefold() in supported:
            return supported[str(candidate).casefold()]
    raise RuntimeError(f"No supported native voice for profile {profile_id}")


def _tts_voice(profile_id: str, locale: str) -> str:
    language = PREVIEW_TEXT[locale]["tts_language"]
    voices = VoiceManager.VOICE_PROFILES[profile_id]["tts_voices"]
    voice = str(voices.get(language) or voices.get("en") or "").strip()
    if not voice:
        raise RuntimeError(f"No TTS voice for profile {profile_id}/{locale}")
    return voice


def _write_silence(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 4_000)
    return path.read_bytes()


def _voice_request_content(path: Path, payload: bytes) -> dict:
    return canonical_request_content(
        [
            {
                "type": "media",
                "item_index": 1,
                "attachment_id": f"preview-input-{uuid4().hex}",
                "modality": "audio",
                "kind": "voice",
                "semantic_role": "voice_message",
                "mime_type": "audio/wav",
                "filename": path.name,
                "caption": "",
                "local_ref": str(path),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "transport": {},
            }
        ]
    )


async def _generate_native(
    *,
    bridge_home: Path,
    openrouter_url: str,
    api_key: str,
    model: str,
    voice: str,
    locale: str,
    output_path: Path,
) -> str:
    request_id = f"voice-preview-{locale}-{voice}-{uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="hashi-voice-preview-") as raw_temp:
        temporary = Path(raw_temp)
        silence = temporary / "input.wav"
        silence_payload = _write_silence(silence)
        config = SimpleNamespace(
            name="voice-preview-generator",
            engine="openrouter-api",
            model=model,
            workspace_dir=temporary,
            system_md=None,
            extra={
                "input_modalities": ["text", "audio"],
                "input_policy": "audio_required",
                "input_transports": {"audio": ["inline"]},
                "input_formats": {"audio": ["wav"]},
                "output_modalities": ["text", "audio"],
                "output_formats": {"audio": ["pcm16"]},
                "supported_voices": list(SUPPORTED_NATIVE_VOICES),
                "api_surface": "chat_completions",
                "output_streaming": "sse",
                "provider_output_transcript": True,
                "function_calling": False,
                "native_audio_voice": voice,
                "native_audio_format": "pcm16",
                "native_audio_retention_seconds": 60,
                "audio_model_tools": False,
                "_native_audio_claim_request_id": request_id,
            },
        )
        global_config = SimpleNamespace(
            openrouter_url=openrouter_url,
            base_media_dir=temporary,
            bridge_home=temporary,
            project_root=bridge_home,
        )
        adapter = OpenRouterAdapter(config, global_config, api_key=api_key)
        if not await adapter.initialize():
            raise RuntimeError("OpenRouter preview adapter did not initialize")
        adapter.sys_prompt = (
            "You are making a short voice preview. Speak exactly the requested "
            "sentence once. Do not add, omit, paraphrase, explain, or greet. "
            "Use a natural, neutral speaking style."
        )
        sentence = PREVIEW_TEXT[locale]["native"]
        prompt = (
            f"Read this exact sentence in {PREVIEW_TEXT[locale]['language']}: "
            f"{sentence}"
        )
        try:
            response = await adapter.generate_response(
                prompt,
                request_id,
                request_content=_voice_request_content(silence, silence_payload),
            )
            if not response.is_success:
                raise RuntimeError(response.error or "Native preview generation failed")
            audio_part = next(
                (part for part in response.content if part.get("type") == "audio"),
                None,
            )
            if not audio_part:
                raise RuntimeError("Native preview response contained no audio")
            store = adapter._native_audio_asset_store_instance()
            store.claim(
                str(audio_part["asset_id"]),
                owner_id="voice-preview-generator",
                session_id="voice-preview-generator",
                request_id=request_id,
            )
            _metadata, audio_payload = store.read_bytes(
                str(audio_part["asset_id"]),
                owner_id="voice-preview-generator",
                session_id="voice-preview-generator",
            )
            source = temporary / "native.wav"
            source.write_bytes(audio_payload)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            converted = temporary / "native.ogg"
            await convert_audio_to_ogg("ffmpeg", source, converted)
            staged = output_path.with_name(f".{output_path.stem}-{uuid4().hex}.ogg")
            shutil.copyfile(converted, staged)
            os.replace(staged, output_path)
            return str(response.text or "").strip()
        finally:
            await adapter.shutdown()


async def _generate_tts(
    *,
    locale: str,
    voice: str,
    output_path: Path,
) -> None:
    provider = build_provider("edge", ffmpeg_cmd="ffmpeg")
    with tempfile.TemporaryDirectory(prefix="hashi-tts-preview-") as raw_temp:
        temporary = Path(raw_temp)
        asset = await provider.synthesize(
            text=PREVIEW_TEXT[locale]["tts"],
            output_dir=temporary,
            stem="preview",
            voice_name=voice,
            rate=0,
            max_chars=240,
            provider_options={},
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        staged = output_path.with_name(f".{output_path.stem}-{uuid4().hex}.ogg")
        shutil.copyfile(asset.ogg_path, staged)
        os.replace(staged, output_path)


def _record(path: Path, **metadata) -> dict:
    payload = path.read_bytes()
    return {
        **metadata,
        "path": str(path),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


async def generate(args: argparse.Namespace) -> Path:
    bridge_home = args.bridge_home.resolve()
    config = _read_json(bridge_home / "agents.json")
    secrets = _read_json(bridge_home / "secrets.json")
    api_key = str(secrets.get("openrouter_key") or "").strip()
    if not api_key:
        raise RuntimeError("secrets.json has no openrouter_key")
    openrouter_url = str(
        (config.get("global") or {}).get("openrouter_url")
        or "https://openrouter.ai/api/v1/chat/completions"
    )
    output_root = (
        args.output_root.resolve()
        if args.output_root
        else bridge_home
        / "media"
        / "_voice_previews"
        / VoiceManager.VOICE_PREVIEW_VERSION
    )
    entries: list[dict] = []
    for locale in args.locale:
        for profile_id in VoiceManager.VOICE_PROFILES:
            profile_root = output_root / locale / profile_id
            native_path = profile_root / "native.ogg"
            tts_path = profile_root / "tts.ogg"
            native_voice = _native_voice(profile_id)
            tts_voice = _tts_voice(profile_id, locale)
            if args.force or not native_path.exists():
                transcript = await _generate_native(
                    bridge_home=bridge_home,
                    openrouter_url=openrouter_url,
                    api_key=api_key,
                    model=args.native_model,
                    voice=native_voice,
                    locale=locale,
                    output_path=native_path,
                )
            else:
                transcript = ""
            if args.force or not tts_path.exists():
                await _generate_tts(
                    locale=locale,
                    voice=tts_voice,
                    output_path=tts_path,
                )
            entries.extend(
                [
                    _record(
                        native_path,
                        locale=locale,
                        profile=profile_id,
                        renderer="native",
                        provider="openrouter-api",
                        model=args.native_model,
                        voice=native_voice,
                        script=PREVIEW_TEXT[locale]["native"],
                        provider_transcript=transcript,
                    ),
                    _record(
                        tts_path,
                        locale=locale,
                        profile=profile_id,
                        renderer="tts",
                        provider="edge",
                        model="edge-tts",
                        voice=tts_voice,
                        script=PREVIEW_TEXT[locale]["tts"],
                    ),
                ]
            )
    manifest = output_root / "manifest.json"
    payload = {
        "version": VoiceManager.VOICE_PREVIEW_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    staged_manifest = manifest.with_name(f".{manifest.name}.{uuid4().hex}.tmp")
    staged_manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(staged_manifest, manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bridge-home",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument("--output-root", type=Path)
    parser.add_argument(
        "--locale",
        action="append",
        choices=tuple(PREVIEW_TEXT),
        default=None,
        help="Locale to generate; repeat for multiple locales (default: all)",
    )
    parser.add_argument(
        "--native-model",
        default="openai/gpt-audio-mini",
    )
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if not args.locale:
        args.locale = list(PREVIEW_TEXT)
    manifest = asyncio.run(generate(args))
    print(f"Voice previews generated: {manifest}")


if __name__ == "__main__":
    main()
