"""Incremental HTTP response decoding for common compressed bodies."""
from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator


async def decoded_response_bytes(response, *, chunk_size: int = 65536) -> AsyncIterator[bytes]:
    """Yield decoded response bytes, bounding a single gzip expansion.

    HTTPX handles all other encodings as before. A gzip stream may contain
    multiple members; trailing invalid data is an error rather than success.
    """
    coding = str(response.headers.get("content-encoding") or "").strip().casefold()
    if coding != "gzip":
        async for chunk in response.aiter_bytes(chunk_size=chunk_size):
            yield chunk
        return

    decoder = None
    saw_input = False
    yielded = 0
    async for raw in response.aiter_raw(chunk_size=65536):
        pending = raw
        if pending:
            saw_input = True
        while pending:
            if decoder is None:
                decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
            part = decoder.decompress(pending, chunk_size)
            if decoder.eof:
                pending = decoder.unused_data
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
                    pending = decoder.unused_data
                    decoder = None
                if part:
                    yield part
                    yielded += 1
                    if yielded % 32 == 0:
                        await asyncio.sleep(0)
                if not part or pending:
                    break
    if saw_input and decoder is not None and not decoder.eof:
        raise ValueError("incomplete gzip response")
# HASHI-Fingerprint: hfp_1a9ea2dd495e40fca44815cd8c233515
