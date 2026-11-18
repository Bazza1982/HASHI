"""Bounded secret redaction for streamed managed HTTP response bytes."""
from __future__ import annotations

import re


def fold_percent(value):
    """Canonicalize only hexadecimal digits in percent-encoded bytes."""
    pattern = rb"%[0-9a-fA-F]{2}" if isinstance(value, bytes) else r"%[0-9a-fA-F]{2}"
    return re.sub(pattern, lambda match: match.group(0).lower(), value)


def _trailing_prefix(text: bytes, pattern: bytes) -> int:
    """Length of the longest suffix of text that starts pattern."""
    failure = [0] * len(pattern)
    matched = 0
    for index in range(1, len(pattern)):
        while matched and pattern[index] != pattern[matched]:
            matched = failure[matched - 1]
        if pattern[index] == pattern[matched]:
            matched += 1
        failure[index] = matched
    matched = 0
    for byte in text[-max(0, len(pattern) - 1):] if len(pattern) > 1 else b"":
        while matched and byte != pattern[matched]:
            matched = failure[matched - 1]
        if byte == pattern[matched]:
            matched += 1
        if matched == len(pattern):
            matched = failure[matched - 1]
    return matched


class StreamingRedactor:
    """Redact bounded patterns across chunks without revealing range fragments."""

    def __init__(self, patterns, *, partial: bool = False) -> None:
        if type(partial) is not bool:
            raise ValueError("partial response must be explicit")
        values = tuple(patterns)
        if any(not isinstance(item, bytes) or not item for item in values):
            raise ValueError("redaction patterns must be nonempty bytes")
        self._patterns = tuple(sorted(set(values), key=lambda item: (-len(item), item)))
        plain = [item for item in self._patterns if b"%" not in item]
        encoded = [fold_percent(item) for item in self._patterns if b"%" in item]

        def matcher(items):
            # Lookahead retains overlapping candidates; escaped literals do
            # not turn a credential into an executable regular expression.
            return re.compile(b"(?=(" + b"|".join(re.escape(item) for item in items) + b"))") if items else None

        self._plain_matcher = matcher(plain)
        self._encoded_matcher = matcher(encoded)
        self._max = max(map(len, self._patterns), default=1)
        self._buffer = b""
        self._leading = bool(partial and self._patterns)
        self._trailing = partial
        self._closed = False
        self.redacted = False
        self.marker = b""
        if self._patterns:
            used = set().union(*(set(item) for item in self._patterns))
            for item in self._patterns:
                if b"%" in item:
                    used.update(item.lower())
                    used.update(item.upper())
            # An ASCII sentinel remains valid in both UTF-8 text and binary.
            candidates = list(range(33, 127)) + list(range(0, 33)) + [127]
            sentinel = next((bytes([item]) for item in candidates if item not in used), None)
            if sentinel is None:
                raise ValueError("no safe redaction sentinel is available")
            description = b"[credential redacted]"
            self.marker = (sentinel + description + sentinel if all(
                (fold_percent(item) not in fold_percent(description) if b"%" in item
                 else item not in description) for item in self._patterns
            ) else sentinel)

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes, *, final: bool = False) -> bytes:
        if self._closed or not isinstance(data, bytes) or type(final) is not bool:
            raise ValueError("invalid or completed redaction stream")
        if not self._patterns:
            self._closed = final
            return data

        self._buffer += data
        if self._leading:
            if len(self._buffer) < self._max and not final:
                return b""
            # Complete matches are handled first by the ordinary scanner.
            if not any(
                (fold_percent(self._buffer).startswith(fold_percent(pattern)) if b"%" in pattern
                 else self._buffer.startswith(pattern)) for pattern in self._patterns
            ):
                fragment = max(
                    _trailing_prefix(
                        (self._buffer.lower() if b"%" in pattern else self._buffer)[::-1],
                        (pattern.lower() if b"%" in pattern else pattern)[::-1],
                    ) for pattern in self._patterns
                )
                if fragment:
                    self._buffer = self.marker + self._buffer[fragment:]
                    self.redacted = True
            self._leading = False

        limit = len(self._buffer) if final else max(0, len(self._buffer) - self._max + 1)
        offset = 0
        result = bytearray()
        streams = []
        for matcher, content in (
            (self._plain_matcher, self._buffer),
            (self._encoded_matcher, fold_percent(self._buffer) if self._encoded_matcher else b""),
        ):
            if matcher is not None:
                iterator = iter(matcher.finditer(content))
                streams.append([iterator, next(iterator, None)])
        while offset < limit:
            for stream in streams:
                while stream[1] is not None and stream[1].start() < offset:
                    stream[1] = next(stream[0], None)
            hits = [stream[1] for stream in streams
                    if stream[1] is not None and stream[1].start() < limit]
            if not hits:
                result.extend(self._buffer[offset:limit])
                offset = limit
                break
            match = min(hits, key=lambda item: (item.start(), -(item.end(1) - item.start(1))))
            position, length = match.start(), match.end(1) - match.start(1)
            result.extend(self._buffer[offset:position])
            result.extend(self.marker)
            offset = position + length
            self.redacted = True
        self._buffer = self._buffer[offset:]
        if final:
            if self._trailing:
                fragment = max(
                    _trailing_prefix(
                        bytes(result).lower() if b"%" in pattern else bytes(result),
                        pattern.lower() if b"%" in pattern else pattern,
                    ) for pattern in self._patterns
                )
                if fragment:
                    del result[-fragment:]
                    result.extend(self.marker)
                    self.redacted = True
            self._closed = True
        return bytes(result)
