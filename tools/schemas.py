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
            "name": "browser_type_text",
            "description": (
                "Type text into a contenteditable element on a page using CDP Input.insertText. "
                "Unlike browser_fill, this triggers real browser input events (including beforeinput), "
                "making it compatible with React-controlled editors such as LinkedIn's post composer. "
                "Requires the HASHI browser bridge extension."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL the tab is already on (used for routing)."},
                    "selector": {"type": "string", "description": "CSS selector of the contenteditable element."},
                    "text": {"type": "string", "description": "Text to insert (supports Unicode/emoji)."},
                    "timeout_ms": {"type": "integer", "description": "Max wait for element to appear. Default 10000."},
                    "cdp_url": {"type": "string", "description": "CDP endpoint to reuse existing browser."},
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

DESKTOP_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": (
                "Take a screenshot of the Linux virtual desktop (Xvfb / XRDP session). "
                "Returns a base64-encoded PNG plus display metadata. "
                "Works even when the Windows host screen is locked. "
                "Optionally annotate with grid overlay or save to a file path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "display": {
                        "type": "string",
                        "description": "X11 display to capture, e.g. ':10' or ':0'. Auto-detected if omitted.",
                    },
                    "annotate": {
                        "type": "boolean",
                        "description": "Overlay a grid to help identify coordinates. Default false.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Optional absolute path to save the PNG file.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_mouse_move",
            "description": "Move the mouse cursor to absolute (x, y) on the Linux virtual desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate in desktop pixels."},
                    "y": {"type": "integer", "description": "Y coordinate in desktop pixels."},
                    "display": {"type": "string", "description": "X11 display. Auto-detected if omitted."},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": (
                "Click the mouse at (x, y) on the Linux virtual desktop. "
                "Supports left/right/middle buttons and double-click."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate."},
                    "y": {"type": "integer", "description": "Y coordinate."},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button. Default 'left'.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Click count. Use 2 for double-click. Default 1.",
                    },
                    "display": {"type": "string", "description": "X11 display. Auto-detected if omitted."},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_type",
            "description": (
                "Type text into the currently focused window on the Linux virtual desktop. "
                "Handles all characters including spaces, dashes, and Unicode via xdotool. "
                "Click the target window first with desktop_click if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type."},
                    "delay_ms": {
                        "type": "integer",
                        "description": "Delay between keystrokes in milliseconds. Default 30.",
                    },
                    "display": {"type": "string", "description": "X11 display. Auto-detected if omitted."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_key",
            "description": (
                "Press a key or key combination on the Linux virtual desktop. "
                "Examples: 'ctrl+s', 'alt+F4', 'Return', 'Escape', 'ctrl+z', 'super'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key or combo, e.g. 'ctrl+s', 'alt+F4', 'Return', 'space'.",
                    },
                    "display": {"type": "string", "description": "X11 display. Auto-detected if omitted."},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_scroll",
            "description": "Scroll the mouse wheel on the Linux virtual desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                        "description": "Scroll direction. Default 'down'.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Number of scroll steps. Default 3.",
                    },
                    "x": {"type": "integer", "description": "Optional X coordinate to scroll at."},
                    "y": {"type": "integer", "description": "Optional Y coordinate to scroll at."},
                    "display": {"type": "string", "description": "X11 display. Auto-detected if omitted."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_info",
            "description": (
                "Get info about the Linux virtual desktop: active DISPLAY, "
                "current mouse position, connected displays, and tool availability."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "display": {"type": "string", "description": "X11 display to query. Auto-detected if omitted."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_window_list",
            "description": (
                "List visible top-level windows on the Linux X11 desktop, including active-window state "
                "and basic geometry when available."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "display": {"type": "string", "description": "X11 display to query. Auto-detected if omitted."},
                    "title_contains": {
                        "type": "string",
                        "description": "Optional case-insensitive title filter.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_window_focus",
            "description": "Focus a visible Linux X11 window by id or title match.",
            "parameters": {
                "type": "object",
                "properties": {
                    "display": {"type": "string", "description": "X11 display to target. Auto-detected if omitted."},
                    "window_id": {"type": "integer", "description": "Exact X11 window id to focus."},
                    "title_contains": {
                        "type": "string",
                        "description": "Fallback case-insensitive title match when window_id is omitted.",
                    },
                },
            },
        },
    },
]

WINDOWS_USE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "windows_screenshot",
            "description": (
                "Take a screenshot of the real Windows desktop via the Windows host. "
                "Returns a base64-encoded PNG plus metadata from the Windows-side executor. "
                "Works when called from Windows or from WSL agents through powershell.exe interop. "
                "The Windows desktop usually needs to be unlocked for reliable results. "
                "On multi-display hosts, call windows_info first, inspect displays, and pass display=N explicitly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' prefers usecomputer for lower-latency screenshots, else falls back to windows-mcp.",
                    },
                    "annotate": {
                        "type": "boolean",
                        "description": "Overlay a grid if supported by the backend. Default false.",
                    },
                    "display": {
                        "type": "integer",
                        "description": "Optional Windows display index for screenshot backends that support it.",
                    },
                    "window": {
                        "type": "integer",
                        "description": "Optional target window id for backends that support window capture.",
                    },
                    "save_path": {
                        "type": "string",
                        "description": "Optional absolute path to save the PNG file. WSL paths are converted automatically.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_mouse_move",
            "description": "Move the mouse cursor to absolute (x, y) on the real Windows desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate in Windows desktop pixels."},
                    "y": {"type": "integer", "description": "Y coordinate in Windows desktop pixels."},
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' prefers usecomputer for lower-latency pointer moves.",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_click",
            "description": (
                "Click the mouse at (x, y) on the real Windows desktop. "
                "Supports left/right/middle buttons and double-click."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "X coordinate."},
                    "y": {"type": "integer", "description": "Y coordinate."},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button. Default 'left'.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Click count. Use 2 for double-click. Default 1.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' prefers usecomputer for lower-latency clicks.",
                    },
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_drag",
            "description": (
                "Drag the mouse on the real Windows desktop from one absolute coordinate to another. "
                "Useful for desktop file drag-and-drop and other pointer-drag gestures."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_x": {"type": "integer", "description": "Drag start X coordinate."},
                    "from_y": {"type": "integer", "description": "Drag start Y coordinate."},
                    "to_x": {"type": "integer", "description": "Drag end X coordinate."},
                    "to_y": {"type": "integer", "description": "Drag end Y coordinate."},
                    "curve_x": {"type": "integer", "description": "Optional Bezier control point X for a curved drag path."},
                    "curve_y": {"type": "integer", "description": "Optional Bezier control point Y for a curved drag path."},
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button to hold during the drag. Default 'left'.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' prefers usecomputer/native helper for drag gestures.",
                    },
                },
                "required": ["from_x", "from_y", "to_x", "to_y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_type",
            "description": (
                "Type text into the currently focused window on the real Windows desktop. "
                "Click the target window first with windows_click if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type."},
                    "x": {"type": "integer", "description": "Optional X coordinate. Required for explicit windows-mcp typing."},
                    "y": {"type": "integer", "description": "Optional Y coordinate. Required for explicit windows-mcp typing."},
                    "focus_first": {
                        "type": "boolean",
                        "description": "If a window selector is provided, focus it before typing. Default true.",
                    },
                    "window_id": {"type": "integer", "description": "Optional target window id to focus before typing."},
                    "pid": {"type": "integer", "description": "Optional target process id to focus before typing."},
                    "title_contains": {"type": "string", "description": "Optional partial window title to focus before typing."},
                    "exact_title": {"type": "string", "description": "Optional exact window title to focus before typing."},
                    "input_method": {
                        "type": "string",
                        "enum": ["auto", "keys", "paste"],
                        "description": "How to enter text. Auto uses clipboard paste only for tabular or multiline text; plain text uses key input.",
                    },
                    "restore_clipboard": {
                        "type": "boolean",
                        "description": "Restore the previous clipboard after paste input. Default false for reliability with Office apps.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' prefers usecomputer for typing because it works without coordinates.",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_key",
            "description": (
                "Press a key or key combination on the real Windows desktop. "
                "Examples: 'ctrl+s', 'alt+f4', 'Return', 'Escape', 'ctrl+z'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key or combo, e.g. 'ctrl+s', 'alt+f4', 'Return', 'space'.",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' prefers usecomputer for keyboard shortcuts.",
                    },
                    "focus_first": {
                        "type": "boolean",
                        "description": "If a window selector is provided, focus it before sending the shortcut. Default true.",
                    },
                    "window_id": {"type": "integer", "description": "Optional target window id to focus before sending the shortcut."},
                    "pid": {"type": "integer", "description": "Optional target process id to focus before sending the shortcut."},
                    "title_contains": {"type": "string", "description": "Optional partial window title to focus before sending the shortcut."},
                    "exact_title": {"type": "string", "description": "Optional exact window title to focus before sending the shortcut."},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_scroll",
            "description": "Scroll the mouse wheel on the real Windows desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "left", "right"],
                        "description": "Scroll direction. Default 'down'.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Number of scroll steps. Default 3.",
                    },
                    "x": {"type": "integer", "description": "Optional X coordinate to scroll at."},
                    "y": {"type": "integer", "description": "Optional Y coordinate to scroll at."},
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' prefers usecomputer for lower-latency scroll.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_helper_warmup",
            "description": (
                "Pre-start and health-check the Windows helper service before doing real desktop actions. "
                "Useful to hide the first-call startup delay from later screenshot or input steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_reset_input_state",
            "description": (
                "Explicitly release common modifier keys and mouse buttons on the real Windows desktop, "
                "then report the current foreground window and keyboard layout. "
                "Use when automation may have left the user's desktop input state feeling wrong."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_info",
            "description": (
                "Get info about the Windows desktop bridge: current mouse position, "
                "connected displays, visible windows, foreground window, keyboard layout, and executor availability. "
                "Useful to confirm WSL-to-Windows interop is working. "
                "Use this first on multi-display systems before taking screenshots or driving input."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' prefers usecomputer for bridge inspection.",
                    },
                    "include_windows": {
                        "type": "boolean",
                        "description": "Include visible window list. Default true.",
                    },
                    "include_displays": {
                        "type": "boolean",
                        "description": "Include connected display metadata. Default true.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_window_list",
            "description": "List visible top-level windows on the real Windows desktop. Useful for selecting a target window before focus, type, or close, especially after choosing the correct display with windows_info.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' uses the built-in PowerShell window enumerator.",
                    },
                    "pid": {"type": "integer", "description": "Optional process id filter."},
                    "title_contains": {"type": "string", "description": "Optional partial title filter."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_window_focus",
            "description": "Bring a Windows window to the foreground by window id, pid, exact title, or partial title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' uses the built-in PowerShell window controller.",
                    },
                    "window_id": {"type": "integer", "description": "Optional target window id."},
                    "pid": {"type": "integer", "description": "Optional target process id."},
                    "title_contains": {"type": "string", "description": "Optional partial title match."},
                    "exact_title": {"type": "string", "description": "Optional exact title match."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "windows_window_close",
            "description": "Close a Windows window by selector. Can optionally dismiss an unsaved prompt with 'n' and force-kill the process if needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": ["auto", "usecomputer", "windows-mcp"],
                        "description": "Desktop executor backend. 'auto' uses the built-in PowerShell window controller.",
                    },
                    "window_id": {"type": "integer", "description": "Optional target window id."},
                    "pid": {"type": "integer", "description": "Optional target process id."},
                    "title_contains": {"type": "string", "description": "Optional partial title match."},
                    "exact_title": {"type": "string", "description": "Optional exact title match."},
                    "dismiss_unsaved": {
                        "type": "boolean",
                        "description": "If the app shows an unsaved prompt, press 'n' after close. Default false.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "If the window is still open after waiting, force-kill its process. Default false.",
                    },
                    "wait_ms": {
                        "type": "integer",
                        "description": "Milliseconds to wait after the initial close request before checking again. Default 1200.",
                    },
                },
            },
        },
    },
]

TOOL_SCHEMAS.extend(WINDOWS_USE_TOOL_SCHEMAS)
TOOL_SCHEMAS.extend(DESKTOP_TOOL_SCHEMAS)

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
    "browser_type_text",
    "browser_evaluate",
]:
    if _tool_name in {s["function"]["name"] for s in TOOL_SCHEMAS}:
        fn = next(s["function"] for s in TOOL_SCHEMAS if s["function"]["name"] == _tool_name)
        fn["parameters"]["properties"].update(_BROWSER_EXTRA_FIELDS)

# Map tool name -> schema for quick lookup
TOOL_SCHEMA_MAP = {s["function"]["name"]: s for s in TOOL_SCHEMAS}

ALL_TOOL_NAMES = list(TOOL_SCHEMA_MAP.keys())
