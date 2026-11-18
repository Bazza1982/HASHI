"""Small-model-first daily and development tools for HASHI PAO Functions.

No authority, model, or Agent lifecycle is owned here. Every filesystem path is
resolved through the existing Tool Registry Workzone rules, and every nested
read or write re-uses the same canonical read-scope plus protected-path
projection as ``file_search`` / ``repo_discover`` (PR #170). Tool time budgets
bound individual searches/commands, not the HERV3 Turn.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tools.builtins import _resolve_path
from tools.repo_discover import MetadataReader, MetadataUnavailable, _git_dirs_for, _inspect

_SCAN_IGNORES = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"})
_MAX_INSPECT_BYTES = 8 * 1024 * 1024
# Streamed copy chunk; module-level so regression tests can shrink it to make
# cancellation deterministically interruptible.
_COPY_CHUNK_BYTES = 1 << 20
# How long a cancelled mutation call may keep its worker thread to settle
# cleanly before the cancellation is reported with unknown in-flight effects.
_CANCEL_SETTLE_SECONDS = 5.0


class _CancelledMutation(Exception):
    """Cooperative stop inside a threaded file mutation."""


def _json(**value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _error(code: str, message: str, *, next_step: str = "") -> str:
    return _json(ok=False, error={"code": code, "message": message}, next_step=next_step)


def _cancelled(copied: int, *, note: str = "") -> str:
    return _json(ok=False,
                 error={"code": "cancelled",
                        "message": "Tool call cancelled; no further writes will be made by this call."},
                 copied=copied, partial=True, next_step=note)


def _path(raw: str | None, roots: tuple[Path, ...], cwd: Path) -> Path:
    return _resolve_path(str(raw or "."), roots, cwd)


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if os.name == "nt" and hasattr(path, "is_junction") and path.is_junction():
            return True
    except OSError:
        return False
    return False


def _in_scope(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _protected_hit(path: Path, protected: tuple[Path, ...]) -> bool:
    return any(path == item or path.is_relative_to(item) for item in protected)


# ---------------------------------------------------------------------------
# project_locate — folder names plus authorised Git metadata only
# ---------------------------------------------------------------------------

def _project_locate(args: dict, read_roots: tuple[Path, ...],
                    protected_read_paths: tuple[str, ...], cwd: Path) -> str:
    needle = str(args.get("name") or "").strip().casefold()
    if not needle:
        return _error("missing_name", "Give a project or repository name.")
    root = _path(args.get("root"), read_roots, cwd)
    if not root.is_dir():
        return _error("not_directory", f"Search root is not a directory: {root}")
    depth_limit = max(0, min(4, int(args.get("max_depth", 2))))
    result_limit = max(1, min(20, int(args.get("limit", 8))))
    protected = tuple(Path(item).resolve() for item in protected_read_paths)
    reader = MetadataReader(read_roots, protected_read_paths, cwd)
    queue: list[tuple[Path, int]] = [(root, 0)]
    visited, matches = 0, []
    deadline = time.monotonic() + 15.0
    partial_reason = ""
    while queue:
        if visited >= 3000 or time.monotonic() >= deadline:
            partial_reason = "directory_budget" if visited >= 3000 else "tool_timeout"
            break
        folder, depth = queue.pop(0)
        visited += 1
        # Do not follow link/junction shortcuts into other Workzones, and never
        # report trees whose physical location is protected.
        if _is_link(folder) or _protected_hit(folder, protected):
            continue
        info = None
        try:
            # Resolves and authorises .git (directory or worktree gitdir file),
            # commondir, config and HEAD before any of them is read.
            info = _inspect(folder, reader)
        except (MetadataUnavailable, OSError):
            info = None
        name = folder.name.casefold()
        git_name, remote_hit, branch_hit = "", False, False
        if info is not None:
            for remote in info.get("remotes") or []:
                remote_name = str(remote.get("name") or "").casefold()
                url = str(remote.get("url") or "").casefold()
                if needle in remote_name or needle in url:
                    remote_hit = True
                if not git_name and url:
                    git_name = Path(url.rstrip("/").removesuffix(".git")).name.casefold()
            branch = str(info.get("branch") or "").casefold()
            branch_hit = bool(branch and needle in branch)
        if needle in name or remote_hit or branch_hit:
            score = ((3 if needle == git_name else 0)
                     + (2 if needle == name else 0)
                     + (1 if git_name else 0))
            matches.append({"path": str(folder), "folder": folder.name,
                            "git_repo": info is not None,
                            "remote_repo": git_name or None, "_score": score})
        if depth >= depth_limit:
            continue
        try:
            entries = sorted(os.scandir(folder), key=lambda item: item.name.casefold())
            for item in entries:
                if item.name in _SCAN_IGNORES:
                    continue
                try:
                    if item.is_dir(follow_symlinks=False):
                        child = Path(item.path)
                        if _is_link(child):
                            continue
                        queue.append((child, depth + 1))
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    matches.sort(key=lambda item: (-item.pop("_score"), len(item["path"]), item["path"].casefold()))
    return _json(ok=True, root=str(root), query=needle, scanned_directories=visited,
                 matches=matches[:result_limit], total_matches=len(matches),
                 partial=bool(partial_reason), stop_reason=partial_reason or "completed",
                 next_step=("Specify a narrower root or greater max_depth to continue."
                            if partial_reason or not matches else ""))


# ---------------------------------------------------------------------------
# code_search — protected paths and links are excluded before any engine reads
# ---------------------------------------------------------------------------

def _rg_literal(relative: str) -> str:
    """Escape a relative path into a literal gitignore-style glob for rg."""
    escaped: list[str] = []
    for index, char in enumerate(relative.replace("\\", "/")):
        if char in "*?[]\\":
            escaped.append("\\" + char)
        elif char in "!#" and index == 0:
            escaped.append("\\" + char)
        else:
            escaped.append(char)
    return "".join(escaped)


def _rg_guard(root: Path, roots: tuple[Path, ...], protected: tuple[Path, ...],
              deadline: float) -> list[str] | None:
    """Build rg exclusion globs so ripgrep cannot open protected or out-of-scope
    files, including through symlinks/junctions. Returns None when the metadata
    walk cannot complete inside the guard budget (caller must fall back to the
    authorised Python engine instead of launching rg)."""
    globs: list[str] = []
    for item in protected:
        try:
            relative = item.relative_to(root)
        except ValueError:
            continue
        escaped = _rg_literal(relative.as_posix())
        globs.append(f"!{escaped}")
        if item.is_dir():
            globs.append(f"!{escaped}/**")
    stack: list[Path] = [root]
    entries_seen = 0
    while stack:
        if time.monotonic() >= deadline or entries_seen > 20000:
            return None
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                entries_seen += 1
                if time.monotonic() >= deadline or entries_seen > 20000:
                    return None
                child = Path(entry.path)
                linked = _is_link(child)
                if not linked:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(child)
                    except OSError:
                        pass
                    continue
                target = child.resolve()
                if _protected_hit(target, protected) or not _in_scope(target, roots):
                    escaped = _rg_literal(child.relative_to(root).as_posix())
                    globs.append(f"!{escaped}")
                    try:
                        if entry.is_dir():
                            globs.append(f"!{escaped}/**")
                    except OSError:
                        pass
    return globs


async def _code_search(args: dict, read_roots: tuple[Path, ...],
                       protected_read_paths: tuple[str, ...], cwd: Path) -> str:
    query = str(args.get("query") or "")
    if not query:
        return _error("missing_query", "Supply a search query.")
    root = _path(args.get("path"), read_roots, cwd)
    if not root.exists():
        return _error("not_found", f"Search path does not exist: {root}")
    mode = str(args.get("mode", "content"))
    if mode not in {"content", "files"}:
        return _error("invalid_mode", "mode must be content or files")
    regex = args.get("regex") is True
    max_results = max(1, min(100, int(args.get("max_results", 30))))
    timeout = max(1.0, min(60.0, float(args.get("timeout_s", 15))))
    glob = str(args.get("glob") or "").strip()
    protected = tuple(Path(item).resolve() for item in protected_read_paths)
    if _protected_hit(root, protected):
        return _error("protected_path", f"Search path is inside a protected location: {root}")
    binary = shutil.which("rg")
    if binary:
        guard_globs = _rg_guard(root, read_roots, protected,
                                time.monotonic() + min(timeout, 5.0))
        if guard_globs is None:
            # The link/protected inventory could not complete; never launch rg
            # without knowing every exclusion it needs.
            if regex:
                return _error("ripgrep_guard_failed",
                              "Could not verify protected/link exclusions within the guard budget, "
                              "and regex search needs ripgrep.",
                              next_step="Narrow the search path and repeat.")
            return await asyncio.to_thread(
                _literal_search_fallback, query, root, read_roots, protected,
                mode, glob, max_results, timeout
            )
        command = [binary, "--color", "never", "--no-messages"]
        if glob:
            # User filters must come FIRST: ripgrep's glob matching is
            # last-match-wins, and every exclusion below is appended after the
            # user's filters so a broad positive glob (e.g. *.txt) can never
            # re-include a protected or out-of-scope path.
            command.extend(["--glob", glob])
        # Guard globs are literal exclusion patterns (e.g. !secret, !secret/**);
        # each one needs its own --glob option so ripgrep cannot treat it as
        # a positional path argument. They are appended AFTER user filters so
        # the protected/link exclusions are always the final word.
        for excluded in guard_globs:
            command.extend(["--glob", excluded])
        if mode == "files":
            command.extend(["--files", "--hidden", "--glob", "!.git/**"])
        else:
            command.extend(["--no-heading", "--line-number", "--smart-case",
                            "--max-columns", "350", "--max-columns-preview", "--hidden",
                            "--glob", "!.git/**"])
            if not regex:
                command.append("--fixed-strings")
            command.extend(["--", query])
        command.append(str(root))
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        lines: list[str] = []
        reason = "completed"
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "tool_timeout"
                    break
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
                except asyncio.TimeoutError:
                    reason = "tool_timeout"
                    break
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if mode == "files" and query.casefold() not in line.casefold():
                    continue
                if not line:
                    continue
                if sum(len(item) for item in lines) + len(line) > 14000:
                    reason = "output_budget"
                    break
                lines.append(line[:600])
                if len(lines) >= max_results:
                    reason = "result_limit"
                    break
        finally:
            if reason == "completed":
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    reason = "process_wait_timeout"
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
        if proc.returncode not in (0, 1, -9) and reason == "completed":
            return _error("ripgrep_failed", "ripgrep failed. Check the path or regex.",
                          next_step="Try a literal query (regex=false) or use shell.")
        return _json(ok=True, path=str(root), mode=mode, engine="ripgrep", matches=lines,
                     count=len(lines), partial=reason != "completed",
                     stop_reason=reason,
                     next_step=("Narrow the path/glob or refine the query and repeat."
                                if reason != "completed" else ""))
    if regex:
        return _error("ripgrep_unavailable", "Regex search needs ripgrep (rg) on PATH.",
                      next_step="Install rg, or search literal text with regex=false.")
    return await asyncio.to_thread(
        _literal_search_fallback, query, root, read_roots, protected, mode, glob,
        max_results, timeout
    )


def _literal_search_fallback(query: str, root: Path, roots: tuple[Path, ...],
                             protected: tuple[Path, ...], mode: str, glob: str,
                             limit: int, timeout: float) -> str:
    """Authorised Python fallback. Every candidate's physical path is checked
    against the read scope and protected paths before its content is opened;
    links and junctions are never followed into protected or out-of-scope
    locations (directory links are not descended at all)."""
    import fnmatch
    deadline = time.monotonic() + timeout
    matches: list[str] = []
    scanned = 0
    reason = "completed"
    file_budget = 6000

    def consider(path: Path, linked: bool) -> bool:
        nonlocal scanned, reason
        if time.monotonic() >= deadline:
            reason = "tool_timeout"
            return False
        if len(matches) >= limit or sum(len(item) for item in matches) >= 14000:
            reason = "result_limit" if len(matches) >= limit else "output_budget"
            return False
        if glob and not fnmatch.fnmatch(path.name, glob):
            return True
        physical = path.resolve() if linked else path
        if _protected_hit(physical, protected):
            return True
        if linked and not _in_scope(physical, roots):
            return True
        if scanned >= file_budget:
            reason = "file_budget"
            return False
        scanned += 1
        if mode == "files":
            if query.casefold() in str(path).casefold():
                matches.append(str(path))
            return True
        try:
            if path.stat().st_size > _MAX_INSPECT_BYTES:
                return True
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if time.monotonic() >= deadline:
                        reason = "tool_timeout"
                        return False
                    if query.casefold() in line.casefold():
                        matches.append(f"{path}:{number}:{line.strip()[:350]}")
                        if len(matches) >= limit:
                            reason = "result_limit"
                            return False
                        if sum(len(item) for item in matches) >= 14000:
                            reason = "output_budget"
                            return False
        except (OSError, UnicodeError):
            pass
        return True

    if root.is_file():
        consider(root, linked=_is_link(root))
    else:
        stack: list[Path] = [root]
        while stack and reason == "completed":
            current = stack.pop()
            try:
                iterator = os.scandir(current)
            except OSError:
                continue
            with iterator:
                entries = sorted(iterator, key=lambda entry: entry.name.casefold())
            for entry in entries:
                if entry.name in _SCAN_IGNORES:
                    continue
                child = Path(entry.path)
                linked = _is_link(child)
                if not linked:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not _protected_hit(child, protected):
                                stack.append(child)
                            continue
                    except OSError:
                        continue
                    if not consider(child, linked=False):
                        break
                else:
                    if not consider(child, linked=True):
                        break
    return _json(ok=True, path=str(root), mode=mode, engine="python_literal",
                 matches=matches, count=len(matches), scanned_files=scanned,
                 partial=reason != "completed", stop_reason=reason,
                 next_step=("Refine query/path to continue; ripgrep is faster when installed."
                            if reason != "completed" else ""))


# ---------------------------------------------------------------------------
# git_inspect — Git metadata locations are authorised before Git runs
# ---------------------------------------------------------------------------

def _git_command(repo: Path, argv: list[str], *, timeout: float = 12) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["GIT_PAGER"] = "cat"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "--no-pager", *argv],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=False, env=environment,
    )


def _safe_remote(text: str) -> str:
    lines = []
    for line in text.splitlines()[:100]:
        parts = line.split()
        if len(parts) < 2:
            continue
        name, address = parts[:2]
        if "://" in address:
            parsed = urlsplit(address)
            host = parsed.hostname or "remote"
            address = f"{host}{parsed.path}"
        elif "@" in address and ":" in address:
            address = address.partition("@")[-1].replace(":", "/", 1)
        else:
            address = Path(address).name
        lines.append(f"{name}: {address.rstrip('/')}")
    return "\n".join(dict.fromkeys(lines))


def _find_repo_root(start: Path, read_roots: tuple[Path, ...],
                    reader: MetadataReader) -> tuple[Path | None, MetadataUnavailable | None]:
    """Walk up from the inspected folder to the nearest authorised repository,
    resolving and authorising each candidate's Git pointer before reading it."""
    current = start
    while True:
        try:
            git_dir, _, _ = _git_dirs_for(current, reader)
        except (MetadataUnavailable, OSError) as exc:
            if isinstance(exc, MetadataUnavailable):
                return None, exc
            return None, MetadataUnavailable("metadata_unavailable")
        if git_dir is not None:
            return current, None
        parent = current.parent
        if parent == current:
            return None, None
        if not _in_scope(parent, read_roots):
            return None, None
        current = parent


