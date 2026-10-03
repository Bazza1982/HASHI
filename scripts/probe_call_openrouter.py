"""Explicit, small OpenRouter /call media probe; no keys or audio in logs."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path

import aiohttp
from orchestrator.enterprise.secret_refs import ConnectorSecretResolver
from orchestrator.frontend_call.adapters import MediaAdapters
from orchestrator.frontend_call.config import validate_target
from orchestrator.frontend_call.contract import CallError


async def probe(args):
    instance = Path(args.instance_dir)
    document = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    targets = {row["id"]: validate_target(row) for row in document["targets"]}
    secrets = json.loads((instance / "secrets.json").read_text(encoding="utf-8"))
    adapter = MediaAdapters(secret_resolver=ConnectorSecretResolver(secrets=secrets))
    if args.mode == "auth":
        key = secrets.get("openrouter-api_key", "")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as session:
            async with session.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
                allow_redirects=False,
            ) as response:
                return {"mode": "auth", "http_status": response.status, "key_shape_valid": key.startswith("sk-or-v1-"), "key_trimmed": key == key.strip()}
    if args.mode == "tts":
        slot = {"voice_id": args.voice, "options": {"style": args.style} if args.style else {}}
        try:
            result = await adapter.synthesize(targets["openrouter-gemini-tts"], slot, args.text)
        except CallError as exc:
            return {"mode": "tts", "status": "rejected", "error_code": exc.code}
        audio = base64.b64decode(result["content_b64"], validate=True)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(audio)
        return {
            "mode": "tts",
            "media_type": result["media_type"],
            "bytes": len(audio),
            "provider_receipt": result.get("provider_receipt"),
        }
    source = Path(args.input)
    if source.stat().st_size > 1024 * 1024:
        raise ValueError("STT probe input exceeds 1 MiB")
    audio = source.read_bytes()
    result = await adapter.transcribe(targets["openrouter-whisper"], {"options": {}}, audio)
    return {"mode": "stt", "text": result["text"], "provider_receipt": result.get("provider_receipt")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-dir", required=True)
    parser.add_argument("--profile", required=True)
    sub = parser.add_subparsers(dest="mode", required=True)
    tts = sub.add_parser("tts")
    tts.add_argument("--voice", default="Achernar")
    tts.add_argument("--style", default="")
    tts.add_argument("--text", default="这是 HASHI2 的语音测试。")
    tts.add_argument("--output", required=True)
    stt = sub.add_parser("stt")
    stt.add_argument("--input", required=True)
    sub.add_parser("auth")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
