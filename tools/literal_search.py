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


def _regex_guard(pattern_text: str, *, ignore_case: bool = False) -> str | None:
    """Return a reason when a regex cannot be safely bounded to a record.

    ``re`` has no timeout, so nested quantifiers (``(.+)+``) can show
    super-linear behaviour even on a single bounded record.  Refusing them
    explicitly is more honest than pretending a bounded scan found no match.
    """
    # This heuristic is not a proof of safety; the managed worker deadline
    # remains the boundary for patterns it does not recognize.
    if re.search(r"\([^()]*(?:[+*]|\{\d+,\})[^()]*\)[+*{?]", pattern_text):
        return 'nested_quantifiers'
    # Repeating literal alternatives where one is a prefix of another can
    # backtrack exponentially (for example, a failing search for (a|aa)+).
    # Keep disjoint alternatives and finite repetitions available.
    group_starts = set()
    escaped = in_class = False
    for index, char in enumerate(pattern_text):
        if escaped:
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '[' and not in_class:
            in_class = True
        elif char == ']' and in_class:
            in_class = False
        elif char == '(' and not in_class:
            group_starts.add(index)
    repeated = r"\((?:\?:)?(\w+(?:\|\w+)+)\)(?:[+*]|\{\d+,\})"
    for group in re.finditer(repeated, pattern_text):
        if group.start() not in group_starts:
            continue
        alternatives = group.group(1).split('|')
        if ignore_case:
            alternatives = [item.lower() for item in alternatives]
        if any(left.startswith(right) or right.startswith(left)
               for i, left in enumerate(alternatives)
               for right in alternatives[i + 1:]):
            return 'overlapping_repeated_alternation'
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
    eof = saved.get('eof', False)
    skipping = saved.get('skipping_overlong', False)
    positions = saved.get('pattern_positions')
    coverage_warning = saved.get('coverage_warning')
    matches: list[dict] = []
    characters = 0
    encoding_warning = False
    page = False
    chunk_chars = max(1, min(chunk_chars, CHUNK_CHARS))

    with path.open('r', encoding='utf-8-sig', errors='replace', newline='') as stream:
        if saved:
            stream.seek(saved['cookie'])

        def read_chunk():
            nonlocal buffer, characters, encoding_warning, eof
            chunk = stream.read(chunk_chars)
            if not chunk:
                eof = True
                return
            buffer += chunk
            characters += len(chunk)
            encoding_warning |= '\ufffd' in chunk
            if progress:
                progress(characters)

        while True:
            nl = buffer.find('\n')
            if skipping:
                if nl != -1:
                    consumed = nl + 1
                    offset += consumed
                    lines += 1
                    buffer = buffer[consumed:]
                    skipping = False
                    continue
                # Discard each chunk while skipping, rather than accumulating
                # an arbitrarily large record. Offset remains in characters.
                offset += len(buffer)
                buffer = ''
                if eof:
                    skipping = False
                    break
                if character_budget is not None and characters >= character_budget:
                    break
                read_chunk()
                continue

            if nl == -1 and not eof:
                # A final CR might be the first half of CRLF, so allow it
                # beyond the logical record limit until the next character.
                pending_length = len(buffer) - int(buffer.endswith('\r'))
                if pending_length > MAX_REGEX_RECORD_CHARS:
                    coverage_warning = coverage_warning or _OVERLONG_MESSAGE
                    skipping = True
                    continue
                if character_budget is not None and characters >= character_budget:
                    break
                read_chunk()
                continue

            if nl == -1:
                if not buffer:
                    break
                record_end = len(buffer)
                consume_end = record_end
            else:
                record_end = nl - int(nl > 0 and buffer[nl - 1] == '\r')
                consume_end = nl + 1
            if record_end > MAX_REGEX_RECORD_CHARS:
                coverage_warning = coverage_warning or _OVERLONG_MESSAGE
                skipping = True
                continue

            segment = buffer[:record_end]
            if positions is None:
                positions = [[0, False] for _ in patterns]
            while len(matches) < max_results:
                candidates = []
                for index, (term, pattern) in enumerate(patterns):
                    position, skip_empty = positions[index]
                    found_iter = pattern.finditer(segment, position)
                    if skip_empty:
                        next(found_iter, None)
                    found = next(found_iter, None)
                    if found is not None:
                        candidates.append((found.start(), index, term, found))
                if not candidates:
                    break
                start, index, term, found = min(candidates, key=lambda item: item[:2])
                end = found.end()
                matches.append({
                    'term': term,
                    'line': lines + 1,
                    'character_offset': offset + start,
                    'excerpt': segment[max(0, start - context_chars):end + context_chars],
                })
                # After an empty match finditer can yield a nonempty match at
                # the same position. Remember the empty match to skip only it.
                positions[index] = [end, end == start]

            if len(matches) >= max_results:
                # At a known EOF, check the final record for another match
                # before issuing a cursor. The managed worker deadline still
                # bounds expensive expressions during this lookahead.
                at_end = eof and consume_end == len(buffer)
                if at_end:
                    remaining = False
                    for (_, pattern), (position, skip_empty) in zip(patterns, positions):
                        found_iter = pattern.finditer(segment, position)
                        if skip_empty:
                            next(found_iter, None)
                        if next(found_iter, None) is not None:
                            remaining = True
                            break
                    if not remaining:
                        buffer = ''
                        offset += consume_end
                        lines += int(nl != -1)
                        positions = None
                page = True
                break

            # Keep a record intact across page boundaries so ^, lookbehind,
            # and other line-relative expressions see the original line.
            buffer = buffer[consume_end:]
            offset += consume_end
            lines += int(nl != -1)
            positions = None
            if character_budget is not None and characters >= character_budget:
                break

        exhausted = eof and not buffer and not skipping and positions is None
        complete = exhausted and not coverage_warning
        continuation = None if exhausted else {
            'cookie': stream.tell(), 'buffer': buffer, 'offset': offset,
            'lines': lines, 'eof': eof, 'skipping_overlong': skipping,
            'pattern_positions': positions, 'coverage_warning': coverage_warning,
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
        for term, pattern in patterns:
            reason = _regex_guard(term, ignore_case=bool(pattern.flags & re.IGNORECASE))
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
