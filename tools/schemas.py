"""
Tool JSON Schema definitions in OpenAI function-calling format.
These are injected into the OpenRouter API payload when tool use is enabled.
"""

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command on the local machine and return stdout/stderr. "
                "Use for file operations, running scripts, checking system state, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to run.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30, max 120).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": (
                "Read a file from disk and return its contents. "
                "Supports optional offset (start line) and limit (max lines)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or workspace-relative file path.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": "Line number to start from, 1-based (default 1).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum lines to return (default 500).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": (
                "Write content to a file, creating it or overwriting if it exists. "
                "Parent directories are created automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute or workspace-relative file path.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write to the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using Brave Search API. "
                "Returns titles, URLs, and snippets for top results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a URL and return its content converted to Markdown. "
                "Useful for reading documentation, articles, or web pages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch.",
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 10000).",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_list",
            "description": (
                "List files and directories at a given path. "
                "Returns names, types (file/dir), and sizes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path to list (absolute or workspace-relative).",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional glob pattern to filter results, e.g. '*.py'.",
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "If true, list recursively (default false).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Apply a unified diff patch to a file. "
                "The patch must be in standard unified diff format (--- / +++ / @@ headers). "
                "Safer than file_write for incremental code edits."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File to patch (absolute or workspace-relative).",
                    },
                    "patch": {
                        "type": "string",
                        "description": "Unified diff string. Must include @@ hunk headers.",
                    },
                },
                "required": ["path", "patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_list",
            "description": (
                "List running processes. Returns PID, name, CPU%, MEM%, and command line. "
                "Optionally filter by name substring."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional substring to filter process names.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of processes to return (default 30).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_kill",
            "description": (
                "Send a signal to a process by PID. "
                "Default signal is SIGTERM (graceful). Use signal=9 for SIGKILL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pid": {
                        "type": "integer",
                        "description": "Process ID to signal.",
                    },
                    "signal": {
                        "type": "integer",
                        "description": "Signal number (default 15 = SIGTERM, 9 = SIGKILL).",
                    },
                },
                "required": ["pid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "telegram_send",
            "description": (
                "Send a Telegram message to a chat ID or to another HASHI agent by agent_id. "
                "Use this for agent-to-agent communication or notifications."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Message text to send (Markdown supported).",
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Telegram chat ID to send to.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "HASHI agent ID to send to (alternative to chat_id).",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Make an HTTP request (GET, POST, PUT, DELETE, PATCH) to any URL. "
                "Returns status code, headers summary, and response body. "
                "Use for calling external APIs or posting data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to request.",
                    },
                    "method": {
                        "type": "string",
                        "description": "HTTP method: GET, POST, PUT, DELETE, PATCH (default GET).",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Optional HTTP headers as key-value pairs.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Request body (for POST/PUT/PATCH). Send JSON as a string.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30).",
                    },
                },
                "required": ["url"],
            },
        },
    },
]

# Map tool name -> schema for quick lookup
TOOL_SCHEMA_MAP = {s["function"]["name"]: s for s in TOOL_SCHEMAS}

ALL_TOOL_NAMES = list(TOOL_SCHEMA_MAP.keys())