def _git_inspect(args: dict, read_roots: tuple[Path, ...],
                 protected_read_paths: tuple[str, ...], cwd: Path) -> str:
    operation = str(args.get("operation") or "").strip()
    if operation not in {"status", "branches", "log", "diff", "remote"}:
        return _error("invalid_operation", "Choose status, branches, log, diff or remote.")
    path = _path(args.get("path"), read_roots, cwd)
    if not path.is_dir():
        return _error("not_directory", f"Git path is not a directory: {path}")
    reader = MetadataReader(read_roots, protected_read_paths, cwd)
    try:
        repo, failure = _find_repo_root(path, read_roots, reader)
        if failure is not None:
            return _error("metadata_denied",
                          f"Git metadata for {path} could not be authorised: {failure.code}.",
                          next_step="Choose a repository whose .git metadata is inside the authorised Workzone.")
        if repo is None:
            return _error("not_git_repository", f"No Git repository found at {path}.")
        git_dir, common_dir, _worktree = _git_dirs_for(repo, reader)
        # Authorise the exact metadata locations Git reads for these operations
        # (pointer, gitdir, commondir, config, HEAD) before executing anything.
        metadata = [git_dir / "config", git_dir / "HEAD"]
        if common_dir is not None:
            metadata.extend([common_dir / "config", git_dir / "config.worktree",
                             common_dir / "HEAD"])
        for location in metadata:
            try:
                reader.resolve(location)
            except MetadataUnavailable as exc:
                return _error("metadata_denied",
                              f"Git metadata {location.name} could not be authorised: {exc.code}.",
                              next_step="Choose a repository whose .git metadata is inside the authorised Workzone.")
        limit = max(1, min(100, int(args.get("limit", 20))))
        if operation == "status":
            command = ["status", "--short", "--branch", "--untracked-files=normal"]
        elif operation == "branches":
            command = ["branch", "--list", "--no-color"]
        elif operation == "log":
            command = ["log", f"-{limit}", "--date=short", "--format=%h %ad %s"]
        elif operation == "diff":
            command = ["diff", "--no-ext-diff", "--no-textconv", "--stat"]
            if args.get("cached") is True:
                command.append("--cached")
        else:
            command = ["remote", "-v"]
        output = _git_command(repo, command)
    except FileNotFoundError:
        return _error("git_unavailable", "Git is not on PATH.",
                      next_step="Install Git or use shell in an environment where Git is available.")
    except subprocess.TimeoutExpired:
        return _error("tool_timeout", "This Git inspection exceeded its 12-second tool budget.",
                      next_step="Inspect a narrower repository/path; the Agent Turn continues.")
    if output.returncode:
        return _error("git_failed", output.stderr[:700] or f"Git exit code {output.returncode}")
    raw = _safe_remote(output.stdout) if operation == "remote" else output.stdout
    truncated = len(raw) > 18000
    return _json(ok=True, repository=str(repo), operation=operation,
                 output=raw[:18000], partial=truncated, stop_reason="output_limit" if truncated else "completed")


