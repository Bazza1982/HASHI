"""Per-invocation HASHI Tool Gateway configuration for supported Fixed CLIs."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from tools.gateway.context import live_workbench_api_base_url, write_gateway_context
from tools.gateway.mcp_stdio import exposed_tool_name


SERVER_NAME = "hashi_tools"


def prepare_hashi_mcp(adapter: Any, *, backend: str) -> dict[str, Any] | None:
    registry = getattr(adapter, "tool_registry", None)
    if registry is None or not registry.allowed_tool_names():
        adapter._hashi_mcp_enabled = False
        return None
    state_dir = Path(adapter.config.workspace_dir) / "backend_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    context_path = state_dir / f"{backend}-hashi-mcp-context.json"
    workbench_url = live_workbench_api_base_url(registry, adapter.global_config)
    write_gateway_context(
        registry,
        context_path,
        workbench_api_base_url=workbench_url,
        backend=backend,
    )
    project_root = Path(adapter.global_config.project_root).resolve()
    server_script = project_root / "tools" / "gateway" / "mcp_stdio.py"
    if not server_script.is_file():
        raise FileNotFoundError(f"HASHI MCP gateway is missing: {server_script}")
    descriptor = {
        "name": SERVER_NAME,
        "command": sys.executable,
        "args": ["-m", "tools.gateway.mcp_stdio", "--context", str(context_path)],
        "cwd": str(project_root),
        "context_path": str(context_path),
        "exposed_tools": [
            exposed_tool_name(name) for name in registry.allowed_tool_names()
        ],
    }
    adapter._hashi_mcp_descriptor = descriptor
    adapter._hashi_mcp_enabled = True
    return descriptor


def write_claude_mcp_config(adapter: Any, descriptor: dict[str, Any]) -> Path:
    target = Path(adapter.config.workspace_dir) / "backend_state" / "claude-hashi-mcp.json"
    payload = {
        "mcpServers": {
            descriptor["name"]: {
                "command": descriptor["command"],
                "args": descriptor["args"],
                "cwd": descriptor["cwd"],
            }
        }
    }
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return target
