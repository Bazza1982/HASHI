from __future__ import annotations

import asyncio
import html
import json
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp
import yaml


def source_requires_manual_permission(source: str) -> bool:
    normalized = (source or "").strip().lower()
    if not normalized:
        return True
    automated_prefixes = (
        "scheduler",
        "bridge:",
        "bridge-transfer:",
        "hchat-reply:",
        "cos-query:",
        "ticket:",
        "loop_skill",
        "startup",
    )
    return normalized.startswith(automated_prefixes)


def remote_backend_block_reason(runtime: Any, source: str) -> str | None:
    engine = (runtime.config.active_backend or "").strip().lower()
    if engine not in {"openrouter-api", "deepseek-api"}:
        return None
    if not source_requires_manual_permission(source):
        return None
    return (
        f"Blocked {engine} for source '{source}'. Remote API backends are reserved for user-initiated requests only; "
        "automated/agent-originated flows must not use them."
    )


async def handle_remote_backend_block(runtime: Any, item: Any) -> bool:
    reason = remote_backend_block_reason(runtime, item.source)
    if not reason:
        return False
    runtime.error_logger.warning(reason)
    if item.deliver_to_telegram:
        await runtime.send_long_message(
            item.chat_id,
            f"⚠️ {reason}",
            request_id=item.request_id,
            purpose="remote-backend-policy",
        )
    return True


def remote_config_snapshot(runtime: Any) -> dict[str, Any]:
    root = runtime.global_config.project_root
    config_path = root / "remote" / "config.yaml"
    agents_path = root / "agents.json"
    instances_path = root / "instances.json"
    data: dict[str, Any] = {}
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    server = data.get("server") or {}
    discovery = data.get("discovery") or {}
    configured_port = server.get("port") or 8766
    try:
        agents = json.loads(agents_path.read_text(encoding="utf-8-sig")) if agents_path.exists() else {}
    except Exception:
        agents = {}
    global_cfg = agents.get("global") or {}
    if global_cfg.get("remote_port"):
        configured_port = global_cfg.get("remote_port")
    instance_id = str(global_cfg.get("instance_id") or "").strip().lower()
    if instances_path.exists() and instance_id:
        try:
            instances = json.loads(instances_path.read_text(encoding="utf-8")).get("instances", {}) or {}
            configured_port = (instances.get(instance_id) or {}).get("remote_port") or configured_port
        except Exception:
            pass
    return {
        "root": root,
        "port": int(configured_port or 8766),
        "use_tls": bool(server.get("use_tls", True)),
        "backend": str(discovery.get("backend") or "lan"),
    }


def remote_urls(runtime: Any, path: str) -> list[str]:
    cfg = remote_config_snapshot(runtime)
    port = int(cfg["port"])
    schemes = ("https", "http") if cfg["use_tls"] else ("http", "https")
    return [f"{scheme}://127.0.0.1:{port}{path}" for scheme in schemes]


async def fetch_remote_json(runtime: Any, path: str) -> tuple[dict[str, Any] | None, str | None]:
    timeout = aiohttp.ClientTimeout(total=4)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for url in remote_urls(runtime, path):
            try:
                async with session.get(url, ssl=False) as resp:
                    if resp.status >= 500:
                        continue
                    return await resp.json(), url
            except Exception:
                continue
    return None, None


async def await_remote_start_health(
    runtime: Any,
    *,
    process: Any,
    cfg: dict[str, Any],
    cmd: list[str],
    log_path: Path,
    timeout_s: float = 8.0,
) -> tuple[bool, str]:
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        if process.returncode is not None:
            return False, build_remote_start_failure_message(
                runtime,
                cfg=cfg,
                cmd=cmd,
                reason="process exited before /health became ready",
                log_path=log_path,
                exit_code=process.returncode,
            )
        health, health_url = await fetch_remote_json(runtime, "/health")
        if health:
            return True, str(health_url or "")
        await asyncio.sleep(0.5)

    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=2)
    except Exception:
        with suppress(Exception):
            process.kill()
    return False, build_remote_start_failure_message(
        runtime,
        cfg=cfg,
        cmd=cmd,
        reason="health endpoint did not become ready within timeout",
        log_path=log_path,
        exit_code=process.returncode,
    )