# ---------------------------------------------------------------------------
# file_manage — validated recursive copy/move with staged writes and
# cooperative cancellation
# ---------------------------------------------------------------------------

def _collect_tree(source: Path, scope_roots: tuple[Path, ...],
                  protected: tuple[Path, ...], cancel: threading.Event
                  ) -> tuple[list[tuple[str, Path, Path, Path | None]], str | None]:
    """Validate every source descendant before anything is written. Returns
    (items, error_json); items are (kind, source_path, relative, link_target)."""
    items: list[tuple[str, Path, Path, Path | None]] = []
    stack: list[Path] = [source]
    while stack:
        if cancel.is_set():
            return [], _cancelled(0)
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError as exc:
            return [], _error("scan_failed", f"Could not enumerate {current}: {type(exc).__name__}: {exc}")
        with iterator:
            entries = sorted(iterator, key=lambda entry: entry.name.casefold())
        for entry in entries:
            child = Path(entry.path)
            relative = child.relative_to(source)
            linked = _is_link(child)
            if linked:
                target = child.resolve()
                if not _in_scope(target, scope_roots) or _protected_hit(target, protected):
                    return [], _error(
                        "unsafe_source",
                        f"Source link escapes the authorised scope or points into a protected location: {child}",
                        next_step="Remove the link from the source tree or copy the linked files directly.")
                items.append(("link", child, relative, target))
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if _protected_hit(child, protected):
                return [], _error("protected_source",
                                  f"Source contains a protected descendant: {child}",
                                  next_step="Copy the non-protected files individually.")
            if is_dir:
                items.append(("dir", child, relative, None))
                stack.append(child)
            else:
                items.append(("file", child, relative, None))
    items.sort(key=lambda item: len(item[2].parts))
    return items, None


