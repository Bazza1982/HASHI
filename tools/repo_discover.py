"""Read-only local repository discovery for HERV3.

Answers "where is the local checkout of repository X?" without scanning
repository contents. It inspects candidate directories directly under the
requested parents and reads Git metadata (``.git`` directory or worktree
``gitdir`` file, remotes and HEAD branch). A repository whose folder name
differs from its GitHub name is found through its remote URL or remote name.

Deliberately no daemon, no index, no network access and no new authorization
system: the existing read scope applies exactly as it does for file_search,
and every candidate outside that scope is skipped without being traversed.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from tools.file_search import outcome, failed, resolve_search_budget

MAX_RESULTS_DEFAULT = 20
MAX_RESULTS_CAP = 100
MAX_ROOTS = 32


class MetadataUnavailable(Exception):
    """An existing read boundary or filesystem error prevents discovery."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MetadataReader:
    """Apply the tool's existing read scope to every physical metadata path."""

    def __init__(self, read_roots, protected_read_paths, cwd, *, allow_wsl=False):
        self.read_roots = read_roots
        self.protected = tuple(Path(path).resolve() for path in protected_read_paths)
        self.cwd = cwd
        self.allow_wsl = allow_wsl

    def resolve(self, path: Path) -> Path:
        from tools.builtins import _resolve_path
        try:
            actual = _resolve_path(str(path), self.read_roots, self.cwd,
                                   allow_wsl_windows_drive_paths=self.allow_wsl)
        except ValueError:
            raise MetadataUnavailable("metadata_out_of_scope") from None
        except (OSError, RuntimeError):
            raise MetadataUnavailable("metadata_unavailable") from None
        if any(actual == protected or actual.is_relative_to(protected)
               for protected in self.protected):
            raise MetadataUnavailable("metadata_protected")
        return actual

    def text(self, path: Path) -> str | None:
        actual = self.resolve(path)
        try:
            return actual.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return None  # Optional metadata or an absent fallback is normal.
        except OSError:
            raise MetadataUnavailable("metadata_unavailable") from None