def format_remote_age(timestamp: Any) -> str:
    try:
        value = int(float(timestamp or 0))
    except (TypeError, ValueError):
        return "n/a"
    if value <= 0:
        return "n/a"
    delta = max(0, int(time.time()) - value)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def remote_peer_presence(runtime: Any, peer: dict[str, Any]) -> tuple[int, str, str]:
    props = peer.get("properties") or {}
    live_status = str(props.get("live_status") or "").strip().lower()
    state = str(props.get("handshake_state") or "unknown")
    last_handshake_at = props.get("last_handshake_at")
    last_seen_ok = props.get("last_seen_ok")
    last_seen_error = props.get("last_seen_error")
    last_error = props.get("last_error")
    last_age = format_remote_age(last_handshake_at)
    stale = last_age != "n/a" and isinstance(last_handshake_at, (int, float, str))
    if stale:
        try:
            stale = (time.time() - float(last_handshake_at)) > 45
        except (TypeError, ValueError):
            stale = False
    if live_status == "online":
        return 0, "🟢 online", state
    if live_status == "stale":
        return 2, "🟠 stale", state
    if live_status == "offline":
        return 3, "🔴 offline", state
    if state in {"handshake_timed_out", "handshake_rejected", "unreachable"}:
        return 3, "🔴 offline", state
    if state == "handshake_in_progress" and (last_seen_error or last_error) and not last_seen_ok:
        return 3, "🔴 offline", state
    if state == "handshake_accepted" and not stale:
        return 0, "🟢 online", state
    if state == "handshake_in_progress":
        return 1, "🟡 connecting", state
    if state in {"handshake_pending", "unknown"}:
        return 1, "🟡 pending", state
    if state == "handshake_accepted" and stale:
        return 2, "🟠 stale", state
    return 3, "🔴 offline", state


def load_remote_instances(runtime: Any) -> dict[str, dict[str, Any]]:
    path = runtime.global_config.project_root / "instances.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    instances = data.get("instances") or {}
    return instances if isinstance(instances, dict) else {}