def _dest_tree_safe(destination: Path, write_roots: tuple[Path, ...],
                    protected: tuple[Path, ...]) -> tuple[bool, Path | None]:
    """Existing destination trees must contain no link/junction (writes could be
    redirected outside the Workzone through it) and no protected entry."""
    stack: list[Path] = [destination]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            entries = list(iterator)
        for entry in entries:
            child = Path(entry.path)
            if _is_link(child):
                return False, child
            if _protected_hit(child, protected):
                return False, child
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(child)
            except OSError:
                pass
    return True, None


def _place_file(source_file: Path, dest_file: Path, cancel: threading.Event) -> None:
    """Stream a file into a staging temp file in the destination directory, then
    atomically replace the destination. Cancellation stops the stream between
    chunks and removes the staging file."""
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=dest_file.parent,
                                         prefix=".hashi-copy-", delete=False) as stream:
            staged = Path(stream.name)
        with source_file.open("rb") as src, staged.open("wb") as dst:
            while True:
                if cancel.is_set():
                    raise _CancelledMutation()
                chunk = src.read(_COPY_CHUNK_BYTES)
                if not chunk:
                    break
                dst.write(chunk)
        os.replace(staged, dest_file)
        try:
            shutil.copystat(source_file, dest_file, follow_symlinks=False)
        except OSError:
            pass
    except BaseException:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _copy_tree(source: Path, destination: Path, scope_roots: tuple[Path, ...],
               protected: tuple[Path, ...], cancel: threading.Event,
               overwrite: bool) -> tuple[int, list[str], str | None]:
    """Validated, staged recursive copy. Never follows destination links and
    never opens protected or out-of-scope source descendants."""
    if destination.exists():
        if not destination.is_dir():
            return 0, [], _error("not_directory",
                                 f"Destination exists as a file: {destination}")
        if not overwrite:
            return 0, [], _error("destination_exists", f"Destination already exists: {destination}",
                                 next_step="Choose another destination or explicitly set overwrite=true.")
        safe, offender = _dest_tree_safe(destination, scope_roots, protected)
        if not safe:
            return 0, [], _error(
                "unsafe_destination",
                f"Existing destination contains a link or protected entry that a merge copy could write through: {offender}",
                next_step="Choose an empty destination or remove the link before merging.")
    else:
        destination.mkdir(parents=True, exist_ok=True)
    items, failure = _collect_tree(source, scope_roots, protected, cancel)
    if failure is not None:
        return 0, [], failure
    copied = 0
    skipped_links: list[str] = []
    for kind, child, relative, target in items:
        if cancel.is_set():
            return copied, skipped_links, _cancelled(copied)
        dest_path = destination / relative
        try:
            if kind == "dir":
                dest_path.mkdir(parents=True, exist_ok=True)
                continue
            if kind == "link":
                if dest_path.exists() or _is_link(dest_path):
                    if not overwrite:
                        return copied, skipped_links, _error("destination_exists",
                                                            f"Destination already exists: {dest_path}")
                    dest_path.unlink()
                try:
                    dest_path.symlink_to(target, target_is_directory=target.is_dir())
                    copied += 1
                except (OSError, NotImplementedError, ValueError):
                    skipped_links.append(str(relative))
                continue
            if dest_path.exists():
                if _is_link(dest_path):
                    # Replace the link itself; never follow it.
                    dest_path.unlink()
                elif dest_path.is_dir():
                    return copied, skipped_links, _error("destination_conflict",
                                                         f"A directory occupies {dest_path}")
                elif not overwrite:
                    return copied, skipped_links, _error("destination_exists",
                                                         f"Destination already exists: {dest_path}")
            _place_file(child, dest_path, cancel)
            copied += 1
        except _CancelledMutation:
            return copied, skipped_links, _cancelled(copied)
        except OSError as exc:
            return copied, skipped_links, _error("copy_failed",
                                                 f"{type(exc).__name__}: {exc} while writing {dest_path}")
    return copied, skipped_links, None


