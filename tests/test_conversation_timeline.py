from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from orchestrator.bridge_memory import BridgeMemoryStore


def _write_recent_context(path, exchanges):
    rows = []
    for user_ts, assistant_ts, user_text, assistant_text in exchanges:
        rows.extend(
            (
                {
                    "role": "user",
                    "text": user_text,
                    "source": "text",
                    "ts": user_ts,
                },
                {
                    "role": "assistant",
                    "text": assistant_text,
                    "source": "her-v2",
                    "ts": assistant_ts,
                },
            )
        )
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_completed_exchange_ledger_recovers_transcript_and_survives_turn_clear(tmp_path):
    _write_recent_context(
        tmp_path / "recent_context.jsonl",
        [
            (
                "2026-08-24T14:35:00+10:00",
                "2026-08-24T14:36:00+10:00",
                "Tell me what is in the Health folder",
                "The Health folder contains five Excel files.",
            ),
            (
                "2026-08-24T16:41:00+10:00",
                "2026-08-24T16:42:00+10:00",
                "Where did we just get to?",
                "I answered from the wrong older topic.",
            ),
        ],
    )

    store = BridgeMemoryStore(tmp_path)
    recovered = store.get_completed_exchanges(limit=10)

    assert [entry["user_text"] for entry in recovered] == [
        "Tell me what is in the Health folder",
        "Where did we just get to?",
    ]
    assert all(entry["origin"] == "transcript" for entry in recovered)
    assert all(entry["exchange_id"] > 0 for entry in recovered)

    store.record_turn("user", "text", "disposable working user turn")
    store.record_turn("assistant", "her-v2", "disposable working answer")
    assert store.clear_turns() == 2

    after_clear = store.get_completed_exchanges(limit=10)
    assert [entry["user_text"] for entry in after_clear] == [
        "Tell me what is in the Health folder",
        "Where did we just get to?",
    ]


def test_transcript_reconciliation_deduplicates_live_completed_exchange(tmp_path):
    store = BridgeMemoryStore(tmp_path)
    store.record_completed_exchange(
        "same user",
        "same assistant",
        "text",
        user_ts="2026-08-24T18:00:00+10:00",
        assistant_ts="2026-08-24T18:00:05+10:00",
        origin="primary",
        origin_ref="session:req-1",
    )
    _write_recent_context(
        tmp_path / "recent_context.jsonl",
        [
            (
                "2026-08-24T18:00:01+10:00",
                "2026-08-24T18:00:06+10:00",
                "same user",
                "same assistant",
            )
        ],
    )

    assert store.reconcile_recent_transcript() == 0
    with sqlite3.connect(store.db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM conversation_exchanges"
        ).fetchone()[0]
    assert count == 1


def test_completed_exchange_query_keeps_logs_but_excludes_pre_fresh_requests(tmp_path):
    store = BridgeMemoryStore(tmp_path)
    store.record_completed_exchange(
        "old request",
        "old answer completed late",
        "text",
        user_ts="2026-08-25T10:00:00+10:00",
        assistant_ts="2026-08-25T10:30:05+10:00",
        origin_ref="old",
    )
    store.record_completed_exchange(
        "new request",
        "new answer",
        "text",
        user_ts="2026-08-25T10:30:01+10:00",
        assistant_ts="2026-08-25T10:30:06+10:00",
        origin_ref="new",
    )
    cutoff = datetime.fromisoformat("2026-08-25T10:30:00+10:00").timestamp()

    assert [
        row["user_text"]
        for row in store.get_completed_exchanges(limit=10, after_epoch=cutoff)
    ] == ["new request"]

    # The boundary is a read filter, not deletion.
    assert [
        row["user_text"] for row in store.get_completed_exchanges(limit=10)
    ] == ["old request", "new request"]
