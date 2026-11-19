"""Incremental HTTP response decoding for common compressed bodies."""
from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator


async def decoded_response_bytes(response, *, chunk_size: int = 65536) -> AsyncIterator[bytes]:
    """Yield decoded bytes, bounding single-layer gzip and deflate expansion.

    HTTPX handles all other encodings as before. Gzip may contain multiple
    members. Deflate supports both zlib-wrapped and raw streams, like HTTPX.
    """
    coding = str(response.headers.get("content-encoding") or "").strip().casefold()
    if coding not in {"gzip", "deflate"}:
        async for chunk in response.aiter_bytes(chunk_size=chunk_size):
            yield chunk
        return

    decoder = None
    saw_input = False
    yielded = 0
    first_attempt = True
    finished = False
    probe = b""
    async for raw in response.aiter_raw(chunk_size=65536):
        if finished:
            # HTTPX's deflate decoder ignores trailing bytes after the stream.
            continue
        pending = probe + raw
        probe = b""
        if pending:
            saw_input = True
        if coding == "deflate" and decoder is None and len(pending) < 2:
            probe = pending
            continue
        while pending:
            if decoder is None:
                wbits = (zlib.MAX_WBITS | 16) if coding == "gzip" else zlib.MAX_WBITS
                decoder = zlib.decompressobj(wbits)
                first_attempt = True
            try:
                part = decoder.decompress(pending, chunk_size)
            except zlib.error:
                if coding != "deflate" or not first_attempt:
                    raise
                decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                part = decoder.decompress(pending, chunk_size)
            first_attempt = False
            if decoder.eof:
                pending = decoder.unused_data if coding == "gzip" else b""
                finished = coding == "deflate"
                decoder = None
            else:
                pending = decoder.unconsumed_tail
            if part:
                yield part
                yielded += 1
                if yielded % 32 == 0:
                    await asyncio.sleep(0)
            # zlib can hold output internally even after consuming its input.
            # Drain it in bounded pieces before reading another compressed chunk.
            while decoder is not None and not pending and len(part) == chunk_size:
                part = decoder.decompress(b"", chunk_size)
                if decoder.eof:
                    pending = decoder.unused_data if coding == "gzip" else b""
                    finished = coding == "deflate"
                    decoder = None
                if part:
                    yield part
                    yielded += 1
                    if yielded % 32 == 0:
                        await asyncio.sleep(0)
                if not part or pending:
                    break
    if probe or (saw_input and decoder is not None and not decoder.eof):
        raise ValueError(f"incomplete {coding} response")
# HASHI-Fingerprint: hfp_1a9ea2dd495e40fca44815cd8c233515
