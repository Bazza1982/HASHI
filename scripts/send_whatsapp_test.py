import argparse
import asyncio
import json
import sys
from contextlib import suppress
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from orchestrator.pathing import build_bridge_paths, resolve_path_value


def load_whatsapp_config(config_path: Path) -> dict:
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    return (raw.get("global", {}).get("whatsapp") or {}).copy()


async def send_test_message(number: str, text: str, session_dir: str | None, bridge_home: str | None):
    from neonize.aioze.client import NewAClient
    from neonize.aioze.events import ConnectedEv, DisconnectedEv
    from neonize.client import build_jid

    paths = build_bridge_paths(ROOT_DIR, bridge_home=bridge_home)
    wa_cfg = load_whatsapp_config(paths.config_path)
    session_root = resolve_path_value(
        session_dir or wa_cfg.get("session_dir", "@home/wa_session"),
        config_dir=paths.config_path.parent,
        bridge_home=paths.bridge_home,
    ) or (paths.bridge_home / "wa_session")
    session_root.mkdir(parents=True, exist_ok=True)
    client_name = str(session_root / "bridge-u-f")

    connected = asyncio.Event()
    disconnected = asyncio.Event()
    send_result: dict[str, str | bool] = {"ok": False, "error": ""}

    client = NewAClient(client_name)

    @client.event(ConnectedEv)
    async def _on_connected(_, __):
        connected.set()

    @client.event(DisconnectedEv)
    async def _on_disconnected(_, __):
        disconnected.set()

    connect_task = await client.connect()
    try:
        await asyncio.wait_for(connected.wait(), timeout=30.0)
        jid = build_jid(number.lstrip("+"), "s.whatsapp.net")
        await client.send_message(jid, text)
        send_result["ok"] = True
    except Exception as e:
        send_result["error"] = str(e)
    finally:
        connect_task.cancel()
        try:
            await connect_task
        except asyncio.CancelledError:
            pass
        with suppress(Exception):
            await client.disconnect()

    return send_result


def main():
    parser = argparse.ArgumentParser(description="Send a one-off WhatsApp test message using bridge-u-f session.")
    parser.add_argument("number", help="Destination number in E.164 format, e.g. +6583031585")
    parser.add_argument(
        "--text",
        default="bridge-u-f WhatsApp outbound test",
        help="Message text to send.",
    )
    parser.add_argument(
        "--session-dir",
        help="Override the WhatsApp session directory. Defaults to agents.json global.whatsapp.session_dir.",
    )
    parser.add_argument(
        "--bridge-home",
        help="Override the bridge home directory. Defaults to BRIDGE_HOME or the code root.",
    )
    args = parser.parse_args()

    result = asyncio.run(send_test_message(args.number, args.text, args.session_dir, args.bridge_home))
    if result["ok"]:
        print(f"Sent WhatsApp test message to {args.number}")
    else:
        print(f"Failed to send WhatsApp test message to {args.number}: {result['error']}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
