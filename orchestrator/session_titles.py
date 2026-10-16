from __future__ import annotations

import re
import unicodedata


_SYSTEM_TITLES = frozenset(
    {
        "",
        "new session",
        "new conversation",
        "new chat",
        "新建对话",
        "新增對話",
        "新工作区",
        "新工作區",
        "新しい会話",
        "新しいチャット",
        "새 대화",
        "neue unterhaltung",
        "neuer chat",
        "nouvelle conversation",
        "новая беседа",
        "новый чат",
        "محادثة جديدة",
    }
)
_DEFAULT_AGENT_TITLE = re.compile(r"^\S+\s+default$", re.IGNORECASE)
_LEADING_MARKUP = re.compile(r"^(?:[#>*`~_-]+|\d+[.)]|[-+*])\s*")
_ENGLISH_FILLER = re.compile(
    r"^(?:(?:please|could you|can you|would you)\s+|(?:help me|i want to|i need to)\s+)+",
    re.IGNORECASE,
)
_CHINESE_FILLER = re.compile(r"^(?:请(?:帮我|协助我)?|麻烦(?:帮我)?|帮我|我想(?:请您)?|我需要)\s*")


def is_system_session_title(title: str | None) -> bool:
    """Return whether a title is an untouched system placeholder."""

    normalized = unicodedata.normalize("NFKC", str(title or "")).strip().casefold()
    return normalized in _SYSTEM_TITLES or bool(_DEFAULT_AGENT_TITLE.fullmatch(normalized))


def automatic_session_title(text: str | None) -> str | None:
    """Create a short deterministic title from user-visible first-turn text."""

    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("\u200b", " ").strip()
    value = re.sub(r"^```[^\n]*\n?", "", value)
    value = _LEADING_MARKUP.sub("", value).strip()
    value = re.split(r"[\r\n。！？!?]+", value, maxsplit=1)[0]
    value = re.sub(r"\s+", " ", value).strip(" \t\"'“”‘’`。，、；;:：-—")
    value = _CHINESE_FILLER.sub("", value).strip()
    value = _ENGLISH_FILLER.sub("", value).strip()
    if not value:
        return None

    has_cjk = bool(re.search(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", value))
    if has_cjk:
        return value if len(value) <= 24 else f"{value[:23].rstrip()}…"

    words = value.split()
    shortened = " ".join(words[:8])
    truncated = len(words) > 8
    if len(shortened) > 56:
        shortened = shortened[:56].rsplit(" ", 1)[0] or shortened[:56]
        truncated = True
    shortened = shortened.rstrip(".,;:-")
    return f"{shortened}…" if truncated and shortened else shortened or None
