"""
telegram_send_file_cli.py — CLI tool for agents to send files via Telegram.

Works with ANY backend (claude-cli, codex-cli, gemini-cli, openrouter-api, etc.)
by calling directly from bash.

Usage:
    python tools/telegram_send_file_cli.py --path /tmp/chart.png
    python tools/telegram_send_file_cli.py --path /tmp/chart.png --caption "Daily report"
    python tools/telegram_send_file_cli.py --path /tmp/doc.pdf --type document

File type is auto-detected from extension:
    .jpg/.jpeg/.png/.webp → photo
    .mp4/.mov/.avi/.mkv   → video
    .mp3/.ogg/.flac/.wav/.m4a → audio
    everything else       → document

Override with --type photo|document|video|audio|voice
"""

import argparse
import hashlib
import json
import mimetypes
import os
import sys
from pathlib import Path
from urllib import request as urllib_request

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


def _load_secrets() -> dict:
    for candidate in [ROOT / "secrets.json"]:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    print("Error: secrets.json not found", file=sys.stderr)
    sys.exit(1)


def _load_agents_json() -> dict:
    p = ROOT / "agents.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8-sig"))
    print("Error: agents.json not found", file=sys.stderr)
    sys.exit(1)


def _detect_current_agent() -> str | None:
    """Resolve the current HASHI agent name from runtime context."""
    env_name = os.environ.get("HASHI_AGENT_NAME") or os.environ.get("AGENT_NAME")
    if env_name:
        return str(env_name).strip()

    cwd = Path.cwd().resolve()
    parts = list(cwd.parts)
    if "workspaces" in parts:
        idx = parts.index("workspaces")
        if idx + 1 < len(parts):
            return parts[idx + 1]

    return None


def _resolve_token(secrets: dict, agent_name: str | None) -> str:
    """Resolve the Telegram bot token for the current HASHI agent."""
    if not agent_name:
        print("Error: could not determine current HASHI agent name", file=sys.stderr)
        sys.exit(1)

    token = secrets.get(agent_name)
    if token:
        return str(token)

    print(f"Error: no Telegram bot token found for agent '{agent_name}'", file=sys.stderr)
    sys.exit(1)


def _resolve_chat_id(secrets: dict) -> str:
    """Resolve the current request's Telegram chat ID from runtime context."""
    chat_id = (
        os.environ.get("HASHI_AUTHORIZED_TELEGRAM_ID")
        or os.environ.get("AUTHORIZED_TELEGRAM_ID")
        or secrets.get("_authorized_telegram_id")
    )
    if chat_id and int(chat_id) != 0:
        return str(chat_id)

    print("Error: no current Telegram chat_id available in HASHI runtime context", file=sys.stderr)
    sys.exit(1)


