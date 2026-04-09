# HASHI Tool Catalog

Full reference for all tools available to HASHI agents via function calling.
To use a tool, call it directly as a function — do not simulate or describe what you would do.

## Core Tools

### bash
Execute shell commands on the host Windows 11 machine.
| Param | Required | Description |
|-------|----------|-------------|
| `command` | Yes | Shell command to execute |
| `timeout` | No | Timeout in seconds (default 30) |

### file_read
Read a file's contents.
| Param | Required | Description |
|-------|----------|-------------|
| `path` | Yes | File path (absolute or relative to workspace) |
| `offset` | No | Start line number |
| `limit` | No | Max lines to return |

### file_write
Write or create a file.
| Param | Required | Description |
|-------|----------|-------------|
| `path` | Yes | File path |
| `content` | Yes | Content to write |
| `mode` | No | `"write"` (default, overwrite) or `"append"` |

### file_list
List files and directories.
| Param | Required | Description |
|-------|----------|-------------|
| `path` | Yes | Directory path |
| `recursive` | No | Boolean, recurse into subdirectories |
| `pattern` | No | Glob filter (e.g. `"*.py"`) |

## System Tools

### apply_patch
Apply a unified diff patch to a file.
| Param | Required | Description |
|-------|----------|-------------|
| `path` | Yes | Target file path |
| `patch` | Yes | Unified diff content |

### process_list
List running processes with PID, name, CPU%, MEM%.
| Param | Required | Description |
|-------|----------|-------------|
| `filter` | No | Filter by process name substring |
| `limit` | No | Max results (default 30) |

### process_kill
Terminate a process by PID.
| Param | Required | Description |
|-------|----------|-------------|
| `pid` | Yes | Process ID |
| `signal` | No | Signal number (default 15/SIGTERM, use 9 for force kill) |

## Web & Network Tools

### web_search
Search the web via Brave Search API.
| Param | Required | Description |
|-------|----------|-------------|
| `query` | Yes | Search query |
| `count` | No | Max results to return |

### web_fetch
Fetch a URL and return its content.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | URL to fetch |

### http_request
Make an HTTP request with full control.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Request URL |
| `method` | No | GET/POST/PUT/DELETE/PATCH (default GET) |
| `headers` | No | Object of HTTP headers |
| `body` | No | Request body string |
| `timeout` | No | Timeout in seconds |

## Communication Tools

### telegram_send
Send a Telegram message.
| Param | Required | Description |
|-------|----------|-------------|
| `text` | Yes | Message text |
| `chat_id` | No | Target chat ID |
| `agent_id` | No | Target agent name (resolves chat ID automatically) |

### Cross-agent chat (hchat)
Not a function tool — use via bash:
```
python tools/hchat_send.py --to <agent_name> --from <your_name> --text "message"
```

## Browser Tools

All browser tools can attach to the user's Chrome via `cdp_url="http://localhost:9222"` to reuse login sessions, or launch a standalone headless browser.

### browser_session
Execute a multi-step browser workflow on a single page. Most powerful browser tool.
| Param | Required | Description |
|-------|----------|-------------|
| `steps` | Yes | Array of action objects (see actions below) |
| `url` | No | Initial URL to navigate to |
| `cdp_url` | No | Chrome DevTools Protocol URL |
| `headed` | No | Boolean, show browser window |

**Step actions:** `goto`, `click`, `fill`, `submit`, `key`, `scroll`, `scroll_to`, `hover`, `select`, `wait_for`, `screenshot`, `get_text`, `evaluate`, `wait`, `back`, `forward`, `reload`.

Example steps:
```json
[
  {"action": "goto", "url": "https://example.com"},
  {"action": "click", "selector": "#btn"},
  {"action": "fill", "selector": "#q", "text": "hello"},
  {"action": "screenshot"}
]
```

### browser_screenshot
Take a screenshot of a page.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `full_page` | No | Capture full page (bool) |
| `wait_ms` | No | Wait before screenshot |
| `cdp_url` | No | CDP URL |
| `headed` | No | Show browser window |

### browser_get_text
Get visible text from a JS-rendered page.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `max_length` | No | Max characters to return |
| `wait_ms` | No | Wait for rendering |
| `cdp_url` | No | CDP URL |

### browser_get_html
Get fully rendered HTML after JS execution.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `max_length` | No | Max characters to return |
| `wait_ms` | No | Wait for rendering |
| `cdp_url` | No | CDP URL |

### browser_click
Click an element by CSS selector.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `selector` | Yes | CSS selector |
| `cdp_url` | No | CDP URL |

### browser_fill
Fill a form field, optionally submit.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `selector` | Yes | CSS selector for input |
| `text` | Yes | Text to fill |
| `submit` | No | Press Enter after filling (bool) |
| `cdp_url` | No | CDP URL |

### browser_evaluate
Execute JavaScript on a page.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `script` | Yes | JavaScript to execute |
| `wait_ms` | No | Wait before executing |
| `cdp_url` | No | CDP URL |

### browser_scroll
Scroll a page by pixels or to an element.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `x` | No | Horizontal scroll pixels |
| `y` | No | Vertical scroll pixels |
| `selector` | No | Scroll element into view |
| `cdp_url` | No | CDP URL |

### browser_hover
Hover over an element.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `selector` | Yes | CSS selector |
| `cdp_url` | No | CDP URL |

### browser_key
Press keyboard keys.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `key` | Yes | Key to press (e.g. "Enter", "Control+a") |
| `selector` | No | Focus element first |
| `cdp_url` | No | CDP URL |

### browser_select
Select from a dropdown.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `selector` | Yes | CSS selector for select element |
| `value` | No | Option value |
| `label` | No | Option label text |
| `index` | No | Option index |
| `cdp_url` | No | CDP URL |

### browser_wait_for
Wait for a CSS selector to appear.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `selector` | Yes | CSS selector to wait for |
| `timeout_ms` | No | Max wait time in ms |
| `cdp_url` | No | CDP URL |

### browser_get_attribute
Get an HTML attribute value from an element.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `selector` | Yes | CSS selector |
| `attribute` | Yes | Attribute name |
| `cdp_url` | No | CDP URL |

### browser_drag
Drag and drop between elements.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `source` | Yes | Source CSS selector |
| `target` | Yes | Target CSS selector |
| `cdp_url` | No | CDP URL |

### browser_upload
Upload a file via file input.
| Param | Required | Description |
|-------|----------|-------------|
| `url` | Yes | Page URL |
| `selector` | Yes | CSS selector for file input |
| `file_path` | Yes | Local file path to upload |
| `cdp_url` | No | CDP URL |