def peer_network_hosts(peer: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    props = peer.get("properties") or {}
    hosts: list[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        host = str(value or "").strip()
        if not host or host in {"127.0.0.1", "localhost", "0.0.0.0"}:
            return
        if host in seen:
            return
        seen.add(host)
        hosts.append(host)

    for key in ("lan_ip", "tailscale_ip", "api_host"):
        _add(entry.get(key))
    for field in ("address_candidates", "observed_candidates"):
        for item in props.get(field) or []:
            if not isinstance(item, dict):
                continue
            scope = str(item.get("scope") or "").strip().lower()
            if scope in {"lan", "overlay", "routable", "peer"}:
                _add(item.get("host"))
    return hosts


def render_remote_peer_endpoints(runtime: Any, peer: dict[str, Any]) -> list[str]:
    instance_id = str(peer.get("instance_id") or "").strip().lower()
    entry = load_remote_instances(runtime).get(instance_id, {}) if instance_id else {}
    route_host = str(peer.get("resolved_route_host") or peer.get("host") or entry.get("api_host") or "?").strip() or "?"
    route_port = str(peer.get("resolved_route_port") or peer.get("port") or entry.get("remote_port") or "?").strip() or "?"
    network_hosts = peer_network_hosts(peer, entry if isinstance(entry, dict) else {})
    display_network_host = str(peer.get("display_network_host") or "").strip()
    if display_network_host and display_network_host not in network_hosts:
        network_hosts.insert(0, display_network_host)
    same_host = bool(peer.get("same_host")) or bool(str((entry or {}).get("same_host_loopback") or "").strip())

    if same_host and route_host in {"127.0.0.1", "localhost"}:
        network_host = network_hosts[0] if network_hosts else ""
        line = f"route: <code>{html.escape(route_host)}:{html.escape(route_port)}</code>  ·  <code>same host</code>"
        if network_host:
            line += f"  ·  network: <code>{html.escape(network_host)}:{html.escape(route_port)}</code>"
        return [line]

    if network_hosts and route_host not in network_hosts and route_host not in {"?", ""}:
        return [
            f"route: <code>{html.escape(route_host)}:{html.escape(route_port)}</code>",
            f"network: <code>{html.escape(network_hosts[0])}:{html.escape(route_port)}</code>",
        ]

    return [f"addr: <code>{html.escape(route_host)}:{html.escape(route_port)}</code>"]


def render_remote_peer_block(runtime: Any, peer: dict[str, Any]) -> list[str]:
    props = peer.get("properties") or {}
    _rank, presence, state = remote_peer_presence(runtime, peer)
    instance_id = html.escape(str(peer.get("instance_id") or "unknown"))
    port = html.escape(str(peer.get("port") or "?"))
    backend = html.escape(str(props.get("preferred_backend") or props.get("discovery") or "unknown"))
    agents = len(props.get("remote_agents") or [])
    last_handshake = html.escape(format_remote_age(props.get("last_handshake_at")))
    last_seen_ok = html.escape(format_remote_age(props.get("last_seen_ok")))
    state_safe = html.escape(state)
    endpoint_lines = render_remote_peer_endpoints(runtime, peer)
    lines = [
        f"{presence} <b>{instance_id}</b>",
        *endpoint_lines,
        f"backend: <code>{backend}</code>  ·  port: <code>{port}</code>  ·  agents: <code>{agents}</code>",
        f"state: <code>{state_safe}</code>  ·  last handshake: <code>{last_handshake}</code>  ·  last seen: <code>{last_seen_ok}</code>",
    ]
    last_error = html.escape(str(props.get("last_error") or "").strip())
    if last_error:
        lines.append(f"error: <code>{last_error}</code>")
    refresh_error = html.escape(str(props.get("last_refresh_error") or "").strip())
    if refresh_error:
        lines.append(f"refresh: <code>{refresh_error}</code>")
    return lines


def remote_start_log_path(runtime: Any) -> Path:
    log_dir = runtime.global_config.project_root / "tmp"
    log_dir.mkdir(parents=True, exist_ok=True)
    agent_name = getattr(runtime.config, "agent_name", None) or "agent"
    return log_dir / f"{agent_name}_remote_startup.log"


def read_remote_start_log_excerpt(path: Path, max_chars: int = 1200) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return ""
    if not text:
        return ""
    return text[-max_chars:]


def build_remote_start_failure_message(
    runtime: Any,
    *,
    cfg: dict[str, Any],
    cmd: list[str],
    reason: str,
    log_path: Path,
    exit_code: int | None = None,
) -> str:
    cmd_text = html.escape(" ".join(str(part) for part in cmd))
    reason_text = html.escape(str(reason or "unknown startup failure"))
    lines = [
        "🔴 Hashi Remote failed to start.",
        f"Reason: <code>{reason_text}</code>",
        f"Port: <code>{cfg['port']}</code>  ·  TLS: <code>{'on' if cfg['use_tls'] else 'off'}</code>  ·  discovery: <code>{cfg['backend']}</code>",
    ]
    if exit_code is not None:
        lines.append(f"Exit code: <code>{exit_code}</code>")
    lines.append(f"Command: <code>{cmd_text}</code>")
    excerpt = read_remote_start_log_excerpt(log_path)
    if excerpt:
        lines.append(f"log tail: <code>{html.escape(excerpt)}</code>")
    else:
        lines.append(f"log file: <code>{html.escape(str(log_path))}</code>")
    return "\n".join(lines)


async def cmd_remote(runtime: Any, update: Any, context: Any) -> None:
    """Start/stop Hashi Remote. Usage: /remote [on|off|status|list]"""
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    arg = (context.args[0].lower() if context.args else "").strip()
    cfg = remote_config_snapshot(runtime)
    alive = runtime._remote_process is not None and runtime._remote_process.returncode is None

    if arg == "status" or not arg:
        health, health_url = await fetch_remote_json(runtime, "/health")
        status, _status_url = await fetch_remote_json(runtime, "/protocol/status")
        if not health:
            if alive:
                await runtime._reply_text(
                    update,
                    "🟡 Hashi Remote process is running, but the API did not respond.\n"
                    f"PID: {runtime._remote_process.pid}\n"
                    f"Expected port: {cfg['port']}  ·  TLS: {'on' if cfg['use_tls'] else 'off'}"
                )
            else:
                await runtime._reply_text(update, "⚪ Hashi Remote is not running. Use /remote on to start.")
            return
        instance = health.get("instance") or {}
        peers = list((health.get("peers") or []))
        lines = [
            "🟢 <b>Hashi Remote Status</b>",
            f"Instance: <code>{instance.get('instance_id') or runtime.global_config.project_root.name.upper()}</code>",
            f"API: <code>{health_url}</code>",
            f"Port: <code>{cfg['port']}</code>  ·  TLS: <code>{'on' if cfg['use_tls'] else 'off'}</code>",
            f"Discovery: <code>{cfg['backend']}</code>",
            f"Process: <code>{'running' if alive else 'external/unknown'}</code>" + (f" (PID {runtime._remote_process.pid})" if alive else ""),
            f"Peers: <code>{len(peers)}</code>",
        ]
        if status:
            inflight = int(status.get("inflight_count") or 0)
            lines.append(f"Inflight: <code>{inflight}</code>")
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    if arg == "list":
        data, _url = await fetch_remote_json(runtime, "/peers")
        peers = list((data or {}).get("peers") or [])
        if not peers:
            await runtime._reply_text(update, "⚪ No remote peers are currently visible.")
            return
        peers = sorted(
            peers,
            key=lambda peer: (
                remote_peer_presence(runtime, peer)[0],
                str(peer.get("instance_id") or ""),
            ),
        )
        counts = {"online": 0, "attention": 0, "offline": 0}
        for peer in peers:
            rank, _presence, _state = remote_peer_presence(runtime, peer)
            if rank == 0:
                counts["online"] += 1
            elif rank in {1, 2}:
                counts["attention"] += 1
            else:
                counts["offline"] += 1
        lines = [
            "📡 <b>Remote Instances</b>",
            f"online: <code>{counts['online']}</code>  ·  attention: <code>{counts['attention']}</code>  ·  offline: <code>{counts['offline']}</code>",
            f"refreshed: <code>{datetime.now().strftime('%H:%M:%S')}</code>",
            "",
        ]
        for idx, peer in enumerate(peers):
            lines.extend(render_remote_peer_block(runtime, peer))
            if idx != len(peers) - 1:
                lines.append("")
        await runtime._reply_text(update, "\n".join(lines), parse_mode="HTML")
        return

    if arg == "off":
        if runtime._remote_process is None or runtime._remote_process.returncode is not None:
            await runtime._reply_text(update, "⚪ Hashi Remote is not running.")
            return
        runtime._remote_process.terminate()
        try:
            await asyncio.wait_for(runtime._remote_process.wait(), timeout=5)
        except asyncio.TimeoutError:
            runtime._remote_process.kill()
        runtime._remote_process = None
        await runtime._reply_text(update, "🔴 Hashi Remote stopped.")
        return

    if arg == "on":
        if alive:
            await runtime._reply_text(update, "🟢 Already running (PID %d)." % runtime._remote_process.pid)
            return

        root = cfg["root"]
        venv_python = root / ".venv" / "bin" / "python3"
        if not venv_python.exists():
            venv_python = root / ".venv" / "Scripts" / "python.exe"
        if not venv_python.exists():
            await runtime._reply_text(
                update,
                f"🔴 Hashi Remote could not start.\nMissing interpreter: <code>{html.escape(str(venv_python))}</code>",
                parse_mode="HTML",
            )
            return

        cmd = [str(venv_python), "-m", "remote"]
        cmd.extend(["--port", str(cfg["port"])])
        if not cfg["use_tls"]:
            cmd.append("--no-tls")
        if cfg["backend"] in {"lan", "tailscale", "both"}:
            cmd.extend(["--discovery", cfg["backend"]])
        log_path = remote_start_log_path(runtime)
        with suppress(Exception):
            log_path.unlink()
        log_handle = log_path.open("ab")
        try:
            runtime._remote_process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root),
                stdout=log_handle,
                stderr=log_handle,
            )
        finally:
            log_handle.close()

        ok, detail = await await_remote_start_health(
            runtime,
            process=runtime._remote_process,
            cfg=cfg,
            cmd=cmd,
            log_path=log_path,
        )
        if not ok:
            runtime._remote_process = None
            await runtime._reply_text(update, detail, parse_mode="HTML")
            return
        await runtime._reply_text(
            update,
            f"🟢 Hashi Remote started (PID {runtime._remote_process.pid})\n"
            f"   Port {cfg['port']} · TLS {'on' if cfg['use_tls'] else 'off'} · discovery {cfg['backend']}\n"
            f"   API <code>{html.escape(detail)}</code>\n"
            "   Use /remote off to stop.",
            parse_mode="HTML",
        )
        return

    await runtime._reply_text(update, "Usage: /remote [on|off|status|list]")
