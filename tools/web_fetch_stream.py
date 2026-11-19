"""Bounded, Run-scoped streaming for fetched HTML and converted text."""
from __future__ import annotations

import asyncio
import codecs
import json
import os
import re
import tempfile
import threading
from contextlib import ExitStack, aclosing
from html.parser import HTMLParser
from typing import Any, Iterator


def _parser_memory_budget() -> int | None:
    raw = os.environ.get("HASHI_WEB_FETCH_PARSER_MEMORY_CHARS")
    if raw is None:
        return 8 * 1024 * 1024
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("HASHI_WEB_FETCH_PARSER_MEMORY_CHARS must be a nonnegative integer") from exc
    if value < 0:
        raise ValueError("HASHI_WEB_FETCH_PARSER_MEMORY_CHARS must be a nonnegative integer")
    return value or None


def _check_parser_budget(converter, budget: int | None) -> None:
    if budget is None:
        return
    parser = getattr(converter, "parser", converter)
    buffered = len(getattr(parser, "rawdata", "")) + len(getattr(converter, "pending", ""))
    if buffered > budget:
        raise ValueError(
            "HTML parser memory budget exceeded; configure HASHI_WEB_FETCH_PARSER_MEMORY_CHARS "
            "to a larger value or zero for no configured limit"
        )


class _SpoolCancelled(Exception):
    pass


def _check_cancelled(cancelled: threading.Event | None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise _SpoolCancelled()


def _utf8_chunks(stream, *, cancelled=None) -> Iterator[str]:
    stream.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    while chunk := stream.read(64 * 1024):
        _check_cancelled(cancelled)
        text = decoder.decode(chunk, final=False)
        if text:
            yield text
    _check_cancelled(cancelled)
    final = decoder.decode(b"", final=True)
    if final:
        yield final


async def _run_spool_task(function, *args):
    cancelled = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, *args, cancelled=cancelled))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as exc:
        cancelled.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (Exception, asyncio.CancelledError):
            pass
        setattr(exc, "hashi_tool_details", {
            "foreground_cleanup": {"status": "stopped" if task.done() else "unverified"},
            "coverage_complete": False,
        })
        raise


class _PlainTextWriter:
    """Match the legacy fallback's newline collapsing and outer whitespace trim."""

    def __init__(self, stream):
        self.stream = stream
        self.pending_newlines = ""
        self.started = False
        self.last_nonspace = 0

    def _write(self, text: str) -> None:
        if not self.started:
            text = text.lstrip()
            if not text:
                return
            self.started = True
        start = self.stream.tell()
        self.stream.write(text.encode("utf-8"))
        trimmed = text.rstrip()
        if trimmed:
            self.last_nonspace = start + len(trimmed.encode("utf-8"))

    def write(self, text: str) -> None:
        text = self.pending_newlines + text
        self.pending_newlines = ""
        trailing = len(text) - len(text.rstrip("\n"))
        if trailing:
            self.pending_newlines = "\n" * min(2, trailing)
            text = text[:-trailing]
        if text:
            self._write(re.sub(r"\n{3,}", "\n\n", text))

    def finish(self) -> None:
        self._write(self.pending_newlines)
        self.stream.truncate(self.last_nonspace)
        self.stream.seek(0)


class _PlainHTML(HTMLParser):
    def __init__(self, stream):
        super().__init__(convert_charrefs=False)
        self.writer = _PlainTextWriter(stream)

    def handle_data(self, data: str) -> None:
        self.writer.write(data)

    def handle_entityref(self, name: str) -> None:
        self.writer.write("&" + name + ";")

    def handle_charref(self, name: str) -> None:
        self.writer.write("&#" + name + ";")

    def finish(self) -> None:
        self.close()
        self.writer.finish()


class _MarkdownHTML:
    def __init__(self, stream, html2text):
        self.stream = stream
        self.pending = ""
        self.continuing_stressed = False
        self.parser = html2text.HTML2Text(out=self._write)
        self.parser.ignore_links = False
        self.parser.ignore_images = True
        self.parser.body_width = 0

    def _write(self, text: str) -> None:
        if text:
            self.stream.write(text.encode("utf-8"))
            # HTML2Text's default output callback maintains this parser state.
            self.parser.lastWasNL = text[-1] == "\n"

    def _feed_piece(self, text: str, *, final: bool = False) -> None:
        if self.continuing_stressed:
            boundaries = [position for position in (text.find("<"), text.find("&")) if position >= 0]
            boundary = min(boundaries) if boundaries else len(text)
            ending = bool(boundaries) or final
            part = text[:boundary].rstrip() if ending else text[:boundary]
            if part:
                preceding = self.parser.preceding_stressed
                self.parser.preceding_stressed = False
                self.parser.feed(part)
                self.parser.preceding_stressed = preceding or self.parser.preceding_stressed
            if boundaries:
                self.continuing_stressed = False
                self.parser.feed(text[boundary:])
            return
        stressed = self.parser.stressed
        self.parser.feed(text)
        if stressed and "<" not in text and "&" not in text:
            self.continuing_stressed = True

    def feed(self, text: str) -> None:
        # HTML2Text's formatting state assumes ordinary short text segments are
        # passed together. Hold a bounded window and split at markup or
        # whitespace before forcing a split in an unusually long token.
        self.pending += text
        while len(self.pending) > 64 * 1024:
            window = self.pending[:64 * 1024]
            boundary = max(window.rfind(">") + 1, window.rfind("<"))
            if boundary <= 0:
                boundary = max(window.rfind(" "), window.rfind("\n"), window.rfind("\t"))
            if boundary <= 0:
                boundary = 64 * 1024
            self._feed_piece(self.pending[:boundary])
            self.pending = self.pending[boundary:]

    def finish(self) -> None:
        if self.pending:
            self._feed_piece(self.pending, final=True)
            self.pending = ""
        self.parser.feed("")
        self.parser.finish()
        self.stream.seek(0)


