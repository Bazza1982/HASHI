"""Compact high-level tools for HERV3 (also usable by larger models).

Schemas are intentionally short and example-first. Existing Tool Registry remains
the only permission owner; this file declares no independent authority.
"""

from __future__ import annotations


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _s(description: str) -> dict:
    return {"type": "string", "description": description}


TOOLKIT_SCHEMAS = [
    _tool(
        "tool_find",
        "Find and reveal existing HASHI tools for a task. Example: "
        '{"query":"read Excel"} or {"pack":"office_basic"}. '
        "This only changes which permitted tool schemas are shown; it never grants permission.",
        {
            "query": _s("What you want to do; Chinese or English."),
            "pack": _s("Optional pack: office_basic, development, daily, web, browser, system, etc."),
            "names": {"type": "array", "items": {"type": "string"}, "description": "Optional exact tool names."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
        [],
    ),
    _tool(
        "project_locate",
        "Find a project by folder name or Git remote within an accessible root; "
        'example {"name":"SOK","root":"D:/Projects"}. Returns ranked paths, not an invented guess.',
        {
            "name": _s("Project name, folder fragment, or Git repository name."),
            "root": _s("Optional known search root; defaults to current working directory."),
            "max_depth": {"type": "integer", "minimum": 0, "maximum": 4, "default": 2},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
        },
        ["name"],
    ),
    _tool(
        "code_search",
        "Quick bounded source search, backed by ripgrep when available. "
        'Example {"query":"def execute","path":"src","mode":"content"}. '
        "A tool timeout returns a partial result, never ends the Agent Turn.",
        {
            "query": _s("Literal text or regex pattern; for filenames, name fragment."),
            "path": _s("Root to search; defaults to current working directory."),
            "mode": {"type": "string", "enum": ["content", "files"], "default": "content"},
            "regex": {"type": "boolean", "default": False},
            "glob": _s("Optional file glob, e.g. *.py."),
            "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            "timeout_s": {"type": "number", "minimum": 1, "maximum": 60, "default": 15},
        },
        ["query"],
    ),
    _tool(
        "git_inspect",
        "Read Git status, branches, log, diff or remotes without changing the repository. "
        'Example {"operation":"status","path":"D:/Projects/content_generation"}.',
        {
            "operation": {"type": "string", "enum": ["status", "branches", "log", "diff", "remote"]},
            "path": _s("Repository path; defaults to current working directory."),
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            "cached": {"type": "boolean", "default": False, "description": "Use staged Git diff."},
        },
        ["operation"],
    ),
    _tool(
        "file_manage",
        "Create a folder, copy or move a file/folder within existing writable Workzones. "
        'Example {"operation":"copy","source":"a.txt","destination":"backup/a.txt"}. '
        "No deletion; set overwrite=true only when replacement is intended.",
        {
            "operation": {"type": "string", "enum": ["mkdir", "copy", "move"]},
            "source": _s("Existing file or folder for copy/move."),
            "destination": _s("New file/folder path, or folder to create."),
            "overwrite": {"type": "boolean", "default": False},
            "recursive": {"type": "boolean", "default": False, "description": "Required for directory copy/move."},
        },
        ["operation", "destination"],
    ),
    _tool(
        "text_replace",
        "Replace exact text in an existing UTF-8 file. Checks occurrence count before writing. "
        'Example {"path":"app.py","find":"old_name","replace":"new_name","expected_count":1}.',
        {
            "path": _s("Exact file path within a writable Workzone."),
            "find": _s("Exact literal text to find."),
            "replace": _s("Replacement text."),
            "expected_count": {"type": "integer", "minimum": 1, "default": 1},
            "preview_only": {"type": "boolean", "default": False},
        },
        ["path", "find", "replace"],
    ),
    _tool(
        "data_inspect",
        "Preview JSON, JSONL, CSV or TSV with field names and sample records. "
        'Example {"path":"data/results.csv","limit":5}. Does not modify data.',
        {
            "path": _s("Exact data file path within an accessible Workzone."),
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 5},
            "format": {"type": "string", "enum": ["auto", "json", "jsonl", "csv", "tsv"], "default": "auto"},
        },
        ["path"],
    ),
]

TOOLKIT_TOOL_NAMES = tuple(item["function"]["name"] for item in TOOLKIT_SCHEMAS)
TOOLKIT_READ_ONLY_TOOLS = frozenset(
    {"tool_find", "project_locate", "code_search", "git_inspect", "data_inspect"}
)
