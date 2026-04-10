"""
Obsidian Tool Pack — JSON Schema definitions (OpenAI function-calling format).

These schemas are backend-agnostic and work with any LLM that supports
function/tool calling. Extend HASHI's global TOOL_SCHEMAS with these.

Usage in tools/schemas.py:
    from tools.obsidian_mcp.schemas import OBSIDIAN_TOOL_SCHEMAS
    TOOL_SCHEMAS.extend(OBSIDIAN_TOOL_SCHEMAS)
"""

OBSIDIAN_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "obsidian_read_note",
            "description": (
                "Read the full content of a note in the Obsidian vault. "
                "Returns the raw Markdown text including frontmatter. "
                "Use vault-relative paths, e.g. 'Papers/Smith_2023_carbon_emission.md'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path to the note, e.g. 'Papers/Smith_2023_carbon_emission.md'.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_write_note",
            "description": (
                "Create a new note or overwrite an existing note in the Obsidian vault. "
                "Provide the full Markdown content including frontmatter. "
                "Use this to create paper notes following the Paper Naming Protocol."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path for the note, e.g. 'Papers/Smith_2023_carbon_emission.md'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full Markdown content to write, including YAML frontmatter.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_append_note",
            "description": (
                "Append content to the end of an existing note in the Obsidian vault. "
                "Useful for adding new annotations, findings, or references to a paper note."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path to the note.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Markdown content to append.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_list_folder",
            "description": (
                "List all notes and subfolders within a given folder in the Obsidian vault. "
                "Returns a list of file/folder names. Use to check for filename collisions "
                "before creating a new paper note."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Vault-relative folder path, e.g. 'Papers/' or '' for root.",
                    }
                },
                "required": ["folder"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_search",
            "description": (
                "Full-text search across all notes in the Obsidian vault. "
                "Returns a list of matching notes with excerpts. "
                "Use to find papers by keyword, author, theme, or any content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_get_active",
            "description": (
                "Get the content of the note currently open/active in Obsidian. "
                "Useful for context-aware assistance — knowing what the user is currently reading."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "obsidian_open_note",
            "description": (
                "Tell Obsidian to open and display a specific note in the UI. "
                "Use after creating a new note to bring it into view for the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Vault-relative path to the note to open.",
                    }
                },
                "required": ["path"],
            },
        },
    },
]

# Convenience: flat list of all tool names from this pack
OBSIDIAN_TOOL_NAMES = [s["function"]["name"] for s in OBSIDIAN_TOOL_SCHEMAS]
