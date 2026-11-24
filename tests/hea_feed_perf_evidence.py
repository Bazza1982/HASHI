"""HEA feed query-plan and performance evidence (Part H).

Builds a realistic multi-owner, multi-session database, seeds terminal events,
then prints EXPLAIN QUERY PLAN plus measured feed query durations.
"""
from __future__ import annotations

import random
import tempfile
import time
from pathlib import Path

import sys

# Import the *current* repository checkout, never another installed or
# historical HASHI package. This script lives in <repo>/tests/.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from orchestrator.session_store import SessionStore
from orchestrator.automation_events import terminal_events_page

random.seed(20261120)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions.db", instance_id="HASHI-PERF")
        owners = [f"user:{i}" for i in range(200)]
        sessions = []
        for owner in owners:
            for agent in ("agent-a", "agent-b"):
                sessions.append(
                    store.resolve_primary_session(owner_id=owner, agent_id=agent, establish=True)
                )
        events = 0
        started = time.monotonic()
        with store._lock, store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for index, session in enumerate(sessions):
                kind = "run.completed" if index % 3 else "run.failed"
                for _ in range(20):
                    store._append_event(
                        connection,
                        session_id=session["session_id"],
                        run_id=None,
                        kind=kind,
                        status="completed" if kind == "run.completed" else "failed",
                        phase="terminal",
                    )
                    events += 1
        seed_seconds = time.monotonic() - started
        print(f"seeded {len(sessions)} sessions / {events} terminal events in {seed_seconds:.2f}s")

        with store._lock, store._connection() as connection:
            plan = connection.execute(
                """EXPLAIN QUERY PLAN
                SELECT i.feed_id AS cursor, i.event_id, i.run_id, i.session_id,
                       i.kind, i.created_at, i.agent_id,
                       COALESCE(r.idempotency_key, '') AS run_idempotency_key
                FROM automation_event_index AS i
                JOIN sessions AS s ON s.session_id = i.session_id
                LEFT JOIN runs AS r ON r.run_id = i.run_id
                WHERE i.owner_id = ?
                  AND s.status != 'deleted'
                  AND i.feed_id > ?
                ORDER BY i.feed_id ASC LIMIT ?""",
                ("user:7", 400, 51),
            ).fetchall()
        print("EXPLAIN QUERY PLAN (owner-scoped page):")
        for row in plan:
            print("  " + " | ".join(str(item) for item in row))

        target = "user:7"
        durations = []
        for _ in range(20):
            started = time.monotonic()
            page = terminal_events_page(
                store, owner_id=target, after=random.randint(0, events - 100), limit=50
            )
            durations.append((time.monotonic() - started) * 1000)
            assert len(page["events"]) <= 50
        print(f"page query (limit=50): median {sorted(durations)[len(durations)//2]:.2f}ms, "
              f"max {max(durations):.2f}ms over 20 samples")

        durations = []
        for _ in range(20):
            started = time.monotonic()
            terminal_events_page(store, owner_id=target, after=events - 1, limit=50)
            durations.append((time.monotonic() - started) * 1000)
        print(f"idle long-poll check: median {sorted(durations)[len(durations)//2]:.2f}ms, "
              f"max {max(durations):.2f}ms over 20 samples")


if __name__ == "__main__":
    main()
# HASHI-Fingerprint: hfp_82f77fd9fc9a4145a158b5de7b5d00ff