def _remove_tree(root: Path, cancel: threading.Event) -> None:
    """Bottom-up removal that unlinks links/junctions instead of following them."""
    stack: list[tuple[Path, bool]] = [(root, False)]
    while stack:
        if cancel.is_set():
            raise _CancelledMutation()
        current, ready = stack.pop()
        if ready:
            try:
                if _is_link(current):
                    current.unlink()
                elif current.is_dir():
                    current.rmdir()
                else:
                    current.unlink()
            except OSError:
                pass
            continue
        stack.append((current, True))
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            entries = list(iterator)
        for entry in entries:
            child = Path(entry.path)
            linked = _is_link(child)
            try:
                if linked or not entry.is_dir(follow_symlinks=False):
                    stack.append((child, True))
                else:
                    stack.append((child, False))
            except OSError:
                stack.append((child, True))


def _file_manage(args: dict, write_roots: tuple[Path, ...],
                 read_roots: tuple[Path, ...],
                 protected_read_paths: tuple[str, ...], cwd: Path,
                 cancel: threading.Event | None = None) -> str:
    cancel = cancel or threading.Event()
    protected = tuple(Path(item).resolve() for item in protected_read_paths)
    operation = str(args.get("operation") or "").strip()
    if operation not in {"mkdir", "copy", "move"}:
        return _error("invalid_operation", "Choose mkdir, copy or move.")
    if not args.get("destination"):
        return _error("missing_destination", "Supply destination.")
    destination = _path(args["destination"], write_roots, cwd)
    if _protected_hit(destination, protected):
        return _error("protected_destination",
                      f"Destination is inside a protected location: {destination}")
    if operation == "mkdir":
        if destination.exists() and not destination.is_dir():
            return _error("not_directory", f"An existing file occupies {destination}.")
        existed = destination.exists()
        destination.mkdir(parents=True, exist_ok=True)
        return _json(ok=True, operation=operation, destination=str(destination), changed=not existed)
    if not args.get("source"):
        return _error("missing_source", "Supply source for copy or move.")
    source_roots = read_roots if operation == "copy" else write_roots
    source = _path(args["source"], source_roots, cwd)
    if not source.exists():
        return _error("not_found", f"Source does not exist: {source}")
    if _protected_hit(source, protected):
        return _error("protected_source", f"Source is inside a protected location: {source}")
    if source == destination:
        return _json(ok=True, operation=operation, source=str(source),
                     destination=str(destination), changed=False)
    recursive = args.get("recursive") is True
    if source.is_dir() and not recursive:
        return _error("recursive_required", "Directory copy/move requires recursive=true.",
                      next_step="Repeat with recursive=true or choose a file.")
    if source.is_dir() and destination.is_relative_to(source):
        return _error("nested_destination", "Cannot put a directory inside itself.")
    overwrite = args.get("overwrite") is True
    if destination.exists() and not overwrite:
        return _error("destination_exists", f"Destination already exists: {destination}",
                      next_step="Choose another destination or explicitly set overwrite=true.")
    if operation == "move" and destination.exists() and destination.is_dir():
        return _error("directory_replace", "Move cannot overwrite an existing directory.",
                      next_step="Use copy with overwrite=true to merge, or choose an unused destination.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if operation == "copy":
        if source.is_dir():
            if destination.exists() and destination.is_file():
                return _error("not_directory",
                              f"Destination exists as a file: {destination}")
            copied, skipped, failure = _copy_tree(source, destination, read_roots,
                                                  protected, cancel, overwrite=overwrite)
            if failure is not None:
                return failure
            return _json(ok=True, operation=operation, source=str(source),
                         destination=str(destination), changed=copied > 0 or bool(skipped),
                         copied_files=copied, skipped_links=skipped)
        target = destination / source.name if destination.is_dir() else destination
        if target.exists() and not overwrite:
            return _error("destination_exists", f"Destination already exists: {target}",
                          next_step="Choose another destination or explicitly set overwrite=true.")
        if target.exists() and target.is_dir() and not _is_link(target):
            return _error("not_directory", f"An existing directory occupies {target}")
        if _is_link(target) and (_protected_hit(target.resolve(), protected)
                                 or not _in_scope(target.resolve(), write_roots)):
            return _error("unsafe_destination",
                          f"Destination link escapes the authorised scope or points into a protected location: {target}")
        try:
            _place_file(source, target, cancel)
        except _CancelledMutation:
            return _cancelled(0)
        return _json(ok=True, operation=operation, source=str(source),
                     destination=str(target), changed=True)
    # move
    if source.is_dir():
        items, failure = _collect_tree(source, write_roots, protected, cancel)
        if failure is not None:
            return failure
        try:
            os.replace(source, destination)
            return _json(ok=True, operation=operation, source=str(source),
                         destination=str(destination), changed=True)
        except OSError:
            copied, skipped, failure = _copy_tree(source, destination, write_roots,
                                                  protected, cancel, overwrite=False)
            if failure is not None:
                return failure
            try:
                _remove_tree(source, cancel)
            except _CancelledMutation:
                return _cancelled(copied, note="Destination was written; source removal is incomplete.")
            return _json(ok=True, operation=operation, source=str(source),
                         destination=str(destination), changed=True,
                         moved_via_copy=copied, skipped_links=skipped)
    if destination.exists() and overwrite:
        if _is_link(destination) and (not _in_scope(destination.resolve(), write_roots)
                                      or _protected_hit(destination.resolve(), protected)):
            return _error("unsafe_destination",
                          f"Destination link escapes the authorised scope or points into a protected location: {destination}")
        # os.replace is atomic on one filesystem. Do not unlink the old
        # destination before verifying that a replacement can succeed.
        try:
            os.replace(source, destination)
        except OSError:
            return _error("move_replace_failed", "Atomic replacement failed; existing destination was preserved.",
                          next_step="Choose a new destination or use shell for an explicit cross-volume move.")
        return _json(ok=True, operation=operation, source=str(source),
                     destination=str(destination), changed=True)
    try:
        os.replace(source, destination)
    except OSError:
        if cancel.is_set():
            return _cancelled(0)
        shutil.move(str(source), str(destination))
    return _json(ok=True, operation=operation, source=str(source),
                 destination=str(destination), changed=True)


# ---------------------------------------------------------------------------
# text_replace — exact-count, UTF-8, atomic, changed-since-read check
# ---------------------------------------------------------------------------

def _text_replace(args: dict, write_roots: tuple[Path, ...], cwd: Path,
                  cancel: threading.Event | None = None) -> str:
    cancel = cancel or threading.Event()
    raw = str(args.get("path") or "").strip()
    find = args.get("find")
    replace = args.get("replace")
    if not raw or not isinstance(find, str) or not find or not isinstance(replace, str):
        return _error("invalid_input", "Provide path, non-empty literal find, and string replace.")
    target = _path(raw, write_roots, cwd)
    if not target.is_file():
        return _error("not_file", f"Not an existing file: {target}")
    if target.stat().st_size > _MAX_INSPECT_BYTES:
        return _error("large_file", "This exact-text editor handles files up to 8 MiB.",
                      next_step="Use apply_patch or shell for a larger file.")
    old = target.read_text(encoding="utf-8")
    count = old.count(find)
    expected = max(1, int(args.get("expected_count", 1)))
    if count != expected:
        return _error("occurrence_mismatch", f"Expected {expected} matches; found {count}.",
                      next_step="Inspect the file, update find text, or set the exact expected_count.")
    preview = args.get("preview_only") is True
    if preview or find == replace:
        return _json(ok=True, operation="text_replace", path=str(target),
                     matches=count, changed=False, preview_only=preview)
    next_content = old.replace(find, replace)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="",
                                         dir=target.parent, prefix=".hashi-edit-", delete=False) as stream:
            staged = Path(stream.name)
            stream.write(next_content)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(target, staged)
        if cancel.is_set():
            return _cancelled(0)
        # Concurrent-change guard: the file must still contain exactly what we
        # read; otherwise an external edit would be silently lost.
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            return _error("read_back_failed",
                          f"Could not re-read {target.name} before replacing: {type(exc).__name__}",
                          next_step="Re-read the file and repeat the exact edit.")
        if current != old:
            return _error("concurrent_change",
                          "The file changed while this edit was being prepared; the original was not replaced.",
                          next_step="Re-read the file and repeat the exact edit.")
        os.replace(staged, target)
    finally:
        if staged is not None and staged.exists():
            staged.unlink()
    return _json(ok=True, operation="text_replace", path=str(target),
                 matches=count, changed=True)


