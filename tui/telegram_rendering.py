"""Safe Rich renderers for messages captured from Telegram-style commands."""
from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from rich.markdown import Markdown
from rich.style import Style
from rich.text import Text


_STYLE_TAGS = {
    "b": Style(bold=True),
    "strong": Style(bold=True),
    "i": Style(italic=True),
    "em": Style(italic=True),
    "u": Style(underline=True),
    "ins": Style(underline=True),
    "s": Style(strike=True),
    "strike": Style(strike=True),
    "del": Style(strike=True),
    "code": Style(color="#c7ff8a"),
    "pre": Style(color="#c7ff8a"),
    "tg-spoiler": Style(dim=True),
    "blockquote": Style(italic=True, color="#9be7ff"),
}
_BLOCK_TAGS = frozenset({"p", "pre", "blockquote"})


def _safe_terminal_text(value: Any) -> str:
    """Remove terminal control characters while preserving layout text."""

    return "".join(
        character
        for character in str(value or "")
        if character in {"\n", "\t"}
        or (ord(character) >= 32 and ord(character) != 127)
    )


def _safe_link(value: str) -> str | None:
    candidate = str(value or "").strip()
    try:
        scheme = urlsplit(candidate).scheme.casefold()
    except ValueError:
        return None
    return candidate if scheme in {"http", "https", "mailto"} else None


class _TelegramHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.output = Text()
        self._stack: list[tuple[str, Style]] = []

    def _style(self) -> Style:
        style = Style()
        for _tag, part in self._stack:
            style += part
        return style

    def _newline(self) -> None:
        if self.output.plain and not self.output.plain.endswith("\n"):
            self.output.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized == "br":
            self.output.append("\n")
            return
        if normalized in _BLOCK_TAGS:
            self._newline()
        style = _STYLE_TAGS.get(normalized)
        if normalized == "a":
            href = _safe_link(dict(attrs).get("href") or "")
            style = Style(link=href, underline=bool(href), color="#71b7ff")
        if style is None:
            # Unsupported tags remain literal text and can never become Rich
            # markup or terminal control sequences.
            self.output.append(self.get_starttag_text() or f"<{tag}>", self._style())
            return
        self._stack.append((normalized, style))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "br":
            self.output.append("\n")
        else:
            self.output.append(self.get_starttag_text() or f"<{tag}/>", self._style())

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == normalized:
                del self._stack[index]
                if normalized in _BLOCK_TAGS:
                    self._newline()
                return
        if normalized not in {"br"}:
            self.output.append(f"</{tag}>", self._style())

    def handle_data(self, data: str) -> None:
        self.output.append(data, self._style())


def telegram_html_to_rich(value: Any) -> Text:
    """Convert the supported Telegram HTML subset to safe Rich text."""

    parser = _TelegramHTMLParser()
    safe_value = _safe_terminal_text(value)
    try:
        parser.feed(safe_value)
        parser.close()
    except Exception:
        return Text(safe_value)
    return parser.output


def command_message_renderable(message: dict[str, Any]):
    """Render one unified slash-command response message safely."""

    text = _safe_terminal_text(message.get("text"))
    meta = message.get("meta") if isinstance(message.get("meta"), dict) else {}
    parse_mode = str(meta.get("parse_mode") or "").split(".")[-1].casefold()
    if parse_mode == "html":
        return telegram_html_to_rich(text)
    if parse_mode in {"markdown", "markdownv2"}:
        return Markdown(text, code_theme="monokai", hyperlinks=False)
    return Text(text)


__all__ = ["command_message_renderable", "telegram_html_to_rich"]
