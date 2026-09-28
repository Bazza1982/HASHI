"""Publish one file through the standard Telegram Frontend Connector.

This compatibility CLI keeps its historic command line, but no longer calls
the Telegram Bot API or writes a parallel delivery record. It commits a
standard FC Event/outbox task and lets the Telegram Connector render it.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent

_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv"}
_AUDIO_EXTS = {".mp3", ".ogg", ".flac", ".wav", ".m4a"}


def _detect_file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _PHOTO_EXTS:
        return "photo"
    if suffix in _VIDEO_EXTS:
        return "video"
    if suffix in _AUDIO_EXTS:
        return "audio"
    return "document"


def _load_json(path: Path, *, label: str) -> dict:
    if not path.exists():
        raise SystemExit(f"Error: {label} not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise SystemExit(f"Error: {label} is invalid")
    return value


def _detect_current_agent(explicit: str | None = None) -> str | None:
    if str(explicit or "").strip():
        return str(explicit).strip().casefold()
    env_name = os.environ.get("HASHI_AGENT_NAME") or os.environ.get("AGENT_NAME")
    if env_name:
        return str(env_name).strip().casefold()
    return None


def _agent_config(config: dict, agent_id: str) -> dict:
    for raw in config.get("agents") or ():
        if isinstance(raw, dict) and str(raw.get("name") or "").casefold() == agent_id:
            return raw
    raise SystemExit(f"Error: agent is not configured: {agent_id}")


def _token_for(secrets: dict, agent: dict, agent_id: str) -> str:
    key = str(agent.get("telegram_token_key") or agent_id).strip()
    token = str(secrets.get(key) or secrets.get(agent_id) or "").strip()
    if not token or token == "WORKBENCH_ONLY_NO_TOKEN":
        raise SystemExit(f"Error: no Telegram connector token for agent '{agent_id}'")
    return token


def _authorized_id(secrets: dict) -> int:
    raw = (
        os.environ.get("HASHI_AUTHORIZED_TELEGRAM_ID")
        or os.environ.get("AUTHORIZED_TELEGRAM_ID")
        or secrets.get("_authorized_telegram_id")
    )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SystemExit("Error: no authorized Telegram identity is configured") from exc
    if value == 0:
        raise SystemExit("Error: no authorized Telegram identity is configured")
    return value


def _publication_id(path: Path, caption: str, file_type: str, explicit: str) -> str:
    if explicit:
        return explicit
    request_id = str(
        os.environ.get("HASHI_TOOL_CALL_ID")
        or os.environ.get("HASHI_REQUEST_ID")
        or ""
    ).strip()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    invocation = request_id or uuid4().hex
    material = f"{invocation}\0{digest}\0{caption}\0{file_type}"
    return "telegram-cli-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


async def _run(args: argparse.Namespace) -> dict:
    file_path = Path(args.path).expanduser().resolve(strict=True)
    if not file_path.is_file():
        raise SystemExit(f"Error: not a file: {args.path}")
    config = _load_json(ROOT / "agents.json", label="agents.json")
    secrets_path = Path(
        os.environ.get("HASHI_SECRETS_PATH") or ROOT / "secrets.json"
    )
    secrets = _load_json(secrets_path, label="secrets.json")
    agent_id = _detect_current_agent(args.agent)
    if not agent_id:
        raise SystemExit("Error: set HASHI_AGENT_NAME for this FC publication")
    agent = _agent_config(config, agent_id)
    authorized_id = _authorized_id(secrets)
    chat_id = int(args.chat_id) if args.chat_id is not None else authorized_id
    owner_id = str(os.environ.get("HASHI_OWNER_ID") or f"user:{authorized_id}")
    file_type = args.file_type if args.file_type != "auto" else _detect_file_type(file_path)
    semantic_role = "voice_message" if file_type == "voice" else ""
    from orchestrator.frontend_telegram_connector import (
        publish_explicit_telegram_notification,
    )

    return await publish_explicit_telegram_notification(
        root=ROOT,
        instance_id=str(
            os.environ.get("HASHI_INSTANCE_ID")
            or (config.get("global") or {}).get("instance_id")
            or "HASHI"
        ),
        agent_id=agent_id,
        owner_id=owner_id,
        authorized_id=authorized_id,
        chat_id=chat_id,
        token=_token_for(secrets, agent, agent_id),
        publication_id=_publication_id(
            file_path,
            str(args.caption or ""),
            file_type,
            str(args.publication_id or ""),
        ),
        file_path=file_path,
        caption=str(args.caption or ""),
        semantic_role=semantic_role,
        presentation_role=file_type,
        session_db_path=os.environ.get("HASHI_SESSION_STORE_DB") or None,
        attachment_root=os.environ.get("HASHI_SESSION_ATTACHMENT_ROOT") or None,
        agent_lifecycle_id=str(agent.get("agent_lifecycle_id") or ""),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish a file through HASHI's Telegram Frontend Connector"
    )
    parser.add_argument("--path", required=True, help="Path to the file to publish")
    parser.add_argument("--caption", default=None, help="Optional caption")
    parser.add_argument(
        "--type",
        dest="file_type",
        default="auto",
        choices=["auto", "photo", "document", "video", "audio", "voice"],
        help="Preferred Telegram media rendition",
    )
    parser.add_argument("--chat-id", default=None, help="Explicit Telegram endpoint")
    parser.add_argument(
        "--agent",
        default=None,
        help="Configured Agent identity (otherwise use HASHI_AGENT_NAME)",
    )
    parser.add_argument(
        "--publication-id",
        default=None,
        help="Stable FC idempotency identity for a safe replay",
    )
    parser.add_argument("--retain-indefinite", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--retention-seconds", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    result = asyncio.run(_run(args))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result.get("accepted") else 1)


if __name__ == "__main__":
    main()
