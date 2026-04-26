"""
Protocol manager for Hashi Remote peer-to-peer messaging.

This is the service-owned control plane for:
  - peer handshake
  - active agent directory exchange
  - merged peer state inspection
  - protocol message ingress
  - transcript-based reply correlation
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)


def _normalize_identity(value: str) -> str:
    value = str(value or "").strip().lower()
    return "".join(ch for ch in value if ch.isalnum())

PROTOCOL_VERSION = "2.0"
DEFAULT_CAPABILITIES = [
    "handshake_v2",
    "agent_directory_v1",
    "protocol_message_v1",
    "agent_reply_v1",
]


class ProtocolManager:
    def __init__(
        self,
        *,
        hashi_root: Path,
        instance_info: dict,
        peer_registry,
        workbench_port: int,
        max_allowed_ttl: int = 8,
        handshake_timeout_seconds: int = 8,
        poll_interval_seconds: float = 0.5,
        settle_window_seconds: float = 2.0,
        reply_soft_timeout_seconds: int = 45,
        reply_hard_timeout_seconds: int = 180,
    ):
        self._hashi_root = hashi_root
        self._instance_info = instance_info
        self._peer_registry = peer_registry
        self._workbench_port = workbench_port
        self._max_allowed_ttl = max(1, int(max_allowed_ttl))
        self._handshake_timeout_seconds = max(2, int(handshake_timeout_seconds))
        self._poll_interval_seconds = max(0.2, float(poll_interval_seconds))
        self._settle_window_seconds = max(0.5, float(settle_window_seconds))
        self._reply_soft_timeout_seconds = max(5, int(reply_soft_timeout_seconds))
        self._reply_hard_timeout_seconds = max(self._reply_soft_timeout_seconds, int(reply_hard_timeout_seconds))
        self._state_dir = Path.home() / ".hashi-remote"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        instance_key = str(instance_info.get("instance_id") or "hashi").lower()
        self._inflight_path = self._state_dir / f"protocol_inflight_{instance_key}.json"
        self._inflight: dict[str, dict[str, Any]] = self._load_json(self._inflight_path).get("messages", {})
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_handshake_run = 0.0

    def get_protocol_status(self) -> dict:
        peers = []
        if self._peer_registry:
            for peer in self._peer_registry.get_peers():
                peers.append(self._peer_registry.get_peer_state(peer.instance_id))
        local_profile = self._local_network_profile()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "display_handle": self.display_handle,
            "capabilities": list(DEFAULT_CAPABILITIES),
            "local_agents": self.get_local_agents_snapshot(),
            "local_network_profile": local_profile,
            "peers": peers,
            "inflight_count": len(self._inflight),
            "max_allowed_ttl": self._max_allowed_ttl,
            "reply_soft_timeout_seconds": self._reply_soft_timeout_seconds,
            "reply_hard_timeout_seconds": self._reply_hard_timeout_seconds,
        }

    @property
    def display_handle(self) -> str:
        return f"@{str(self._instance_info.get('instance_id', 'hashi')).lower()}"

    def _local_network_profile(self) -> dict:
        from remote.peer.base import PeerInfo
        from remote.peer.lan import build_local_network_profile

        info = PeerInfo(
            instance_id=str(self._instance_info.get("instance_id") or "HASHI"),
            display_name=str(self._instance_info.get("display_name") or self._instance_info.get("instance_id") or "HASHI"),
            host=str(self._instance_info.get("api_host") or "127.0.0.1"),
            port=int(self._instance_info.get("remote_port") or 0),
            workbench_port=int(self._instance_info.get("workbench_port") or 18800),
            platform=str(self._instance_info.get("platform") or "unknown"),
            hashi_version=str(self._instance_info.get("hashi_version") or "unknown"),
            display_handle=self.display_handle,
            protocol_version=PROTOCOL_VERSION,
            capabilities=list(DEFAULT_CAPABILITIES),
        )
        return build_local_network_profile(info)

    def get_local_agents_snapshot(self) -> list[dict]:
        agents_path = self._hashi_root / "agents.json"
        if not agents_path.exists():
            return []
        try:
            data = json.loads(agents_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return []
        snapshot = []
        for agent in data.get("agents", []):
            if not agent.get("is_active", True):
                continue
            snapshot.append(
                {
                    "agent_name": agent["name"],
                    "agent_address": f"{agent['name']}@{str(self._instance_info.get('instance_id', 'HASHI')).lower()}",
                    "display_name": agent.get("display_name", agent["name"]),
                    "is_active": True,
                    "updated_at": int(time.time()),
                }
            )
        return snapshot

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Reset any stale handshake_in_progress states left over from a previous run.
        # These would otherwise block the handshake cycle indefinitely.
        if self._peer_registry:
            for peer in self._peer_registry.get_peers():
                state = str((peer.properties or {}).get("handshake_state") or "")
                if state == "handshake_in_progress":
                    self._peer_registry.mark_handshake_result(peer.instance_id, state="handshake_pending")
        # Bootstrap known peers from instances.json before first handshake cycle.
        # This ensures peers are reachable even when mDNS multicast fails
        # (e.g. WSL2 → physical LAN boundary).
        asyncio.create_task(self._bootstrap_known_peers())
        self._task = asyncio.create_task(self._control_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _bootstrap_known_peers(self) -> None:
        """
        Probe peers listed in instances.json and register reachable ones.

        This is a fallback for environments where mDNS multicast doesn't cross
        network boundaries (e.g. WSL2 to physical LAN). Any instance that has
        a remote_port and a reachable host is injected into the peer registry so
        the normal handshake cycle can then proceed with it.
        """
        await asyncio.sleep(2)  # Give mDNS a moment to discover what it can
        local_id = str(self._instance_info.get("instance_id") or "").upper()
        instances = self._load_instances()
        for key, entry in instances.items():
            if not isinstance(entry, dict):
                continue
            instance_id = str(entry.get("instance_id") or key).upper()
            if instance_id == local_id:
                continue
            if self._peer_registry and self._peer_registry.get_peer(instance_id):
                continue  # Already known via mDNS
            remote_port = entry.get("remote_port")
            if not remote_port:
                continue
            seen_hosts = self._candidate_hosts_for_entry(entry)

            for host in seen_hosts:
                if self._probe_route(host, int(remote_port), timeout=2):
                    from remote.peer.base import PeerInfo
                    peer = PeerInfo(
                        instance_id=instance_id,
                        display_name=str(entry.get("display_name") or instance_id),
                        host=host,
                        port=int(remote_port),
                        workbench_port=int(entry.get("workbench_port") or 18800),
                        platform=str(entry.get("platform") or "unknown"),
                        hashi_version=str(entry.get("hashi_version") or "unknown"),
                        display_handle=f"@{instance_id.lower()}",
                        protocol_version=str(entry.get("protocol_version") or "1.0"),
                        capabilities=list(entry.get("capabilities") or []),
                        properties={
                            "discovery": "bootstrap",
                            "address_candidates": list(entry.get("address_candidates") or []),
                            "observed_candidates": list(entry.get("observed_candidates") or []),
                            "host_identity": _normalize_identity(entry.get("host_identity") or ""),
                            "environment_kind": str(entry.get("environment_kind") or "").strip().lower(),
                        },
                    )
                    if self._peer_registry:
                        self._peer_registry.on_peers_changed([peer])
                        logger.info("Bootstrap: registered %s @ %s:%d", instance_id, host, remote_port)
                    break
                else:
                    logger.debug("Bootstrap: %s @ %s:%d not reachable", instance_id, host, remote_port)

    async def _control_loop(self) -> None:
        while self._running:
            try:
                now = time.time()
                if now - self._last_handshake_run >= 5:
                    await self._handshake_once()
                    self._last_handshake_run = now
                await self._process_inflight_once()
            except Exception as exc:
                logger.warning("Protocol control loop failed: %s", exc)
            await asyncio.sleep(self._poll_interval_seconds)

    async def _handshake_once(self) -> None:
        if not self._peer_registry:
            return
        for peer in self._peer_registry.get_peers():
            state = str((peer.properties or {}).get("handshake_state") or "handshake_pending")
            last_handshake_at = float((peer.properties or {}).get("last_handshake_at") or 0)
            should_revalidate = state == "handshake_accepted" and (time.time() - last_handshake_at) >= 30
            if state == "handshake_in_progress":
                continue
            if state == "handshake_accepted" and not should_revalidate:
                continue
            self._peer_registry.mark_handshake_result(peer.instance_id, state="handshake_in_progress")
            local_profile = self._local_network_profile()
            payload = {
                "from_instance": self._instance_info.get("instance_id"),
                "display_handle": self.display_handle,
                "protocol_version": PROTOCOL_VERSION,
                "capabilities": list(DEFAULT_CAPABILITIES),
                "hashi_version": self._instance_info.get("hashi_version", "unknown"),
                "agents": self.get_local_agents_snapshot(),
                "remote_port": self._instance_info.get("remote_port") or 0,
                "workbench_port": self._instance_info.get("workbench_port") or 18800,
                "platform": self._instance_info.get("platform") or "unknown",
                "host_identity": local_profile.get("host_identity"),
                "environment_kind": local_profile.get("environment_kind"),
                "address_candidates": list(local_profile.get("address_candidates") or []),
                "observed_candidates": list(local_profile.get("observed_candidates") or []),
            }
            candidate_hosts = self._candidate_hosts_for_peer(peer)

            succeeded = False
            for host in candidate_hosts:
                url = f"http://{host}:{peer.port}/protocol/handshake"
                try:
                    result = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda u=url: self._post_json(u, payload, timeout=self._handshake_timeout_seconds),
                    )
                    if str(result.get("status") or "").lower() == "handshake_reject":
                        self._peer_registry.mark_handshake_result(
                            peer.instance_id,
                            state="handshake_rejected",
                            last_error=str(result.get("reason") or "rejected"),
                        )
                        succeeded = True
                        break
                    # If a fallback host worked, re-register peer with the working host
                    if host != peer.host:
                        from remote.peer.base import PeerInfo
                        updated = dataclasses.replace(peer, host=host)
                        updated.properties = {
                            key: value
                            for key, value in dict(peer.properties or {}).items()
                            if key not in {
                                "preferred_backend",
                                "alternate_backends",
                                "handshake_state",
                                "last_handshake_at",
                                "last_error",
                                "remote_agents",
                            }
                        }
                        updated.properties["discovery"] = "bootstrap_fallback"
                        self._peer_registry.on_peers_changed([updated])
                        logger.info("Handshake: switched %s host from %s to %s", peer.instance_id, peer.host, host)
                    self._peer_registry.mark_handshake_result(
                        peer.instance_id,
                        state="handshake_accepted",
                        protocol_version=str(result.get("protocol_version") or PROTOCOL_VERSION),
                        capabilities=list(result.get("capabilities") or []),
                        remote_agents=list(result.get("agents") or []),
                    )
                    succeeded = True
                    break
                except Exception as exc:
                    logger.debug("Handshake: %s @ %s:%d failed: %s", peer.instance_id, host, peer.port, exc)

            if not succeeded:
                self._peer_registry.mark_handshake_result(
                    peer.instance_id,
                    state="handshake_timed_out",
                    last_error=f"all hosts unreachable: {candidate_hosts}",
                )

    def handle_handshake(self, payload: dict) -> dict:
        from_instance = str(payload.get("from_instance") or "").strip().upper()
        if not from_instance:
            return {"status": "handshake_reject", "reason": "missing from_instance"}
        if from_instance == str(self._instance_info.get("instance_id") or "").upper():
            return {"status": "handshake_reject", "reason": "self handshake rejected"}

        # Reverse-register the sender as a peer so we can reach them back.
        # The sender's IP comes from the HTTP request (_client_ip injected by server.py).
        client_ip = str(payload.get("_client_ip") or "").strip()
        remote_port = int(payload.get("remote_port") or 0)
        if client_ip and remote_port and self._peer_registry:
            from remote.peer.base import PeerInfo
            peer = PeerInfo(
                instance_id=from_instance,
                display_name=str(payload.get("display_handle") or from_instance),
                host=client_ip,
                port=remote_port,
                workbench_port=int(payload.get("workbench_port") or 18800),
                platform=str(payload.get("platform") or "unknown"),
                hashi_version=str(payload.get("hashi_version") or "unknown"),
                display_handle=str(payload.get("display_handle") or f"@{from_instance.lower()}"),
                protocol_version=str(payload.get("protocol_version") or PROTOCOL_VERSION),
                capabilities=list(payload.get("capabilities") or []),
                properties={
                    "discovery": "handshake_inbound",
                    "address_candidates": list(payload.get("address_candidates") or []),
                    "observed_candidates": list(payload.get("observed_candidates") or []),
                    "host_identity": _normalize_identity(payload.get("host_identity") or ""),
                    "environment_kind": str(payload.get("environment_kind") or "").strip().lower(),
                },
            )
            self._peer_registry.on_peers_changed([peer])
            logger.info(
                "Handshake: reverse-registered %s @ %s:%d",
                from_instance, client_ip, remote_port,
            )

        local_profile = self._local_network_profile()
        return {
            "status": "handshake_accept",
            "instance_id": self._instance_info.get("instance_id"),
            "display_handle": self.display_handle,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": list(DEFAULT_CAPABILITIES),
            "hashi_version": self._instance_info.get("hashi_version", "unknown"),
            "agents": self.get_local_agents_snapshot(),
            "remote_port": self._instance_info.get("remote_port") or 0,
            "workbench_port": self._instance_info.get("workbench_port") or 18800,
            "platform": self._instance_info.get("platform") or "unknown",
            "host_identity": local_profile.get("host_identity"),
            "environment_kind": local_profile.get("environment_kind"),
            "address_candidates": list(local_profile.get("address_candidates") or []),
            "observed_candidates": list(local_profile.get("observed_candidates") or []),
        }

    async def handle_protocol_message(self, payload: dict) -> tuple[int, dict]:
        message_type = str(payload.get("message_type") or "agent_message").strip().lower()
        if message_type == "agent_reply":
            return await self._handle_agent_reply(payload)

        normalized_ttl = min(max(int(payload.get("ttl") or self._max_allowed_ttl), 0), self._max_allowed_ttl)
        if normalized_ttl <= 0:
            return 400, self._error_payload("delivery_expired", "TTL expired or invalid", retryable=False, payload=payload)

        message_id = str(payload.get("message_id") or "").strip()
        conversation_id = str(payload.get("conversation_id") or "").strip()
        from_instance = str(payload.get("from_instance") or "").strip().upper()
        from_agent = str(payload.get("from_agent") or "").strip().lower()
        to_agent = str(payload.get("to_agent") or "").strip().lower()
        route_trace = [str(x).upper() for x in (payload.get("route_trace") or []) if str(x).strip()]
        local_instance = str(self._instance_info.get("instance_id") or "").upper()

        if not all([message_id, conversation_id, from_instance, from_agent, to_agent]):
            return 400, self._error_payload("invalid_message", "Missing required protocol message fields", retryable=False, payload=payload)
        if local_instance in route_trace:
            return 409, self._error_payload("loop_detected", "Local instance already present in route_trace", retryable=False, payload=payload)

        existing = self._inflight.get(message_id)
        if existing:
            state = str(existing.get("state") or "")
            if state in {"reply_sent", "completed"}:
                return 409, self._error_payload("duplicate_message", "Message already completed", retryable=False, payload=payload)
            if state in {"delivery_in_progress", "delivered_to_local_queue", "assistant_started", "assistant_streaming"}:
                return 202, {
                    "ok": True,
                    "message_type": "ack",
                    "message_id": message_id,
                    "conversation_id": conversation_id,
                    "accepted": True,
                    "state": state,
                    "request_id": existing.get("request_id"),
                    "normalized_ttl": existing.get("ttl", normalized_ttl),
                }

        # If message is addressed to a different instance, forward it there.
        to_instance = str(payload.get("to_instance") or "").strip().upper()
        if to_instance and to_instance != local_instance:
            peer = self._peer_registry.get_peer(to_instance) if self._peer_registry else None
            if peer is None:
                return 404, self._error_payload(
                    "target_instance_not_found",
                    f"Target instance '{to_instance}' not in peer registry",
                    retryable=True, payload=payload,
                )
            # Add ourselves to route_trace before forwarding
            forward_payload = dict(payload)
            forward_payload["route_trace"] = list(route_trace) + [local_instance]
            forward_payload["hop_count"] = int(payload.get("hop_count") or 0) + 1
            forward_payload["ttl"] = normalized_ttl - 1
            fwd_hosts = self._candidate_hosts_for_peer(peer)
            fwd_exc = None
            for fwd_host in fwd_hosts:
                fwd_url = f"http://{fwd_host}:{peer.port}/protocol/message"
                try:
                    result = await asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda u=fwd_url: self._post_json(u, forward_payload, timeout=4),
                    )
                    return 202, result
                except Exception as exc:
                    fwd_exc = exc
                    logger.debug("Forward: %s @ %s:%d failed: %s", to_instance, fwd_host, peer.port, exc)
            return 502, self._error_payload(
                "forward_failed",
                f"Failed to forward to {to_instance} (tried {fwd_hosts}): {fwd_exc}",
                retryable=True, payload=payload,
            )

        local_agents = {item["agent_name"] for item in self.get_local_agents_snapshot()}
        if to_agent not in local_agents:
            return 404, self._error_payload("target_agent_not_found", f"Target agent '{to_agent}' not found", retryable=False, payload=payload)

        prompt_text = self._render_remote_message_prompt(from_agent, from_instance, payload.get("body") or {})
        start_offset = await self._get_transcript_offset(to_agent)
        request_id = await self._enqueue_local_prompt(to_agent, prompt_text)
        if not request_id:
            return 502, self._error_payload("local_enqueue_failed", "Workbench enqueue failed", retryable=True, payload=payload)

        self._inflight[message_id] = {
            "message_id": message_id,
            "conversation_id": conversation_id,
            "from_instance": from_instance,
            "from_agent": from_agent,
            "to_instance": local_instance,
            "to_agent": to_agent,
            "request_id": request_id,
            "prompt_text": prompt_text,
            "state": "delivered_to_local_queue",
            "matched_user_prompt": False,
            "transcript_offset_at_enqueue": start_offset,
            "last_seen_offset": start_offset,
            "assistant_segments": [],
            "reply_target_agent": from_agent,
            "settle_deadline": 0,
            "reply_soft_deadline": time.time() + self._reply_soft_timeout_seconds,
            "reply_hard_deadline": time.time() + self._reply_hard_timeout_seconds,
            "updated_at": int(time.time()),
            "ttl": normalized_ttl,
        }
        self._save_inflight()
        return 202, {
            "ok": True,
            "message_type": "ack",
            "message_id": message_id,
            "conversation_id": conversation_id,
            "accepted": True,
            "state": "delivered_to_local_queue",
            "request_id": request_id,
            "normalized_ttl": normalized_ttl,
        }

    async def _handle_agent_reply(self, payload: dict) -> tuple[int, dict]:
        to_agent = str(payload.get("to_agent") or "").strip().lower()
        from_agent = str(payload.get("from_agent") or "").strip().lower()
        from_instance = str(payload.get("from_instance") or "").strip().upper()
        body = payload.get("body") or {}
        if not to_agent:
            return 400, self._error_payload("invalid_reply", "Missing to_agent for agent_reply", retryable=False, payload=payload)
        local_agents = {item["agent_name"] for item in self.get_local_agents_snapshot()}
        if to_agent not in local_agents:
            return 404, self._error_payload("target_agent_unavailable", f"Reply target '{to_agent}' is unavailable", retryable=True, payload=payload)
        prompt_text = self._render_remote_reply_prompt(from_agent, from_instance, body)
        request_id = await self._enqueue_local_prompt(to_agent, prompt_text)
        if not request_id:
            return 502, self._error_payload("local_enqueue_failed", "Failed to inject reply into local agent", retryable=True, payload=payload)
        return 202, {
            "ok": True,
            "message_type": "ack",
            "accepted": True,
            "state": "reply_delivered_locally",
            "request_id": request_id,
            "in_reply_to": payload.get("in_reply_to"),
            "conversation_id": payload.get("conversation_id"),
        }

    def _render_remote_message_prompt(self, from_agent: str, from_instance: str, body: dict) -> str:
        text = str((body or {}).get("text") or "").strip()
        return f"System exchange message from {from_agent}@{from_instance}:\n{text}"

    def _render_remote_reply_prompt(self, from_agent: str, from_instance: str, body: dict) -> str:
        text = str((body or {}).get("text") or "").strip()
        return f"System exchange reply from {from_agent}@{from_instance}:\n{text}"

    async def _enqueue_local_prompt(self, agent_name: str, text: str) -> str | None:
        url = f"http://127.0.0.1:{self._workbench_port}/api/chat"
        payload = {"agent": agent_name, "text": text}
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._post_json(url, payload, timeout=10),
            )
            if result.get("ok"):
                return str(result.get("request_id") or "")
        except Exception as exc:
            logger.warning("Protocol local enqueue failed: %s", exc)
        return None

    async def _get_transcript_offset(self, agent_name: str) -> int:
        url = f"http://127.0.0.1:{self._workbench_port}/api/transcript/{agent_name}?limit=1"
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._get_json(url, timeout=10),
            )
            return int(result.get("offset") or 0)
        except Exception:
            return 0

    async def _poll_transcript(self, agent_name: str, offset: int) -> dict:
        url = f"http://127.0.0.1:{self._workbench_port}/api/transcript/{agent_name}/poll?offset={offset}"
        return await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self._get_json(url, timeout=10),
        )

    async def _process_inflight_once(self) -> None:
        if not self._inflight:
            return
        dirty = False
        now = time.time()
        for message_id, item in list(self._inflight.items()):
            state = str(item.get("state") or "")
            if state in {"reply_sent", "failed", "timed_out"}:
                continue
            if now >= float(item.get("reply_hard_deadline") or 0):
                item["state"] = "timed_out"
                item["updated_at"] = int(now)
                self._inflight[message_id] = item
                dirty = True
                continue
            if state == "reply_failed" and item.get("reply_text"):
                sent = await self._send_agent_reply(item, str(item.get("reply_text") or ""))
                item["state"] = "reply_sent" if sent else "reply_failed"
                item["updated_at"] = int(now)
                self._inflight[message_id] = item
                dirty = True
                continue
            try:
                changed = await self._advance_inflight_item(item, now=now)
                dirty = dirty or changed
                self._inflight[message_id] = item
            except Exception as exc:
                logger.warning("Failed processing inflight %s: %s", message_id, exc)
        if dirty:
            self._save_inflight()

    async def _advance_inflight_item(self, item: dict, *, now: float) -> bool:
        agent_name = str(item.get("to_agent") or "").lower()
        data = await self._poll_transcript(agent_name, int(item.get("last_seen_offset") or 0))
        item["last_seen_offset"] = int(data.get("offset") or item.get("last_seen_offset") or 0)
        messages = data.get("messages") or []
        changed = False

        for message in messages:
            role = str(message.get("role") or "")
            text = str(message.get("text") or "")
            if not text:
                continue
            if not item.get("matched_user_prompt") and role == "user" and text == item.get("prompt_text"):
                item["matched_user_prompt"] = True
                item["state"] = "matched_user_prompt"
                changed = True
                continue
            if item.get("matched_user_prompt") and role == "assistant":
                segments = list(item.get("assistant_segments") or [])
                if not segments or segments[-1] != text:
                    segments.append(text)
                    item["assistant_segments"] = segments
                    item["state"] = "assistant_streaming" if len(segments) > 1 else "assistant_started"
                    item["settle_deadline"] = now + self._settle_window_seconds
                    changed = True

        if item.get("assistant_segments") and float(item.get("settle_deadline") or 0) and now >= float(item.get("settle_deadline") or 0):
            reply_text = "\n\n".join(str(x).strip() for x in item.get("assistant_segments") or [] if str(x).strip()).strip()
            if not reply_text:
                item["state"] = "failed"
                changed = True
                return changed
            sent = await self._send_agent_reply(item, reply_text)
            item["reply_text"] = reply_text
            item["state"] = "reply_sent" if sent else "reply_failed"
            item["updated_at"] = int(now)
            changed = True
        return changed

    async def _send_agent_reply(self, item: dict, reply_text: str) -> bool:
        route = self._resolve_peer_route(str(item.get("from_instance") or ""))
        if route is None:
            logger.warning("Cannot send reply for %s: origin peer unavailable", item.get("message_id"))
            return False
        payload = {
            "message_type": "agent_reply",
            "message_id": f"{item['message_id']}:reply",
            "conversation_id": item.get("conversation_id"),
            "in_reply_to": item.get("message_id"),
            "from_instance": self._instance_info.get("instance_id"),
            "from_agent": item.get("to_agent"),
            "to_instance": item.get("from_instance"),
            "to_agent": item.get("reply_target_agent") or item.get("from_agent"),
            "body": {"text": reply_text},
            "hop_count": 0,
            "ttl": min(int(item.get("ttl") or self._max_allowed_ttl), self._max_allowed_ttl),
            "route_trace": [str(self._instance_info.get("instance_id") or "").upper()],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        url = f"http://{route['host']}:{route['port']}/protocol/message"
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._post_json(url, payload, timeout=10),
            )
            return bool(result.get("ok", True))
        except Exception as exc:
            logger.warning("Reply send failed to %s: %s", route.get("instance_id"), exc)
            return False

    def _error_payload(self, code: str, message: str, *, retryable: bool, payload: dict) -> dict:
        return {
            "ok": False,
            "message_type": "error",
            "body": {
                "code": code,
                "message": message,
                "retryable": bool(retryable),
                "failed_message_id": payload.get("message_id"),
                "conversation_id": payload.get("conversation_id"),
                "from_instance": payload.get("from_instance"),
                "from_agent": payload.get("from_agent"),
                "to_instance": payload.get("to_instance") or self._instance_info.get("instance_id"),
                "to_agent": payload.get("to_agent"),
                "details": {},
            },
        }

    def _get_json(self, url: str, timeout: int = 10) -> dict:
        req = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post_json(self, url: str, payload: dict, timeout: int = 10) -> dict:
        req = urllib_request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _load_json(self, path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _load_instances(self) -> dict:
        path = self._hashi_root / "instances.json"
        data = self._load_json(path)
        return data.get("instances", {}) if isinstance(data, dict) else {}

    def _same_machine_hint(self, entry: dict) -> bool:
        if not isinstance(entry, dict):
            return False
        if str(entry.get("same_host_loopback") or "").strip():
            return True
        instances = self._load_instances()
        local_entry = instances.get(str(self._instance_info.get("instance_id") or "").lower(), {})
        local_platform = str(self._instance_info.get("platform") or local_entry.get("platform") or "").lower()
        target_platform = str(entry.get("platform") or "").lower()
        if {local_platform, target_platform} != {"windows", "wsl"}:
            return False
        if local_platform == "windows" and entry.get("wsl_root_from_windows"):
            return True
        if local_platform == "wsl" and entry.get("wsl_root"):
            return True
        local_identity = _normalize_identity(local_entry.get("host_identity") or "")
        target_identity = _normalize_identity(entry.get("host_identity") or "")
        if local_identity and target_identity and local_identity == target_identity:
            return True
        local_hosts = {
            str(local_entry.get("api_host") or "").strip().lower(),
            str(local_entry.get("lan_ip") or "").strip().lower(),
            str(local_entry.get("tailscale_ip") or "").strip().lower(),
        }
        target_hosts = {
            str(entry.get("api_host") or "").strip().lower(),
            str(entry.get("lan_ip") or "").strip().lower(),
            str(entry.get("tailscale_ip") or "").strip().lower(),
        }
        local_hosts.discard("")
        target_hosts.discard("")
        return bool(local_hosts and target_hosts and local_hosts.intersection(target_hosts))

    def _candidate_hosts_for_entry(self, entry: dict) -> list[str]:
        hosts: list[str] = []
        loopback = str(entry.get("same_host_loopback") or "").strip()
        if loopback:
            hosts.append(loopback)
        elif self._same_machine_hint(entry):
            hosts.append("127.0.0.1")
        for item in entry.get("address_candidates") or []:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip()
            scope = str(item.get("scope") or "").strip().lower()
            if not host:
                continue
            if scope == "same_host" and host not in hosts and self._same_machine_hint(entry):
                hosts.append(host)
            elif scope in {"lan", "overlay", "routable"} and host not in hosts:
                hosts.append(host)
        for host in (entry.get("lan_ip"), entry.get("tailscale_ip"), entry.get("api_host")):
            host = str(host or "").strip()
            if host and host not in hosts and host not in {"0.0.0.0", "localhost"}:
                hosts.append(host)
        return hosts

    def _candidate_hosts_for_peer(self, peer) -> list[str]:
        entry = self._load_instances().get(str(peer.instance_id or "").lower(), {})
        hosts = self._candidate_hosts_for_entry(entry) if isinstance(entry, dict) else []
        same_host_hint = self._same_machine_hint(entry) if isinstance(entry, dict) else False
        for item in (peer.properties or {}).get("address_candidates") or []:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host") or "").strip()
            scope = str(item.get("scope") or "").strip().lower()
            if not host or host in hosts:
                continue
            if scope == "same_host" and same_host_hint:
                hosts.append(host)
            elif scope in {"lan", "overlay", "routable", "peer"}:
                hosts.append(host)
        peer_host = str(peer.host or "").strip()
        if peer_host and peer_host not in hosts:
            hosts.append(peer_host)
        if not hosts and peer_host:
            hosts.append(peer_host)
        return hosts

    def _probe_route(self, host: str, port: int, timeout: int = 2) -> bool:
        req = urllib_request.Request(f"http://{host}:{port}/health", method="GET")
        try:
            with urllib_request.urlopen(req, timeout=timeout):
                return True
        except HTTPError:
            return True
        except URLError:
            return False
        except Exception:
            return True

    def _resolve_peer_route(self, instance_id: str):
        peer = self._peer_registry.get_peer(str(instance_id or "")) if self._peer_registry else None
        if peer is not None:
            return {"host": peer.host, "port": peer.port, "instance_id": peer.instance_id}
        entry = self._load_instances().get(str(instance_id or "").lower())
        if not isinstance(entry, dict):
            return None
        port = entry.get("remote_port")
        if not port:
            return None
        candidates = self._candidate_hosts_for_entry(entry)
        if not candidates:
            return None
        selected_host = candidates[0]
        for host in candidates:
            if self._probe_route(host, int(port)):
                selected_host = host
                break
        return {
            "host": selected_host,
            "port": int(port),
            "instance_id": str(entry.get("instance_id") or instance_id).upper(),
        }

    def _save_inflight(self) -> None:
        self._inflight_path.write_text(
            json.dumps({"messages": self._inflight}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
