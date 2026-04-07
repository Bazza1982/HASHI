"""
HASHI Obsidian Tool Pack

Provides backend-agnostic tools for any HASHI agent to read, write,
search, and manage notes in an Obsidian vault via the Local REST API plugin.

Quick start:
    from tools.obsidian_mcp.schemas import OBSIDIAN_TOOL_SCHEMAS, OBSIDIAN_TOOL_NAMES
    from tools.obsidian_mcp.client import ObsidianClient
"""
from tools.obsidian_mcp.client import ObsidianClient
from tools.obsidian_mcp.schemas import OBSIDIAN_TOOL_SCHEMAS, OBSIDIAN_TOOL_NAMES

__all__ = ["ObsidianClient", "OBSIDIAN_TOOL_SCHEMAS", "OBSIDIAN_TOOL_NAMES"]
