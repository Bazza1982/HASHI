#!/usr/bin/env python3
"""Archive and clear HASHI dual-brain continuity notebooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ModuleNotFoundError:  # Windows native runtime has no fcntl.
    fcntl = None


DEFAULT_ROOTS = (
    Path("/home/lily/projects/hashi"),
    Path("/home/lily/projects/hashi2"),
    Path("/mnt/c/Users/thene/projects/HASHI"),
)
NOTEBOOK_RELATIVE_PATH = Path("memory") / "left_brain_continuity.jsonl"
ARCHIVE_DIR_NAME = "left_brain_archives"
DEFAULT_LOG_RELATIVE_PATH = Path("workspaces") / "lily" / "logs" / "dual_brain_notepad_reset.jsonl"
TIMEZONE = "Australia/Sydney"
FALLBACK_TIMEZONE = timezone(timedelta(hours=10), name="AEST")


def _lock_file(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _local_timezone():
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        return FALLBACK_TIMEZONE


@dataclass(frozen=True)
class ResetResult:
    root: Path
    agent: str
    notebook: Path
    status: str
    bytes_read: int = 0
    lines_read: int = 0
    archive_path: Path | None = None
    reason: str = ""


def _unique_existing_roots(roots: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def _discover_agent_workspaces(root: Path, agents: list[str] | None = None) -> list[Path]:
    workspace_root = root / "workspaces"
    if not workspace_root.exists():
        return []
    if agents:
        return [workspace_root / agent for agent in agents if (workspace_root / agent).exists()]
    return sorted(path for path in workspace_root.iterdir() if path.is_dir())


def _safe_fragment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-")[:80] or "manual"


def _line_count(content: str) -> int:
    if not content:
        return 0
    return len(content.splitlines())


def _archive_and_clear_notebook(
    root: Path,
    agent: str,
    notebook: Path,
    *,
    dry_run: bool,
    trigger: str,
    publish_id: str,
    now: datetime,
) -> ResetResult:
    if not notebook.exists():
        return ResetResult(root=root, agent=agent, notebook=notebook, status="skipped", reason="missing")
    if notebook.stat().st_size == 0:
        return ResetResult(root=root, agent=agent, notebook=notebook, status="skipped", reason="empty")

    if dry_run:
        content = notebook.read_text(encoding="utf-8")
        return ResetResult(
            root=root,
            agent=agent,
            notebook=notebook,
            status="dry-run",
            bytes_read=len(content.encode("utf-8")),
            lines_read=_line_count(content),
            reason="would archive and clear",
        )

    with notebook.open("r+", encoding="utf-8") as handle:
        _lock_file(handle)
        try:
            content = handle.read()
            if not content:
                return ResetResult(root=root, agent=agent, notebook=notebook, status="skipped", reason="empty")

            content_bytes = content.encode("utf-8")
            archive_dir = notebook.parent / ARCHIVE_DIR_NAME / now.strftime("%Y-%m-%d")
            archive_dir.mkdir(parents=True, exist_ok=True)
            filename = (
                f"left_brain_continuity_{now.strftime('%Y%m%d_%H%M%S')}_"
                f"{_safe_fragment(trigger)}.jsonl"
            )
            archive_path = archive_dir / filename
            archive_path.write_text(content, encoding="utf-8")
            manifest = {
                "archived_at": now.isoformat(),
                "trigger": trigger,
                "publish_id": publish_id,
                "root": str(root),
                "agent": agent,
                "source_path": str(notebook),
                "archive_path": str(archive_path),
                "bytes": len(content_bytes),
                "lines": _line_count(content),
                "sha256": hashlib.sha256(content_bytes).hexdigest(),
            }
            archive_path.with_suffix(".manifest.json").write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            handle.seek(0)
            handle.truncate(0)
            handle.flush()
        finally:
            _unlock_file(handle)

    return ResetResult(
        root=root,
        agent=agent,
        notebook=notebook,
        status="cleared",
        bytes_read=len(content_bytes),
        lines_read=_line_count(content),
        archive_path=archive_path,
    )


def reset_notepads(
    roots: list[Path],
    *,
    agents: list[str] | None = None,
    dry_run: bool = False,
    trigger: str = "manual",
    publish_id: str = "",
    now: datetime | None = None,
) -> list[ResetResult]:
    now = now or datetime.now(_local_timezone())
    results: list[ResetResult] = []
    for root in _unique_existing_roots(roots):
        for workspace in _discover_agent_workspaces(root, agents):
            notebook = workspace / NOTEBOOK_RELATIVE_PATH
            results.append(
                _archive_and_clear_notebook(
                    root,
                    workspace.name,
                    notebook,
                    dry_run=dry_run,
                    trigger=trigger,
                    publish_id=publish_id,
                    now=now,
                )
            )
    return results


def _result_to_log_dict(result: ResetResult) -> dict[str, object]:
    return {
        "root": str(result.root),
        "agent": result.agent,
        "notebook": str(result.notebook),
        "status": result.status,
        "bytes_read": result.bytes_read,
        "lines_read": result.lines_read,
        "archive_path": str(result.archive_path) if result.archive_path else "",
        "reason": result.reason,
    }


def _default_log_paths(roots: list[Path]) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for root in _unique_existing_roots(roots):
        path = root / DEFAULT_LOG_RELATIVE_PATH
        if path not in seen:
            paths.append(path)
            seen.add(path)
    return paths


def _append_jsonl_log(log_paths: list[Path], event: dict[str, object]) -> None:
    for log_path in log_paths:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            _lock_file(handle)
            try:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            finally:
                _unlock_file(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Archive and clear HASHI dual-brain notepads.")
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        help="HASHI root to scan. Defaults to known local HASHI roots.",
    )
    parser.add_argument("--agent", action="append", help="Limit clearing to one agent. Repeatable.")
    parser.add_argument("--dry-run", action="store_true", help="Report actions without writing files.")
    parser.add_argument("--trigger", default="manual", help="Operational trigger name for the archive manifest.")
    parser.add_argument("--publish-id", default="", help="Wiki publish id or run id that triggered the reset.")
    parser.add_argument(
        "--log-file",
        action="append",
        type=Path,
        help="JSONL audit log path. Defaults to workspaces/lily/logs/dual_brain_notepad_reset.jsonl in each HASHI root.",
    )
    args = parser.parse_args(argv)

    roots = args.root if args.root else list(DEFAULT_ROOTS)
    now = datetime.now(_local_timezone())
    log_paths = [path.expanduser().resolve() for path in args.log_file] if args.log_file else _default_log_paths(roots)
    print("[dual_brain_notepad_reset] mode=reset")
    print(f"[dual_brain_notepad_reset] dry_run={bool(args.dry_run)}")
    print(f"[dual_brain_notepad_reset] trigger={args.trigger}")
    print(f"[dual_brain_notepad_reset] publish_id={args.publish_id or '(none)'}")
    print("[dual_brain_notepad_reset] roots=" + ", ".join(str(path) for path in roots))
    print("[dual_brain_notepad_reset] agents=" + (", ".join(args.agent) if args.agent else "all"))
    print("[dual_brain_notepad_reset] log_files=" + ", ".join(str(path) for path in log_paths))

    try:
        results = reset_notepads(
            roots,
            agents=args.agent,
            dry_run=args.dry_run,
            trigger=args.trigger,
            publish_id=args.publish_id,
            now=now,
        )
    except Exception as exc:
        _append_jsonl_log(
            log_paths,
            {
                "ts": now.isoformat(),
                "mode": "reset",
                "success": False,
                "dry_run": bool(args.dry_run),
                "trigger": args.trigger,
                "publish_id": args.publish_id,
                "roots": [str(path) for path in roots],
                "agents": args.agent or "all",
                "error": f"{type(exc).__name__}: {exc}",
                "results": [],
            },
        )
        print(f"[dual_brain_notepad_reset] failure={type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not results:
        _append_jsonl_log(
            log_paths,
            {
                "ts": now.isoformat(),
                "mode": "reset",
                "success": True,
                "dry_run": bool(args.dry_run),
                "trigger": args.trigger,
                "publish_id": args.publish_id,
                "roots": [str(path) for path in roots],
                "agents": args.agent or "all",
                "cleared": 0,
                "checked": 0,
                "results": [],
            },
        )
        print("[dual_brain_notepad_reset] result=no matching workspaces")
        return 0

    cleared = 0
    for result in results:
        if result.status == "cleared":
            cleared += 1
        archive = f" archive={result.archive_path}" if result.archive_path else ""
        reason = f" reason={result.reason}" if result.reason else ""
        print(
            "[dual_brain_notepad_reset] "
            f"agent={result.agent} status={result.status} notebook={result.notebook} "
            f"bytes={result.bytes_read} lines={result.lines_read}{archive}{reason}"
        )
    _append_jsonl_log(
        log_paths,
        {
            "ts": now.isoformat(),
            "mode": "reset",
            "success": True,
            "dry_run": bool(args.dry_run),
            "trigger": args.trigger,
            "publish_id": args.publish_id,
            "roots": [str(path) for path in roots],
            "agents": args.agent or "all",
            "cleared": cleared,
            "checked": len(results),
            "results": [_result_to_log_dict(result) for result in results],
        },
    )
    print(f"[dual_brain_notepad_reset] success=true cleared={cleared} checked={len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
