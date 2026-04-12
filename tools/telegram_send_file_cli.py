"""
telegram_send_file_cli.py — CLI tool for agents to send files via Telegram.

Works with ANY backend (claude-cli, codex-cli, gemini-cli, openrouter-api, etc.)
by calling directly from bash.

Usage:
    python tools/telegram_send_file_cli.py --path /tmp/chart.png
    python tools/telegram_send_file_cli.py --path /tmp/chart.png --caption "Daily report"
    python tools/telegram_send_file_cli.py --path /tmp/doc.pdf --type document
    python tools/telegram_send_file_cli.py --path /tmp/chart.png --agent zelda

File type is auto-detected from extension:
    .jpg/.jpeg/.png/.webp → photo
    .mp4/.mov/.avi/.mkv   → video
    .mp3/.ogg/.flac/.wav/.m4a → audio
    everything else       → document

Override with --type photo|document|video|audio
"""

import argparse
import json
import mimetypes
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
        return json.loads(p.read_text(encoding="utf-8"))
    print("Error: agents.json not found", file=sys.stderr)
    sys.exit(1)


def _resolve_token(secrets: dict, agents_cfg: dict, agent_name: str | None) -> str:
    """Resolve Telegram bot token for the given agent (or first available)."""
    agents = agents_cfg.get("agents", [])

    if agent_name:
        for ag in agents:
            if ag.get("name") == agent_name:
                token_key = ag.get("telegram_token_key", agent_name)
                token = secrets.get(token_key)
                if token:
                    return token
                break

    # Fallback: try common keys
    for key in ["arale", "akane", "hashiko"]:
        if secrets.get(key) and len(str(secrets[key])) > 20:
            return secrets[key]

    print("Error: no Telegram bot token found", file=sys.stderr)
    sys.exit(1)


def _resolve_chat_id(agents_cfg: dict, secrets: dict) -> str:
    """Resolve authorized Telegram chat ID."""
    # secrets.json first
    auth_id = secrets.get("authorized_telegram_id")
    if auth_id and int(auth_id) != 0:
        return str(auth_id)
    # agents.json global
    g = agents_cfg.get("global", {})
    auth_id = g.get("authorized_id")
    if auth_id and int(auth_id) != 0:
        return str(auth_id)
    print("Error: no authorized_telegram_id found", file=sys.stderr)
    sys.exit(1)


def send_file(file_path: Path, caption: str | None, file_type: str,
              token: str, chat_id: str) -> bool:
    """Send file via Telegram Bot API using urllib (no extra deps)."""
    import io

    method_map = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "audio": "sendAudio",
        "document": "sendDocument",
    }
    field_map = {
        "photo": "photo",
        "video": "video",
        "audio": "audio",
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
    write(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n')
    write(f"{chat_id}\r\n")

    # caption field
    if caption:
        write(f"--{boundary}\r\n")
        write(f'Content-Disposition: form-data; name="caption"\r\n\r\n')
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


def main():
    parser = argparse.ArgumentParser(description="Send files via Telegram from any HASHI agent")
    parser.add_argument("--path", required=True, help="Path to the file to send")
    parser.add_argument("--caption", default=None, help="Optional caption")
    parser.add_argument("--type", dest="file_type", default="auto",
                        choices=["auto", "photo", "document", "video", "audio"],
                        help="File type (default: auto-detect)")
    parser.add_argument("--agent", default=None, help="Agent name (for token resolution)")
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
    agents_cfg = _load_agents_json()
    token = _resolve_token(secrets, agents_cfg, args.agent)
    chat_id = args.chat_id or _resolve_chat_id(agents_cfg, secrets)

    success = send_file(file_path, args.caption, file_type, token, chat_id)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
