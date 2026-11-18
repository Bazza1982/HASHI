"""Bounded literal-search candidates from PAO history, without another index.

The final renderer-specific literal check remains in the existing FC. This
filter deliberately includes ambiguous HTML/entity/media rows so changing
display text cannot create false negatives. No search text is persisted.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata

MAX_SCAN_ROWS = 4000
MAX_SCAN_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_QUERY_CHARACTERS = 16 * 1024
MAX_CONFIGURABLE_QUERY_CHARACTERS = 1024 * 1024


def history_search_query_limit():
    raw = os.environ.get("HASHI_HISTORY_SEARCH_MAX_QUERY_CHARS")
    if raw is None:
        return DEFAULT_MAX_QUERY_CHARACTERS
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise ValueError("HASHI_HISTORY_SEARCH_MAX_QUERY_CHARS must be a positive integer") from None
    if not 1 <= limit <= MAX_CONFIGURABLE_QUERY_CHARACTERS:
        raise ValueError("HASHI_HISTORY_SEARCH_MAX_QUERY_CHARS is outside the supported range")
    return limit


def history_search_limits():
    limit = history_search_query_limit()
    # A JSON-escaped non-BMP character can require twelve wire bytes. Keep
    # room for the opaque cursor and envelope without truncating the query.
    return {"max_query_characters": limit, "max_request_bytes": limit * 12 + 4096,
            "max_scan_rows": MAX_SCAN_ROWS, "max_scan_bytes": MAX_SCAN_BYTES}


def literal_candidate(query):
    if (not isinstance(query, str) or not query
            or any(ord(char) < 9 or 10 < ord(char) < 13 or 13 < ord(char) < 32
                   or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in query)):
        raise ValueError("history search query is invalid")
    limit = history_search_query_limit()
    if len(query) > limit:
        raise ValueError(f"history search query exceeds configured limit of {limit} characters")
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    # New Unicode assignments can differ across Python/renderer versions.
    # Unknown characters are retained for the renderer, never ruled out here.
    unknown_query = any(unicodedata.category(char) == "Cn" for char in query)

    def candidate(row):
        values = [row["text"] or "", row["display_text"] or ""]
        if unknown_query or any(pattern.search(value) for value in values):
            return True
        if any("<" in value or "&" in value for value in values):
            return True  # markup/entities may join or decode display characters
        if any(not value.isascii() and any(unicodedata.category(char) == "Cn" for char in value)
               for value in values):
            return True
        if row["display_text"] is not None:
            return False  # canonical display text overrides attachment captions
        try:
            parts = json.loads(row["content_json"] or "[]")
        except (ValueError, TypeError):
            return True
        return (not isinstance(parts, list)
                or any(not isinstance(part, dict) or part.get("type") != "text" for part in parts))

    return candidate


def collect_candidates(rows, *, query, limit):
    accepts = literal_candidate(query)
    result, last, scanned, size, more = [], None, 0, 0, False
    for row in rows:
        if scanned >= MAX_SCAN_ROWS or size >= MAX_SCAN_BYTES or len(result) >= limit:
            more = True
            break  # Lookahead is not consumed: the cursor uses the last examined row.
        last = row
        scanned += 1
        size += sum(len(str(row[key] or "").encode("utf-8")) for key in ("text", "display_text", "content_json", "message_context_json"))
        if accepts(row):
            result.append(row)
    return result, last, more, scanned
