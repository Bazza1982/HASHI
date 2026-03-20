#!/usr/bin/env python3
"""Backfill sqlite-vec rows for an existing bridge_memory.sqlite database."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from orchestrator.bridge_memory import BridgeMemoryStore


def migrate_workspace(workspace_dir: Path, batch_size: int = 50):
    store = BridgeMemoryStore(workspace_dir)
    if not store._vec_enabled or not store._vec_dim:
        reason = store._vec_reason or "sqlite-vec/BGE-M3 not available"
        raise RuntimeError(f"Native vector index is not enabled: {reason}")

    db_path = workspace_dir / "bridge_memory.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    with store._connect() as conn:
        memory_rows = conn.execute(
            """
            SELECT m.id, m.content
            FROM memories m
            LEFT JOIN memory_vec v ON v.memory_id = m.id
            WHERE v.memory_id IS NULL
            ORDER BY m.id
            """
        ).fetchall()
        turn_rows = conn.execute(
            """
            SELECT t.id, t.text
            FROM turns t
            LEFT JOIN turns_vec v ON v.turn_id = t.id
            WHERE v.turn_id IS NULL
            ORDER BY t.id
            """
        ).fetchall()

        print(f"Backfilling {len(memory_rows)} memories and {len(turn_rows)} turns into {db_path}")

        for index, row in enumerate(memory_rows, start=1):
            embedding = store.encoder.encode(row["content"] or "")
            store._upsert_vec(conn, "memory_vec", "memory_id", int(row["id"]), embedding)
            if index % batch_size == 0:
                conn.commit()
                print(f"  memories: {index}/{len(memory_rows)}")
        conn.commit()

        for index, row in enumerate(turn_rows, start=1):
            embedding = store.encoder.encode(row["text"] or "")
            store._upsert_vec(conn, "turns_vec", "turn_id", int(row["id"]), embedding)
            if index % max(batch_size * 2, 100) == 0:
                conn.commit()
                print(f"  turns: {index}/{len(turn_rows)}")
        conn.commit()

    print("Vector backfill complete.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "workspace",
        help="Workspace directory containing bridge_memory.sqlite",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Commit interval for memory rows (default: 50)",
    )
    args = parser.parse_args()

    migrate_workspace(Path(args.workspace).expanduser().resolve(), batch_size=max(1, args.batch_size))


if __name__ == "__main__":
    main()
