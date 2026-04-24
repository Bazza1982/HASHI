from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.browser_bridge_live_acceptance import write_live_acceptance_runbook


def build_live_probe_plan(
    root_dir: Path,
    *,
    rollback_commit: str,
    live_socket_path: str = "/tmp/hashi-browser-bridge.sock",
    benign_url: str = "https://example.com",
) -> dict[str, Any]:
    runbook = write_live_acceptance_runbook(root_dir, rollback_commit=rollback_commit)
    script_path = str(Path(__file__).resolve().parent / "browser_bridge_smoke_runner.py")
    plan = {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": runbook["mode"],
        "rollback_commit": rollback_commit,
        "live_socket_path": live_socket_path,
        "benign_url": benign_url,
        "steps": [
            {
                "id": "healthcheck",
                "argv": ["python3", script_path, "healthcheck", "--socket", live_socket_path, "--wait-for-socket-s", "2"],
            },
            {
                "id": "ping",
                "argv": ["python3", script_path, "ping", "--socket", live_socket_path, "--wait-for-socket-s", "2"],
            },
            {
                "id": "active_tab",
                "argv": [
                    "python3",
                    script_path,
                    "active_tab",
                    "--socket",
                    live_socket_path,
                    "--url",
                    benign_url,
                    "--wait-for-socket-s",
                    "2",
                ],
            },
            {
                "id": "get_text",
                "argv": [
                    "python3",
                    script_path,
                    "get_text",
                    "--socket",
                    live_socket_path,
                    "--url",
                    benign_url,
                    "--wait-for-socket-s",
                    "2",
                ],
            },
            {
                "id": "screenshot",
                "argv": [
                    "python3",
                    script_path,
                    "screenshot",
                    "--socket",
                    live_socket_path,
                    "--url",
                    benign_url,
                    "--wait-for-socket-s",
                    "2",
                    "--out",
                    str(root_dir / "logs" / "live_probe_screenshot.txt"),
                ],
            },
        ],
        "abort_conditions": runbook["abort_conditions"],
    }
    return plan


def write_live_probe_plan(
    root_dir: Path,
    *,
    rollback_commit: str,
    live_socket_path: str = "/tmp/hashi-browser-bridge.sock",
    benign_url: str = "https://example.com",
) -> dict[str, Any]:
    plan = build_live_probe_plan(
        root_dir,
        rollback_commit=rollback_commit,
        live_socket_path=live_socket_path,
        benign_url=benign_url,
    )
    path = root_dir / "state" / "live_probe_plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a concrete, non-destructive live probe plan for Option D")
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--root", required=True)
    parser.add_argument("--rollback-commit", required=True)
    parser.add_argument("--live-socket-path", default="/tmp/hashi-browser-bridge.sock")
    parser.add_argument("--benign-url", default="https://example.com")
    args = parser.parse_args()

    plan = write_live_probe_plan(
        Path(args.root),
        rollback_commit=args.rollback_commit,
        live_socket_path=args.live_socket_path,
        benign_url=args.benign_url,
    )
    print(json.dumps(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