# ---------------------------------------------------------------------------
# data_inspect — bounded JSON/CSV previews (unchanged behaviour)
# ---------------------------------------------------------------------------

def _compact_preview(value: Any, depth: int = 0) -> Any:
    """Keep every response valid JSON and inside the model-visible output budget."""
    if isinstance(value, str):
        return value[:220] + ("…" if len(value) > 220 else "")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 4:
        return f"<{type(value).__name__} nested value>"
    if isinstance(value, dict):
        return {
            str(key)[:100]: _compact_preview(item, depth + 1)
            for key, item in list(value.items())[:12]
        }
    if isinstance(value, list):
        return [_compact_preview(item, depth + 1) for item in value[:12]]
    return str(value)[:220]


def _sample_budget(rows: list[Any], budget: int = 9500) -> tuple[list[Any], bool]:
    selected = []
    total = 0
    for raw in rows:
        item = _compact_preview(raw)
        encoded = json.dumps(item, ensure_ascii=False)
        if selected and total + len(encoded) > budget:
            return selected, True
        selected.append(item)
        total += len(encoded)
    return selected, False


def _data_inspect(args: dict, read_roots: tuple[Path, ...], cwd: Path) -> str:
    if not args.get("path"):
        return _error("missing_path", "Supply data file path.")
    path = _path(args["path"], read_roots, cwd)
    if not path.is_file():
        return _error("not_file", f"Not an existing file: {path}")
    if path.stat().st_size > _MAX_INSPECT_BYTES:
        return _error("large_file", "Preview reads files up to 8 MiB.",
                      next_step="Use file_read with offsets or a targeted shell data command.")
    fmt = str(args.get("format", "auto"))
    if fmt == "auto":
        fmt = {".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
               ".csv": "csv", ".tsv": "tsv"}.get(path.suffix.lower(), "")
    if fmt not in {"json", "jsonl", "csv", "tsv"}:
        return _error("unknown_format", "Select JSON, JSONL, CSV or TSV.")
    limit = max(1, min(30, int(args.get("limit", 5))))
    try:
        if fmt in {"csv", "tsv"}:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream, delimiter="," if fmt == "csv" else "\t")
                fields = reader.fieldnames or []
                rows = [row for _, row in zip(range(limit), reader)]
            samples, clipped = _sample_budget(rows)
            return _json(ok=True, format=fmt, path=str(path), columns=[name[:100] for name in fields[:100]],
                         samples=samples, sample_count=len(samples), preview_truncated=clipped)
        if fmt == "jsonl":
            records = []
            with path.open("r", encoding="utf-8-sig") as stream:
                for line in stream:
                    if line.strip():
                        records.append(json.loads(line))
                    if len(records) >= limit:
                        break
            samples, clipped = _sample_budget(records)
            return _json(ok=True, format=fmt, path=str(path),
                         samples=samples, sample_count=len(samples), preview_truncated=clipped)
        with path.open("r", encoding="utf-8-sig") as stream:
            payload = json.load(stream)
        if isinstance(payload, list):
            samples, clipped = _sample_budget(payload[:limit])
            return _json(ok=True, format=fmt, path=str(path), kind="array",
                         total_items=len(payload), samples=samples, preview_truncated=clipped)
        if isinstance(payload, dict):
            sample = _compact_preview({k: payload[k] for k in list(payload)[:limit]})
            return _json(ok=True, format=fmt, path=str(path), kind="object",
                         keys=[str(k)[:100] for k in list(payload)[:60]], sample=sample)
        return _json(ok=True, format=fmt, path=str(path), kind=type(payload).__name__,
                     sample=_compact_preview(payload))
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        return _error("parse_failed", f"Could not parse {path.name}: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# tool_find — existing registry authority; schemas only
# ---------------------------------------------------------------------------

def _tool_find_selections(args: dict, available: tuple[str, ...]) -> list[str]:
    from tools.registry import TOOL_TIERS, resolve_tiers
    from tools.schemas import TOOL_SCHEMA_MAP
    accessible = set(available)
    limit = max(1, min(20, int(args.get("limit", 8))))
    names = args.get("names")
    if isinstance(names, list) and names:
        return [name for name in dict.fromkeys(str(item) for item in names)
                if name in accessible][:limit]
    pack = str(args.get("pack") or "").strip().casefold().replace(" ", "_")
    aliases = {
        "dev": "development", "coding": "development", "code": "development",
        "office": "office_basic", "excel": "office_basic", "word": "office_basic",
        "files": "daily", "file": "daily", "daily_tools": "daily",
    }
    if pack:
        pack = aliases.get(pack, pack)
        return [name for name in dict.fromkeys(resolve_tiers([pack]))
                if name in accessible][:limit]
    query = str(args.get("query") or "").strip().casefold()
    if not query:
        return [name for name in dict.fromkeys(resolve_tiers(["core", "development"]))
                if name in accessible][:limit]
    tokens = [word for word in re.split(r"[\s,;:/._-]+", query) if word]
    concepts = {
        "excel": "office", "spreadsheet": "office", "word": "office",
        "表格": "office", "文档": "office", "办公": "office",
        "代码": "code", "搜索": "search", "项目": "project", "仓库": "git",
        "文件": "file", "浏览器": "browser", "网页": "web",
        "测试": "verification", "验证": "verification", "终端": "shell",
    }
    tokens.extend(concepts[term] for term in concepts if term in query)
    scores = []
    for name in available:
        schema = TOOL_SCHEMA_MAP.get(name, {})
        description = str((schema.get("function") or {}).get("description") or "").casefold()
        lowered = name.casefold()
        score = sum(5 if token in lowered else 1 if token in description else 0
                    for token in tokens)
        if score:
            scores.append((-score, name))
    scores.sort()
    return [name for _, name in scores[:limit]]


def tool_find(args: dict, available: tuple[str, ...]) -> str:
    from tools.registry import TOOL_TIERS
    from tools.schemas import TOOL_SCHEMA_MAP
    selection = _tool_find_selections(args, available)
    return _json(ok=True, tools=[
        {"name": name,
         "description": str(TOOL_SCHEMA_MAP[name]["function"]["description"])[:250],
         "required": TOOL_SCHEMA_MAP[name]["function"]["parameters"].get("required", [])}
        for name in selection
    ], packs=sorted(TOOL_TIERS), discoverable=True,
    note="These tools are already authorised/available; in HERV3 on-demand mode their schemas are visible on the next model call. Call them normally.")


def tool_path_requests(tool_name: str, args: dict, cwd: Path) -> list[tuple[str, str]]:
    """Project toolkit path operations onto existing canonical path gates."""
    home = str(cwd)
    def raw(key: str) -> str:
        return str(args.get(key) or home)
    if tool_name == "project_locate":
        return [("file_read", raw("root"))]
    if tool_name in {"code_search", "git_inspect", "data_inspect"}:
        return [("file_read", raw("path"))]
    if tool_name == "text_replace":
        return [("file_write", raw("path"))]
    if tool_name == "file_manage":
        action = str(args.get("operation") or "")
        request = [("file_write", raw("destination"))]
        if action in {"copy", "move"}:
            request.append(("file_read" if action == "copy" else "file_write", raw("source")))
        return request
    return []


def _mutation_cancelled(status: str, *, note: str = "", copied: int | None = None
                        ) -> asyncio.CancelledError:
    details: dict = {"toolkit_mutation": {"status": status}}
    if copied is not None:
        details["toolkit_mutation"]["copied"] = copied
    if note:
        details["toolkit_mutation"]["note"] = note
    exc = asyncio.CancelledError()
    exc.hashi_tool_details = details
    return exc


async def execute_toolkit(registry: Any, name: str, args: dict,
                          protected_read_paths: tuple[str, ...] = ()) -> str:
    """Called only after Tool Registry admission/audit/fingerprint dispatch.

    ``file_manage`` and ``text_replace`` mutate the filesystem inside a worker
    thread with cooperative cancellation: on task cancellation the worker is
    signalled, given a bounded settle window, and the outcome is reported as
    stopped (no further writes) or in-flight (unknown effects) — never as a
    clean cancellation while writes continue.
    """
    if name == "tool_find":
        all_allowed = tuple(item["function"]["name"]
                            for item in registry.get_tool_definitions(tiers=None))
        return tool_find(args, all_allowed)
    read_roots, _, cwd = registry._read_scope()
    write_roots = tuple(registry.access_roots)
    try:
        if name == "code_search":
            return await _code_search(args, read_roots, protected_read_paths, cwd)
        if name in {"file_manage", "text_replace"}:
            cancel = threading.Event()
            loop = asyncio.get_running_loop()

            def run() -> str:
                if name == "file_manage":
                    return _file_manage(args, write_roots, read_roots,
                                        protected_read_paths, cwd, cancel)
                return _text_replace(args, write_roots, cwd, cancel)

            future = loop.run_in_executor(None, run)
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                cancel.set()
                try:
                    settled = await asyncio.wait_for(asyncio.shield(future),
                                                     timeout=_CANCEL_SETTLE_SECONDS)
                except asyncio.TimeoutError:
                    raise _mutation_cancelled(
                        "in_flight",
                        note="Worker thread did not settle within the cancellation grace; final effect is unconfirmed."
                    ) from None
                # The worker settled cleanly. A settled mutation either
                # completed (report truthfully) or aborted cooperatively.
                if isinstance(settled, str) and settled.lstrip().startswith("{"):
                    try:
                        payload = json.loads(settled)
                    except ValueError:
                        payload = None
                    if (isinstance(payload, dict) and payload.get("ok") is False
                            and str((payload.get("error") or {}).get("code")) == "cancelled"):
                        raise _mutation_cancelled(
                            "stopped", copied=payload.get("copied"),
                            note=str(payload.get("next_step") or "No further writes were made.")
                        ) from None
                return settled
        dispatch = {
            "project_locate": lambda: _project_locate(args, read_roots, protected_read_paths, cwd),
            "git_inspect": lambda: _git_inspect(args, read_roots, protected_read_paths, cwd),
            "data_inspect": lambda: _data_inspect(args, read_roots, cwd),
        }
        if name not in dispatch:
            return _error("unknown_tool", f"No toolkit implementation for {name}.")
        return await asyncio.to_thread(dispatch[name])
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        return _error("tool_failed", f"{name}: {type(exc).__name__}: {exc}")