def _replace_placeholder(chunks: Iterator[str]) -> Iterator[str]:
    token = "&nbsp_place_holder;"
    pending = ""
    for chunk in chunks:
        text = pending + chunk
        pending = ""
        while True:
            index = text.find(token)
            if index < 0:
                break
            if index:
                yield text[:index]
            yield " "
            text = text[index + len(token):]
        keep = min(len(text), len(token) - 1)
        while keep and text[-keep:] != token[:keep]:
            keep -= 1
        if keep:
            if len(text) > keep:
                yield text[:-keep]
            pending = text[-keep:]
        elif text:
            yield text
    if pending:
        yield pending


def _copy_requested_text(chunks: Iterator[str], output, maximum: int | None, *, cancelled=None) -> tuple[int, bool]:
    count = 0
    truncated = False
    for text in chunks:
        _check_cancelled(cancelled)
        if maximum is not None and len(text) > maximum - count:
            text = text[:max(0, maximum - count)]
            truncated = True
        if text:
            output.write(text.encode("utf-8"))
            count += len(text)
        if truncated:
            break
    if truncated:
        marker = "\n...[content truncated]"
        output.write(marker.encode("utf-8"))
        count += len(marker)
    output.seek(0)
    return count, truncated


def _select_text(converted, selected, maximum, markdown, *, cancelled=None):
    chunks = _utf8_chunks(converted, cancelled=cancelled)
    if markdown:
        chunks = _replace_placeholder(chunks)
    return _copy_requested_text(chunks, selected, maximum, cancelled=cancelled)


def _retain_text(store, binding, source_tool, selected, prefix, *, cancelled=None):
    def chunks():
        _check_cancelled(cancelled)
        yield prefix
        yield from _utf8_chunks(selected, cancelled=cancelled)

    return store.retain_text_chunks(binding, source_tool, chunks())


def _bounded_preview(prefix: str, stream) -> str:
    stream.seek(0)
    preview = prefix + stream.read(65536).decode("utf-8", errors="ignore")
    low, high = 0, min(len(preview), 12000)
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(preview[:middle], ensure_ascii=False)) <= 12000:
            low = middle
        else:
            high = middle - 1
    return preview[:low]


async def fetch_web_text(args: dict[str, Any], *, max_length: int | None = None, retained_output=None):
    """Stream a web page into a private spool and page its converted text."""
    from tools.builtins import BuiltinExecutionResult

    url = str(args.get("url", "")).strip()
    if not url:
        return "Error: no URL provided"
    raw_limit = args.get("max_length", max_length)
    maximum = None if raw_limit is None else int(raw_limit)
    if maximum is not None and maximum < 0:
        return "Error: max_length must be nonnegative"

    try:
        parser_budget = _parser_memory_budget()
    except ValueError as exc:
        return f"Error: fetching URL: {exc}"

    make_capture = (retained_output[0].temporary_capture_file if retained_output is not None
                    else lambda: tempfile.TemporaryFile(mode="w+b"))
    prefix = f"[Fetched: {url}]\n\n"
    try:
        import httpx

        with ExitStack() as files:
            converted = files.enter_context(make_capture())
            selected = files.enter_context(make_capture())
            try:
                import html2text
            except ImportError:
                converter = _PlainHTML(converted)
                markdown = False
            else:
                converter = _MarkdownHTML(converted, html2text)
                markdown = True

            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (compatible; HASHI/2.2)"},
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    from tools.http_response_stream import decoded_response_bytes

                    encoding = response.encoding or "utf-8"
                    try:
                        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
                    except LookupError:
                        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    async with aclosing(decoded_response_bytes(response)) as decoded_chunks:
                        async for chunk in decoded_chunks:
                            text = decoder.decode(chunk, final=False)
                            if text:
                                converter.feed(text)
                                _check_parser_budget(converter, parser_budget)
                    final = decoder.decode(b"", final=True)
                    if final:
                        converter.feed(final)
                        _check_parser_budget(converter, parser_budget)
            converter.finish()

            count, _ = await _run_spool_task(_select_text, converted, selected, maximum, markdown)
            total = len(prefix) + count
            if total <= 20000 or retained_output is None:
                selected.seek(0)
                return prefix + selected.read().decode("utf-8")

            store, binding, source_tool = retained_output
            try:
                reference = await _run_spool_task(_retain_text, store, binding, source_tool, selected, prefix)
            except (OSError, ValueError, TimeoutError):
                reference = None
            preview = _bounded_preview(prefix, selected)
            if reference:
                output = (preview + f"\n[truncated by HASHI; use tool_output_read reference={reference} "
                          "offset=0; retained while this Run is active]")
            else:
                output = preview + "\n[truncated by HASHI; complete-result retention is unavailable for this Run]"
            return BuiltinExecutionResult(output, {
                "complete_content": False,
                "output_continuation": reference,
                "output_retention": store.retention if reference else "unavailable",
            })
    except Exception as exc:
        return f"Error: fetching URL: {exc}"
# HASHI-Fingerprint: hfp_950374e5992641d0858494548f2201de
