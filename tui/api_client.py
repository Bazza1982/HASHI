"""Async client for direct or Hashi-Remote-proxied Workbench access."""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

import aiohttp

from orchestrator.runtime_defaults import DEFAULT_WORKBENCH_LOCALHOST_URL

logger = logging.getLogger(__name__)


class TuiApiClient:
    """Talk to one Workbench while keeping transcript offsets instance-local.

    The launch instance uses local Workbench URLs directly. A switched peer
    uses only the launch instance's loopback Hashi Remote proxy; the TUI never
    connects to a peer Workbench port itself.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_WORKBENCH_LOCALHOST_URL,
        *,
        fallback_base_urls: list[str] | tuple[str, ...] | None = None,
        expected_instance_id: str | None = None,
        remote_url: str | None = None,
        target_instance: str | None = None,
    ):
        bases = [str(base_url or DEFAULT_WORKBENCH_LOCALHOST_URL).rstrip("/")]
        for candidate in fallback_base_urls or ():
            normalized = str(candidate or "").rstrip("/")
            if normalized and normalized not in bases:
                bases.append(normalized)
        self._bases = bases
        self.base = bases[0]
        self.expected_instance_id = str(expected_instance_id or target_instance or "").strip().upper()
        self.remote_url = str(remote_url or "").rstrip("/")
        self.target_instance = str(target_instance or "").strip().upper()
        self._offsets: dict[str, int] = {}

    @property
    def proxied(self) -> bool:
        return bool(self.remote_url and self.target_instance)

    async def _read_json_response(self, response: aiohttp.ClientResponse) -> dict:
        """Parse JSON when possible and preserve plain-text server errors."""
        body = await response.text()
        try:
            data = json.loads(body)
            return data if isinstance(data, dict) else {"ok": False, "error": "Invalid JSON object"}
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": body.strip() or f"HTTP {response.status}",
                "status": response.status,
            }

    async def _direct_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        timeout: float = 10,
    ) -> dict:
        ordered_bases = [self.base, *(base for base in self._bases if base != self.base)]
        last_error: Exception | None = None
        for base in ordered_bases:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                    async with session.request(method, f"{base}{path}", json=json_body) as response:
                        data = await self._read_json_response(response)
                if response.status >= 400:
                    data.setdefault("ok", False)
                    data.setdefault("status", response.status)
                    return data
                if base != self.base:
                    logger.info("TUI selected local Workbench route: %s", base)
                    self.base = base
                return data
            except (aiohttp.ClientError, TimeoutError) as exc:
                last_error = exc
                logger.debug("TUI Workbench route unavailable: url=%s%s error=%s", base, path, exc)
        logger.warning("TUI Workbench request failed: path=%s error=%s", path, last_error)
        return {"ok": False, "error": str(last_error or "Workbench unavailable")}

    async def _proxy_request(
        self,
        operation: str,
        *,
        agent: str | None = None,
        text: str | None = None,
        offset: int = 0,
        limit: int = 20,
        timeout: float = 25,
    ) -> dict:
        payload = {
            "target_instance": self.target_instance,
            "operation": operation,
            "offset": int(offset),
            "limit": int(limit),
        }
        if agent is not None:
            payload["agent"] = agent
        if text is not None:
            payload["text"] = text
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.post(f"{self.remote_url}/tui/proxy", json=payload) as response:
                    envelope = await self._read_json_response(response)
            if response.status >= 400 or not envelope.get("ok"):
                envelope.setdefault("ok", False)
                envelope.setdefault("status", response.status)
                return envelope
            actual = str(envelope.get("target_instance") or "").strip().upper()
            if actual != self.target_instance:
                logger.error(
                    "TUI proxy returned wrong instance: expected=%s actual=%s",
                    self.target_instance,
                    actual or "missing",
                )
                return {"ok": False, "error": "target_identity_mismatch"}
            result = envelope.get("result")
            if not isinstance(result, dict):
                return {"ok": False, "error": "invalid_proxy_response"}
            return result
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.warning(
                "TUI Remote proxy request failed: target=%s operation=%s error=%s",
                self.target_instance,
                operation,
                exc,
            )
            return {"ok": False, "error": str(exc)}

    async def health_info(self) -> dict:
        if self.proxied:
            data = await self._proxy_request("health", timeout=5)
        else:
            data = await self._direct_request("GET", "/api/health", timeout=2)
        actual = str(data.get("instance_id") or "").strip().upper()
        if data.get("ok") and self.expected_instance_id and actual != self.expected_instance_id:
            logger.error(
                "TUI Workbench identity mismatch: expected=%s actual=%s",
                self.expected_instance_id,
                actual or "missing",
            )
            return {
                "ok": False,
                "error": "instance_identity_mismatch",
                "expected_instance_id": self.expected_instance_id,
                "actual_instance_id": actual,
            }
        return data

    async def health(self) -> bool:
        return bool((await self.health_info()).get("ok"))

    async def agents_info(self) -> dict:
        """Return the full agent-directory response for transactional checks."""
        return (
            await self._proxy_request("agents")
            if self.proxied
            else await self._direct_request("GET", "/api/agents")
        )

    async def list_agents(self) -> list[dict]:
        data = await self.agents_info()
        agents = data.get("agents", [])
        return agents if isinstance(agents, list) else []

    async def send_chat(self, agent: str, text: str) -> dict:
        """Send a text message without bypassing the selected transport."""
        if self.proxied:
            return await self._proxy_request("chat", agent=agent, text=text)
        return await self._direct_request(
            "POST",
            "/api/chat",
            json_body={"agent": agent, "text": text},
            timeout=25,
        )

    async def poll_transcript(self, agent: str) -> list[dict]:
        offset = self._offsets.get(agent, 0)
        if self.proxied:
            data = await self._proxy_request("transcript_poll", agent=agent, offset=offset)
        else:
            encoded_agent = quote(agent, safe="")
            data = await self._direct_request(
                "GET",
                f"/api/transcript/{encoded_agent}/poll?offset={offset}",
            )
        if not data.get("ok", True) and data.get("error"):
            logger.warning("TUI transcript poll failed: agent=%s error=%s", agent, data.get("error"))
            return []
        new_offset = data.get("offset", offset)
        if isinstance(new_offset, int) and new_offset > offset:
            self._offsets[agent] = new_offset
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    async def get_recent_transcript(self, agent: str, limit: int = 20) -> list[dict]:
        if self.proxied:
            data = await self._proxy_request("transcript_recent", agent=agent, limit=limit)
        else:
            encoded_agent = quote(agent, safe="")
            data = await self._direct_request(
                "GET",
                f"/api/transcript/{encoded_agent}?limit={int(limit)}",
            )
        if not data.get("ok", True) and data.get("error"):
            logger.warning("TUI transcript load failed: agent=%s error=%s", agent, data.get("error"))
            return []
        offset = data.get("offset", 0)
        self._offsets[agent] = offset if isinstance(offset, int) else 0
        messages = data.get("messages", [])
        return messages if isinstance(messages, list) else []

    def reset_offset(self, agent: str):
        self._offsets.pop(agent, None)
