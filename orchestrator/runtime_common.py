from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class QueuedRequest:
    request_id: str
    chat_id: int
    prompt: str
    source: str
    summary: str
    created_at: str
    silent: bool = False
    is_retry: bool = False
    deliver_to_telegram: bool = True
    active_habits: list[dict] | None = None
    skip_memory_injection: bool = False


def _safe_excerpt(text: str, limit: int = 160) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def _md_to_html(text: str) -> str:
    code_blocks = []

    def _save_code_block(match):
        code_blocks.append(match.group(2))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n?([\s\S]*?)```", _save_code_block, text)

    inline_codes = []

    def _save_inline_code(match):
        inline_codes.append(match.group(1))
        return f"\x00INLINE{len(inline_codes) - 1}\x00"

    text = re.sub(r"`([^`\n]+)`", _save_inline_code, text)

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"(?<![A-Za-z0-9*])\*([^*<>]+?)\*(?![A-Za-z0-9*])", r"<i>\1</i>", text)

    for i, code in enumerate(code_blocks):
        safe = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00CODEBLOCK{i}\x00", f"<pre>{safe}</pre>")
    for i, code in enumerate(inline_codes):
        safe = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace(f"\x00INLINE{i}\x00", f"<code>{safe}</code>")
    return text


def _print_user_message(agent_name: str, text: str, media_tag: str = ""):
    if not text:
        return
    prefix = f"[{media_tag}] " if media_tag else ""
    line = f"\033[38;5;117m[User] >> {prefix}{text}\033[0m"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        safe = line.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")
        print(safe, flush=True)


def _print_thinking(agent_name: str, text: str):
    if not text:
        return
    c_dim = "\033[38;5;240m"
    c_reset = "\033[0m"
    line = f"{c_dim}[{agent_name}] 💭 {text}{c_reset}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        safe = line.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")
        print(safe, flush=True)


def _print_final_response(agent_name: str, text: str):
    if not text:
        return
    c_reset = "\033[0m"
    c_green = "\033[38;5;114m"
    c_border = "\033[38;5;242m"
    line = "-" * 50
    for part in (
        f"\n{c_border}{line}\n",
        f"[{agent_name}] final response:{c_reset}\n",
        f"{c_green}{text}{c_reset}\n",
        f"{c_border}{line}{c_reset}\n\n",
    ):
        try:
            print(part, end="", flush=True)
        except (UnicodeEncodeError, OSError):
            safe = part.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")
            print(safe, end="", flush=True)


def resolve_authorized_telegram_ids(extra: dict | None, global_authorized_id: int) -> tuple[int, ...]:
    candidates: list[int] = []
    raw = (extra or {}).get("authorized_telegram_ids")
    if raw is not None:
        if isinstance(raw, (list, tuple)):
            candidates.extend(raw)
        else:
            candidates.append(raw)

    ids: list[int] = []
    for item in candidates:
        if item is None:
            continue
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if value not in ids:
            ids.append(value)

    if not ids and global_authorized_id:
        ids.append(global_authorized_id)

    return tuple(ids)
