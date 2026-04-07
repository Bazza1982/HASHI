"""
Obsidian Tool Pack — HTTP client and async executors.

Wraps the Obsidian Local REST API plugin endpoints.
All methods are async and return strings (for HASHI ToolRegistry compatibility).

API docs: https://github.com/coddingtonbear/obsidian-local-rest-api
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger("Tools.Obsidian")


class ObsidianClient:
    """
    Async HTTP client for the Obsidian Local REST API.

    Credentials are injected from HASHI secrets.json:
        obsidian_base_url  (default: http://127.0.0.1:27123)
        obsidian_api_key   (required — generate in plugin settings)
    """

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, endpoint: str, params: Optional[dict] = None) -> httpx.Response:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            return await client.get(
                f"{self.base_url}{endpoint}",
                headers=self.headers,
                params=params,
            )

    async def _put(self, endpoint: str, content: str) -> httpx.Response:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            return await client.put(
                f"{self.base_url}{endpoint}",
                headers={**self.headers, "Content-Type": "text/markdown"},
                content=content.encode("utf-8"),
            )

    async def _post(self, endpoint: str, data: Optional[dict] = None, content: str = "") -> httpx.Response:
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            if data is not None:
                return await client.post(
                    f"{self.base_url}{endpoint}",
                    headers=self.headers,
                    json=data,
                )
            else:
                return await client.post(
                    f"{self.base_url}{endpoint}",
                    headers={**self.headers, "Content-Type": "text/markdown"},
                    content=content.encode("utf-8"),
                )

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    async def read_note(self, path: str) -> str:
        resp = await self._get(f"/vault/{path}")
        if resp.status_code == 200:
            return resp.text
        return f"Error {resp.status_code}: {resp.text}"

    async def write_note(self, path: str, content: str) -> str:
        resp = await self._put(f"/vault/{path}", content)
        if resp.status_code in (200, 204):
            return f"Note written: {path}"
        return f"Error {resp.status_code}: {resp.text}"

    async def append_note(self, path: str, content: str) -> str:
        resp = await self._post(f"/vault/{path}", content=f"\n{content}")
        if resp.status_code in (200, 204):
            return f"Content appended to: {path}"
        return f"Error {resp.status_code}: {resp.text}"

    async def list_folder(self, folder: str) -> str:
        folder = folder.rstrip("/") + "/"
        resp = await self._get(f"/vault/{folder}")
        if resp.status_code == 200:
            data = resp.json()
            files = data.get("files", [])
            if not files:
                return f"Folder '{folder}' is empty."
            return "\n".join(files)
        return f"Error {resp.status_code}: {resp.text}"

    async def search(self, query: str, limit: int = 20) -> str:
        resp = await self._post("/search/simple/", data={"query": query})
        if resp.status_code == 200:
            results = resp.json()
            if not results:
                return "No results found."
            output = []
            for i, r in enumerate(results[:limit], 1):
                path = r.get("filename", "")
                excerpt = r.get("context", {}).get("content", "")[:200]
                output.append(f"{i}. {path}\n   {excerpt}")
            return "\n\n".join(output)
        return f"Error {resp.status_code}: {resp.text}"

    async def get_active(self) -> str:
        resp = await self._get("/active/")
        if resp.status_code == 200:
            return resp.text
        if resp.status_code == 404:
            return "No note is currently active in Obsidian."
        return f"Error {resp.status_code}: {resp.text}"

    async def open_note(self, path: str) -> str:
        resp = await self._post(f"/open/{path}")
        if resp.status_code in (200, 204):
            return f"Obsidian opened: {path}"
        return f"Error {resp.status_code}: {resp.text}"

    # ------------------------------------------------------------------
    # Dispatcher — called by HASHI ToolRegistry
    # ------------------------------------------------------------------

    async def dispatch(self, tool_name: str, args: dict) -> str:
        try:
            if tool_name == "obsidian_read_note":
                return await self.read_note(args["path"])
            elif tool_name == "obsidian_write_note":
                return await self.write_note(args["path"], args["content"])
            elif tool_name == "obsidian_append_note":
                return await self.append_note(args["path"], args["content"])
            elif tool_name == "obsidian_list_folder":
                return await self.list_folder(args.get("folder", ""))
            elif tool_name == "obsidian_search":
                return await self.search(args["query"], int(args.get("limit", 20)))
            elif tool_name == "obsidian_get_active":
                return await self.get_active()
            elif tool_name == "obsidian_open_note":
                return await self.open_note(args["path"])
            else:
                return f"Unknown obsidian tool: {tool_name}"
        except httpx.ConnectError:
            return (
                "Cannot connect to Obsidian Local REST API. "
                "Please ensure Obsidian is open and the Local REST API plugin is enabled."
            )
        except Exception as e:
            logger.exception("Obsidian tool error")
            return f"Error: {e}"
