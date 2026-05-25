#!/usr/bin/env python3
"""
Watch HASHI2 remote health, exercise probe chats, and keep a rolling 7-day
stability window.

This script is designed to be launched by the HASHI scheduler heartbeat, while
still being usable by a human operator from the shell.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STATE_PATH = ROOT / "state" / "hashi_remote_watchdog_state.json"
LOG_PATH = ROOT / "logs" / "hashi_remote_watchdog.jsonl"
TASKS_PATH = ROOT / "tasks.json"
CLAIM_PATH = ROOT / "state" / "remote_runtime_claim.json"
REMOTE_LOG_PATH = ROOT / "logs" / "hashi-remote-supervisor.log"
DEFAULT_HEARTBEAT_ID = "lin_yueru-loop-hashi-remote-watchdog-7d"
DEFAULT_REMOTE_PORT = 8767
DEFAULT_SENDER = "lin_yueru"
STABILITY_WINDOW = timedelta(days=7)
PROCESS_SIGNATURE = f"python3 -m remote --hashi-root {ROOT} --supervised"


@dataclass
class ProbeTarget:
    instance_id: str
    agent_name: str
    required_online: bool = False

    @property
    def address(self) -> str:
        return f"{self.agent_name}@{self.instance_id}"


def _now() -> datetime:
    return datetime.now().astimezone()


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _request_json(url: str, timeout: int = 5) -> dict[str, Any]:
    req = urllib_request.Request(url, method="GET")
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _tail_text(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return ""
    rows = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(rows[-lines:])


def _runtime_claim_port() -> int:
    claim = _load_json(CLAIM_PATH, {})
    try:
        port = int(claim.get("port") or 0)
    except Exception:
        port = 0
    return port or DEFAULT_REMOTE_PORT


def _remote_status(port: int) -> tuple[bool, dict[str, Any] | None, str | None]:
    url = f"http://127.0.0.1:{port}/protocol/status"
    try:
        return True, _request_json(url, timeout=5), None
    except Exception as exc:
        return False, None, str(exc)


def _matching_remote_pids() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", PROCESS_SIGNATURE],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def _kill_remote_processes() -> list[int]:
    killed: list[int] = []
    for pid in _matching_remote_pids():
        try:
            os.kill(pid, signal.SIGTERM)
            killed.append(pid)
        except ProcessLookupError:
            continue
    if killed:
        time.sleep(2)
    return killed


def _start_remote() -> bool:
    python_bin = ROOT / ".venv" / "bin" / "python3"
    cmd = [
        str(python_bin if python_bin.exists() else "python3"),
        "-m",
        "remote",
        "--hashi-root",
        str(ROOT),
        "--supervised",
    ]
    with REMOTE_LOG_PATH.open("a", encoding="utf-8") as log_fh:
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(4)
    return True


def _restart_remote() -> dict[str, Any]:
    killed = _kill_remote_processes()
    started = _start_remote()
    port = _runtime_claim_port()
    ok, status, error = _remote_status(port)
    return {
        "killed_pids": killed,
        "started": started,
        "port": port,
        "ok": ok,
        "error": error,
        "status_display_handle": (status or {}).get("display_handle"),
    }


def _remote_agents_from_peer(peer_state: dict[str, Any]) -> list[str]:
    canonical = dict(peer_state.get("canonical") or {})
    props = dict(canonical.get("properties") or {})
    agents = props.get("remote_agents") or []
    names: list[str] = []
    for item in agents:
        name = str((item or {}).get("agent_name") or "").strip().lower()
        if name and name not in names:
            names.append(name)
    return names


def _peer_live_status(peer_state: dict[str, Any]) -> str:
    canonical = dict(peer_state.get("canonical") or {})
    props = dict(canonical.get("properties") or {})
    return str(props.get("live_status") or "").strip().lower()


def _peer_is_online(peer_state: dict[str, Any]) -> bool:
    return _peer_live_status(peer_state) == "online"


def _peer_summary(peer_state: dict[str, Any] | None) -> dict[str, Any]:
    canonical = dict((peer_state or {}).get("canonical") or {})
    props = dict(canonical.get("properties") or {})
    return {
        "instance_id": str(canonical.get("instance_id") or (peer_state or {}).get("instance_id") or "").upper(),
        "remote_port": int(canonical.get("port") or 0),
        "workbench_port": int(canonical.get("workbench_port") or 0),
        "live_status": str(props.get("live_status") or "").strip().lower(),
        "handshake_state": str(props.get("handshake_state") or "").strip().lower(),
        "preferred_backend": str(props.get("preferred_backend") or "").strip().lower(),
        "host": str(canonical.get("host") or "").strip(),
        "agents": _remote_agents_from_peer(peer_state or {}),
    }


def _pick_agent(instance_id: str, peer_state: dict[str, Any]) -> str | None:
    preferred = {
        "HASHI1": ["lily"],
        "HASHI9": ["hashiko"],
        "INTEL": ["lily", "agent1", "agent2"],
        "MSI": ["ying", "hashiko", "agent1"],
    }
    available = _remote_agents_from_peer(peer_state)
    for name in preferred.get(instance_id.upper(), []):
        if name.lower() in available:
            return name.lower()
    if not available:
        defaults = preferred.get(instance_id.upper(), [])
        if defaults:
            return defaults[0].lower()
    return available[0] if available else None


def _select_probe_targets(status: dict[str, Any]) -> list[ProbeTarget]:
    peers = {str(item.get("instance_id") or "").upper(): item for item in status.get("peers") or []}
    selected: list[ProbeTarget] = []
    for instance_id in ("HASHI1", "HASHI9"):
        peer = peers.get(instance_id)
        agent = _pick_agent(instance_id, peer or {}) if peer else None
        if agent:
            selected.append(ProbeTarget(instance_id, agent))
    for instance_id in ("INTEL", "MSI"):
        peer = peers.get(instance_id)
        if not peer or not _peer_is_online(peer):
            continue
        agent = _pick_agent(instance_id, peer)
        if agent:
            selected.append(ProbeTarget(instance_id, agent, required_online=True))
    return selected


def _run_command(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _probe_target(sender: str, target: ProbeTarget) -> dict[str, Any]:
    check_cmd = [
        "python3",
        str(ROOT / "tools" / "hchat_send.py"),
        "--check",
        "--to",
        target.address,
        "--from",
        sender,
    ]
    checked = _run_command(check_cmd, timeout=40)
    check_ok = checked.returncode == 0
    delivered = None
    if check_ok:
        text = f"watchdog probe from {sender}@HASHI2 at {_now().isoformat()}"
        send_cmd = [
            "python3",
            str(ROOT / "tools" / "hchat_send.py"),
            "--to",
            target.address,
            "--from",
            sender,
            "--text",
            text,
        ]
        delivered = _run_command(send_cmd, timeout=80)
    return {
        "instance_id": target.instance_id,
        "agent": target.agent_name,
        "required_online": target.required_online,
        "check_ok": check_ok,
        "check_stdout": (checked.stdout or "").strip(),
        "check_stderr": (checked.stderr or "").strip(),
        "send_ok": delivered.returncode == 0 if delivered is not None else False,
        "send_stdout": ((delivered.stdout or "").strip() if delivered is not None else ""),
        "send_stderr": ((delivered.stderr or "").strip() if delivered is not None else ""),
    }


def _disable_heartbeat(task_id: str, reason: str) -> bool:
    data = _load_json(TASKS_PATH, {"heartbeats": [], "crons": [], "nudges": []})
    changed = False
    for item in data.get("heartbeats", []):
        if item.get("id") != task_id:
            continue
        item["enabled"] = False
        meta = item.setdefault("loop_meta", {})
        meta["stopped_reason"] = reason
        meta["stopped_at"] = _now().isoformat()
        changed = True
        break
    if changed:
        _save_json(TASKS_PATH, data)
    return changed


def _initial_state(task_id: str, deadline_at: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "started_at": _now().isoformat(),
        "stable_since": _now().isoformat(),
        "deadline_at": deadline_at,
        "bugs_found_count": 0,
        "last_bug_at": None,
        "last_bug_summary": None,
        "last_success_at": None,
        "last_run_at": None,
        "last_run_status": None,
        "runs": 0,
    }


def _deadline_reached(state: dict[str, Any], now: datetime) -> bool:
    raw = str(state.get("deadline_at") or "").strip()
    if not raw:
        return False
    try:
        return now >= datetime.fromisoformat(raw)
    except Exception:
        return False


def _mark_bug(state: dict[str, Any], summary: str) -> None:
    active_bug_statuses = {
        "probe_failed",
        "no_targets",
        "unresolved_remote_down",
        "unresolved_after_restart",
    }
    if (
        str(state.get("last_bug_summary") or "").strip() == summary
        and str(state.get("last_run_status") or "").strip() in active_bug_statuses
    ):
        return
    now = _now()
    state["bugs_found_count"] = int(state.get("bugs_found_count") or 0) + 1
    state["last_bug_at"] = now.isoformat()
    state["last_bug_summary"] = summary
    state["stable_since"] = now.isoformat()
    state["deadline_at"] = (now + STABILITY_WINDOW).isoformat()


def _state_summary(state: dict[str, Any]) -> str:
    return (
        f"runs={state.get('runs', 0)} bugs={state.get('bugs_found_count', 0)} "
        f"stable_since={state.get('stable_since')} deadline_at={state.get('deadline_at')}"
    )


def run_watchdog(task_id: str, sender: str, deadline_at: str) -> dict[str, Any]:
    now = _now()
    state = _load_json(STATE_PATH, None)
    if not isinstance(state, dict) or state.get("task_id") != task_id:
        state = _initial_state(task_id, deadline_at)

    state["runs"] = int(state.get("runs", 0) or 0) + 1
    state["last_run_at"] = now.isoformat()
    port = _runtime_claim_port()
    ok, status, error = _remote_status(port)

    result: dict[str, Any] = {
        "ok": False,
        "task_id": task_id,
        "port": port,
        "remote_up": ok,
        "restart_attempted": False,
        "restart_result": None,
        "probe_targets": [],
        "peer_statuses": {},
        "issues": [],
        "should_stop": False,
        "state_summary": None,
    }

    if not ok:
        issue = f"HASHI2 remote is down on port {port}: {error}"
        result["issues"].append(issue)
        restart = _restart_remote()
        result["restart_attempted"] = True
        result["restart_result"] = restart
        if not restart.get("ok"):
            _mark_bug(state, issue)
            state["last_run_status"] = "unresolved_remote_down"
            result["state_summary"] = _state_summary(state)
            _save_json(STATE_PATH, state)
            _append_jsonl(LOG_PATH, {"ts": now.isoformat(), "event": "watchdog_run", "result": result, "state": state})
            return result
        port = int(restart.get("port") or port)
        ok, status, error = _remote_status(port)
        result["remote_up"] = ok
        result["port"] = port
        if not ok:
            issue = f"HASHI2 remote still down after restart: {error}"
            result["issues"].append(issue)
            _mark_bug(state, issue)
            state["last_run_status"] = "unresolved_after_restart"
            result["state_summary"] = _state_summary(state)
            _save_json(STATE_PATH, state)
            _append_jsonl(LOG_PATH, {"ts": now.isoformat(), "event": "watchdog_run", "result": result, "state": state})
            return result

    assert status is not None
    peers = {str(item.get("instance_id") or "").upper(): item for item in status.get("peers") or []}
    for instance_id in ("HASHI1", "HASHI9", "INTEL", "MSI"):
        peer_state = peers.get(instance_id)
        if peer_state:
            result["peer_statuses"][instance_id] = _peer_summary(peer_state)

    targets = _select_probe_targets(status)
    if not targets:
        issue = "No probe targets resolved from /protocol/status."
        result["issues"].append(issue)
        _mark_bug(state, issue)
        state["last_run_status"] = "no_targets"
        result["state_summary"] = _state_summary(state)
        _save_json(STATE_PATH, state)
        _append_jsonl(LOG_PATH, {"ts": now.isoformat(), "event": "watchdog_run", "result": result, "state": state})
        return result

    for target in targets:
        probe = _probe_target(sender, target)
        result["probe_targets"].append(probe)
        if not (probe["check_ok"] and probe["send_ok"]):
            result["issues"].append(
                f"Probe failed for {target.address}: check_ok={probe['check_ok']} send_ok={probe['send_ok']}"
            )

    probe_success = {
        str(item.get("instance_id") or "").upper(): bool(item.get("check_ok") and item.get("send_ok"))
        for item in result["probe_targets"]
    }

    for instance_id in ("HASHI1", "HASHI9"):
        summary = result["peer_statuses"].get(instance_id) or {}
        if not summary:
            result["issues"].append(f"{instance_id} missing from /protocol/status peer set")
            continue
        if summary.get("live_status") == "online":
            continue
        if probe_success.get(instance_id):
            continue
        result["issues"].append(
            f"{instance_id} reported {summary.get('live_status') or 'unknown'} "
            f"(handshake={summary.get('handshake_state') or 'unknown'}, port={summary.get('remote_port') or 0})"
        )

    if result["issues"]:
        _mark_bug(state, "; ".join(result["issues"]))
        state["last_run_status"] = "probe_failed"
    else:
        result["ok"] = True
        state["last_success_at"] = now.isoformat()
        state["last_run_status"] = "healthy"
        if _deadline_reached(state, now):
            result["should_stop"] = True
            result["stop_reason"] = "stable_window_complete"
            _disable_heartbeat(task_id, "stable_window_complete")

    result["state_summary"] = _state_summary(state)
    _save_json(STATE_PATH, state)
    _append_jsonl(LOG_PATH, {"ts": now.isoformat(), "event": "watchdog_run", "result": result, "state": state})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch HASHI2 remote health and probe chats.")
    parser.add_argument("--task-id", default=DEFAULT_HEARTBEAT_ID)
    parser.add_argument("--sender", default=DEFAULT_SENDER)
    parser.add_argument("--deadline-at", default=(_now() + STABILITY_WINDOW).isoformat())
    args = parser.parse_args(argv)

    result = run_watchdog(
        task_id=str(args.task_id),
        sender=str(args.sender),
        deadline_at=str(args.deadline_at),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
