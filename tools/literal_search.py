"""Bounded UTF-8 literal scanning shared by log_query and scoped search.

Continuation keeps a TextIO seek cookie and a bounded overlap buffer. It is
internal JSON data, never executable state supplied by a model.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

CHUNK_CHARS = 64 * 1024


def scan_literal(path: Path, *, terms: tuple[str, ...], case_sensitive: bool,
                 max_results: int, context_chars: int, state: dict | None = None,
                 character_budget: int | None = None, progress: Callable | None = None,
                 chunk_chars: int = CHUNK_CHARS) -> dict:
    patterns = [(term, re.compile(re.escape(term), 0 if case_sensitive else re.IGNORECASE))
                for term in terms]
    longest = max(map(len, terms))
    saved = state or {}
    buffer = saved.get('buffer', '')
    offset = saved.get('offset', 0)
    lines = saved.get('lines', 0)
    cursor = saved.get('cursor', 0)
    eof = saved.get('eof', False)
    need_read = not bool(saved)
    matches, characters = [], 0
    encoding_warning = False
    with path.open('r', encoding='utf-8-sig', errors='replace', newline='') as stream:
        if saved:
            stream.seek(saved['cookie'])
        while True:
            if need_read and not eof:
                chunk = stream.read(chunk_chars)
                eof = not chunk
                buffer += chunk
                characters += len(chunk)
                encoding_warning |= '\ufffd' in chunk
                if progress:
                    progress(characters)
            safe_end = offset + len(buffer) if eof else max(cursor, offset + len(buffer) - context_chars - longest)
            local = max(0, cursor - offset)
            limit = safe_end - offset
            while local < limit and len(matches) < max_results:
                candidates = [(found.start(), index, term, found)
                              for index, (term, pattern) in enumerate(patterns)
                              if (found := pattern.search(buffer, local)) is not None
                              and found.start() < limit]
                if not candidates:
                    local = limit
                    break
                start, _, term, found = min(candidates, key=lambda item: item[:2])
                matches.append({'term': term, 'line': lines + buffer.count('\n', 0, start) + 1,
                                'character_offset': offset + start,
                                'excerpt': buffer[max(0, start-context_chars):found.end()+context_chars]})
                local = max(found.end(), start + 1)
            cursor = offset + local
            page = len(matches) >= max_results
            complete = eof and cursor >= offset + len(buffer)
            # There is no need to materialise an arbitrarily long line.
            drop = max(0, cursor - offset - context_chars)
            lines += buffer.count('\n', 0, drop)
            buffer = buffer[drop:]
            offset += drop
            continuation = None if complete else {
                'cookie': stream.tell(), 'buffer': buffer, 'offset': offset,
                'lines': lines, 'cursor': cursor, 'eof': eof,
            }
            if complete or page or (character_budget is not None and characters >= character_budget):
                break
            need_read = True
    return {'path': str(path), 'file_size_bytes': path.stat().st_size, 'literal_only': True,
            'case_sensitive': case_sensitive, 'terms': list(terms), 'matches': matches,
            'result_limit_reached': page, 'characters_read': characters,
            'encoding_warning': encoding_warning, 'continuation': continuation,
            'coverage_complete': complete}
