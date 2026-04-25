from __future__ import annotations

import asyncio
import base64
import json
import traceback
import sys

from fastmcp.client import Client, UvxStdioTransport


def _serialize_item(item) -> dict:
    item_type = getattr(item, "type", None)
    entry = {"type": item_type}
    if item_type == "text":
        entry["text"] = getattr(item, "text", "")
    elif item_type == "image":
        entry["data"] = getattr(item, "data", "")
        entry["mimeType"] = getattr(item, "mimeType", getattr(item, "mime_type", "image/png"))
    else:
        for name in ("text", "data", "mimeType", "mime_type"):
            value = getattr(item, name, None)
            if value is not None:
                entry[name] = value
    return entry


async def _run(payload: dict) -> dict:
    transport = UvxStdioTransport("windows-mcp")
    async with Client(transport) as client:
        action = payload.get("action")
        if action == "list_tools":
            tools = await client.list_tools()
            return {"ok": True, "tools": [tool.name for tool in tools]}

        result = await client.call_tool(payload["tool"], payload.get("arguments") or {})
        return {
            "ok": True,
            "content": [_serialize_item(item) for item in getattr(result, "content", []) or []],
        }


def main() -> int:
    try:
        if len(sys.argv) < 2:
            raise ValueError("missing payload argument")
        payload = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
        result = asyncio.run(_run(payload))
    except Exception as exc:
        result = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    print("HASHI_JSON:" + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