def _remote_url(value: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    value = value.strip().strip('"')
    try:
        parsed = urlsplit(value)
        if parsed.netloc:
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1],
                               parsed.path, "", ""))
        # Git also permits SCP-style user@host:path remotes.
        return value.rsplit("@", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    except ValueError:
        return "[unavailable remote URL]"


def _read_remotes(git_dir: Path, common_dir: Path | None,
                  reader: MetadataReader) -> list[dict]:
    """Read effective local configs only; never follow includes or credentials."""
    configs = ([common_dir / "config"] if common_dir is not None else [])
    configs.append(git_dir / "config")
    if common_dir is not None:
        configs.append(git_dir / "config.worktree")
    remotes: dict[str, dict] = {}
    for config in dict.fromkeys(configs):
        content = reader.text(config)
        if content is None:
            continue
        section: str | None = None
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("[") and line.endswith("]"):
                inner = line[1:-1].strip()
                if inner.startswith("remote "):
                    section = inner[len("remote "):].strip().strip('"').strip("'")
                else:
                    section = None
                continue
            if section and "=" in line:
                key, _, value = line.partition("=")
                if key.strip().lower() == "url":
                    remotes.setdefault(section, {})["url"] = _remote_url(value)
    return [{"name": name, **values} for name, values in remotes.items() if values.get("url")]


def _git_dirs_for(candidate: Path, reader: MetadataReader
                  ) -> tuple[Path | None, Path | None, bool]:
    """Resolve and authorise pointers before examining their metadata."""
    dot = reader.resolve(candidate / ".git")
    if dot.is_dir():
        return dot, None, False
    if dot.is_file():
        content = reader.text(dot)
        if content and content.strip().startswith("gitdir:"):
            target = content.strip()[len("gitdir:"):].strip()
            git_dir = Path(target)
            if not git_dir.is_absolute():
                git_dir = candidate / git_dir
            git_dir = reader.resolve(git_dir)
            if not git_dir.is_dir():
                raise MetadataUnavailable("metadata_unavailable")
            relative = reader.text(git_dir / "commondir")
            common_dir = reader.resolve(git_dir / relative.strip()) if relative else git_dir
            if not common_dir.is_dir():
                raise MetadataUnavailable("metadata_unavailable")
            return git_dir, common_dir, True
    return None, None, False


def _branch_for(git_dir: Path, common_dir: Path | None,
                reader: MetadataReader) -> str | None:
    # A linked worktree's HEAD owns its branch; shared HEAD is only a fallback
    # for missing metadata, never for denied reads or a detached worktree.
    for base in dict.fromkeys((git_dir, common_dir)):
        if base is None:
            continue
        content = reader.text(base / "HEAD")
        if content is None:
            continue
        head = content.strip()
        if head.startswith("ref: refs/heads/"):
            return head[len("ref: refs/heads/"):]
        return head[:12] if head else None
    return None


def _inspect(candidate: Path, reader: MetadataReader) -> dict | None:
    git_dir, common_dir, worktree = _git_dirs_for(candidate, reader)
    if git_dir is None:
        return None
    return {
        "path": str(candidate),
        "name": candidate.name,
        "remotes": _read_remotes(git_dir, common_dir, reader),
        "branch": _branch_for(git_dir, common_dir, reader),
        "worktree": worktree,
    }


def _matches(query: str, info: dict) -> bool:
    needle = query.casefold()
    if needle in info["name"].casefold():
        return True
    for remote in info["remotes"]:
        if needle in remote["name"].casefold():
            return True
        if needle in remote.get("url", "").casefold():
            return True
    return False


def _discover(roots: list[dict], resolved_read_roots: list[Path], query: str,
              max_results: int, include_root_itself: bool, include_hidden: bool,
              deadline: float | None, reader: MetadataReader) -> dict:
    matches: list[dict] = []
    coverage: list[dict] = []
    errors: list[dict] = []
    counters = {"directories_enumerated": 0, "files_enumerated": 0,
                "repos_inspected": 0, "matches": 0,
                "skipped_out_of_scope": 0, "errors": 0}
    stop_reason = "completed"
    warnings: list[str] = []

    def inspect(candidate: Path) -> dict | None:
        counters["repos_inspected"] += 1
        try:
            return _inspect(candidate, reader)
        except (MetadataUnavailable, OSError) as exc:
            counters["errors"] += 1
            if len(errors) < 12:
                errors.append({"path": str(candidate)[:512],
                               "code": exc.code if isinstance(exc, MetadataUnavailable)
                               else "metadata_unavailable"})
            return None

    def authorized(child: Path, linked: bool) -> bool:
        target = child.resolve() if linked else child
        return any(target == root or target.is_relative_to(root)
                   for root in resolved_read_roots)

    for root in roots:
        if deadline is not None and time.time() >= deadline:
            stop_reason = "deadline"
            break
        if root.get("resolved") is None:
            coverage.append({"path": str(root.get("path")), "error": root.get("error"),
                             "exhausted": True})
            counters["errors"] += 1
            continue
        path: Path = root["resolved"]
        if not path.exists():
            coverage.append({"path": str(path), "error": "root_unavailable", "exhausted": True})
            counters["errors"] += 1
            continue
        root_coverage = {"path": str(path), "exhausted": False}
        coverage.append(root_coverage)
        if include_root_itself and path.is_dir():
            info = inspect(path)
            if info and _matches(query, info):
                matches.append(info)
                counters["matches"] += 1
                if len(matches) >= max_results:
                    stop_reason = "page_limit"
                    break
        if stop_reason != "completed":
            break
        try:
            with os.scandir(path) as iterator:
                for entry in iterator:
                    if deadline is not None and time.time() >= deadline:
                        stop_reason = "deadline"
                        break
                    if entry.name.startswith(".") and not include_hidden:
                        continue
                    if not (entry.is_dir(follow_symlinks=False) or entry.is_symlink()):
                        continue
                    counters["directories_enumerated"] += 1
                    child = Path(entry.path)
                    linked = entry.is_symlink() or (
                        hasattr(child, "is_junction") and child.is_junction())
                    if not authorized(child, linked):
                        counters["skipped_out_of_scope"] += 1
                        continue
                    info = inspect(child)
                    if info is None:
                        continue
                    if linked:
                        info["linked"] = True
                    if _matches(query, info):
                        matches.append(info)
                        counters["matches"] += 1
                        if len(matches) >= max_results:
                            stop_reason = "page_limit"
                            break
        except OSError as exc:
            root_coverage["error"] = type(exc).__name__
            counters["errors"] += 1
            if len(errors) < 12:
                errors.append({"path": str(path)[:512], "code": type(exc).__name__})
            continue
        root_coverage["exhausted"] = stop_reason != "deadline"
        if stop_reason != "completed":
            break
    if stop_reason == "deadline":
        warnings.append(
            "Execution budget reached; repository discovery has no continuation "
            "cursor. Narrow the roots, request a longer timeout_seconds, or "
            "inspect a specific directory directly.")
    elif stop_reason == "page_limit":
        warnings.append(
            "Result limit reached before every candidate was inspected; narrow "
            "the roots or raise max_results for a more complete answer.")
    coverage_complete = stop_reason == "completed" and counters["errors"] == 0
    scope_exhausted = stop_reason == "completed"
    return {"matches": matches, "coverage": {"roots": coverage, "errors": errors},
            "counters": counters, "stop_reason": stop_reason, "warnings": warnings,
            "coverage_complete": coverage_complete, "scope_exhausted": scope_exhausted}


async def execute_repo_discover(args, *, read_roots, cwd, search_scope, audit_context,
                                options=None, protected_read_paths=()):
    from tools.builtins import BuiltinExecutionResult, _resolve_path
    query = str(args.get("query") or "").strip()
    if not query or len(query) > 1024:
        return failed("query must be a non-empty string of at most 1024 characters",
                      code="invalid_query")
    max_results = args.get("max_results", MAX_RESULTS_DEFAULT)
    if type(max_results) is not int or not 1 <= max_results <= MAX_RESULTS_CAP:
        return failed(f"max_results must be 1..{MAX_RESULTS_CAP}")
    include_root_itself = args.get("include_root_itself", True)
    if not isinstance(include_root_itself, bool):
        include_root_itself = True
    include_hidden = args.get("include_hidden", False)
    if not isinstance(include_hidden, bool):
        include_hidden = False
    raw_roots = args.get("roots")
    defaults = search_scope.get("preferred_roots") or [
        {"path": str(cwd.resolve()), "provenance": "execution-cwd"}]
    if raw_roots is None:
        entries = defaults
    else:
        if not isinstance(raw_roots, list) or not raw_roots or len(raw_roots) > MAX_ROOTS:
            return failed(f"roots must contain 1 to {MAX_ROOTS} exact paths")
        entries = [{"path": value, "provenance": "tool-explicit"} for value in raw_roots]
    roots = []
    for item in entries:
        raw = item.get("path")
        if not isinstance(raw, str) or not raw or len(raw) > 4096:
            return failed("root must be a non-empty path of at most 4096 characters")
        try:
            resolved = _resolve_path(
                raw, read_roots, cwd,
                allow_wsl_windows_drive_paths=audit_context.get(
                    "allow_wsl_windows_drive_paths", False))
        except (ValueError, OSError) as exc:
            roots.append({"path": raw, "resolved": None, "error": str(exc)})
            continue
        roots.append({"path": raw, "resolved": resolved, "error": None})
    try:
        timeout, budget_policy = resolve_search_budget(
            args, options=options or {}, operation="repo_discover")
    except ValueError as exc:
        return failed(str(exc), code="invalid_timeout")
    deadline = time.time() + timeout if timeout is not None else None
    started = time.monotonic()
    resolved_read_roots = [Path(root).resolve() for root in read_roots]
    reader = MetadataReader(resolved_read_roots, protected_read_paths, cwd,
                            allow_wsl=audit_context.get("allow_wsl_windows_drive_paths", False))
    data = await asyncio.to_thread(
        _discover, roots, resolved_read_roots, query, max_results,
        include_root_itself, include_hidden, deadline, reader)
    data.update(query=query, max_results=max_results, elapsed_ms=int((time.monotonic() - started) * 1000),
                execution_budget={**budget_policy, "effective_seconds": timeout},
                can_continue=False, next_cursor=None)
    status = "success" if data["coverage_complete"] else "partial"
    if data["stop_reason"] != "deadline" and all(
            root.get("error") for root in data["coverage"]["roots"]) and data["coverage"]["roots"]:
        status = "failed"
    error = None
    if status == "failed":
        error = {"code": data["stop_reason"],
                 "message": "No valid repository coverage; inspect coverage errors",
                 "retryable": False}
    return BuiltinExecutionResult(outcome(status, data, error=error),
                                  {"search_outcome": status,
                                   "stop_reason": data["stop_reason"],
                                   "coverage_complete": data["coverage_complete"]})
