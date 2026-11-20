"""Bounded UTF-8 literal scanning shared by log_query and scoped search.

Literal terms use a bounded overlap buffer so a match can never be silently
split across a read chunk.

Regex mode uses a bounded record (line) strategy instead: ``re`` is evaluated
against each complete record and ``.`` never crosses a record boundary, which is
exactly the default regex contract.  A single record longer than
``MAX_REGEX_RECORD_CHARS`` cannot be matched safely and is reported as an
incomplete-coverage warning rather than matched from a truncated fragment.

Continuation keeps a TextIO seek cookie and a bounded overlap buffer. It is
internal JSON data, never executable state supplied by a model.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

CHUNK_CHARS = 64 * 1024
MAX_REGEX_RECORD_CHARS = 1_000_000
_OVERLONG_MESSAGE = (
    'regex record exceeded the bounded scan limit; '
    'matching evidence for that record was not evaluated'
)


def _regex_guard(pattern_text: str) -> str | None:
    """Return a reason when a regex cannot be safely bounded to a record.

    ``re`` has no timeout, so nested quantifiers (``(.+)+``) can show
    super-linear behaviour even on a single bounded record.  Refusing them
    explicitly is more honest than pretending a bounded scan found no match.
    """
    if re.search(r"\([^()]*[+*{][^()]*\)[+*{?]", pattern_text):
        return 'nested_quantifiers'
    return None


def _scan_regex_lines(
    path: Path,
    *,
    patterns: list[tuple[str, re.Pattern[str]]],
    case_sensitive: bool,
    max_results: int,
    context_chars: int,
    state: dict | None,
    character_budget: int | None,
    progress: Callable | None,
    chunk_chars: int,
) -> dict:
    saved = state or {}
    buffer = saved.get('buffer', '')
    offset = saved.get('offset', 0)
    lines = saved.get('lines', 0)
    cursor = saved.get('cursor', 0)
    eof = saved.get('eof', False)
    coverage_warning = saved.get('coverage_warning')
    matches: list[dict] = []
    characters = 0
    encoding_warning = False
    page = False

    with path.open('r', encoding='utf-8-sig', errors='replace', newline='') as stream:
        if saved:
            stream.seek(saved['cookie'])
        while True:
            local = max(0, cursor - offset)
            # Top up the buffer until we hold a complete record (newline), reach
            # EOF, or discover an over-long record.
            while buffer.find('\n', local) == -1 and not eof:
                if len(buffer) - local >= MAX_REGEX_RECORD_CHARS:
                    break
                chunk = stream.read(chunk_chars)
                if not chunk:
                    eof = True
                    break
                buffer += chunk
                characters += len(chunk)
                encoding_warning |= '\ufffd' in chunk
                if progress:
                    progress(characters)
            local = max(0, cursor - offset)
            if eof and local >= len(buffer):
                break
            nl = buffer.find('\n', local)
            if nl != -1:
                record_end = nl
                consume_end = nl + 1
                skip = False
            elif len(buffer) - local >= MAX_REGEX_RECORD_CHARS:
                # Over-long record: consume it fully without matching and mark
                # coverage incomplete rather than fabricate a truncated result.
                coverage_warning = coverage_warning or _OVERLONG_MESSAGE
                while buffer.find('\n', local) == -1 and not eof:
                    chunk = stream.read(chunk_chars)
                    if not chunk:
                        eof = True
                        break
                    buffer += chunk
                    characters += len(chunk)
                    encoding_warning |= '\ufffd' in chunk
                    if progress:
                        progress(characters)
                nl = buffer.find('\n', local)
                record_end = len(buffer) if nl == -1 else nl
                consume_end = len(buffer) if nl == -1 else nl + 1
                skip = True
            else:
                # EOF with a bounded final record that has no trailing newline.
                record_end = len(buffer)
                consume_end = len(buffer)
                skip = False

            last_end = local
            if not skip:
                segment = buffer[local:record_end]
                for term, pattern in patterns:
                    for found in pattern.finditer(segment):
                        if len(matches) >= max_results:
                            break
                        start = found.start()
                        end = max(found.end(), start + 1)
                        matches.append({
                            'term': term,
                            'line': lines + segment.count('\n', 0, start) + 1,
                            'character_offset': offset + local + start,
                            'excerpt': segment[max(0, start - context_chars):end + context_chars],
                        })
                        last_end = local + end
                    if len(matches) >= max_results:
                        break

            cursor = offset + last_end if len(matches) >= max_results else offset + consume_end
            drop = cursor - offset
            lines += buffer.count('\n', 0, drop)
            buffer = buffer[drop:]
            offset += drop
            cursor = offset
            page = len(matches) >= max_results
            if page or (character_budget is not None and characters >= character_budget):
                break

        complete = eof and not buffer and not page and not coverage_warning
        continuation = None if complete else {
            'cookie': stream.tell(), 'buffer': buffer, 'offset': offset,
            'lines': lines, 'cursor': cursor, 'eof': eof,
            'coverage_warning': coverage_warning,
        }

    return {
        'path': str(path),
        'file_size_bytes': path.stat().st_size,
        'literal_only': False,
        'case_sensitive': case_sensitive,
        'terms': [term for term, _ in patterns],
        'matches': matches,
        'result_limit_reached': page,
        'characters_read': characters,
        'encoding_warning': encoding_warning,
        'continuation': continuation,
        'coverage_complete': complete,
        'coverage_warning': coverage_warning,
    }


def scan_literal(path: Path, *, terms: tuple[str, ...], case_sensitive: bool,
                 max_results: int, context_chars: int, state: dict | None = None,
                 character_budget: int | None = None, progress: Callable | None = None,
                 chunk_chars: int = CHUNK_CHARS, regex: bool = False) -> dict:
    patterns = [
        (
            term,
            re.compile(
                term if regex else re.escape(term),
                0 if case_sensitive else re.IGNORECASE,
            ),
        )
        for term in terms
    ]
    if regex:
        for term in terms:
            reason = _regex_guard(term)
            if reason:
                return {
                    'path': str(path),
                    'file_size_bytes': path.stat().st_size,
                    'literal_only': False,
                    'case_sensitive': case_sensitive,
                    'terms': list(terms),
                    'matches': [],
                    'result_limit_reached': False,
                    'characters_read': 0,
                    'encoding_warning': False,
                    'continuation': None,
                    'coverage_complete': False,
                    'coverage_warning': (
                        f'regex pattern unsupported for bounded scanning: {reason}'
                    ),
                }
        return _scan_regex_lines(
            path, patterns=patterns, case_sensitive=case_sensitive,
            max_results=max_results, context_chars=context_chars, state=state,
            character_budget=character_budget, progress=progress,
            chunk_chars=chunk_chars,
        )
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
    return {'path': str(path), 'file_size_bytes': path.stat().st_size, 'literal_only': not regex,
            'case_sensitive': case_sensitive, 'terms': list(terms), 'matches': matches,
            'result_limit_reached': page, 'characters_read': characters,
            'encoding_warning': encoding_warning, 'continuation': continuation,
            'coverage_complete': complete, 'coverage_warning': None}
