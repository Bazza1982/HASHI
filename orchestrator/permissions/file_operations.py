"""Fixed, bounded operations over inherited native file/directory handles."""
from __future__ import annotations

import fnmatch
from hashlib import sha256
import json
import ntpath
import os
import re

from orchestrator.permissions.windows_file_binding import (
    BoundWindowsDirectory, BoundWindowsRead, FileBindingError,
)

FILE_TOOLS = frozenset({"file_read", "file_write", "file_list", "file_search", "apply_patch"})


def strict_patch(original, patch):
    """Apply one exact unified diff in memory, before any file is modified."""
    if not isinstance(patch, str) or len(patch.encode("utf-8")) > 1024 * 1024:
        raise ValueError("invalid or oversized patch")

    # Match the textual content of each line while retaining its original
    # terminator. Unified diffs normally use LF even for CRLF/CR source files.
    def ending(line):
        if line.endswith("\r\n"):
            return "\r\n"
        if line.endswith(("\n", "\r")):
            return line[-1]
        return ""

    # splitlines() also treats Unicode separators as newlines; unified diffs
    # use only CR, LF and CRLF as file line terminators.
    raw_source = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", original)
    source = [
        line[:-len(end)] + "\n" if (end := ending(line)) else line
        for line in raw_source
    ]
    default_ending = next((value for line in raw_source if (value := ending(line))), "\n")
    patch = patch.replace("\r\n", "\n")
    rows = re.findall(r"[^\n]*\n|[^\n]+$", patch)
    if len(rows) < 3 or not rows[0].startswith("--- ") or not rows[1].startswith("+++ "):
        raise ValueError("one standard unified diff is required")
    output, position, number = [], 0, 2
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
        output.extend(raw_source[position:index])
        if (new_start - 1 if new_count else new_start) != len(output):
            raise ValueError("patch output range is inconsistent")
        position, removed, added, number = index, 0, 0, number + 1
        removed_endings, removed_used = [], 0
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
                original_line = raw_source[position]
                position += 1
                removed += 1
                if line[0] == "-":
                    removed_endings.append(ending(original_line))
                else:
                    output.append(original_line)
                    added += 1
                    removed_endings, removed_used = [], 0
            else:
                if text.endswith("\n"):
                    if removed_used < len(removed_endings):
                        chosen = removed_endings[removed_used]
                        removed_used += 1
                    else:
                        chosen = ""
                    if not chosen and output:
                        chosen = ending(output[-1])
                    if not chosen and position < len(raw_source):
                        chosen = ending(raw_source[position])
                    output.append(text[:-1] + (chosen or default_ending))
                else:
                    output.append(text)
                added += 1
        if removed != old_count or added != new_count:
            raise ValueError("patch hunk count is inconsistent")
        hunks += 1
    if not hunks:
        raise ValueError("patch contains no hunks")
    output.extend(raw_source[position:])
    result = "".join(output).encode("utf-8")
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
            or any(type(value) is not bool for value in options.values())):
        raise ValueError("managed search link options must be boolean")
    for name in ("include_hidden", "case_sensitive"):
        if type(arguments.get(name, False)) is not bool:
            raise ValueError("search flags must be boolean")
    for name in ("include", "exclude"):
        value = arguments.get(name, [])
        if not isinstance(value, list) or len(value) > 32 or any(not isinstance(item, str) or len(item) > 512 for item in value):
            raise ValueError("invalid managed path filter")
    maximum, context = arguments.get("max_results", 50), arguments.get("context_chars", 160)
    cursor = arguments.get("cursor")
    if cursor == 0:
        cursor = None
    if (type(maximum) is not int or not 1 <= maximum <= 200
            or type(context) is not int or not 0 <= context <= 2000
            or (cursor is not None and (not isinstance(cursor, str) or len(cursor) > 262144))):
        raise ValueError("invalid search bound or continuation")
    return {**arguments, "query": query, "mode": mode, "match": match,
            "max_results": maximum, "context_chars": context, "cursor": cursor}


def _protected(path, paths):
    value = ntpath.normcase(ntpath.normpath(path))
    return any(value == parent or value.startswith(parent.rstrip("\\") + "\\")
               for parent in (ntpath.normcase(ntpath.normpath(item)) for item in paths))


