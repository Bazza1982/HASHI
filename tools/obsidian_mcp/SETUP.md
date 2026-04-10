# Obsidian Tool Pack — Setup Guide

## Step 1: Obsidian (Windows side)

1. Open Obsidian
2. Settings → Community Plugins → turn off Safe Mode if needed → Browse
3. Search **"Local REST API"** → Install → Enable
4. Go to the plugin settings → click **Generate** to create an API Key
5. Note the port (default: `27123`)

## Step 2: HASHI secrets.json

Add to `/home/lily/projects/hashi/secrets.json`:

```json
{
  "obsidian_api_key": "paste-your-key-here",
  "obsidian_base_url": "http://127.0.0.1:27123"
}
```

## Step 3: Activate schemas in tools/schemas.py

Add these two lines at the bottom of `tools/schemas.py`:

```python
from tools.obsidian_mcp.schemas import OBSIDIAN_TOOL_SCHEMAS, OBSIDIAN_TOOL_NAMES
TOOL_SCHEMAS.extend(OBSIDIAN_TOOL_SCHEMAS)
ALL_TOOL_NAMES.extend(OBSIDIAN_TOOL_NAMES)
```

## Step 4: Activate executor in tools/registry.py

In `ToolRegistry.__init__()`, add:
```python
self._obsidian = None  # lazy-initialized on first use
```

In `ToolRegistry.dispatch()` (wherever tool routing happens), add:
```python
if tool_name.startswith("obsidian_"):
    from tools.obsidian_mcp.client import ObsidianClient
    if self._obsidian is None:
        self._obsidian = ObsidianClient(
            base_url=self.secrets.get("obsidian_base_url", "http://127.0.0.1:27123"),
            api_key=self.secrets.get("obsidian_api_key", ""),
        )
    return await self._obsidian.dispatch(tool_name, args)
```

## Step 5: Enable tools per agent in agents.json

Add the desired tools to the agent's `tools` list:

```json
{
  "name": "ying",
  "tools": [
    "file_read", "file_write", "bash", "web_search",
    "obsidian_read_note",
    "obsidian_write_note",
    "obsidian_append_note",
    "obsidian_list_folder",
    "obsidian_search",
    "obsidian_get_active",
    "obsidian_open_note"
  ]
}
```

## Step 6: Test the connection

```bash
# From WSL, verify Obsidian REST API is reachable:
curl -s -H "Authorization: Bearer YOUR_KEY" http://127.0.0.1:27123/vault/ | head -20
```

If you see a JSON list of files, the connection works. ✅

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `Connection refused` | Obsidian is not open, or plugin not enabled |
| `401 Unauthorized` | Wrong API key in secrets.json |
| `404` on a path | Note path is wrong — use vault-relative paths |
| SSL errors | `obsidian_base_url` should use `http://` not `https://` for local |
