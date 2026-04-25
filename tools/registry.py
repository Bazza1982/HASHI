"""
ToolRegistry — permission-checked tool dispatcher for HASHI V2.2.

Loaded by FlexibleBackendManager and injected into OpenRouterAdapter
when the backend config contains a `tools` key.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.schemas import TOOL_SCHEMA_MAP, ALL_TOOL_NAMES

# Tool tiers — send only what's needed per turn to save context window.
# Models can still *call* any allowed tool; tiers only control which
# schemas are included in the API payload.
TOOL_TIERS: dict[str, list[str]] = {
    "core": ["bash", "file_read", "file_write", "file_list"],
    "system": ["process_list", "process_kill", "apply_patch"],
    "web": ["web_search", "web_fetch", "http_request"],
    "communication": ["telegram_send"],
    "browser": [
        "browser_session", "browser_screenshot", "browser_get_text",
        "browser_get_html", "browser_click", "browser_fill",
        "browser_evaluate", "browser_scroll", "browser_hover",
        "browser_key", "browser_select", "browser_wait_for",
        "browser_get_attribute", "browser_drag", "browser_upload",
    ],
    "desktop": [
        "desktop_screenshot", "desktop_mouse_move", "desktop_click",
        "desktop_type", "desktop_key", "desktop_scroll", "desktop_info",
    ],
    "windows_use": [
        "windows_screenshot", "windows_mouse_move", "windows_click",
        "windows_type", "windows_key", "windows_scroll", "windows_info",
        "windows_helper_warmup",
        "windows_window_list", "windows_window_focus", "windows_window_close",
    ],
}

def resolve_tiers(tier_names: list[str]) -> list[str]:
    """Expand tier names into a flat list of tool names."""
    tools = []
    for t in tier_names:
        if t in TOOL_TIERS:
            tools.extend(TOOL_TIERS[t])
        elif t in ALL_TOOL_NAMES:
            tools.append(t)  # allow individual tool names too
    return tools


@dataclass
class ToolResult:
    tool_call_id: str
    output: str
    is_error: bool = False


class ToolRegistry:
    """
    Manages tool permissions and dispatches execution to builtins.

    Parameters
    ----------
    allowed_tools : list[str]
        Tool names the model is permitted to call.
        Pass ["*"] to allow all tools.
    access_root : Path
        Filesystem sandbox root for file_read / file_write.
    workspace_dir : Path
        CWD for bash commands; base for relative paths.
    secrets : dict
        Bridge secrets dict, used to look up brave_api_key etc.
    tool_options : dict
        Per-tool config dict from agents.json (e.g. bash.timeout_max).
    max_loops : int
        Maximum tool-call iterations per generate_response call.
    """

    def __init__(
        self,
        allowed_tools: list[str],
        access_root: Path,
        workspace_dir: Path,
        secrets: dict,
        tool_options: Optional[dict] = None,
        max_loops: int = 25,
        agents_config: Optional[list] = None,
        audit_context: Optional[dict] = None,
    ):
        self.logger = logging.getLogger("Tools.Registry")
        self.access_root = Path(access_root)
        self.workspace_dir = Path(workspace_dir)
        self.secrets = secrets or {}
        self.tool_options = tool_options or {}
        self.max_loops = max_loops
        self.agents_config = agents_config or []
        self.audit_context = audit_context or {}

        if allowed_tools == ["*"]:
            self._allowed = set(ALL_TOOL_NAMES)
        else:
            self._allowed = set(allowed_tools) & set(ALL_TOOL_NAMES)
            unknown = set(allowed_tools) - set(ALL_TOOL_NAMES) - {"*"}
            if unknown:
                self.logger.warning(f"Unknown tool names in config (ignored): {unknown}")

        self.logger.info(f"ToolRegistry initialized. Allowed: {sorted(self._allowed)}")
        self._obsidian = None  # lazy-initialized on first obsidian_* tool call

    def is_allowed(self, tool_name: str) -> bool:
        return tool_name in self._allowed

    def get_tool_definitions(self, tiers: list[str] | None = None) -> list[dict]:
        """Return OpenAI-format tool definitions filtered to allowed tools.

        If *tiers* is given, only include tools belonging to those tiers
        (intersected with allowed). Pass None to include all allowed tools
        (backwards-compatible default).
        """
        if tiers is not None:
            tier_tools = set(resolve_tiers(tiers))
            subset = self._allowed & tier_tools
            return [TOOL_SCHEMA_MAP[name] for name in ALL_TOOL_NAMES
                    if name in subset]
        return [TOOL_SCHEMA_MAP[name] for name in ALL_TOOL_NAMES if name in self._allowed]

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str = "") -> ToolResult:
        """
        Execute a tool call with permission checking.
        Always returns a ToolResult — never raises.
        """
        if not self.is_allowed(tool_name):
            return ToolResult(
                tool_call_id=tool_call_id,
                output=f"Error: tool '{tool_name}' is not in your allowed tools list",
                is_error=True,
            )

        try:
            output = await self._dispatch(tool_name, arguments)
        except Exception as e:
            self.logger.error(f"Tool '{tool_name}' raised unexpected error: {e}", exc_info=True)
            output = f"Error: unexpected failure in '{tool_name}': {e}"
            return ToolResult(tool_call_id=tool_call_id, output=output, is_error=True)

        is_error = output.startswith("Error:")
        return ToolResult(tool_call_id=tool_call_id, output=output, is_error=is_error)

    async def _dispatch(self, tool_name: str, arguments: dict) -> str:
        from tools.builtins import (
            execute_bash,
            execute_file_read,
            execute_file_write,
            execute_file_list,
            execute_apply_patch,
            execute_process_list,
            execute_process_kill,
            execute_telegram_send,
            execute_telegram_send_file,
            execute_http_request,
            execute_web_search,
            execute_web_fetch,
        )

        opts = self.tool_options

        if tool_name == "bash":
            bash_opts = opts.get("bash", {})
            return await execute_bash(
                arguments,
                workspace_dir=self.workspace_dir,
                timeout_max=int(bash_opts.get("timeout_max", 120)),
                blocked_patterns=bash_opts.get("blocked_patterns"),
            )

        if tool_name == "file_read":
            return await execute_file_read(
                arguments,
                access_root=self.access_root,
                workspace_dir=self.workspace_dir,
            )

        if tool_name == "file_write":
            fw_opts = opts.get("file_write", {})
            return await execute_file_write(
                arguments,
                access_root=self.access_root,
                workspace_dir=self.workspace_dir,
                max_file_size_kb=int(fw_opts.get("max_file_size_kb", 1024)),
            )

        if tool_name == "file_list":
            return await execute_file_list(
                arguments,
                access_root=self.access_root,
                workspace_dir=self.workspace_dir,
            )

        if tool_name == "apply_patch":
            return await execute_apply_patch(
                arguments,
                access_root=self.access_root,
                workspace_dir=self.workspace_dir,
            )

        if tool_name == "process_list":
            return await execute_process_list(arguments)

        if tool_name == "process_kill":
            return await execute_process_kill(arguments)

        if tool_name == "telegram_send":
            return await execute_telegram_send(
                arguments,
                secrets=self.secrets,
                agents_config=self.agents_config,
            )

        if tool_name == "telegram_send_file":
            return await execute_telegram_send_file(
                arguments,
                secrets=self.secrets,
            )

        if tool_name == "http_request":
            return await execute_http_request(arguments)

        if tool_name == "web_search":
            brave_key = self.secrets.get("brave_api_key")
            return await execute_web_search(arguments, brave_api_key=brave_key)

        if tool_name == "web_fetch":
            return await execute_web_fetch(arguments)

        if tool_name.startswith("browser_"):
            from tools.browser import (
                execute_browser_screenshot,
                execute_browser_get_text,
                execute_browser_get_html,
                execute_browser_click,
                execute_browser_fill,
                execute_browser_evaluate,
                execute_browser_scroll,
                execute_browser_hover,
                execute_browser_key,
                execute_browser_select,
                execute_browser_wait_for,
                execute_browser_get_attribute,
                execute_browser_drag,
                execute_browser_upload,
                execute_browser_session,
            )
            _browser_dispatch = {
                "browser_screenshot":    execute_browser_screenshot,
                "browser_get_text":      execute_browser_get_text,
                "browser_get_html":      execute_browser_get_html,
                "browser_click":         execute_browser_click,
                "browser_fill":          execute_browser_fill,
                "browser_evaluate":      execute_browser_evaluate,
                "browser_scroll":        execute_browser_scroll,
                "browser_hover":         execute_browser_hover,
                "browser_key":           execute_browser_key,
                "browser_select":        execute_browser_select,
                "browser_wait_for":      execute_browser_wait_for,
                "browser_get_attribute": execute_browser_get_attribute,
                "browser_drag":          execute_browser_drag,
                "browser_upload":        execute_browser_upload,
                "browser_session":       execute_browser_session,
            }
            if tool_name in _browser_dispatch:
                browser_args = dict(arguments)
                browser_args.setdefault("_audit", self.audit_context)
                return await _browser_dispatch[tool_name](browser_args)
            return f"Error: unknown browser tool '{tool_name}'"

        if tool_name.startswith("desktop_"):
            from tools.desktop import (
                execute_desktop_screenshot,
                execute_desktop_mouse_move,
                execute_desktop_click,
                execute_desktop_type,
                execute_desktop_key,
                execute_desktop_scroll,
                execute_desktop_info,
                execute_desktop_window_list,
                execute_desktop_window_focus,
            )
            _desktop_dispatch = {
                "desktop_screenshot": execute_desktop_screenshot,
                "desktop_mouse_move": execute_desktop_mouse_move,
                "desktop_click": execute_desktop_click,
                "desktop_type": execute_desktop_type,
                "desktop_key": execute_desktop_key,
                "desktop_scroll": execute_desktop_scroll,
                "desktop_info": execute_desktop_info,
                "desktop_window_list": execute_desktop_window_list,
                "desktop_window_focus": execute_desktop_window_focus,
            }
            if tool_name in _desktop_dispatch:
                return await _desktop_dispatch[tool_name](arguments)
            return f"Error: unknown desktop tool '{tool_name}'"

        if tool_name.startswith("windows_"):
            from tools.windows_use import (
                execute_windows_screenshot,
                execute_windows_mouse_move,
                execute_windows_click,
                execute_windows_type,
                execute_windows_key,
                execute_windows_scroll,
                execute_windows_helper_warmup,
                execute_windows_reset_input_state,
                execute_windows_info,
                execute_windows_window_list,
                execute_windows_window_focus,
                execute_windows_window_close,
            )
            _windows_dispatch = {
                "windows_screenshot": execute_windows_screenshot,
                "windows_mouse_move": execute_windows_mouse_move,
                "windows_click": execute_windows_click,
                "windows_type": execute_windows_type,
                "windows_key": execute_windows_key,
                "windows_scroll": execute_windows_scroll,
                "windows_helper_warmup": execute_windows_helper_warmup,
                "windows_reset_input_state": execute_windows_reset_input_state,
                "windows_info": execute_windows_info,
                "windows_window_list": execute_windows_window_list,
                "windows_window_focus": execute_windows_window_focus,
                "windows_window_close": execute_windows_window_close,
            }
            if tool_name in _windows_dispatch:
                return await _windows_dispatch[tool_name](arguments)
            return f"Error: unknown windows tool '{tool_name}'"

        if tool_name.startswith("obsidian_"):
            from tools.obsidian_mcp.client import ObsidianClient
            if self._obsidian is None:
                self._obsidian = ObsidianClient(
                    base_url=self.secrets.get("obsidian_base_url", "http://127.0.0.1:27123"),
                    api_key=self.secrets.get("obsidian_api_key", ""),
                )
            return await self._obsidian.dispatch(tool_name, arguments)

        return f"Error: no executor for tool '{tool_name}'"
