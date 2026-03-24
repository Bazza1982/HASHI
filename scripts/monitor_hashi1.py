#!/usr/bin/env python3
"""
HASHI1 Process Monitor
Checks if HASHI1 (WSL ubuntu process) is running.
Called by the HASHI scheduler heartbeat.
Sends Telegram notification if HASHI1 is down.
"""

import subprocess
import sys
import json
import asyncio
from pathlib import Path

HASHI1_SIGNATURE = "main.py --bridge-home /home/lily/projects/hashi"
BRIDGE_HOME = Path(__file__).parent.parent
SECRETS_FILE = BRIDGE_HOME / "secrets.json"
AGENTS_FILE = BRIDGE_HOME / "agents.json"


def check_hashi1_running() -> bool:
    """Check if HASHI1 process is running in WSL."""
    try:
        result = subprocess.run(
            ["wsl", "-d", "Ubuntu-22.04", "--", "pgrep", "-f", HASHI1_SIGNATURE],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() != ""
    except Exception as e:
        print(f"[monitor_hashi1] WSL check failed: {e}", file=sys.stderr)
        return False  # treat as down if we can't check


def restart_hashi1():
    """Restart HASHI1 in WSL in background."""
    cmd = (
        "cd /home/lily/projects/hashi && "
        "nohup bash bin/bridge-u.sh --resume-last "
        "> /home/lily/projects/hashi/bridge_launch.log 2>&1 &"
    )
    try:
        subprocess.Popen(
            ["wsl", "-d", "Ubuntu-22.04", "--", "bash", "-c", cmd],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True
    except Exception as e:
        print(f"[monitor_hashi1] Restart failed: {e}", file=sys.stderr)
        return False


def get_hashiko_token() -> str | None:
    """Get Hashiko's Telegram bot token from secrets.json."""
    try:
        with open(SECRETS_FILE, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        return secrets.get("hashiko_telegram_token") or secrets.get("telegram_bot_token")
    except Exception:
        return None


def get_authorized_id() -> int | None:
    """Get user's Telegram ID from agents.json global block."""
    try:
        with open(AGENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("global", {}).get("authorized_id")
    except Exception:
        return None


async def send_telegram_message(token: str, chat_id: int, text: str, reply_markup=None):
    """Send a Telegram message via Bot API."""
    import urllib.request
    import urllib.parse

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def main():
    is_running = check_hashi1_running()

    if is_running:
        print("[monitor_hashi1] HASHI1 is online ✅")
        # Return a message that will be sent to the agent as heartbeat result
        # The bridge will display this as a status update
        sys.exit(0)
    else:
        print("[monitor_hashi1] HASHI1 is OFFLINE ❌ — sending Telegram alert")

        token = get_hashiko_token()
        authorized_id = get_authorized_id()

        if not token or not authorized_id:
            print("[monitor_hashi1] Cannot send alert — missing token or authorized_id", file=sys.stderr)
            sys.exit(1)

        message = (
            "⚠️ *HASHI1 掉线警报*\n\n"
            "我检测到 HASHI1 进程已不在运行中！\n\n"
            "是否需要我帮你重启 HASHI1？\n"
            "请回复 `/restart_hashi1` 确认重启 🔄\n"
            "或回复 `/skip_hashi1` 跳过本次"
        )

        reply_markup = {
            "inline_keyboard": [[
                {"text": "✅ 重启 HASHI1", "callback_data": "restart_hashi1"},
                {"text": "❌ 跳过", "callback_data": "skip_hashi1"}
            ]]
        }

        asyncio.run(send_telegram_message(token, authorized_id, message, reply_markup))
        sys.exit(2)  # exit code 2 = offline, alert sent


if __name__ == "__main__":
    main()
