import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import segno


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from orchestrator.pathing import build_bridge_paths, resolve_path_value


def load_whatsapp_config(config_path: Path) -> dict:
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    return (raw.get("global", {}).get("whatsapp") or {}).copy()


async def run_link(session_dir: str | None, timeout_minutes: float, bridge_home: str | None):
    from neonize.aioze.client import NewAClient
    from neonize.aioze.events import ConnectedEv, DisconnectedEv, PairStatusEv

    paths = build_bridge_paths(ROOT_DIR, bridge_home=bridge_home)
    wa_cfg = load_whatsapp_config(paths.config_path)
    session_root = resolve_path_value(
        session_dir or wa_cfg.get("session_dir", "@home/wa_session"),
        config_dir=paths.config_path.parent,
        bridge_home=paths.bridge_home,
    ) or (paths.bridge_home / "wa_session")
    session_root.mkdir(parents=True, exist_ok=True)
    client_name = str(session_root / "bridge-u-f")

    linked = asyncio.Event()
    qr_seen = asyncio.Event()
    client = NewAClient(client_name)

    async def on_qr(_, data_qr: bytes):
        qr_seen.set()
        print("")
        print("Scan this QR with WhatsApp on the phone you want bridge-u-f to use:")
        print("WhatsApp > Linked devices > Link a device")
        print("")
        segno.make_qr(data_qr).terminal(compact=True)
        print("")

    client.qr(on_qr)

    @client.event(ConnectedEv)
    async def _on_connected(_, __):
        print("WhatsApp linked and connected.")
        linked.set()

    @client.event(PairStatusEv)
    async def _on_pair_status(_, ev):
        print(f"Pair status: {ev}")

    @client.event(DisconnectedEv)
    async def _on_disconnected(_, __):
        if not linked.is_set():
            print("WhatsApp disconnected before linking completed.")

    print(f"Using session directory: {session_root}")
    print("Starting WhatsApp linker...")
    connect_task = await client.connect()
    try:
        await asyncio.wait_for(linked.wait(), timeout=timeout_minutes * 60.0)
        await asyncio.sleep(1.0)
    except asyncio.TimeoutError:
        if not qr_seen.is_set():
            print("Timed out before QR was shown.")
        else:
            print("Timed out waiting for WhatsApp link confirmation.")
        raise SystemExit(1)
    finally:
        connect_task.cancel()
        try:
            await connect_task
        except asyncio.CancelledError:
            pass
        await client.disconnect()

    print("Session saved. Future bridge-u-f starts can reuse this login until WhatsApp revokes it.")


def main():
    parser = argparse.ArgumentParser(description="One-off WhatsApp linker for bridge-u-f.")
    parser.add_argument(
        "--session-dir",
        help="Override the WhatsApp session directory. Defaults to agents.json global.whatsapp.session_dir.",
    )
    parser.add_argument(
        "--bridge-home",
        help="Override the bridge home directory. Defaults to BRIDGE_HOME or the code root.",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=float,
        default=5.0,
        help="How long to wait for linking before exiting. Default: 5 minutes.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    asyncio.run(run_link(args.session_dir, args.timeout_minutes, args.bridge_home))


if __name__ == "__main__":
    main()
