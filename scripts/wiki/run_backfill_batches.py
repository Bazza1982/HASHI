#!/usr/bin/env python3
"""Run HASHI wiki backfill in resumable batches."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scripts.wiki.config import WikiConfig, default_config
    from scripts.wiki.fetcher import fetch_new_memories
    from scripts.wiki.run_pipeline import drop_existing_completed_runs
    from scripts.wiki.state import WikiState
else:
    from .config import WikiConfig, default_config
    from .fetcher import fetch_new_memories
    from .run_pipeline import drop_existing_completed_runs
    from .state import WikiState


@dataclass(frozen=True)
class BackfillProgress:
    timestamp: str
    status: str
    batch_index: int
    batches_requested: int
    returncode: int | None
    elapsed_s: float
    watermark_before: int
    watermark_after: int
    classifiable_remaining_before: int
    classifiable_remaining_after: int
    skipped_remaining_after: int
    redacted_remaining_after: int
    command: list[str]
    message: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run resumable HASHI wiki manual backfill batches.")
    parser.add_argument("--batches", type=int, default=42, help="Maximum number of batches to run.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional raw rows to fetch per batch. Omit for historical backfill so completed-row gaps do not cause empty batches.",
    )
    parser.add_argument("--max-classify", type=int, default=250, help="Classifiable rows per batch.")
    parser.add_argument("--sleep-s", type=float, default=0.0, help="Optional pause between batches.")
    parser.add_argument(
        "--skip-consolidation-check",
        action="store_true",
        help="Pass through to run_pipeline.py for emergency/manual use.",
    )
    args = parser.parse_args(argv)

    config = default_config()
    report_dir = config.hashi_root / "workspaces/lily/wiki_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    lock_path = report_dir / "wiki_backfill_runner.lock"
    pid_path = report_dir / "wiki_backfill_runner.pid"
    progress_jsonl = report_dir / "wiki_backfill_progress.jsonl"
    progress_latest = report_dir / "wiki_backfill_progress_latest.json"

    lock_fd = _acquire_lock(lock_path)
    pid_path.write_text(str(os.getpid()) + "\n", encoding="utf-8")
    try:
        return run_batches(
            config,
            args,
            progress_jsonl=progress_jsonl,
            progress_latest=progress_latest,
        )
    finally:
        os.close(lock_fd)
        _remove_if_owned(lock_path)
        _remove_if_owned(pid_path)


def run_batches(
    config: WikiConfig,
    args: argparse.Namespace,
    *,
    progress_jsonl: Path,
    progress_latest: Path,
) -> int:
    for batch_index in range(1, args.batches + 1):
        before = remaining_counts(config)
        if before["classifiable_remaining"] <= 0:
            write_progress(
                progress_jsonl,
                progress_latest,
                BackfillProgress(
                    timestamp=now_local(config),
                    status="complete",
                    batch_index=batch_index - 1,
                    batches_requested=args.batches,
                    returncode=0,
                    elapsed_s=0.0,
                    watermark_before=before["watermark"],
                    watermark_after=before["watermark"],
                    classifiable_remaining_before=0,
                    classifiable_remaining_after=0,
                    skipped_remaining_after=before["skipped_remaining"],
                    redacted_remaining_after=before["redacted_remaining"],
                    command=[],
                    message="No classifiable wiki memories remain.",
                ),
            )
            return 0

        command = [
            sys.executable,
            str(config.hashi_root / "scripts/wiki/run_pipeline.py"),
            "--daily",
            "--classify",
            "--persist-classifications",
            "--pages-dry-run",
            "--max-classify",
            str(args.max_classify),
        ]
        if args.limit is not None:
            command.extend(["--limit", str(args.limit)])
        if args.skip_consolidation_check:
            command.append("--skip-consolidation-check")

        start = time.monotonic()
        result = subprocess.run(
            command,
            cwd=str(config.hashi_root),
            capture_output=True,
            text=True,
            timeout=config.classifier_timeout_s + 120,
        )
        elapsed = time.monotonic() - start
        after = remaining_counts(config)
        status = "ok" if result.returncode == 0 else "failed"
        message = _last_interesting_line(result.stdout) if result.returncode == 0 else (result.stderr or result.stdout)[-2000:]
        write_progress(
            progress_jsonl,
            progress_latest,
            BackfillProgress(
                timestamp=now_local(config),
                status=status,
                batch_index=batch_index,
                batches_requested=args.batches,
                returncode=result.returncode,
                elapsed_s=round(elapsed, 2),
                watermark_before=before["watermark"],
                watermark_after=after["watermark"],
                classifiable_remaining_before=before["classifiable_remaining"],
                classifiable_remaining_after=after["classifiable_remaining"],
                skipped_remaining_after=after["skipped_remaining"],
                redacted_remaining_after=after["redacted_remaining"],
                command=command,
                message=message,
            ),
        )
        print(
            f"[{batch_index}/{args.batches}] {status} "
            f"watermark {before['watermark']} -> {after['watermark']} "
            f"remaining {before['classifiable_remaining']} -> {after['classifiable_remaining']} "
            f"elapsed={elapsed:.2f}s",
            flush=True,
        )
        if result.returncode != 0:
            return result.returncode
        if args.sleep_s > 0:
            time.sleep(args.sleep_s)
    return 0


def remaining_counts(config: WikiConfig) -> dict[str, int]:
    with WikiState(config.wiki_state_db) as state:
        state.init_schema()
        result = fetch_new_memories(config, state)
        result = drop_existing_completed_runs(state, result)
        return {
            "watermark": state.get_last_classified_id(),
            "classifiable_remaining": len(result.classifiable),
            "skipped_remaining": len(result.skipped),
            "redacted_remaining": len(result.redacted),
        }


def write_progress(progress_jsonl: Path, progress_latest: Path, progress: BackfillProgress) -> None:
    payload = asdict(progress)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with progress_jsonl.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    progress_latest.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def now_local(config: WikiConfig) -> str:
    return datetime.now(ZoneInfo(config.timezone)).isoformat()


def _last_interesting_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else "batch completed"


def _acquire_lock(lock_path: Path) -> int:
    try:
        return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise SystemExit(f"Backfill runner lock exists: {lock_path}") from exc


def _remove_if_owned(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
