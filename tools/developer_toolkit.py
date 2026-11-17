"""Small-model-first daily and development tools for HASHI PAO Functions.

No authority, model, or Agent lifecycle is owned here. Every filesystem path is
resolved through the existing Tool Registry Workzone rules. Tool time budgets
bound individual searches/commands, not the HERV3 Turn.
"""

from __future__ import annotations

import asyncio
import configparser
import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tools.builtins import _resolve_path

_SCAN_IGNORES = frozenset({".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache"})
_MAX_INSPECT_BYTES = 8 * 1024 * 1024


def _json(**value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _error(code: str, message: str, *, next_step: str = "") -> str:
    return _json(ok=False, error={"code": code, "message": message}, next_step=next_step)


def _path(raw: str | None, roots: tuple[Path, ...], cwd: Path) -> Path:
    return _resolve_path(str(raw or "."), roots, cwd)


def _git_remote_basename(folder: Path, roots: tuple[Path, ...]) -> str:
    """Inspect only repo identity, never expose Git remote URL credentials."""
    dotgit = folder / ".git"
    if not dotgit.is_dir() or dotgit.is_symlink():
        return ""
    config = (dotgit / "config").resolve()
    if not any(config == root or config.is_relative_to(root) for root in roots):
        return ""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(config, encoding="utf-8")
        url = parser.get('remote "origin"', "url", fallback="")
    except (OSError, configparser.Error):
        return ""
    if not url:
        return ""
    # Handles https://user:token@github.com/owner/repo.git and git@github.com:owner/repo.git
    path = urlsplit(url).path if "://" in url else url.rsplit(":", 1)[-1]
    return Path(path.rstrip("/").removesuffix(".git")).name


def _project_locate(args: dict, read_roots: tuple[Path, ...], cwd: Path) -> str:
    needle = str(args.get("name") or "").strip().casefold()
    if not needle:
        return _error("missing_name", "Give a project or repository name.")
    root = _path(args.get("root"), read_roots, cwd)
    if not root.is_dir():
        return _error("not_directory", f"Search root is not a directory: {root}")
    depth_limit = max(0, min(4, int(args.get("max_depth", 2))))
    result_limit = max(1, min(20, int(args.get("limit", 8))))
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
        # Do not follow symlink directory shortcuts into other Workzones.
        if folder.is_symlink():
            continue
        name = folder.name.casefold()
        git_name = _git_remote_basename(folder, read_roots).casefold()
        if needle in name or (git_name and needle in git_name):
            score = (3 if needle == git_name else 0) + (2 if needle == name else 0) + (1 if git_name else 0)
            matches.append({"path": str(folder), "folder": folder.name,
                            "git_repo": bool((folder / ".git").exists()),
                            "remote_repo": git_name or None, "_score": score})
        if depth >= depth_limit:
            continue
        try:
            entries = sorted(os.scandir(folder), key=lambda item: item.name.casefold())
            for item in entries:
                if item.name in _SCAN_IGNORES:
                    continue
                if item.is_dir(follow_symlinks=False):
                    queue.append((Path(item.path), depth + 1))
        except (OSError, PermissionError):
            continue
    matches.sort(key=lambda item: (-item.pop("_score"), len(item["path"]), item["path"].casefold()))
    return _json(ok=True, root=str(root), query=needle, scanned_directories=visited,
                 matches=matches[:result_limit], total_matches=len(matches),
                 partial=bool(partial_reason), stop_reason=partial_reason or "completed",
                 next_step=("Specify a narrower root or greater max_depth to continue."
                            if partial_reason or not matches else ""))


async def _code_search(args: dict, read_roots: tuple[Path, ...], cwd: Path) -> str:
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
    binary = shutil.which("rg")
    if binary:
        command = [binary, "--color", "never", "--no-messages"]
        if mode == "files":
            command.extend(["--files", "--hidden", "--glob", "!.git/**"])
        else:
            command.extend(["--no-heading", "--line-number", "--smart-case",
                            "--max-columns", "350", "--max-columns-preview", "--hidden",
                            "--glob", "!.git/**"])
            if not regex:
                command.append("--fixed-strings")
            command.extend(["--", query])
        if glob:
            # Insert before -- so the option cannot become a search query.
            index = command.index("--") if "--" in command else len(command)
            command[index:index] = ["--glob", glob]
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
                lines.append(line[:1000])
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
        if proc.returncode not in (0, 1, -9) and not lines and reason == "completed":
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
        _literal_search_fallback, query, root, read_roots, mode, glob, max_results, timeout
    )


def _literal_search_fallback(query: str, root: Path, roots: tuple[Path, ...],
                             mode: str, glob: str, limit: int, timeout: float) -> str:
    import fnmatch
    deadline = time.monotonic() + timeout
    matches: list[str] = []
    scanned = 0
    reason = "completed"
    paths: list[Path] = [root] if root.is_file() else []
    if root.is_dir():
        for current, folders, files in os.walk(root, followlinks=False):
            if time.monotonic() >= deadline or scanned >= 10000:
                reason = "tool_timeout" if time.monotonic() >= deadline else "file_budget"
                break
            folders[:] = [name for name in folders if name not in _SCAN_IGNORES]
            paths.extend(Path(current) / name for name in files)
    for path in paths:
        if time.monotonic() >= deadline:
            reason = "tool_timeout"
            break
        if len(matches) >= limit:
            reason = "result_limit"
            break
        if glob and not fnmatch.fnmatch(path.name, glob):
            continue
        if not any(path.resolve() == base or path.resolve().is_relative_to(base) for base in roots):
            continue
        scanned += 1
        if mode == "files":
            if query.casefold() in str(path).casefold():
                matches.append(str(path))
            continue
        try:
            if path.stat().st_size > _MAX_INSPECT_BYTES:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if query.casefold() in line.casefold():
                        matches.append(f"{path}:{number}:{line.strip()[:350]}")
                        if len(matches) >= limit:
                            reason = "result_limit"
                            break
                    if time.monotonic() >= deadline:
                        reason = "tool_timeout"
                        break
        except (OSError, UnicodeError):
            continue
    return _json(ok=True, path=str(root), mode=mode, engine="python_literal",
                 matches=matches, count=len(matches), scanned_files=scanned,
                 partial=reason != "completed", stop_reason=reason,
                 next_step=("Refine query/path to continue; ripgrep is faster when installed."
                            if reason != "completed" else ""))



