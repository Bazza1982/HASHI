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
            "name": "telegram_send_file",
            "description": (
                "Send a file (image, document, video, or audio) to the user's Telegram chat. "
                "Use this to share generated charts, screenshots, reports, or any local file. "
                "Defaults to sending to the authorized user. Supports photo/document/video/audio types."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to the local file to send.",
                    },
                    "caption": {
                        "type": "string",
                        "description": "Optional caption text for the file.",
                    },
                    "file_type": {
                        "type": "string",
                        "enum": ["auto", "photo", "document", "video", "audio"],
                        "description": (
                            "File type hint. 'auto' (default) detects from extension: "
                            ".jpg/.png/.webp → photo, .mp4/.mov → video, .mp3/.ogg → audio, "
                            "everything else → document."
                        ),
                    },
                    "chat_id": {
                        "type": "string",
                        "description": "Telegram chat ID to send to. Omit to send to the authorized user.",
                    },
                },
                "required": ["path"],
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
    # ------------------------------------------------------------------ browser
    {
        "type": "function",
        "function": {
            "name": "browser_session",
            "description": (
                "Execute a multi-step browser workflow on a single page without reloading between steps. "
                "This is the most powerful browser tool — use it for complex tasks like: login flows, "
                "form wizards, navigating SPAs, scraping paginated content, or any sequence of "
                "interactions. Supports: goto, click, fill, submit, key, scroll, scroll_to, hover, "
                "select, wait_for, screenshot, get_text, evaluate, wait, back, forward, reload. "
                "Set cdp_url to reuse the user's logged-in browser."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Initial URL to navigate to before steps."},
                    "steps": {
                        "type": "array",
                        "description": (
                            "List of action steps. Each step is an object with 'action' key. Examples:\n"
                            '{"action":"click","selector":"#btn"}\n'
                            '{"action":"fill","selector":"#q","text":"hello"}\n'
                            '{"action":"scroll","y":500}\n'
                            '{"action":"wait_for","selector":".result","timeout_ms":5000}\n'
                            '{"action":"screenshot"}\n'
                            '{"action":"get_text"}\n'
                            '{"action":"wait","ms":1000}'
                        ),
                        "items": {"type": "object"},
                    },
                    "cdp_url": {"type": "string", "description": "CDP endpoint to reuse existing browser."},
                    "headed": {"type": "boolean", "description": "Visible browser (standalone only). Default false."},
                },
                "required": ["steps"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "Scroll a page by pixel offset or scroll a specific element into view.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "x": {"type": "integer", "description": "Horizontal scroll pixels. Default 0."},
                    "y": {"type": "integer", "description": "Vertical scroll pixels. Default 500."},
                    "selector": {"type": "string", "description": "If set, scroll this element into view instead of using x/y."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint."},
                    "headed": {"type": "boolean", "description": "Visible browser. Default false."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_hover",
            "description": "Hover the mouse over a CSS-selected element (reveals tooltips, dropdowns, etc).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "selector": {"type": "string", "description": "CSS selector of element to hover."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint."},
                    "headed": {"type": "boolean", "description": "Visible browser. Default false."},
                },
                "required": ["url", "selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_key",
            "description": (
                "Press keyboard key(s) on a page. Examples: 'Enter', 'Tab', 'Escape', "
                "'ArrowDown', 'Control+a', 'Control+c', 'Shift+Tab'. "
                "Optionally focus a selector first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "key": {"type": "string", "description": "Key or key combination to press."},
                    "selector": {"type": "string", "description": "Optional CSS selector to focus before pressing key."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint."},
                    "headed": {"type": "boolean", "description": "Visible browser. Default false."},
                },
                "required": ["url", "key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_select",
            "description": "Select an option from a <select> dropdown by value, visible label, or index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "selector": {"type": "string", "description": "CSS selector of the <select> element."},
                    "value": {"type": "string", "description": "Option value attribute to select."},
                    "label": {"type": "string", "description": "Visible option text to select."},
                    "index": {"type": "integer", "description": "Zero-based option index to select."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint."},
                    "headed": {"type": "boolean", "description": "Visible browser. Default false."},
                },
                "required": ["url", "selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait_for",
            "description": "Navigate to a URL and wait until a CSS selector appears in the DOM. Returns the element's text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "selector": {"type": "string", "description": "CSS selector to wait for."},
                    "timeout_ms": {"type": "integer", "description": "Max wait in milliseconds. Default 10000."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint."},
                    "headed": {"type": "boolean", "description": "Visible browser. Default false."},
                },
                "required": ["url", "selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_attribute",
            "description": "Get the value of an HTML attribute (e.g. href, src, value, data-*) from a CSS-selected element.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "selector": {"type": "string", "description": "CSS selector of the element."},
                    "attribute": {"type": "string", "description": "Attribute name to retrieve, e.g. 'href', 'src', 'value'."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint."},
                    "headed": {"type": "boolean", "description": "Visible browser. Default false."},
                },
                "required": ["url", "selector", "attribute"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_drag",
            "description": "Drag and drop an element from a source CSS selector to a target CSS selector.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "source": {"type": "string", "description": "CSS selector of the element to drag."},
                    "target": {"type": "string", "description": "CSS selector of the drop target."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint."},
                    "headed": {"type": "boolean", "description": "Visible browser. Default false."},
                },
                "required": ["url", "source", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_upload",
            "description": "Upload a local file to a file input element on a page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "selector": {"type": "string", "description": "CSS selector of the file input element."},
                    "file_path": {"type": "string", "description": "Absolute path to the local file to upload."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint."},
                    "headed": {"type": "boolean", "description": "Visible browser. Default false."},
                },
                "required": ["url", "selector", "file_path"],
            },
        },
    },
    # ------------------------------------------------------------------ browser (original 6)
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": (
                "Launch a browser (or attach to the user's running Chrome via CDP), "
                "navigate to a URL, and return a base64-encoded PNG screenshot. "
                "Works with local pages (localhost) and any public URL. "
                "Set cdp_url='http://localhost:9222' to reuse the user's existing "
                "logged-in browser session with all cookies intact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "cdp_url": {
                        "type": "string",
                        "description": "Chrome DevTools Protocol endpoint, e.g. 'http://localhost:9222'. "
                                       "Omit to launch a standalone headless browser.",
                    },
                    "headed": {
                        "type": "boolean",
                        "description": "Launch a visible browser window (standalone mode only). Default false.",
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture full scrollable page. Default false.",
                    },
                    "wait_ms": {
                        "type": "integer",
                        "description": "Extra wait in milliseconds after page load. Default 1500.",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_text",
            "description": (
                "Navigate to a URL, execute all JavaScript, and return the visible "
                "text content of the page. More powerful than web_fetch for JS-heavy "
                "apps (SPAs, dashboards, login-gated pages). "
                "Set cdp_url to reuse the user's logged-in browser."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint for attaching to existing browser."},
                    "headed": {"type": "boolean", "description": "Visible browser (standalone only). Default false."},
                    "wait_ms": {"type": "integer", "description": "Extra wait in ms after load. Default 1500."},
                    "max_length": {"type": "integer", "description": "Max characters to return. Default 15000."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_html",
            "description": (
                "Navigate to a URL and return the fully-rendered HTML (post JS execution). "
                "Useful for inspecting DOM structure of dynamic pages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint for attaching to existing browser."},
                    "headed": {"type": "boolean", "description": "Visible browser (standalone only). Default false."},
                    "wait_ms": {"type": "integer", "description": "Extra wait in ms after load. Default 1500."},
                    "max_length": {"type": "integer", "description": "Max characters to return. Default 20000."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Navigate to a URL and click an element identified by a CSS selector. "
                "Useful for button clicks, navigation, toggling UI elements."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to first."},
                    "selector": {"type": "string", "description": "CSS selector of the element to click."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint to reuse existing browser."},
                    "headed": {"type": "boolean", "description": "Visible browser (standalone only). Default false."},
                },
                "required": ["url", "selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": (
                "Navigate to a URL, fill a form field (CSS selector) with text, "
                "and optionally press Enter to submit. Useful for search boxes, login forms, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "selector": {"type": "string", "description": "CSS selector of the input field."},
                    "text": {"type": "string", "description": "Text to type into the field."},
                    "submit": {"type": "boolean", "description": "Press Enter after filling. Default false."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint to reuse existing browser."},
                    "headed": {"type": "boolean", "description": "Visible browser (standalone only). Default false."},
                },
                "required": ["url", "selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_evaluate",
            "description": (
                "Navigate to a URL and execute custom JavaScript, returning the result. "
                "Use for extracting specific data, checking state, or interacting with the page programmatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to."},
                    "script": {
                        "type": "string",
                        "description": "JS expression/function to evaluate. E.g. '() => document.title'",
                    },
                    "cdp_url": {"type": "string", "description": "CDP endpoint to reuse existing browser."},
                    "headed": {"type": "boolean", "description": "Visible browser (standalone only). Default false."},
                    "wait_ms": {"type": "integer", "description": "Extra wait in ms after load. Default 1000."},
                },
                "required": ["url", "script"],
            },
        },
    },
]

# ------------------------------------------------------------------ obsidian
from tools.obsidian_mcp.schemas import OBSIDIAN_TOOL_SCHEMAS
TOOL_SCHEMAS.extend(OBSIDIAN_TOOL_SCHEMAS)

_BROWSER_EXTRA_FIELDS = {
    "session_id": {
        "type": "string",
        "description": "Optional browser session identifier. Omit to use the agent's default session.",
    },
    "safety_mode": {
        "type": "string",
        "description": "Optional safety mode, e.g. 'read_write' or 'read_only'.",
    },
    "agent_name": {
        "type": "string",
        "description": "Optional agent identity for browser audit logs.",
    },
}

for _tool_name in [
    "browser_session",
    "browser_scroll",
    "browser_hover",
    "browser_key",
    "browser_select",
    "browser_wait_for",
    "browser_get_attribute",
    "browser_drag",
    "browser_upload",
    "browser_screenshot",
    "browser_get_text",
    "browser_get_html",
    "browser_click",
    "browser_fill",
    "browser_evaluate",
]:
    if _tool_name in {s["function"]["name"] for s in TOOL_SCHEMAS}:
        fn = next(s["function"] for s in TOOL_SCHEMAS if s["function"]["name"] == _tool_name)
        fn["parameters"]["properties"].update(_BROWSER_EXTRA_FIELDS)

# Map tool name -> schema for quick lookup
TOOL_SCHEMA_MAP = {s["function"]["name"]: s for s in TOOL_SCHEMAS}

ALL_TOOL_NAMES = list(TOOL_SCHEMA_MAP.keys())
