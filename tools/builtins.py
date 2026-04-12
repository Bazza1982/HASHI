"""
Built-in tool executor implementations for HASHI V2.2.

Each function is a standalone async executor. They are called by ToolRegistry.
All file operations are sandboxed to access_root.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_path(raw_path: str, access_root: Path, workspace_dir: Path) -> Path:
    """
    Resolve a user-supplied path.
    - Absolute paths are kept as-is but verified against access_root.
    - Relative paths are resolved from workspace_dir.
    Raises ValueError if the resolved path escapes access_root.
    """
    p = Path(raw_path)
    if not p.is_absolute():
        p = (workspace_dir / p).resolve()
    else:
        p = p.resolve()

    access_root_resolved = access_root.resolve()
    try:
        p.relative_to(access_root_resolved)
    except ValueError:
        raise ValueError(
            f"Path '{p}' is outside the allowed access scope '{access_root_resolved}'"
        )
    return p


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------

async def execute_bash(
    args: dict,
    workspace_dir: Path,
    timeout_max: int = 120,
    blocked_patterns: Optional[list[str]] = None,
) -> str:
    command = str(args.get("command", "")).strip()
    if not command:
        return "Error: no command provided"

    timeout = min(int(args.get("timeout", 30)), timeout_max)

    # Check blocked patterns
    if blocked_patterns:
        for pattern in blocked_patterns:
            if re.search(pattern, command):
                return f"Error: command blocked by policy (matched: {pattern!r})"

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace_dir),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return f"Error: command timed out after {timeout}s"

        output_parts = []
        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))
        if stderr:
            output_parts.append(f"[stderr]\n{stderr.decode('utf-8', errors='replace')}")

        result = "\n".join(output_parts).strip()
        if proc.returncode != 0:
            result = f"[exit code {proc.returncode}]\n{result}" if result else f"[exit code {proc.returncode}]"

        # Truncate very long output
        if len(result) > 20000:
            result = result[:20000] + "\n...[output truncated]"

        return result or "(no output)"

    except Exception as e:
        return f"Error executing command: {e}"


# ---------------------------------------------------------------------------
# file_read
# ---------------------------------------------------------------------------

async def execute_file_read(
    args: dict,
    access_root: Path,
    workspace_dir: Path,
) -> str:
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: no path provided"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as e:
        return f"Error: {e}"

    if not path.exists():
        return f"Error: file not found: {path}"
    if not path.is_file():
        return f"Error: path is not a file: {path}"

    offset = max(1, int(args.get("offset", 1)))
    limit = int(args.get("limit", 500))

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        selected = lines[offset - 1 : offset - 1 + limit]
        content = "".join(selected)

        header = f"[{path}]"
        if offset > 1 or len(lines) > limit:
            header += f" lines {offset}-{offset + len(selected) - 1} of {len(lines)}"

        return f"{header}\n{content}"
    except Exception as e:
        return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# file_write
# ---------------------------------------------------------------------------

async def execute_file_write(
    args: dict,
    access_root: Path,
    workspace_dir: Path,
    max_file_size_kb: int = 1024,
) -> str:
    raw_path = args.get("path", "")
    content = args.get("content", "")

    if not raw_path:
        return "Error: no path provided"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as e:
        return f"Error: {e}"

    if len(content.encode("utf-8")) > max_file_size_kb * 1024:
        return f"Error: content exceeds max file size of {max_file_size_kb}KB"

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"OK: wrote {len(content)} characters to {path}"
    except Exception as e:
        return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# web_search (Brave Search API)
# ---------------------------------------------------------------------------

async def execute_web_search(
    args: dict,
    brave_api_key: Optional[str],
) -> str:
    if not brave_api_key:
        return "Error: brave_api_key not configured in secrets.json"

    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: no query provided"

    count = min(int(args.get("count", 5)), 20)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": brave_api_key,
                },
                params={"q": query, "count": count},
            )
            response.raise_for_status()
            data = response.json()

        results = data.get("web", {}).get("results", [])
        if not results:
            return f"No results found for: {query}"

        lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("description", "")
            lines.append(f"{i}. {title}\n   {url}\n   {snippet}\n")

        return "\n".join(lines)

    except Exception as e:
        return f"Error during web search: {e}"


# ---------------------------------------------------------------------------
# web_fetch
# ---------------------------------------------------------------------------

async def execute_file_list(
    args: dict,
    access_root: Path,
    workspace_dir: Path,
) -> str:
    raw_path = args.get("path", "")
    if not raw_path:
        return "Error: no path provided"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as e:
        return f"Error: {e}"

    if not path.exists():
        return f"Error: path not found: {path}"
    if not path.is_dir():
        return f"Error: path is not a directory: {path}"

    pattern = args.get("pattern", "*")
    recursive = bool(args.get("recursive", False))

    try:
        import fnmatch
        entries = []
        if recursive:
            all_paths = sorted(path.rglob(pattern))
        else:
            all_paths = sorted(path.glob(pattern))

        for p in all_paths:
            rel = p.relative_to(path)
            kind = "dir" if p.is_dir() else "file"
            try:
                size = p.stat().st_size if p.is_file() else 0
                size_str = f"{size:,}B" if size < 1024 else f"{size//1024:,}KB"
            except Exception:
                size_str = "?"
            entries.append(f"{'[dir] ' if kind=='dir' else '      '}{rel}  {size_str if kind=='file' else ''}")

        if not entries:
            return f"No entries found in {path} (pattern: {pattern})"

        header = f"[{path}]  {len(entries)} items"
        return header + "\n" + "\n".join(entries)
    except Exception as e:
        return f"Error listing directory: {e}"


async def execute_apply_patch(
    args: dict,
    access_root: Path,
    workspace_dir: Path,
) -> str:
    raw_path = args.get("path", "")
    patch_str = args.get("patch", "")

    if not raw_path:
        return "Error: no path provided"
    if not patch_str:
        return "Error: no patch provided"

    try:
        path = _resolve_path(raw_path, access_root, workspace_dir)
    except ValueError as e:
        return f"Error: {e}"

    if not path.exists():
        return f"Error: file not found: {path}"

    try:
        import subprocess
        result = subprocess.run(
            ["patch", "--dry-run", "-u", str(path)],
            input=patch_str.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            return f"Error: patch rejected (dry-run):\n{result.stderr.decode()}"

        result = subprocess.run(
            ["patch", "-u", str(path)],
            input=patch_str.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            return f"Error: patch failed:\n{result.stderr.decode()}"

        out = result.stdout.decode().strip()
        return f"OK: patch applied to {path}" + (f"\n{out}" if out else "")
    except FileNotFoundError:
        return "Error: 'patch' command not found on this system"
    except Exception as e:
        return f"Error applying patch: {e}"


async def execute_process_list(args: dict) -> str:
    filter_str = args.get("filter", "").lower()
    limit = int(args.get("limit", 30))

    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent", "cmdline"]):
            try:
                info = p.info
                name = info.get("name") or ""
                if filter_str and filter_str not in name.lower():
                    continue
                cmd = " ".join(info.get("cmdline") or [])[:80]
                cpu = info.get("cpu_percent") or 0.0
                mem = info.get("memory_percent") or 0.0
                procs.append((info["pid"], name, cpu, mem, cmd))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        procs = procs[:limit]
        if not procs:
            return "No matching processes found."

        lines = ["PID      NAME                     CPU%   MEM%   COMMAND"]
        lines.append("-" * 70)
        for pid, name, cpu, mem, cmd in procs:
            lines.append(f"{pid:<8} {name:<25} {cpu:>5.1f}  {mem:>5.1f}  {cmd}")
        return "\n".join(lines)
    except ImportError:
        return "Error: psutil not installed. Run: pip install psutil"
    except Exception as e:
        return f"Error listing processes: {e}"


async def execute_process_kill(args: dict) -> str:
    pid = args.get("pid")
    if pid is None:
        return "Error: pid is required"

    signal_num = int(args.get("signal", 15))
    pid = int(pid)

    try:
        import psutil, signal as _signal
        try:
            proc = psutil.Process(pid)
            name = proc.name()
        except psutil.NoSuchProcess:
            return f"Error: process {pid} not found"
        except psutil.AccessDenied:
            name = "?"

        import os
        os.kill(pid, signal_num)
        sig_name = {15: "SIGTERM", 9: "SIGKILL", 2: "SIGINT"}.get(signal_num, f"signal {signal_num}")
        return f"OK: sent {sig_name} to PID {pid} ({name})"
    except PermissionError:
        return f"Error: permission denied to signal PID {pid}"
    except ProcessLookupError:
        return f"Error: process {pid} not found"
    except Exception as e:
        return f"Error: {e}"


async def execute_telegram_send(
    args: dict,
    secrets: dict,
    agents_config: Optional[list] = None,
) -> str:
    text = args.get("text", "").strip()
    if not text:
        return "Error: text is required"

    chat_id = args.get("chat_id")
    agent_id = args.get("agent_id")

    # Resolve agent_id -> chat_id via agents config
    if not chat_id and agent_id and agents_config:
        for ag in agents_config:
            if ag.get("id") == agent_id:
                chat_id = ag.get("telegram_chat_id") or ag.get("chat_id")
                token = ag.get("token") or secrets.get(f"{agent_id}_telegram_token")
                break
        if not chat_id:
            return f"Error: could not resolve chat_id for agent '{agent_id}'"
    elif not chat_id:
        return "Error: either chat_id or agent_id must be provided"

    token = args.get("token") or secrets.get("telegram_bot_token")
    if not token:
        return "Error: no telegram token available"

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
            data = resp.json()
            if data.get("ok"):
                return f"OK: message sent to {chat_id}"
            else:
                return f"Error: Telegram API error: {data.get('description', 'unknown')}"
    except Exception as e:
        return f"Error sending Telegram message: {e}"


async def execute_telegram_send_file(
    args: dict,
    secrets: dict,
) -> str:
    """Send a file (photo, document, video, or audio) to a Telegram chat."""
    import mimetypes

    path = args.get("path", "").strip()
    if not path:
        return "Error: path is required"

    from pathlib import Path as _Path
    file_path = _Path(path)
    if not file_path.exists():
        return f"Error: file not found: {path}"
    if not file_path.is_file():
        return f"Error: not a file: {path}"

    caption = args.get("caption", "").strip() or None
    chat_id = args.get("chat_id") or secrets.get("_authorized_telegram_id")
    if not chat_id:
        return "Error: chat_id not provided and authorized_telegram_id not available"

    token = secrets.get("_agent_telegram_token") or secrets.get("telegram_bot_token")
    if not token:
        return "Error: no telegram token available"

    # Determine send method
    file_type = args.get("file_type", "auto").lower()
    if file_type == "auto":
        suffix = file_path.suffix.lower()
        if suffix in (".jpg", ".jpeg", ".png", ".webp"):
            file_type = "photo"
        elif suffix in (".mp4", ".mov", ".avi", ".mkv"):
            file_type = "video"
        elif suffix in (".mp3", ".ogg", ".flac", ".wav", ".m4a"):
            file_type = "audio"
        else:
            file_type = "document"

    method_map = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "audio": "sendAudio",
        "document": "sendDocument",
    }
    field_map = {
        "photo": "photo",
        "video": "video",
        "audio": "audio",
        "document": "document",
    }
    api_method = method_map.get(file_type, "sendDocument")
    field_name = field_map.get(file_type, "document")

    try:
        import httpx
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_type = mime_type or "application/octet-stream"

        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption

        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                files = {field_name: (file_path.name, f, mime_type)}
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/{api_method}",
                    data=data,
                    files=files,
                )
            result = resp.json()
            if result.get("ok"):
                return f"OK: {file_type} sent to {chat_id} ({file_path.name})"
            else:
                return f"Error: Telegram API error: {result.get('description', 'unknown')}"
    except Exception as e:
        return f"Error sending Telegram file: {e}"


async def execute_http_request(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: url is required"

    method = str(args.get("method", "GET")).upper()
    headers = args.get("headers") or {}
    body = args.get("body")
    timeout = min(int(args.get("timeout", 30)), 60)

    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "HASHI/2.2"},
        ) as client:
            req_kwargs: dict = {"headers": headers}
            if body:
                req_kwargs["content"] = body.encode() if isinstance(body, str) else body

            response = await client.request(method, url, **req_kwargs)

        content_type = response.headers.get("content-type", "")
        body_text = response.text
        if len(body_text) > 10000:
            body_text = body_text[:10000] + "\n...[truncated]"

        return (
            f"Status: {response.status_code}\n"
            f"Content-Type: {content_type}\n\n"
            f"{body_text}"
        )
    except Exception as e:
        return f"Error making HTTP request: {e}"


async def execute_web_fetch(
    args: dict,
    max_length: int = 10000,
) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: no URL provided"

    max_len = int(args.get("max_length", max_length))

    try:
        import httpx
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; HASHI/2.2)"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            html = response.text

        # Convert HTML to Markdown if html2text is available
        try:
            import html2text
            h = html2text.HTML2Text()
            h.ignore_links = False
            h.ignore_images = True
            h.body_width = 0
            text = h.handle(html)
        except ImportError:
            # Fallback: basic tag stripping
            import re as _re
            text = _re.sub(r"<[^>]+>", "", html)
            text = _re.sub(r"\n{3,}", "\n\n", text).strip()

        if len(text) > max_len:
            text = text[:max_len] + "\n...[content truncated]"

        return f"[Fetched: {url}]\n\n{text}"

    except Exception as e:
        return f"Error fetching URL: {e}"
