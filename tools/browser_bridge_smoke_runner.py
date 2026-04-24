from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.browser_bridge_harness import validate_harness_artifacts
from tools.browser_extension_bridge import BrowserBridgeError, healthcheck, send_bridge_command


def load_harness_state(root_dir: Path) -> dict[str, Any]:
    validation = validate_harness_artifacts(root_dir)
    if not validation["ok"]:
        raise ValueError(f"harness artifacts missing: {validation['missing']}")
    return {
        "root_dir": str(root_dir),
        "validation": validation,
        "config": validation["config"],
        "smoke_plan": validation["smoke_plan"],
    }


def build_smoke_steps(root_dir: Path, *, repo_root: Path) -> list[dict[str, Any]]:
    state = load_harness_state(root_dir)
    config = state["config"]
    smoke_plan = state["smoke_plan"]
    socket_path = config["socket_path"]
    start_url = smoke_plan["start_url"]
    startup_wait_s = str(smoke_plan.get("startup_wait_s", 3.0))
    screenshot_path = str(root_dir / "logs" / "smoke_screenshot.png")
    script_path = str(repo_root / "tools" / "browser_bridge_smoke_runner.py")

    return [
        {
            "id": "launch_chrome",
            "kind": "manual_windows",
            "command": str(root_dir / "launch_chrome_test.cmd"),
            "description": "Launch isolated Chrome with the test extension bundle.",
        },
        {
            "id": "healthcheck",
            "kind": "wsl_python",
            "argv": [
                "python3",
                script_path,
                "healthcheck",
                "--socket",
                socket_path,
                "--wait-for-socket-s",
                startup_wait_s,
            ],
            "description": "Verify the Unix socket exists and the bridge responds to ping.",
        },
        {
            "id": "ping",
            "kind": "wsl_python",
            "argv": [
                "python3",
                script_path,
                "ping",
                "--socket",
                socket_path,
                "--wait-for-socket-s",
                startup_wait_s,
            ],
            "description": "Send a direct ping request through the extension bridge.",
        },
        {
            "id": "active_tab",
            "kind": "wsl_python",
            "argv": [
                "python3",
                script_path,
                "active_tab",
                "--socket",
                socket_path,
                "--url",
                start_url,
                "--wait-for-socket-s",
                startup_wait_s,
            ],
            "description": "Read the active tab metadata through the extension bridge.",
        },
        {
            "id": "get_text",
            "kind": "wsl_python",
            "argv": [
                "python3",
                script_path,
                "get_text",
                "--socket",
                socket_path,
                "--url",
                start_url,
                "--wait-for-socket-s",
                startup_wait_s,
            ],
            "description": "Fetch page text through the extension bridge.",
        },
        {
            "id": "screenshot",
            "kind": "wsl_python",
            "argv": [
                "python3",
                script_path,
                "screenshot",
                "--socket",
                socket_path,
                "--url",
                start_url,
                "--wait-for-socket-s",
                startup_wait_s,
                "--out",
                screenshot_path,
            ],
            "description": "Capture a screenshot through the extension bridge.",
        },
    ]


def write_smoke_command_plan(root_dir: Path, *, repo_root: Path) -> dict[str, Any]:
    steps = build_smoke_steps(root_dir, repo_root=repo_root)
    output = {
        "root_dir": str(root_dir),
        "steps": steps,
    }
    path = root_dir / "state" / "smoke_commands.json"
    path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    return output


def execute_smoke_step(
    step: dict[str, Any],
    *,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    if step["kind"] == "manual_windows":
        return {
            "id": step["id"],
            "kind": step["kind"],
            "status": "manual_required",
            "command": step["command"],
            "description": step["description"],
        }

    completed = runner(step["argv"], capture_output=True, text=True)
    return {
        "id": step["id"],
        "kind": step["kind"],
        "status": "passed" if completed.returncode == 0 else "failed",
        "argv": step["argv"],
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "description": step["description"],
    }


def execute_smoke_plan(
    root_dir: Path,
    *,
    repo_root: Path,
    runner: Any = subprocess.run,
    stop_on_failure: bool = True,
) -> dict[str, Any]:
    plan = write_smoke_command_plan(root_dir, repo_root=repo_root)
    results: list[dict[str, Any]] = []
    overall_status = "passed"

    for step in plan["steps"]:
        result = execute_smoke_step(step, runner=runner)
        results.append(result)
        if result["status"] == "failed":
            overall_status = "failed"
            if stop_on_failure:
                break
        elif result["status"] == "manual_required" and overall_status != "failed":
            overall_status = "manual_required"

    report = {
        "root_dir": str(root_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": overall_status,
        "results": results,
    }
    report_path = root_dir / "state" / "smoke_results.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def _run_healthcheck(socket_path: Path) -> dict[str, Any]:
    return healthcheck(socket_path=socket_path)


def _run_bridge_action(action: str, socket_path: Path, *, url: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {}
    if url:
        args["url"] = url
    return send_bridge_command(action, args, socket_path=socket_path)


def wait_for_socket(socket_path: Path, timeout_s: float) -> bool:
    if timeout_s <= 0:
        return socket_path.exists()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if socket_path.exists():
            return True
        time.sleep(0.1)
    return socket_path.exists()


def main() -> int:
    parser = argparse.ArgumentParser(description="Option D isolated smoke runner")
    parser.add_argument(
        "command",
        choices=["healthcheck", "ping", "active_tab", "get_text", "screenshot", "write-plan", "execute-plan"],
    )
    parser.add_argument("--socket")
    parser.add_argument("--url")
    parser.add_argument("--out")
    parser.add_argument("--root")
    parser.add_argument("--repo-root")
    parser.add_argument("--wait-for-socket-s", type=float, default=0.0)
    args = parser.parse_args()

    if args.command in {"write-plan", "execute-plan"}:
        if not args.root or not args.repo_root:
            parser.error("--root and --repo-root are required for plan commands")
        root_dir = Path(args.root)
        repo_root = Path(args.repo_root)
        if args.command == "write-plan":
            result = write_smoke_command_plan(root_dir, repo_root=repo_root)
            print(json.dumps(result))
            return 0
        result = execute_smoke_plan(root_dir, repo_root=repo_root)
        print(json.dumps(result))
        return 0 if result["status"] == "passed" else 1

    if not args.socket:
        parser.error("--socket is required for bridge commands")
    socket_path = Path(args.socket)
    if args.wait_for_socket_s and not wait_for_socket(socket_path, args.wait_for_socket_s):
        print(json.dumps({"ok": False, "error": f"socket not ready within {args.wait_for_socket_s}s"}))
        return 1
    try:
        if args.command == "healthcheck":
            result = _run_healthcheck(socket_path)
        else:
            result = _run_bridge_action(args.command, socket_path, url=args.url)
            if args.command == "screenshot" and args.out and result.get("ok") and result.get("output"):
                Path(args.out).write_text(str(result["output"]), encoding="utf-8")
                result = {**result, "saved_to": args.out}
    except BrowserBridgeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1

    print(json.dumps(result))
    return 0 if result.get("ok", result.get("connected", False)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