def listing_options(arguments):
    allowed = {"path", "limit", "max_results", "pattern", "include_hidden", "recursive", "cursor"}
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise ValueError("unsupported managed directory listing arguments")
    path = arguments.get("path")
    limit = arguments.get("max_results", arguments.get("limit", 200))
    pattern = arguments.get("pattern", "*")
    hidden = arguments.get("include_hidden", True)
    recursive = arguments.get("recursive", False)
    cursor = arguments.get("cursor")
    if (not isinstance(path, str) or not path or type(limit) is not int or not 1 <= limit <= 500
            or ("limit" in arguments and "max_results" in arguments and arguments["limit"] != limit)
            or not isinstance(pattern, str) or not pattern or len(pattern) > 512
            or type(hidden) is not bool or type(recursive) is not bool or recursive
            or (cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096))):
        raise ValueError("invalid managed directory listing; use file_search for recursive discovery")
    return {"path": path, "limit": limit, "pattern": pattern, "include_hidden": hidden, "cursor": cursor}


def list_held(bound, arguments, protected, *, cursor_key=None, scope_binding=""):
    """Page the held directory with bounded memory and a Run-bound revision."""
    import base64
    import heapq
    import hmac

    options = listing_options(arguments)
    path = options["path"]
    query = sha256(json.dumps({
        "path": ntpath.normcase(ntpath.normpath(path)),
        "pattern": options["pattern"], "include_hidden": options["include_hidden"],
        "scope": str(scope_binding),
        "protected": sorted(ntpath.normcase(ntpath.normpath(item)) for item in protected),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def restart(reason):
        return json.dumps({"entries": [], "coverage_complete": False, "can_continue": False,
                           "next_cursor": None, "cursor": None, "restart_required": True,
                           "stop_reason": reason}, ensure_ascii=False)

    marker = revision = identity = None
    supplied = options["cursor"]
    if supplied:
        if not isinstance(cursor_key, bytes) or len(cursor_key) < 32 or not scope_binding:
            return restart("continuation_unavailable")
        try:
            version, encoded, signature = supplied.split(".")
            expected = hmac.new(cursor_key, encoded.encode("ascii"), "sha256").hexdigest()
            if version != "held-list-v1" or not hmac.compare_digest(signature, expected):
                return restart("invalid_cursor")
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(base64.b64decode(encoded + padding, altchars=b"-_", validate=True))
            if (not isinstance(payload, dict) or set(payload) != {"query", "identity", "revision", "after"}
                    or payload["query"] != query or not isinstance(payload["after"], str)
                    or not isinstance(payload["revision"], str) or not isinstance(payload["identity"], dict)):
                return restart("invalid_cursor")
            marker, revision, identity = payload["after"], payload["revision"], payload["identity"]
        except (ValueError, TypeError, UnicodeError):
            return restart("invalid_cursor")

    before = bound.identity()
    if identity is not None and identity != before:
        return restart("directory_changed")
    count = xor_digest = sum_digest = 0
    modulus = 1 << 256
    after_key = (marker.casefold(), marker) if marker is not None else None

    def eligible():
        nonlocal count, xor_digest, sum_digest
        for item in bound.entries():
            name = item["name"]
            if (item.get("reparse") or _protected(ntpath.join(path, name), protected)
                    or (not options["include_hidden"] and item.get("hidden"))
                    or not fnmatch.fnmatchcase(name.casefold(), options["pattern"].casefold())):
                continue
            digest = int.from_bytes(sha256(json.dumps(item, ensure_ascii=False, sort_keys=True,
                                                     separators=(",", ":")).encode("utf-8")).digest(), "big")
            count += 1
            xor_digest ^= digest
            sum_digest = (sum_digest + digest) % modulus
            if after_key is None or (name.casefold(), name) > after_key:
                yield item

    selected = heapq.nsmallest(options["limit"] + 1, eligible(),
                               key=lambda item: (item["name"].casefold(), item["name"]))
    after = bound.identity()
    observed_revision = sha256(f"{count}:{xor_digest:064x}:{sum_digest:064x}".encode("ascii")).hexdigest()
    if before != after or (revision is not None and revision != observed_revision):
        return restart("directory_changed")
    complete = len(selected) <= options["limit"]
    entries = selected[:options["limit"]]
    next_cursor = None
    if not complete:
        if not isinstance(cursor_key, bytes) or len(cursor_key) < 32 or not scope_binding:
            return restart("continuation_unavailable")
        payload = json.dumps({"query": query, "identity": before, "revision": observed_revision,
                              "after": entries[-1]["name"]}, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        signature = hmac.new(cursor_key, encoded.encode("ascii"), "sha256").hexdigest()
        next_cursor = f"held-list-v1.{encoded}.{signature}"
    return json.dumps({"entries": entries, "coverage_complete": complete, "can_continue": not complete,
                       "next_cursor": next_cursor, "cursor": next_cursor, "restart_required": False},
                      ensure_ascii=False)


def _match_span(text, query, case_sensitive):
    """Find a literal and map Unicode casefold expansions back to source text."""
    candidate = text if case_sensitive else text.casefold()
    at = candidate.find(query)
    if at < 0:
        return None
    if case_sensitive:
        return at, at + len(query)
    first = last = None
    position = 0
    end = at + len(query)
    for index, char in enumerate(text):
        following = position + len(char.casefold())
        if first is None and following > at:
            first = index
        if following >= end:
            last = index + 1
            break
        position = following
    return (first, last) if first is not None and last is not None else None


def _line_part(segment):
    """Return content, whether a line ended, and whether a trailing CR may pair."""
    if segment.endswith("\r\n"):
        return segment[:-2], True, False
    if segment and segment[-1] in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029":
        return segment[:-1], True, segment[-1] == "\r"
    return segment, False, False


class _SearchLinkError(FileBindingError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _search_target_scope(target, roots, protected):
    """Select an already held root for a normalized target."""
    drive, tail = ntpath.splitdrive(target)
    if not re.fullmatch(r"[A-Za-z]:", drive) or not tail.startswith("\\"):
        raise _SearchLinkError("link_target_outside_scope")
    if _protected(target, protected):
        raise _SearchLinkError("link_target_protected")
    normalized = ntpath.normcase(target)
    candidates = []
    for index, root in enumerate(roots):
        root_normalized = ntpath.normcase(ntpath.normpath(root))
        if normalized == root_normalized or normalized.startswith(root_normalized.rstrip("\\") + "\\"):
            candidates.append((len(root_normalized), index))
    if not candidates:
        raise _SearchLinkError("link_target_outside_scope")
    return max(candidates)[1]


def _search_link_path(path, roots, protected):
    """Resolve one held link lexically; target access still requires a held root."""
    try:
        value = os.readlink(path)
    except OSError as error:
        raise _SearchLinkError("link_unavailable") from error
    if value.startswith("\\\\?\\"):
        value = value[4:]
    if ntpath.isabs(value):
        target = ntpath.normpath(value)
    else:
        drive, _ = ntpath.splitdrive(value)
        if drive:
            raise _SearchLinkError("link_target_outside_scope")
        target = ntpath.normpath(ntpath.join(ntpath.dirname(path), value))
    return target, _search_target_scope(target, roots, protected)


def _open_search_link_target(bindings, roots, index, target, protected, *, directory,
                             first_link, source_volume, cross_filesystems):
    """Resolve each link under held roots while holding every traversed object."""
    api = bindings[index]._api
    handles, chain = [], []
    seen = {(first_link["volume"], first_link["file_id"])}
    try:
        while True:
            root, binding = ntpath.normpath(roots[index]), bindings[index]
            relative = ntpath.relpath(target, root)
            names = [] if relative == "." else relative.split("\\")
            if (any(not name or name in {".", ".."} or name.endswith((" ", "."))
                    or any(char in name for char in ':/*?"<>|' + chr(0) + chr(92)) for name in names)):
                raise _SearchLinkError("link_target_unavailable")
            handle = api.open_child(binding._handles[-1], "", directory=True)
            handles.append(handle)
            api.check(handle, root, directory=True)
            root_identity = BoundWindowsDirectory(api, [handle]).identity()
            if not cross_filesystems and root_identity["volume"] != source_volume:
                raise _SearchLinkError("link_crosses_filesystem")
            current = root
            if not names and not directory:
                raise _SearchLinkError("link_target_unavailable")
            redirected = False
            for number, name in enumerate(names):
                final = number == len(names) - 1
                is_directory = directory if final else True
                current = ntpath.join(current, name)
                handle = api.open_child(handles[-1], name, directory=is_directory,
                                        share=1 if final and not directory else 3)
                handles.append(handle)
                if api.is_reparse(handle):
                    if not api.kernel.CloseHandle(handle):
                        raise FileBindingError("link handle cleanup could not be confirmed")
                    handles.pop()
                    handle = api.open_child(handles[-1], name, directory=is_directory, share=1)
                    handles.append(handle)
                    api.check(handle, current, directory=is_directory, reparse=True)
                    cls = BoundWindowsDirectory if is_directory else BoundWindowsRead
                    identity = cls(api, [handle]).identity()
                    link_key = (identity["volume"], identity["file_id"])
                    if link_key in seen:
                        raise _SearchLinkError("link_cycle")
                    seen.add(link_key)
                    resolved, _ = _search_link_path(current, roots, protected)
                    target = ntpath.normpath(ntpath.join(resolved, *names[number + 1:]))
                    index = _search_target_scope(target, roots, protected)
                    chain.append({"link": identity,
                                  "path": ntpath.normcase(ntpath.normpath(current)),
                                  "resolved": ntpath.normcase(ntpath.normpath(target))})
                    redirected = True
                    break
                api.check(handle, current, directory=is_directory)
            if redirected:
                continue
            cls = BoundWindowsDirectory if directory else BoundWindowsRead
            return cls(api, handles), target, chain
    except Exception:
        if handles:
            BoundWindowsDirectory(api, handles).close()
        raise


def search_held(bindings, roots, arguments, protected, *, cursor_key=None, scope_binding=""):
    """Page a held, scoped search; never treat an unsearched large file as complete."""
    import base64
    import hmac

    options = search_options(arguments)
    if not roots or len(bindings) != len(roots):
        raise ValueError("held search roots and handles do not match")
    query = options["query"] if options.get("case_sensitive") else options["query"].casefold()
    budget = 16 * 1024 * 1024
    chunk_size = 65536
    project_skip = {"node_modules", ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache"}
    query_data = {
        "arguments": {key: value for key, value in arguments.items() if key not in {"cursor", "max_results"}},
        "roots": [ntpath.normcase(ntpath.normpath(path)) for path in roots],
        "protected": sorted(ntpath.normcase(ntpath.normpath(path)) for path in protected),
        "scope": str(scope_binding),
    }
    query_digest = sha256(json.dumps(query_data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()

    def stopped(reason):
        return json.dumps({"matches": [], "coverage_complete": False, "can_continue": False,
                           "cursor": None, "restart_required": True, "stop_reason": reason,
                           "skipped": 0, "bytes_read": 0}, ensure_ascii=False)

    saved = None
    if options["cursor"]:
        if not isinstance(cursor_key, bytes) or len(cursor_key) < 32 or not scope_binding:
            return stopped("continuation_unavailable")
        try:
            version, encoded, signature = options["cursor"].split(".")
            expected = hmac.new(cursor_key, encoded.encode("ascii"), "sha256").hexdigest()
            if version != "held-search-v2" or not hmac.compare_digest(signature, expected):
                return stopped("invalid_cursor")
            payload = base64.b64decode(encoded + "=" * (-len(encoded) % 4),
                                       altchars=b"-_", validate=True)
            saved = json.loads(payload)
            if (not isinstance(saved, dict) or saved.get("query") != query_digest
                    or type(saved.get("entry")) is not int or saved["entry"] < 0
                    or not isinstance(saved.get("prefix"), str)
                    or saved.get("kind") not in {"after", "file"}
                    or type(saved.get("skipped")) is not int or saved["skipped"] < 0):
                return stopped("invalid_cursor")
            if saved["kind"] == "file":
                if (not isinstance(saved.get("path"), str) or not isinstance(saved.get("identity"), dict)
                        or type(saved.get("offset")) is not int or saved["offset"] < 0
                        or type(saved.get("line")) is not int or saved["line"] < 1
                        or type(saved.get("matched")) is not bool
                        or type(saved.get("after_cr")) is not bool):
                    return stopped("invalid_cursor")
        except (ValueError, TypeError, UnicodeError, OverflowError):
            return stopped("invalid_cursor")

    result, skipped, visited, total_bytes, envelope_bytes = [], (saved["skipped"] if saved else 0), 0, 0, 0
    opened = []
    skipped_details = []
    skipped_detail_bytes = 0

    def record_skip(path, reason):
        nonlocal skipped, skipped_detail_bytes
        skipped += 1
        detail = {"path": path, "reason": reason}
        size = len(json.dumps(detail, ensure_ascii=False).encode("utf-8")) + 2
        if len(skipped_details) < 64 and skipped_detail_bytes + size <= 32000:
            skipped_details.append(detail)
            skipped_detail_bytes += size

    queue = list(zip(bindings, roots))
    prefix = sha256()
    try:
        for bound, path in queue:
            prefix.update(json.dumps([ntpath.normcase(ntpath.normpath(path)), bound.identity()],
                                     sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except FileBindingError:
        return stopped("root_unavailable")
    resume_entry = saved["entry"] if saved else 0
    resumed = saved is None or resume_entry == 0
    if saved and resume_entry == 0 and saved["prefix"] != prefix.hexdigest():
        return stopped("scope_changed")

    def page(kind, *, path="", identity=None, offset=0, line=1, matched=False, after_cr=False,
             reason="page_limit", at_entry=None, prefix_digest=None):
        if not isinstance(cursor_key, bytes) or len(cursor_key) < 32 or not scope_binding:
            return json.dumps({"matches": result, "coverage_complete": False, "can_continue": False,
                               "cursor": None, "restart_required": False,
                               "stop_reason": "continuation_unavailable", "skipped": skipped,
                               "skipped_details": skipped_details,
                               "skipped_details_truncated": skipped > len(skipped_details),
                               "files_or_directories_observed": visited, "bytes_read": total_bytes},
                              ensure_ascii=False)
        value = {"query": query_digest, "entry": visited if at_entry is None else at_entry,
                 "prefix": prefix.hexdigest() if prefix_digest is None else prefix_digest,
                 "kind": kind, "skipped": skipped}
        if kind == "file":
            value.update(path=path, identity=identity, offset=offset, line=line,
                         matched=matched, after_cr=after_cr)
        encoded = base64.urlsafe_b64encode(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                      separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
        signature = hmac.new(cursor_key, encoded.encode("ascii"), "sha256").hexdigest()
        cursor = f"held-search-v2.{encoded}.{signature}"
        return json.dumps({"matches": result, "coverage_complete": False, "can_continue": True,
                           "cursor": cursor, "restart_required": False, "stop_reason": reason,
                           "files_or_directories_observed": visited, "bytes_read": total_bytes,
                           "skipped": skipped, "skipped_details": skipped_details,
                           "skipped_details_truncated": skipped > len(skipped_details)}, ensure_ascii=False)

    def append_match(row):
        nonlocal envelope_bytes
        size = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 2
        if envelope_bytes + size > 128000:
            return False
        envelope_bytes += size
        result.append(row)
        return True

    def directory_changed(bound, before):
        return bound.identity() != before

    def release(items):
        for item in reversed(items):
            item.close()
            opened.remove(item)

    link_options = options.get("options") or {}
    follow_links = link_options.get("follow_links", False)
    cross_filesystems = link_options.get("cross_filesystems", False)

    try:
        roots_pending = iter(queue)
        stack = []
        while True:
            if not stack:
                next_root = next(roots_pending, None)
                if next_root is None:
                    break
                bound, path = next_root
                try:
                    before_dir = bound.identity()
                    stack.append((bound, path, path, before_dir, iter(bound.entries(include_metadata=True)), []))
                except FileBindingError:
                    if not resumed:
                        return stopped("scope_changed")
                    record_skip(path, "directory_unavailable")
                    continue
            bound, path, actual_path, before_dir, entries, owned = stack[-1]
            try:
                entry = next(entries)
            except StopIteration:
                if directory_changed(bound, before_dir):
                    return stopped("directory_changed")
                stack.pop()
                if owned:
                    release(owned)
                continue
            except FileBindingError:
                if not resumed:
                    return stopped("scope_changed")
                record_skip(path, "directory_unavailable")
                stack.pop()
                if owned:
                    release(owned)
                continue
            name, target = entry["name"], ntpath.join(path, entry["name"])
            actual_target = ntpath.join(actual_path, name)
            if ((entry["reparse"] and not follow_links) or _protected(target, protected)
                    or _protected(actual_target, protected)
                    or (entry["hidden"] and not options.get("include_hidden"))
                    or (options.get("profile", "project") == "project" and name in project_skip)):
                continue
            if resumed and visited >= resume_entry + 5000:
                if directory_changed(bound, before_dir):
                    return stopped("directory_changed")
                return page("after", reason="entry_budget")
            previous_prefix = prefix.hexdigest()
            visited += 1
            link_owned, link_child, link_info, link_error = [], None, None, None
            if entry["reparse"]:
                try:
                    handle = bound._api.open_child(bound._handles[-1], name,
                                                   directory=entry["directory"], share=1)
                    cls = BoundWindowsDirectory if entry["directory"] else BoundWindowsRead
                    held_link = cls(bound._api, [handle])
                    link_owned.append(held_link)
                    opened.append(held_link)
                    bound._api.check(handle, actual_target, directory=entry["directory"], reparse=True)
                    link_identity = held_link.identity()
                    resolved, root_index = _search_link_path(actual_target, roots, protected)
                    link_child, resolved, chain = _open_search_link_target(
                        bindings, roots, root_index, resolved, protected, directory=entry["directory"],
                        first_link=link_identity, source_volume=before_dir["volume"],
                        cross_filesystems=cross_filesystems)
                    link_owned.append(link_child)
                    opened.append(link_child)
                    target_identity = link_child.identity()
                    link_info = {"link": link_identity, "target": target_identity, "chain": chain,
                                 "resolved": ntpath.normcase(ntpath.normpath(resolved))}
                    if not cross_filesystems and target_identity["volume"] != before_dir["volume"]:
                        raise _SearchLinkError("link_crosses_filesystem")
                    if entry["directory"] and any(
                        (ancestor[3]["volume"], ancestor[3]["file_id"])
                            == (target_identity["volume"], target_identity["file_id"])
                        for ancestor in stack
                    ):
                        raise _SearchLinkError("link_cycle")
                    actual_target = resolved
                except _SearchLinkError as error:
                    link_error = error.reason
                except (FileBindingError, OSError):
                    link_error = "link_target_unavailable"
                if link_error:
                    link_info = {"error": link_error, "detail": link_info}
            prefix.update(json.dumps([ntpath.normcase(ntpath.normpath(target)), entry, link_info],
                                     ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8"))
            at_saved = bool(saved and visited == resume_entry)
            if at_saved:
                if prefix.hexdigest() != saved["prefix"]:
                    return stopped("scope_changed")
                resumed = True
            if link_error:
                if link_owned:
                    release(link_owned)
                if not saved or visited > resume_entry:
                    record_skip(target, link_error)
                continue
            if entry["directory"]:
                child = link_child
                owned_child = link_owned
                try:
                    if child is None:
                        handle = bound._api.open_child(bound._handles[-1], name, directory=True)
                        child = BoundWindowsDirectory(bound._api, [handle])
                        owned_child = [child]
                        opened.append(child)
                        bound._api.check(handle, actual_target, directory=True)
                    stack.append((child, target, actual_target, child.identity(),
                                  iter(child.entries(include_metadata=True)), owned_child))
                except FileBindingError:
                    if owned_child:
                        release(owned_child)
                    if saved and visited <= resume_entry:
                        return stopped("scope_changed")
                    record_skip(target, "directory_unavailable")
                continue
            if saved and (visited < resume_entry or (at_saved and saved["kind"] == "after")):
                if link_owned:
                    release(link_owned)
                continue
            if any(fnmatch.fnmatch(target, pattern) for pattern in options.get("exclude", [])):
                if link_owned:
                    release(link_owned)
                continue
            if options.get("include") and not any(fnmatch.fnmatch(target, pattern) for pattern in options["include"]):
                if link_owned:
                    release(link_owned)
                continue
            if options["mode"] == "path":
                haystack = target if options.get("case_sensitive") else target.casefold()
                matches = fnmatch.fnmatchcase(haystack, query) if options["match"] == "glob" else query in haystack
                if matches and not append_match({"path": target}):
                    return page("after", at_entry=visited - 1,
                                prefix_digest=previous_prefix, reason="result_budget")
                if len(result) >= options["max_results"]:
                    return page("after", reason="result_limit")
                if link_owned:
                    release(link_owned)
                continue

            child = link_child
            try:
                if child is None:
                    # A held read handle denies concurrent writes and deletion.
                    handle = bound._api.open_child(bound._handles[-1], name, directory=False, share=1)
                    child = BoundWindowsRead(bound._api, [handle])
                    bound._api.check(handle, actual_target, directory=False)
                identity = child.identity()
                if at_saved and saved["kind"] == "file":
                    if saved["path"] != target or saved["identity"] != identity:
                        return stopped("file_changed")
                    offset, line_number = saved["offset"], saved["line"]
                    matched, after_cr = saved["matched"], saved["after_cr"]
                    if offset > identity["size"]:
                        return stopped("file_changed")
                else:
                    offset, line_number, matched, after_cr = 0, 1, False, False
                tail = ""
                tail_limit = options["context_chars"] + len(query) + 4
                while offset < identity["size"]:
                    if total_bytes >= budget:
                        if child.identity() != identity:
                            return stopped("file_changed")
                        rewind = len(tail.encode("utf-8")) if not matched else 0
                        return page("file", path=target, identity=identity, offset=offset-rewind,
                                    line=line_number, matched=matched, after_cr=after_cr,
                                    reason="byte_budget")
                    data = child.read_at(offset=offset, max_bytes=min(chunk_size, budget-total_bytes))
                    total_bytes += len(data)
                    if not data:
                        return stopped("file_changed")
                    try:
                        decoded = data.decode("utf-8", errors="strict")
                    except UnicodeDecodeError as error:
                        if (error.reason == "unexpected end of data" and error.end == len(data)
                                and offset + len(data) < identity["size"]):
                            decoded = data[:error.start].decode("utf-8", errors="strict")
                        else:
                            raise
                    if not decoded:
                        if total_bytes >= budget:
                            rewind = len(tail.encode("utf-8")) if not matched else 0
                            return page("file", path=target, identity=identity, offset=offset-rewind,
                                        line=line_number, matched=matched, after_cr=after_cr,
                                        reason="byte_budget")
                        raise UnicodeError("invalid UTF-8")
                    if "\x00" in decoded:
                        record_skip(target, "binary_content")
                        break
                    for segment in decoded.splitlines(keepends=True):
                        original_segment = segment
                        if after_cr:
                            if segment.startswith("\n"):
                                segment = segment[1:]
                            after_cr = False
                        content, ended, trailing_cr = _line_part(segment)
                        combined = tail + content
                        span = None if matched else _match_span(combined, query, bool(options.get("case_sensitive")))
                        if span is not None:
                            first, last = span
                            width = options["context_chars"]
                            row = {"path": target, "line": line_number,
                                   "text": combined[max(0, first-width):last+width]}
                            if not append_match(row):
                                rewind = len(tail.encode("utf-8")) if not matched else 0
                                if child.identity() != identity:
                                    return stopped("file_changed")
                                return page("file", path=target, identity=identity, offset=offset-rewind,
                                            line=line_number, matched=matched, after_cr=after_cr,
                                            reason="result_budget")
                            matched = True
                        offset += len(original_segment.encode("utf-8"))
                        if ended:
                            line_number += 1
                            tail, matched, after_cr = "", False, trailing_cr
                        elif not matched:
                            tail = combined[-tail_limit:]
                        else:
                            tail = ""
                        if len(result) >= options["max_results"]:
                            if child.identity() != identity:
                                return stopped("file_changed")
                            return page("file", path=target, identity=identity, offset=offset,
                                        line=line_number, matched=matched, after_cr=after_cr,
                                        reason="result_limit")
                if child.identity() != identity:
                    return stopped("file_changed")
            except FileBindingError:
                if at_saved and saved["kind"] == "file":
                    return stopped("file_changed")
                record_skip(target, "file_unavailable")
            except UnicodeError:
                record_skip(target, "invalid_utf8")
            finally:
                if link_owned:
                    release(link_owned)
                elif child is not None:
                    child.close()
        if saved and not resumed:
            return stopped("scope_changed")
        return json.dumps({"matches": result, "coverage_complete": not skipped, "can_continue": False,
                           "cursor": None, "restart_required": False,
                           "stop_reason": "some_entries_skipped" if skipped else None,
                           "files_or_directories_observed": visited, "bytes_read": total_bytes,
                           "skipped": skipped, "skipped_details": skipped_details,
                           "skipped_details_truncated": skipped > len(skipped_details),
                           "continuation_note": "A changed traversed prefix requires a fresh search."},
                          ensure_ascii=False)
    finally:
        for bound in reversed(opened):
            bound.close()
