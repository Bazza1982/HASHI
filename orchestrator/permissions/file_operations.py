"""Fixed, bounded operations over inherited native file/directory handles."""
from __future__ import annotations

import fnmatch
from hashlib import sha256
import json
import ntpath
import re

from orchestrator.permissions.windows_file_binding import (
    BoundWindowsDirectory, BoundWindowsRead, FileBindingError,
)

FILE_TOOLS = frozenset({"file_read", "file_write", "file_list", "file_search", "apply_patch"})


def strict_patch(original, patch):
    """Apply one exact unified diff in memory, before any file is modified."""
    if not isinstance(patch, str) or len(patch.encode("utf-8")) > 1024 * 1024:
        raise ValueError("invalid or oversized patch")
    crlf = "\r\n" in original
    if crlf and "\n" in original.replace("\r\n", ""):
        raise ValueError("mixed file line endings require an explicit full-file write")
    original, patch = original.replace("\r\n", "\n"), patch.replace("\r\n", "\n")
    rows = patch.splitlines(keepends=True)
    if len(rows) < 3 or not rows[0].startswith("--- ") or not rows[1].startswith("+++ "):
        raise ValueError("one standard unified diff is required")
    source, output, position, number = original.splitlines(keepends=True), [], 0, 2
    hunks = 0
    while number < len(rows):
        match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\r\n]*[\r\n]*", rows[number])
        if match is None:
            raise ValueError("invalid unified diff hunk")
        start, old_count = int(match[1]), int(match[2] or 1)
        new_start, new_count = int(match[3]), int(match[4] or 1)
        index = start - 1 if old_count else start
        if index < position or index > len(source):
            raise ValueError("patch hunk is outside the original file")
        output.extend(source[position:index])
        if (new_start - 1 if new_count else new_start) != len(output):
            raise ValueError("patch output range is inconsistent")
        position, removed, added, number = index, 0, 0, number + 1
        while number < len(rows) and not rows[number].startswith("@@ "):
            line = rows[number]
            if line.startswith(("--- ", "+++ ")) or not line or line[0] not in " +-":
                raise ValueError("only one file and exact context lines are allowed")
            number += 1
            text = line[1:]
            if number < len(rows) and rows[number].startswith("\\ No newline at end of file"):
                text = text.rstrip("\r\n")
                number += 1
            if line[0] in " -":
                if position >= len(source) or source[position] != text:
                    raise ValueError("patch context does not match the held file")
                position += 1
                removed += 1
            if line[0] in " +":
                output.append(text)
                added += 1
        if removed != old_count or added != new_count:
            raise ValueError("patch hunk count is inconsistent")
        hunks += 1
    if not hunks:
        raise ValueError("patch contains no hunks")
    output.extend(source[position:])
    result = "".join(output)
    result = (result.replace("\n", "\r\n") if crlf else result).encode("utf-8")
    if len(result) > 1024 * 1024:
        raise ValueError("patched file exceeds the supervised limit")
    return result


