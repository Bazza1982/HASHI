"""Line-oriented, cancellable ripgrep scanning for authorized UTF-8 files."""
from __future__ import annotations

import codecs
import os
import re
import subprocess
from pathlib import Path
from typing import Callable


class RegexScanError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _command(binary: str, query: str, case_sensitive: bool) -> list[str]:
    args = [
        binary, "--no-config", "--color", "never", "--line-buffered",
        "--text", "--encoding", "utf-8", "--only-matching", "--line-number",
        "--byte-offset", "--max-columns", "2000", "--max-columns-preview",
    ]
    if not case_sensitive:
        args.append("--ignore-case")
    return [*args, "--", query]


def validate_regex(binary: str, query: str, case_sensitive: bool) -> None:
    result = subprocess.run(
        _command(binary, query, case_sensitive),
        input=b"", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode not in {0, 1}:
        message = result.stderr.decode("utf-8", "replace").strip()
        code = "invalid_regex" if result.returncode == 2 else "regex_backend_error"
        raise RegexScanError(code, message[:1200] or "Regex engine failed")


def validate_python_regex(query: str) -> None:
    """Reject explicit LF matches that cannot be satisfied by line-oriented scanning."""
    from re import _constants, _parser

    try:
        parsed = _parser.parse(query, 0)
        re.compile(query)
    except (re.error, OverflowError, RecursionError) as exc:
        raise RegexScanError("invalid_regex", str(exc)) from exc

    def requires_explicit_lf(rows) -> bool:
        for operation, value in rows:
            if operation is _constants.LITERAL and value == 10:
                return True
            if operation is _constants.SUBPATTERN and requires_explicit_lf(value[-1]):
                return True
            if operation is _constants.BRANCH and any(
                requires_explicit_lf(branch) for branch in value[1]
            ):
                return True
            if operation in {
                _constants.MAX_REPEAT, _constants.MIN_REPEAT, _constants.POSSESSIVE_REPEAT
            } and requires_explicit_lf(value[-1]):
                return True
            if operation is _constants.ASSERT and requires_explicit_lf(value[-1]):
                return True
            if operation is _constants.GROUPREF_EXISTS and any(
                requires_explicit_lf(branch) for branch in value[1:] if branch is not None
            ):
                return True
        return False

    try:
        contains_lf = requires_explicit_lf(parsed)
    except RecursionError as exc:
        raise RegexScanError("invalid_regex", "Regex nesting exceeds the parser capacity") from exc
    if contains_lf:
        raise RegexScanError(
            "invalid_regex",
            "Content regex searches single lines; explicit newline matches are unsupported",
        )


def _identity(stream) -> list[int]:
    stat = os.fstat(stream.fileno())
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _validate_source(source, *, max_line_bytes: int, progress: Callable | None) -> int:
    characters = 0
    line_bytes = 0
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    try:
        while chunk := source.read(64 * 1024):
            characters += len(decoder.decode(chunk))
            if max_line_bytes:
                segments = chunk.split(b"\n")
                line_bytes += len(segments[0])
                if line_bytes > max_line_bytes or any(
                    len(segment) > max_line_bytes for segment in segments[1:]
                ):
                    raise RegexScanError(
                        "regex_line_budget_exceeded",
                        f"A line exceeds the selected {max_line_bytes}-byte regex budget; "
                        "the file was not searched. Increase options.regex_max_line_bytes "
                        "or choose 0 for no separate line budget.",
                    )
                if len(segments) > 1:
                    line_bytes = len(segments[-1])
            if progress:
                progress(characters)
        characters += len(decoder.decode(b"", final=True))
    except UnicodeDecodeError as exc:
        raise RegexScanError("invalid_utf8", "Regex content search requires valid UTF-8") from exc
    source.seek(0)
    return characters


def scan_regex(
    path: Path, *, binary: str, query: str, case_sensitive: bool,
    max_results: int, context_chars: int, expected_signature: list[int],
    max_line_bytes: int = 0,
    state: dict | None = None, progress: Callable | None = None,
) -> dict:
    """Stream bounded match records; a page restart skips already returned matches.

    The file must be valid UTF-8. Ripgrep's line-oriented Rust regex engine
    determines supported syntax; multiline matches are not silently simulated.
    """
    previous = int((state or {}).get("match_index", 0))
    if previous < 0:
        raise RegexScanError("invalid_cursor", "Invalid regex continuation")
    with path.open("rb") as source:
        if _identity(source) != expected_signature:
            raise RegexScanError("file_changed", "File changed before regex scan")
        characters = _validate_source(source, max_line_bytes=max_line_bytes, progress=progress)
        with path.open("rb") as context:
            if _identity(context) != expected_signature:
                raise RegexScanError("file_changed", "File changed before regex context read")
            bom = context.read(3)
            bom_bytes = 3 if bom == b"\xef\xbb\xbf" else 0
            if not bom_bytes:
                context.seek(0)
            cursor_bytes = bom_bytes
            cursor_chars = 0
            preceding = ""
            context_decoder = codecs.getincrementaldecoder("utf-8")("strict")
            process = subprocess.Popen(
                _command(binary, query, case_sensitive),
                stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0,
            )
            matches = []
            index = 0
            exhausted = False
            error = b""
            try:
                assert process.stdout is not None
                while row := process.stdout.readline(65536):
                    if not row.endswith(b"\n"):
                        raise RegexScanError("regex_output_limit", "Regex output record exceeded its bounded preview")
                    index += 1
                    if index <= previous:
                        continue
                    pieces = row[:-1].split(b":", 2)
                    if len(pieces) != 3:
                        raise RegexScanError("regex_output_invalid", "Regex engine returned an invalid match")
                    try:
                        line = int(pieces[0])
                        target = int(pieces[1]) + bom_bytes
                    except ValueError as exc:
                        raise RegexScanError("regex_output_invalid", "Regex engine returned invalid offsets") from exc
                    if target < cursor_bytes:
                        raise RegexScanError("regex_output_invalid", "Regex engine returned unordered offsets")
                    while cursor_bytes < target:
                        chunk = context.read(min(64 * 1024, target - cursor_bytes))
                        if not chunk:
                            raise RegexScanError("file_changed", "File ended before regex match")
                        cursor_bytes += len(chunk)
                        decoded = context_decoder.decode(chunk)
                        cursor_chars += len(decoded)
                        if context_chars:
                            preceding = (preceding + decoded)[-context_chars:]
                    if context_decoder.getstate()[0]:
                        raise RegexScanError("regex_output_invalid", "Regex match did not begin on a UTF-8 boundary")
                    position = context.tell()
                    following = context.read(max(160, context_chars) * 4 + 4)
                    context.seek(position)
                    preview = following.decode("utf-8", "ignore")[:max(160, context_chars)]
                    matches.append({
                        "term": query,
                        "line": line,
                        "character_offset": cursor_chars,
                        "excerpt": preceding + preview,
                        "excerpt_scope": "bounded_from_match_start",
                    })
                    if len(matches) >= max_results:
                        break
                else:
                    exhausted = True
                if exhausted:
                    assert process.stderr is not None
                    error = process.stderr.read()
                    code = process.wait()
                    if code not in {0, 1}:
                        raise RegexScanError(
                            "regex_backend_error",
                            error.decode("utf-8", "replace").strip()[:1200] or "Regex engine failed",
                        )
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
    return {
        "matches": matches,
        "characters_read": characters,
        "encoding_warning": False,
        "continuation": None if exhausted else {"match_index": index},
        "coverage_complete": exhausted,
    }


def scan_python_regex(
    path: Path, *, query: str, case_sensitive: bool, max_results: int,
    context_chars: int, expected_signature: list[int], max_line_bytes: int = 0,
    state: dict | None = None, progress: Callable | None = None,
) -> dict:
    """Portable line-oriented fallback, cancellable with the owning scan worker."""
    previous = int((state or {}).get("match_index", 0))
    if previous < 0:
        raise RegexScanError("invalid_cursor", "Invalid regex continuation")
    try:
        pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
    except (re.error, OverflowError, RecursionError) as exc:
        raise RegexScanError("invalid_regex", str(exc)) from exc
    with path.open("rb") as source:
        if _identity(source) != expected_signature:
            raise RegexScanError("file_changed", "File changed before regex scan")
        characters = _validate_source(source, max_line_bytes=max_line_bytes, progress=progress)
    matches = []
    index = 0
    offset = 0
    previous_tail = ""
    with path.open("rb") as stream:
        if _identity(stream) != expected_signature:
            raise RegexScanError("file_changed", "File changed before regex context read")
        if stream.read(3) != b"\xef\xbb\xbf":
            stream.seek(0)
        for line_number, full_bytes in enumerate(stream, start=1):
            full_line = full_bytes.decode("utf-8", "strict")
            line = full_line[:-1] if full_line.endswith("\n") else full_line
            for found in pattern.finditer(line):
                index += 1
                if index <= previous:
                    continue
                start = found.start()
                left = line[max(0, start - context_chars):start]
                if context_chars and len(left) < context_chars:
                    left = (previous_tail + left)[-context_chars:]
                preview = line[start:start + max(160, context_chars)]
                matches.append({
                    "term": query,
                    "line": line_number,
                    "character_offset": offset + start,
                    "excerpt": left + preview,
                    "excerpt_scope": "bounded_from_match_start",
                })
                if len(matches) >= max_results:
                    return {
                        "matches": matches, "characters_read": characters,
                        "encoding_warning": False, "continuation": {"match_index": index},
                        "coverage_complete": False,
                    }
            offset += len(full_line)
            if context_chars:
                previous_tail = (full_line[-context_chars:] if len(full_line) >= context_chars
                                 else (previous_tail + full_line)[-context_chars:])
    return {
        "matches": matches, "characters_read": characters,
        "encoding_warning": False, "continuation": None, "coverage_complete": True,
    }
