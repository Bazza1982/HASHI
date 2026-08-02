from __future__ import annotations

from collections.abc import AsyncIterator


DEFAULT_READ_CHUNK_SIZE = 64 * 1024


async def iter_stream_lines(
    reader,
    *,
    chunk_size: int = DEFAULT_READ_CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    """Yield byte lines without ``StreamReader.readline()``'s size ceiling.

    ``asyncio`` applies the subprocess stream buffer limit to separator-based
    reads.  A single large JSONL event can therefore make ``readline()`` raise
    even though the child process is healthy.  Fixed-size ``read()`` calls do
    not have that separator limit, so assemble complete lines from chunks and
    retain the same newline-preserving behaviour as ``readline()``.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")

    pending = bytearray()
    while True:
        chunk = await reader.read(chunk_size)
        if not chunk:
            break

        parts = chunk.split(b"\n")
        if len(parts) == 1:
            pending.extend(chunk)
            continue

        pending.extend(parts[0])
        yield bytes(pending) + b"\n"
        pending.clear()

        for part in parts[1:-1]:
            yield part + b"\n"
        pending.extend(parts[-1])

    if pending:
        yield bytes(pending)
