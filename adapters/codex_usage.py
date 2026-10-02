"""Turn usage from Codex's cumulative, per-thread JSON event counters."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

from adapters.base import TokenUsage


def cumulative_usage(raw: Mapping) -> TokenUsage:
    """Validate a Codex ``turn.completed.usage`` snapshot."""

    def count(key: str, *, default: int = 0) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool):
            raise ValueError(f"Invalid Codex usage field {key}")
        result = int(value)
        if result < 0:
            raise ValueError(f"Negative Codex usage field {key}")
        return result

    input_tokens = count("input_tokens")
    cache_key = next(
        (key for key in ("cached_input_tokens", "prompt_cache_hit_tokens") if key in raw),
        None,
    )
    cached = count(cache_key) if cache_key is not None else None
    output_tokens = count("output_tokens")
    reasoning = count(
        "reasoning_output_tokens"
        if "reasoning_output_tokens" in raw else "reasoning_tokens"
    )
    if cached is not None and cached > input_tokens:
        raise ValueError("Codex cached input exceeds input tokens")
    if reasoning > output_tokens:
        raise ValueError("Codex reasoning output exceeds output tokens")
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        thinking_tokens=reasoning,
        prompt_cache_hit_tokens=cached,
        prompt_cache_miss_tokens=(
            input_tokens - cached if cached is not None else None
        ),
    )


def last_logged_usage(
    event_log_path: Path, thread_id: str, *, backup_count: int
) -> TokenUsage | None:
    """Recover a pre-upgrade thread baseline from bounded Codex event logs."""

    current_thread: str | None = None
    latest: TokenUsage | None = None
    paths = [
        event_log_path.with_name(f"{event_log_path.name}.{index}")
        for index in range(backup_count, 0, -1)
    ] + [event_log_path]
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as source:
            for line in source:
                if 'thread.started' not in line and 'turn.completed' not in line:
                    continue
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, Mapping):
                    continue
                if event.get("type") == "thread.started":
                    current_thread = str(event.get("thread_id") or "") or None
                elif event.get("type") == "turn.completed" and current_thread == thread_id:
                    usage = event.get("usage")
                    if isinstance(usage, Mapping):
                        try:
                            latest = cumulative_usage(usage)
                        except (TypeError, ValueError):
                            continue
    return latest


class CodexUsageCheckpoints:
    """Persist the last provider counter per thread across Worker generations."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_usage (
                thread_id TEXT PRIMARY KEY,
                input_tokens INTEGER NOT NULL,
                cached_input_tokens INTEGER,
                output_tokens INTEGER NOT NULL,
                reasoning_output_tokens INTEGER NOT NULL
            )
            """
        )
        return connection

    def has_snapshot(self, thread_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM thread_usage WHERE thread_id = ?", (thread_id,)
            ).fetchone()
        return row is not None

    def record_turn(
        self,
        thread_id: str,
        current: TokenUsage,
        *,
        new_thread: bool,
        logged_baseline: TokenUsage | None = None,
    ) -> TokenUsage | None:
        """Return this turn's delta, or unknown when a safe baseline is absent."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM thread_usage WHERE thread_id = ?", (thread_id,)
                ).fetchone()
                if row is not None:
                    previous = TokenUsage(
                        input_tokens=row["input_tokens"],
                        output_tokens=row["output_tokens"],
                        thinking_tokens=row["reasoning_output_tokens"],
                        prompt_cache_hit_tokens=row["cached_input_tokens"],
                    )
                elif new_thread:
                    previous = TokenUsage(
                        prompt_cache_hit_tokens=(
                            0 if current.prompt_cache_hit_tokens is not None else None
                        )
                    )
                else:
                    previous = logged_baseline

                cache_now = current.prompt_cache_hit_tokens
                cache_before = (
                    previous.prompt_cache_hit_tokens if previous is not None else None
                )
                monotonic = previous is not None and all((
                    current.input_tokens >= previous.input_tokens,
                    current.output_tokens >= previous.output_tokens,
                    current.thinking_tokens >= previous.thinking_tokens,
                    cache_now is None or cache_before is None or cache_now >= cache_before,
                ))
                delta = None
                if monotonic:
                    input_delta = current.input_tokens - previous.input_tokens
                    cache_delta = (
                        cache_now - cache_before
                        if cache_now is not None and cache_before is not None else None
                    )
                    if cache_delta is None or cache_delta <= input_delta:
                        delta = TokenUsage(
                            input_tokens=input_delta,
                            output_tokens=current.output_tokens - previous.output_tokens,
                            thinking_tokens=(
                                current.thinking_tokens - previous.thinking_tokens
                            ),
                            prompt_cache_hit_tokens=cache_delta,
                            prompt_cache_miss_tokens=(
                                input_delta - cache_delta
                                if cache_delta is not None else None
                            ),
                        )
                connection.execute(
                    """
                    INSERT INTO thread_usage (
                        thread_id, input_tokens, cached_input_tokens,
                        output_tokens, reasoning_output_tokens
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(thread_id) DO UPDATE SET
                        input_tokens = excluded.input_tokens,
                        cached_input_tokens = excluded.cached_input_tokens,
                        output_tokens = excluded.output_tokens,
                        reasoning_output_tokens = excluded.reasoning_output_tokens
                    """,
                    (
                        thread_id, current.input_tokens, cache_now,
                        current.output_tokens, current.thinking_tokens,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return delta
