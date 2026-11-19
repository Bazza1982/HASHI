"""Streaming gzip boundaries independent of HTTP transport chunk sizes."""
from __future__ import annotations

import gzip
import random

import pytest

from tools.http_response_stream import decoded_response_bytes


class _RawResponse:
    def __init__(self, body, width, coding="gzip"):
        self.headers = {"content-encoding": coding}
        self.body = body
        self.width = width

    async def aiter_raw(self, *, chunk_size):
        del chunk_size
        for offset in range(0, len(self.body), self.width):
            yield self.body[offset:offset + self.width]


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 7, 4096])
@pytest.mark.parametrize("chunk_size", [1, 257, 65536])
async def test_gzip_members_across_arbitrary_transport_boundaries(width, chunk_size):
    rng = random.Random(1811)
    members = [b"a" * 1500, bytes(rng.randrange(256) for _ in range(2048)), "末尾".encode()]
    wire = b"".join(gzip.compress(member, mtime=0) for member in members)
    chunks = [
        chunk async for chunk in decoded_response_bytes(_RawResponse(wire, width), chunk_size=chunk_size)
    ]
    assert all(0 < len(chunk) <= chunk_size for chunk in chunks)
    assert b"".join(chunks) == b"".join(members)


@pytest.mark.asyncio
async def test_empty_gzip_encoded_response_and_invalid_trailer():
    assert [
        chunk async for chunk in decoded_response_bytes(_RawResponse(b"", 1))
    ] == []
    with pytest.raises((ValueError, OSError)):
        async for _ in decoded_response_bytes(_RawResponse(gzip.compress(b"payload")[:-1], 1)):
            pass
# HASHI-Fingerprint: hfp_2e252e33f96f418ba1a2f27aa9377f15



@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1, 7, 4096])
@pytest.mark.parametrize("chunk_size", [1, 257, 65536])
@pytest.mark.parametrize("raw", [False, True], ids=["zlib", "raw"])
async def test_deflate_decodes_across_transport_boundaries(width, chunk_size, raw):
    import zlib
    rng = random.Random(1811)
    data = b"a" * 4000 + bytes(rng.randrange(256) for _ in range(3000))
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS if raw else zlib.MAX_WBITS)
    wire = compressor.compress(data) + compressor.flush()
    chunks = [
        chunk async for chunk in decoded_response_bytes(
            _RawResponse(wire, width, "deflate"), chunk_size=chunk_size,
        )
    ]
    assert all(0 < len(chunk) <= chunk_size for chunk in chunks)
    assert b"".join(chunks) == data
