from __future__ import annotations

import asyncio

import pytest

from adapters.stream_io import iter_stream_lines


@pytest.mark.asyncio
async def test_iter_stream_lines_accepts_line_larger_than_reader_limit():
    reader = asyncio.StreamReader(limit=32)
    oversized = b"x" * (2 * 1024 * 1024)
    reader.feed_data(oversized + b"\nnext\n")
    reader.feed_eof()

    lines = [line async for line in iter_stream_lines(reader, chunk_size=4096)]

    assert lines == [oversized + b"\n", b"next\n"]


@pytest.mark.asyncio
async def test_iter_stream_lines_preserves_split_lines_and_unterminated_tail():
    reader = asyncio.StreamReader(limit=8)
    reader.feed_data(b"first\r")
    reader.feed_data(b"\nsecond\nlast")
    reader.feed_eof()

    lines = [line async for line in iter_stream_lines(reader, chunk_size=3)]

    assert lines == [b"first\r\n", b"second\n", b"last"]


@pytest.mark.asyncio
async def test_iter_stream_lines_rejects_non_positive_chunk_size():
    reader = asyncio.StreamReader()

    with pytest.raises(ValueError, match="greater than zero"):
        _ = [line async for line in iter_stream_lines(reader, chunk_size=0)]
