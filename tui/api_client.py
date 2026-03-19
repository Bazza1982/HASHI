"""Async HTTP client for talking to the HASHI Workbench API (:18800)."""
from __future__ import annotations

import asyncio
import json
import aiohttp


class TuiApiClient:
    def __init__(self, base_url: str = "http://localhost:18800"):
        self.base = base_url.rstrip("/")
        self._offsets: dict[str, int] = {}  # per-agent transcript byte offset

    async def _read_json_response(self, response: aiohttp.ClientResponse) -> dict:
        """Parse JSON when possible and preserve plain-text server errors."""
        body = await response.text()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {
                "ok": False,
                "error": body.strip() or f"HTTP {response.status}",
                "status": response.status,
            }

    # ── Health ──────────────────────────────────────────────────────────
    async def health(self) -> bool:
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"{self.base}/api/health", timeout=aiohttp.ClientTimeout(total=2))
                return r.status == 200
        except Exception:
            return False

    # ── Agents ──────────────────────────────────────────────────────────
    async def list_agents(self) -> list[dict]:
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"{self.base}/api/agents")
                data = await self._read_json_response(r)
                return data.get("agents", [])
        except Exception:
            return []

    # ── Chat ────────────────────────────────────────────────────────────
    async def send_chat(self, agent: str, text: str) -> dict:
        """POST /api/chat — send a text message to an agent."""
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.post(
                    f"{self.base}/api/chat",
                    json={"agent": agent, "text": text},
                )
                return await self._read_json_response(r)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── Transcript polling ──────────────────────────────────────────────
    async def poll_transcript(self, agent: str) -> list[dict]:
        """GET /api/transcript/{agent}/poll?offset=N — incremental fetch."""
        offset = self._offsets.get(agent, 0)
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"{self.base}/api/transcript/{agent}/poll?offset={offset}")
                data = await self._read_json_response(r)
                new_offset = data.get("offset", offset)
                if new_offset > offset:
                    self._offsets[agent] = new_offset
                return data.get("messages", [])
        except Exception:
            return []

    async def get_recent_transcript(self, agent: str, limit: int = 20) -> list[dict]:
        """GET /api/transcript/{agent} — get recent messages (initial load)."""
        try:
            async with aiohttp.ClientSession() as s:
                r = await s.get(f"{self.base}/api/transcript/{agent}?limit={limit}")
                data = await self._read_json_response(r)
                self._offsets[agent] = data.get("offset", 0)
                return data.get("messages", [])
        except Exception:
            return []

    def reset_offset(self, agent: str):
        self._offsets.pop(agent, None)