def _git_command(repo: Path, argv: list[str], *, timeout: float = 12) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["GIT_PAGER"] = "cat"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-C", str(repo), "--no-pager", *argv],
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


def _git_inspect(args: dict, read_roots: tuple[Path, ...], cwd: Path) -> str:
    operation = str(args.get("operation") or "").strip()
    if operation not in {"status", "branches", "log", "diff", "remote"}:
        return _error("invalid_operation", "Choose status, branches, log, diff or remote.")
    path = _path(args.get("path"), read_roots, cwd)
    if not path.is_dir():
        return _error("not_directory", f"Git path is not a directory: {path}")
    try:
        actual = _git_command(path, ["rev-parse", "--show-toplevel"])
        if actual.returncode != 0:
            return _error("not_git_repository", f"No Git repository found at {path}.")
        repo = _path(actual.stdout.strip(), read_roots, cwd)
        if not repo.is_dir():
            return _error("not_directory", f"Git worktree is not a directory: {repo}")
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


def _file_manage(args: dict, write_roots: tuple[Path, ...],
                 read_roots: tuple[Path, ...], cwd: Path) -> str:
    operation = str(args.get("operation") or "").strip()
    if operation not in {"mkdir", "copy", "move"}:
        return _error("invalid_operation", "Choose mkdir, copy or move.")
    if not args.get("destination"):
        return _error("missing_destination", "Supply destination.")
    destination = _path(args["destination"], write_roots, cwd)
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
            # Preserve links as links: never follow a child symlink into other Workzones.
            shutil.copytree(source, destination, dirs_exist_ok=overwrite, symlinks=True)
        else:
            shutil.copy2(source, destination, follow_symlinks=False)
    else:
        if destination.exists() and overwrite:
            # os.replace is atomic on one filesystem. Do not unlink the old
            # destination before verifying that a replacement can succeed.
            if source.is_dir():
                return _error("directory_replace", "Move cannot overwrite an existing directory.")
            try:
                os.replace(source, destination)
            except OSError:
                return _error("move_replace_failed", "Atomic replacement failed; existing destination was preserved.",
                              next_step="Choose a new destination or use shell for an explicit cross-volume move.")
        else:
            shutil.move(str(source), str(destination))
    return _json(ok=True, operation=operation, source=str(source),
                 destination=str(destination), changed=True)


def _text_replace(args: dict, write_roots: tuple[Path, ...], cwd: Path) -> str:
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
        shutil.copymode(target, staged)
        os.replace(staged, target)
    finally:
        if staged is not None and staged.exists():
            staged.unlink()
    return _json(ok=True, operation="text_replace", path=str(target),
                 matches=count, changed=True)


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
            return _json(ok=True, format=fmt, path=str(path), columns=fields, samples=rows,
                         sample_count=len(rows))
        if fmt == "jsonl":
            records = []
            with path.open("r", encoding="utf-8-sig") as stream:
                for line in stream:
                    if line.strip():
                        records.append(json.loads(line))
                    if len(records) >= limit:
                        break
            return _json(ok=True, format=fmt, path=str(path),
                         samples=records, sample_count=len(records))
        with path.open("r", encoding="utf-8-sig") as stream:
            payload = json.load(stream)
        if isinstance(payload, list):
            return _json(ok=True, format=fmt, path=str(path), kind="array",
                         total_items=len(payload), samples=payload[:limit])
        if isinstance(payload, dict):
            return _json(ok=True, format=fmt, path=str(path), kind="object",
                         keys=list(payload)[:100], sample={k: payload[k] for k in list(payload)[:limit]})
        return _json(ok=True, format=fmt, path=str(path), kind=type(payload).__name__,
                     sample=payload)
    except (OSError, UnicodeError, ValueError, csv.Error) as exc:
        return _error("parse_failed", f"Could not parse {path.name}: {type(exc).__name__}: {exc}")


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


async def execute_toolkit(registry: Any, name: str, args: dict) -> str:
    """Called only after Tool Registry admission/audit/fingerprint dispatch."""
    if name == "tool_find":
        all_allowed = tuple(item["function"]["name"]
                            for item in registry.get_tool_definitions(tiers=None))
        return tool_find(args, all_allowed)
    read_roots, _, cwd = registry._read_scope()
    write_roots = tuple(registry.access_roots)
    try:
        if name == "code_search":
            return await _code_search(args, read_roots, cwd)
        dispatch = {
            "project_locate": lambda: _project_locate(args, read_roots, cwd),
            "git_inspect": lambda: _git_inspect(args, read_roots, cwd),
            "file_manage": lambda: _file_manage(args, write_roots, read_roots, cwd),
            "text_replace": lambda: _text_replace(args, write_roots, cwd),
            "data_inspect": lambda: _data_inspect(args, read_roots, cwd),
        }
        if name not in dispatch:
            return _error("unknown_tool", f"No toolkit implementation for {name}.")
        return await asyncio.to_thread(dispatch[name])
    except asyncio.CancelledError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        return _error("tool_failed", f"{name}: {type(exc).__name__}: {exc}")