def send_file(file_path: Path, caption: str | None, file_type: str,
              token: str, chat_id: str) -> bool:
    """Send file via Telegram Bot API using urllib (no extra deps)."""
    import io

    method_map = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "audio": "sendAudio",
        "voice": "sendVoice",
        "document": "sendDocument",
    }
    field_map = {
        "photo": "photo",
        "video": "video",
        "audio": "audio",
        "voice": "voice",
        "document": "document",
    }

    api_method = method_map.get(file_type, "sendDocument")
    field_name = field_map.get(file_type, "document")
    url = f"https://api.telegram.org/bot{token}/{api_method}"

    mime_type, _ = mimetypes.guess_type(str(file_path))
    mime_type = mime_type or "application/octet-stream"

    # Build multipart form data manually (no requests/httpx dependency)
    boundary = "----HASHIBoundary9876543210"
    body = io.BytesIO()

    def write(s: str):
        body.write(s.encode("utf-8"))

    # chat_id field
    write(f"--{boundary}\r\n")
    write('Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    write(f"{chat_id}\r\n")

    # caption field
    if caption:
        write(f"--{boundary}\r\n")
        write('Content-Disposition: form-data; name="caption"\r\n\r\n')
        write(f"{caption}\r\n")

    # file field
    write(f"--{boundary}\r\n")
    write(f'Content-Disposition: form-data; name="{field_name}"; filename="{file_path.name}"\r\n')
    write(f"Content-Type: {mime_type}\r\n\r\n")
    body.write(file_path.read_bytes())
    write(f"\r\n--{boundary}--\r\n")

    data = body.getvalue()
    req = urllib_request.Request(
        url,
        data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    try:
        with urllib_request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("ok"):
                print(f"OK: {file_type} sent to {chat_id} ({file_path.name})")
                return True
            else:
                print(f"Error: Telegram API: {result.get('description', 'unknown')}", file=sys.stderr)
                return False
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False


def bind_for_frontend(file_path: Path, caption: str | None, agent_name: str | None) -> str:
    """Bind the file to the canonical Session message (best effort).

    Telegram push stays the primary delivery; this binding mirrors the file
    into the shared Session so Workbench can preview/play/download it.
    """
    try:
        sys.path.insert(0, str(ROOT))
        from orchestrator.session_store import (
            MAX_SESSION_ATTACHMENT_BYTES,
            SessionStore,
        )

        owner_id = str(
            os.environ.get("HASHI_OWNER_ID") or os.environ.get("OWNER_ID") or ""
        ).strip()
        request_id = str(os.environ.get("HASHI_REQUEST_ID") or "").strip()
        session_id = str(os.environ.get("HASHI_SESSION_ID") or "").strip()
        if not agent_name:
            return "bind skipped: unknown agent"
        if not owner_id:
            return "bind skipped: no owner context"
        agent_key = str(agent_name).strip().casefold()
        instance_id = str(os.environ.get("HASHI_INSTANCE_ID") or "HASHI").strip()
        db_path = Path(
            os.environ.get("HASHI_SESSION_STORE_DB")
            or str(ROOT / "state" / "sessions.sqlite3")
        )
        attachment_root = Path(
            os.environ.get("HASHI_SESSION_ATTACHMENT_ROOT")
            or str(ROOT / "media" / "session_attachments")
        )
        store = SessionStore(
            str(db_path),
            instance_id=instance_id,
            attachment_root=str(attachment_root),
        )
        if not session_id:
            resolved = store.resolve_primary_session(owner_id=owner_id, agent_id=agent_key)
            session_id = str(resolved.get("session_id") or "").strip()
        if not session_id:
            return "bind skipped: no primary session"
        if not request_id:
            recent = store.recent_session_runs(
                session_id=session_id, owner_id=owner_id, limit=1
            )
            request_id = (
                str((recent[0] or {}).get("request_id") or "").strip() if recent else ""
            )
        if not request_id:
            return "bind skipped: no active run"
        size_bytes = int(file_path.stat().st_size)
        if size_bytes <= 0 or size_bytes > MAX_SESSION_ATTACHMENT_BYTES:
            return "bind skipped: file exceeds Session size limit"
        mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        mime_type = mime_type.split(";", 1)[0].strip().lower()
        payload = file_path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        idempotency_key = f"telegram-cli:{digest}:{size_bytes}"
        existing = store.run_output_attachment_group(
            request_id=request_id,
            session_id=session_id,
            owner_id=owner_id,
            agent_id=agent_key,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            count = int(existing.get("attachment_count") or 0)
            return f"bound (replayed): {count} attachment(s)"
        staged = store.stage_attachment(
            session_id=session_id,
            owner_id=owner_id,
            filename=file_path.name,
            media_type=mime_type,
            size_bytes=size_bytes,
            sha256=digest,
            semantic_role="audio_attachment" if mime_type.startswith("audio/") else "",
        )
        store.upload_attachment_bytes(
            session_id=session_id,
            owner_id=owner_id,
            attachment_id=staged["attachment_id"],
            payload=payload,
            audio_direction="output",
        )
        store.commit_attachment(
            session_id=session_id,
            owner_id=owner_id,
            attachment_id=staged["attachment_id"],
        )
        bound = store.bind_run_output_attachments(
            request_id=request_id,
            session_id=session_id,
            owner_id=owner_id,
            agent_id=agent_key,
            idempotency_key=idempotency_key,
            request_digest=digest,
            attachments=[{"attachment_id": staged["attachment_id"], "caption": caption or ""}],
        )
        count = len(bound.get("attachments") or [])
        return f"bound: {count} attachment(s) to canonical Session"
    except Exception as exc:
        return f"bind failed: {exc}"


def main():
    parser = argparse.ArgumentParser(description="Send files via Telegram from any HASHI agent")
    parser.add_argument("--path", required=True, help="Path to the file to send")
    parser.add_argument("--caption", default=None, help="Optional caption")
    parser.add_argument("--type", dest="file_type", default="auto",
                        choices=["auto", "photo", "document", "video", "audio", "voice"],
                        help="File type (default: auto-detect)")
    parser.add_argument("--chat-id", default=None, help="Override chat ID")
    args = parser.parse_args()

    file_path = Path(args.path)
    if not file_path.exists():
        print(f"Error: file not found: {args.path}", file=sys.stderr)
        sys.exit(1)
    if not file_path.is_file():
        print(f"Error: not a file: {args.path}", file=sys.stderr)
        sys.exit(1)

    file_type = args.file_type if args.file_type != "auto" else _detect_file_type(file_path)

    secrets = _load_secrets()
    _load_agents_json()  # fail early if HASHI config is missing
    detected_agent = _detect_current_agent()
    token = _resolve_token(secrets, detected_agent)
    chat_id = args.chat_id or _resolve_chat_id(secrets)

    bind_summary = bind_for_frontend(file_path, args.caption, detected_agent)
    success = send_file(file_path, args.caption, file_type, token, chat_id)
    print(f"[bind] {bind_summary}")
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