def search_options(arguments):
    allowed = {"query", "roots", "mode", "match", "profile", "include_hidden",
               "case_sensitive", "include", "exclude", "max_results", "context_chars", "cursor", "options"}
    if set(arguments) - allowed:
        raise ValueError("unsupported managed search argument")
    query, mode, match = arguments.get("query"), arguments.get("mode", "path"), arguments.get("match", "literal")
    if not isinstance(query, str) or not query.strip() or len(query) > 1024:
        raise ValueError("a bounded nonempty query is required")
    if mode not in {"path", "content"} or match not in {"literal", "glob"} or (mode == "content" and match != "literal"):
        raise ValueError("use literal content or literal/glob path search")
    if arguments.get("profile", "project") not in {"project", "expanded"}:
        raise ValueError("invalid search profile")
    options = arguments.get("options") or {}
    if (not isinstance(options, dict) or set(options) - {"follow_links", "cross_filesystems"}
            or any(value for value in options.values())):
        raise ValueError("managed search does not follow links or cross filesystems")
    for name in ("include_hidden", "case_sensitive"):
        if type(arguments.get(name, False)) is not bool:
            raise ValueError("search flags must be boolean")
    for name in ("include", "exclude"):
        value = arguments.get(name, [])
        if not isinstance(value, list) or len(value) > 32 or any(not isinstance(item, str) or len(item) > 512 for item in value):
            raise ValueError("invalid managed path filter")
    maximum, context = arguments.get("max_results", 50), arguments.get("context_chars", 160)
    cursor, cursor_line = arguments.get("cursor", 0), 0
    fingerprint = sha256(json.dumps({key: value for key, value in arguments.items() if key != "cursor"},
                                   sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
    if isinstance(cursor, str):
        parts = cursor.split(":")
        if (len(parts) != 4 or parts[:2] != ["held-v1", fingerprint]
                or not parts[2].isdecimal() or not parts[3].isdecimal()):
            raise ValueError("managed search continuation belongs to another query")
        cursor, cursor_line = int(parts[2]), int(parts[3])
    if (type(maximum) is not int or not 1 <= maximum <= 200 or type(context) is not int or not 0 <= context <= 2000
            or type(cursor) is not int or not 0 <= cursor <= 100000 or not 0 <= cursor_line <= 1000000):
        raise ValueError("invalid search bound or continuation")
    return {**arguments, "query": query, "mode": mode, "match": match, "max_results": maximum,
            "context_chars": context, "cursor": cursor, "cursor_line": cursor_line, "fingerprint": fingerprint}


def _protected(path, paths):
    value = ntpath.normcase(ntpath.normpath(path))
    return any(value == parent or value.startswith(parent.rstrip("\\") + "\\")
               for parent in (ntpath.normcase(ntpath.normpath(item)) for item in paths))


def list_held(bound, arguments, protected):
    limit = arguments.get("limit", 200)
    if set(arguments) - {"path", "limit"} or type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("managed directory listing supports path and limit 1..500")
    result, complete = [], True
    for item in bound.entries():
        if _protected(ntpath.join(arguments["path"], item["name"]), protected):
            continue
        if len(result) >= limit:
            complete = False
            break
        result.append(item)
    return json.dumps({"entries": result, "coverage_complete": complete}, ensure_ascii=False)


def search_held(bindings, roots, arguments, protected):
    options = search_options(arguments)
    result, skipped, visited, total_bytes, complete = [], 0, 0, 0, True
    query = options["query"] if options.get("case_sensitive") else options["query"].casefold()
    project_skip = {"node_modules", ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
    queue = list(zip(bindings, roots))
    next_cursor, next_line = options["cursor"], options["cursor_line"]
    opened = []
    envelope_bytes = 0
    def append_match(row):
        nonlocal envelope_bytes
        size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 2
        if envelope_bytes + size > 128000:
            return False
        envelope_bytes += size
        result.append(row)
        return True
    try:
        while queue:
            bound, path = queue.pop(0)
            try:
                for entry in bound.entries():
                    name, target = entry["name"], ntpath.join(path, entry["name"])
                    if (entry["reparse"] or _protected(target, protected)
                            or (entry["hidden"] and not options.get("include_hidden"))
                            or (options.get("profile", "project") == "project" and name in project_skip)):
                        continue
                    visited += 1
                    if visited > options["cursor"] + 5000:
                        next_cursor, next_line = visited - 1, 0
                        complete = False
                        break
                    if entry["directory"]:
                        try:
                            handle = bound._api.open_child(bound._handles[-1], name, directory=True)
                            child = BoundWindowsDirectory(bound._api, [handle])
                            opened.append(child)
                            bound._api.check(handle, target, directory=True)
                            queue.append((child, target))
                        except FileBindingError:
                            skipped += 1
                        continue
                    if visited < options["cursor"] or (visited == options["cursor"] and not options["cursor_line"] and options["mode"] == "path"):
                        continue
                    if any(fnmatch.fnmatch(target, pattern) for pattern in options.get("exclude", [])):
                        continue
                    if options.get("include") and not any(fnmatch.fnmatch(target, pattern) for pattern in options["include"]):
                        continue
                    next_cursor, next_line = visited, 0
                    haystack = target if options.get("case_sensitive") else target.casefold()
                    if options["mode"] == "path":
                        matched = fnmatch.fnmatchcase(haystack, query) if options["match"] == "glob" else query in haystack
                        if matched:
                            if not append_match({"path": target}):
                                next_cursor, next_line, complete = visited - 1, 0, False
                                break
                    else:
                        # Every file is opened relative to an already held
                        # parent and checked for reparse/hard-link substitution.
                        if entry["size"] > 1024 * 1024 or total_bytes + entry["size"] > 16 * 1024 * 1024:
                            skipped += 1
                            continue
                        child = None
                        try:
                            handle = bound._api.open_child(bound._handles[-1], name, directory=False)
                            child = BoundWindowsRead(bound._api, [handle])
                            opened.append(child)
                            bound._api.check(handle, target, directory=False)
                            data = child.read(max_bytes=1024 * 1024)
                            total_bytes += len(data)
                            text = data.decode("utf-8", errors="strict")
                            if "\x00" in text:
                                continue
                            for number, line in enumerate(text.splitlines(), 1):
                                if visited == options["cursor"] and number <= options["cursor_line"]:
                                    continue
                                candidate = line if options.get("case_sensitive") else line.casefold()
                                at = candidate.find(query)
                                if at >= 0:
                                    width = options["context_chars"]
                                    if not append_match({"path": target, "line": number,
                                                         "text": line[max(0, at-width):at+len(query)+width]}):
                                        next_cursor, next_line, complete = visited, number - 1, False
                                        break
                                    if len(result) >= options["max_results"]:
                                        next_cursor, next_line = visited, number
                                        break
                        except (FileBindingError, UnicodeError):
                            skipped += 1
                        finally:
                            if child is not None:
                                child.close()
                    if not complete or len(result) >= options["max_results"]:
                        complete = False
                        break
            except FileBindingError:
                skipped += 1
            if not complete:
                break
        return json.dumps({"matches": result, "coverage_complete": complete and not skipped,
                           "files_or_directories_observed": visited, "bytes_read": total_bytes,
                           "skipped": skipped, "cursor": None if complete else (
                               f"held-v1:{options['fingerprint']}:{next_cursor}:{next_line}"),
                           "continuation_note": "Continuation rescans the current tree; changed files may appear again."},
                          ensure_ascii=False)
    finally:
        for bound in reversed(opened):
            bound.close()
