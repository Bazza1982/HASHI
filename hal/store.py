"""HAL v0.1 durable store.

A private SQLite file, deliberately separate from the Session database so HAL
data can never collide with production conversation state and so the whole HAL
feature can be removed by deleting one file. Single-process access is expected
(the Workbench API server owns the runtime loop and the rule endpoints); a
module-level lock keeps concurrent requests honest.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from hal.runtime import (
    EXEC_ACTIVE,
    EXEC_FAILED,
    RECEIPT_DISPATCHED,
    RECEIPT_FAILED,
    program_to_json,
)

_LOCK = threading.Lock()
_WAL_PRAGMAS = ("PRAGMA journal_mode=WAL;", "PRAGMA synchronous=NORMAL;")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hal_rules (
    rule_id      TEXT PRIMARY KEY,
    owner_id     TEXT NOT NULL,
    name         TEXT NOT NULL,
    script       TEXT NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 1,
    enabled      INTEGER NOT NULL DEFAULT 0,
    enable_cursor INTEGER,
    migration_key TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hal_rules_owner ON hal_rules(owner_id);

CREATE TABLE IF NOT EXISTS hal_workflow_execs (
    exec_id          TEXT PRIMARY KEY,
    owner_id         TEXT NOT NULL,
    rule_id          TEXT NOT NULL,
    rule_revision    INTEGER NOT NULL,
    rule_name        TEXT NOT NULL,
    workflow_index   INTEGER NOT NULL DEFAULT 0,
    stage            INTEGER NOT NULL,
    state            TEXT NOT NULL,
    trigger_event_id TEXT NOT NULL,
    trigger_agent    TEXT,
    trigger_kind     TEXT,
    program_blob     TEXT NOT NULL,
    detail           TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hal_exec_rule_event
    ON hal_workflow_execs(rule_id, workflow_index, trigger_event_id);
CREATE INDEX IF NOT EXISTS idx_hal_exec_owner ON hal_workflow_execs(owner_id, created_at);

CREATE TABLE IF NOT EXISTS hal_dispatch_receipts (
    exec_id         TEXT NOT NULL,
    stage           INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    kind            TEXT NOT NULL,
    target          TEXT,
    payload         TEXT NOT NULL,
    state           TEXT NOT NULL,
    detail          TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_retry_at   REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (exec_id, stage)
);

CREATE TABLE IF NOT EXISTS hal_exec_events (
    exec_id  TEXT NOT NULL,
    event_id TEXT NOT NULL,
    PRIMARY KEY (exec_id, event_id)
);

CREATE TABLE IF NOT EXISTS hal_feed_cursor (
    owner_id TEXT PRIMARY KEY,
    cursor   INTEGER NOT NULL
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


class HalStoreError(ValueError):
    pass


class HalStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with _LOCK:
            for pragma in _WAL_PRAGMAS:
                self._conn.execute(pragma)
            self._conn.executescript(_SCHEMA)
            self._migrate_exec_dedupe_index()
            self._migrate_rule_enable_cursor()
            self._migrate_rule_migration_key()
            self._migrate_receipt_retry()
            self._conn.commit()

    def _migrate_exec_dedupe_index(self) -> None:
        """Recreate the exec dedupe index with workflow_index.

        v0.1 once shipped the unique index as (rule_id, trigger_event_id); a rule
        with two workflows triggered by the same event would collide and drop one
        execution. Drop the stale single-column index when present and recreate
        the composite one so the two workflows stay distinct.

        Caller must already hold ``_LOCK`` (this runs inside ``__init__``).
        """
        try:
            self._conn.execute("DROP INDEX IF EXISTS idx_hal_exec_rule_event")
        except sqlite3.Error:
            pass
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hal_exec_rule_event "
            "ON hal_workflow_execs(rule_id, workflow_index, trigger_event_id)"
        )

    def _migrate_rule_enable_cursor(self) -> None:
        """Add the per-rule enable baseline column to pre-existing stores.

        v0.1 anchored only an owner cursor; a second rule enabled after events
        were already written (but not yet consumed) would replay that history.
        Each rule now records the feed position captured at enable time.
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(hal_rules)")}
        if "enable_cursor" not in cols:
            self._conn.execute("ALTER TABLE hal_rules ADD COLUMN enable_cursor INTEGER")

    def _migrate_rule_migration_key(self) -> None:
        """Add the optional migration-key column to pre-existing stores.

        The key is a stable, client-supplied identity (e.g. the source HEA rule
        id + target instance) used to make a migration create idempotent: a
        repeated create with the same key returns the SAME draft instead of a
        duplicate. It is never used to rewrite or disable an existing rule.

        Caller must already hold ``_LOCK`` (this runs inside ``__init__``).
        """
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(hal_rules)")}
        if "migration_key" not in cols:
            self._conn.execute("ALTER TABLE hal_rules ADD COLUMN migration_key TEXT")
        # The unique index is created here (not in _SCHEMA) so it is only built
        # AFTER the column exists on both fresh and pre-existing stores.
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_hal_rules_migration_key "
            "ON hal_rules(owner_id, migration_key)"
        )

    def _migrate_receipt_retry(self) -> None:
        """Add bounded-retry bookkeeping to pre-existing receipt stores."""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(hal_dispatch_receipts)")}
        if "attempts" not in cols:
            self._conn.execute(
                "ALTER TABLE hal_dispatch_receipts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "next_retry_at" not in cols:
            self._conn.execute(
                "ALTER TABLE hal_dispatch_receipts ADD COLUMN next_retry_at REAL"
            )

    def close(self):
        with _LOCK:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # --- rules -----------------------------------------------------------

    def upsert_rule(
        self,
        *,
        owner_id: str,
        name: str,
        script: str,
        rule_id: str | None = None,
        expected_revision: int | None = None,
        migration_key: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        name = (name or "").strip()
        if not name:
            raise HalStoreError("rule name is required")
        if not (script or "").strip():
            raise HalStoreError("rule script is required")
        now = _now()
        with _LOCK:
            created = False
            if rule_id is None:
                rule_id = f"hl{uuid.uuid4().hex}"
                created = True
                self._conn.execute(
                    "INSERT INTO hal_rules (rule_id, owner_id, name, script, revision, "
                    "enabled, migration_key, created_at, updated_at) "
                    "VALUES (?,?,?,?,1,0,?,?,?)",
                    (rule_id, owner_id, name, script, migration_key, now, now),
                )
            else:
                row = self._conn.execute(
                    "SELECT revision FROM hal_rules WHERE rule_id=? AND owner_id=?",
                    (rule_id, owner_id),
                ).fetchone()
                if row is None:
                    created = True
                    self._conn.execute(
                        "INSERT INTO hal_rules (rule_id, owner_id, name, script, revision, "
                        "enabled, migration_key, created_at, updated_at) "
                        "VALUES (?,?,?,?,1,0,?,?,?)",
                        (rule_id, owner_id, name, script, migration_key, now, now),
                    )
                else:
                    if expected_revision is not None and int(row["revision"]) != expected_revision:
                        raise HalStoreError("rule revision conflict; refresh before saving")
                    # v0.1 draft semantics: any save to an existing rule returns
                    # it to draft (disabled); the client re-enables explicitly.
                    self._conn.execute(
                        "UPDATE hal_rules SET name=?, script=?, revision=revision+1, "
                        "enabled=0, updated_at=? WHERE rule_id=? AND owner_id=?",
                        (name, script, now, rule_id, owner_id),
                    )
            self._conn.commit()
            rule = self._conn.execute(
                "SELECT * FROM hal_rules WHERE rule_id=? AND owner_id=?",
                (rule_id, owner_id),
            ).fetchone()
        return dict(rule), created

    def get_rule(self, owner_id: str, rule_id: str) -> dict[str, Any] | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM hal_rules WHERE rule_id=? AND owner_id=?",
                (rule_id, owner_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def find_rule_by_migration_key(
        self, owner_id: str, migration_key: str
    ) -> dict[str, Any] | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM hal_rules WHERE owner_id=? AND migration_key=?",
                (owner_id, migration_key),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_rules(self, owner_id: str) -> list[dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM hal_rules WHERE owner_id=? ORDER BY created_at ASC",
                (owner_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_enabled(
        self,
        owner_id: str,
        rule_id: str,
        enabled: bool,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT revision FROM hal_rules WHERE rule_id=? AND owner_id=?",
                (rule_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                raise HalStoreError("rule revision conflict; refresh before saving")
            self._conn.execute(
                "UPDATE hal_rules SET enabled=?, updated_at=? WHERE rule_id=? AND owner_id=?",
                (1 if enabled else 0, _now(), rule_id, owner_id),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM hal_rules WHERE rule_id=? AND owner_id=?",
                (rule_id, owner_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_rule_enable_cursor(self, owner_id: str, rule_id: str, cursor: int) -> None:
        """Record the feed position a rule was enabled at (per-rule baseline)."""
        with _LOCK:
            self._conn.execute(
                "UPDATE hal_rules SET enable_cursor=?, updated_at=? WHERE rule_id=? AND owner_id=?",
                (int(cursor), _now(), rule_id, owner_id),
            )
            self._conn.commit()

    def enable_rule(
        self,
        *,
        owner_id: str,
        rule_id: str,
        expected_revision: int | None,
        baseline_cursor: int,
    ) -> dict[str, Any] | None:
        """Atomically enable one rule.

        The revision check, per-rule enable baseline, enabled-state update and
        the initial owner-cursor anchor all happen under ONE lock acquisition and
        ONE commit, so a concurrent enable/edit cannot interleave and corrupt a
        baseline. Raises :class:`HalStoreError` on revision conflict; returns
        ``None`` when the rule does not exist. On conflict nothing is written,
        including the owner cursor.
        """
        now = _now()
        with _LOCK:
            row = self._conn.execute(
                "SELECT revision FROM hal_rules WHERE rule_id=? AND owner_id=?",
                (rule_id, owner_id),
            ).fetchone()
            if row is None:
                return None
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                raise HalStoreError("rule revision conflict; refresh before saving")
            self._conn.execute(
                "UPDATE hal_rules SET enabled=1, enable_cursor=?, updated_at=? "
                "WHERE rule_id=? AND owner_id=?",
                (int(baseline_cursor), now, rule_id, owner_id),
            )
            # Anchor the owner cursor only when absent; never overwrite an
            # existing owner cursor, which would drop already-consumed history
            # for other enabled rules on the same owner.
            self._conn.execute(
                "INSERT INTO hal_feed_cursor (owner_id, cursor) VALUES (?,?) "
                "ON CONFLICT(owner_id) DO NOTHING",
                (owner_id, int(baseline_cursor)),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM hal_rules WHERE rule_id=? AND owner_id=?",
                (rule_id, owner_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def enabled_owner_ids(self) -> list[str]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT DISTINCT owner_id FROM hal_rules WHERE enabled=1"
            ).fetchall()
        return [str(r["owner_id"]) for r in rows]

    def enabled_rule_count(self, owner_id: str | None = None) -> int:
        with _LOCK:
            if owner_id is None:
                row = self._conn.execute("SELECT COUNT(*) AS n FROM hal_rules WHERE enabled=1").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM hal_rules WHERE enabled=1 AND owner_id=?",
                    (owner_id,),
                ).fetchone()
        return int(row["n"])

    # --- workflow executions ---------------------------------------------

    def create_exec(
        self,
        *,
        owner_id: str,
        rule_id: str,
        rule_revision: int,
        rule_name: str,
        workflow_index: int,
        exec_id: str,
        trigger_event_id: str,
        trigger_agent: str | None,
        trigger_kind: str,
        program_blob: Any,
        detail: str | None = None,
    ) -> bool:
        """True when the row was newly created (unique per rule+event)."""
        now = _now()
        with _LOCK:
            try:
                self._conn.execute(
                    "INSERT INTO hal_workflow_execs (exec_id, owner_id, rule_id, "
                    "rule_revision, rule_name, workflow_index, stage, state, "
                    "trigger_event_id, trigger_agent, trigger_kind, program_blob, "
                    "detail, created_at, updated_at) VALUES (?,?,?,?,?,?,0,?,?,?,?,?,?,?,?)",
                    (
                        exec_id,
                        owner_id,
                        rule_id,
                        rule_revision,
                        rule_name,
                        workflow_index,
                        EXEC_ACTIVE,
                        trigger_event_id,
                        trigger_agent,
                        trigger_kind,
                        json.dumps(program_to_json(program_blob)),
                        detail,
                        now,
                        now,
                    ),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def update_exec(
        self,
        exec_id: str,
        *,
        stage: int | None = None,
        state: str | None = None,
        detail: str | None = None,
    ) -> None:
        sets: list[str] = ["updated_at=?"]
        values: list[Any] = [_now()]
        if stage is not None:
            sets.append("stage=?")
            values.append(stage)
        if state is not None:
            sets.append("state=?")
            values.append(state)
        if detail is not None:
            sets.append("detail=?")
            values.append(detail)
        values.append(exec_id)
        with _LOCK:
            self._conn.execute(
                f"UPDATE hal_workflow_execs SET {', '.join(sets)} WHERE exec_id=?",
                values,
            )
            self._conn.commit()

    def advance_exec_if_active(
        self,
        *,
        owner_id: str,
        exec_id: str,
        stage: int,
        state: str,
        detail: str | None = None,
    ) -> bool:
        """Conditionally advance an execution only if it is still active.

        A concurrent disable settles the execution to ``disabled``; this update
        must never overwrite that settlement. Returns ``True`` when the update
        applied and ``False`` when the execution was settled concurrently.
        """
        now = _now()
        with _LOCK:
            cursor = self._conn.execute(
                "UPDATE hal_workflow_execs SET stage=?, state=?, detail=?, updated_at=? "
                "WHERE exec_id=? AND owner_id=? AND state=?",
                (int(stage), state, detail, now, exec_id, owner_id, EXEC_ACTIVE),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def fail_exec_if_active(
        self,
        *,
        owner_id: str,
        exec_id: str,
        detail: str | None = None,
    ) -> bool:
        """Conditionally fail an execution only if it is still active.

        Never overwrites a concurrent disable/settlement. Returns ``True`` when
        the execution was transitioned to ``failed``.
        """
        now = _now()
        with _LOCK:
            cursor = self._conn.execute(
                "UPDATE hal_workflow_execs SET state=?, detail=?, updated_at=? "
                "WHERE exec_id=? AND owner_id=? AND state=?",
                (EXEC_FAILED, detail, now, exec_id, owner_id, EXEC_ACTIVE),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def get_exec(self, owner_id: str, exec_id: str) -> dict[str, Any] | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM hal_workflow_execs WHERE exec_id=? AND owner_id=?",
                (exec_id, owner_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_active_execs(self, owner_id: str) -> list[dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM hal_workflow_execs WHERE owner_id=? AND state=? "
                "ORDER BY created_at ASC",
                (owner_id, EXEC_ACTIVE),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_execs(
        self, owner_id: str, limit: int = 50, rule_id: str | None = None
    ) -> list[dict[str, Any]]:
        # Filtering by rule belongs in the query: applying the limit first and
        # filtering afterwards would let other rules' runs crowd out the
        # history of the rule being inspected.
        where = "owner_id=?"
        params: list[Any] = [owner_id]
        if rule_id:
            where += " AND rule_id=?"
            params.append(rule_id)
        params.append(max(1, min(int(limit), 200)))
        with _LOCK:
            rows = self._conn.execute(
                "SELECT exec_id, rule_id, rule_name, rule_revision, workflow_index, "
                "stage, state, trigger_event_id, trigger_agent, trigger_kind, detail, "
                "created_at, updated_at FROM hal_workflow_execs WHERE " + where + " "
                "ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_rule_execs_disabled(
        self, owner_id: str, rule_id: str, detail: str
    ) -> int:
        """Explicitly settle a rule's in-flight executions when it is disabled.

        Returns the number of active executions that were moved to 'disabled'.
        """
        with _LOCK:
            cursor = self._conn.execute(
                "UPDATE hal_workflow_execs SET state='disabled', detail=?, "
                "updated_at=? WHERE owner_id=? AND rule_id=? AND state='active'",
                (detail, _now(), owner_id, rule_id),
            )
            self._conn.commit()
            return cursor.rowcount

    # --- receipts and consumed events -------------------------------------

    def upsert_receipt(
        self,
        *,
        exec_id: str,
        stage: int,
        idempotency_key: str,
        kind: str,
        target: str | None,
        payload: str,
        state: str,
        detail: str | None,
        attempts: int | None = None,
        next_retry_at: float | None = None,
    ) -> None:
        """Persist a dispatch receipt.

        ``attempts`` and ``next_retry_at`` are only written when explicitly
        supplied; omitted values leave the existing bookkeeping untouched so a
        pre-dispatch state write can never reset a retry counter.
        """
        now = _now()
        attempts_val = 0 if attempts is None else int(attempts)
        next_retry_val = None if next_retry_at is None else float(next_retry_at)
        update_extra = []
        if attempts is not None:
            update_extra.append("attempts=excluded.attempts")
        if next_retry_at is not None:
            update_extra.append("next_retry_at=excluded.next_retry_at")
        conflict = (
            "state=excluded.state, detail=excluded.detail, updated_at=excluded.updated_at"
            + (", " + ", ".join(update_extra) if update_extra else "")
        )
        with _LOCK:
            self._conn.execute(
                "INSERT INTO hal_dispatch_receipts (exec_id, stage, idempotency_key, "
                "kind, target, payload, state, detail, attempts, next_retry_at, "
                "created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                f"ON CONFLICT(exec_id, stage) DO UPDATE SET {conflict}",
                (
                    exec_id,
                    stage,
                    idempotency_key,
                    kind,
                    target,
                    payload,
                    state,
                    detail,
                    attempts_val,
                    next_retry_val,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def admit_stage_dispatch(
        self,
        *,
        owner_id: str,
        rule_id: str,
        exec_id: str,
        stage: int,
        idempotency_key: str,
        kind: str,
        target: str | None,
        payload: str,
        pre_state: str,
        detail: str | None = None,
    ) -> str:
        """Atomically admit one stage dispatch.

        In a single lock acquisition + commit: verify the rule is still enabled
        and the execution is still active, then write the pre-dispatch receipt.
        This closes the window where a concurrent disable lands between the
        state check and the receipt write.

        Returns one of ``admitted``, ``rule_disabled``, ``exec_not_active`` or
        ``already_terminal``. A terminal (dispatched/failed) receipt is never
        overwritten; the pre-state write never resets retry bookkeeping.
        """
        now = _now()
        with _LOCK:
            rule_row = self._conn.execute(
                "SELECT enabled FROM hal_rules WHERE rule_id=? AND owner_id=?",
                (rule_id, owner_id),
            ).fetchone()
            if rule_row is None or not int(rule_row["enabled"]):
                return "rule_disabled"
            exec_row = self._conn.execute(
                "SELECT state FROM hal_workflow_execs WHERE exec_id=? AND owner_id=?",
                (exec_id, owner_id),
            ).fetchone()
            if exec_row is None or str(exec_row["state"]) != EXEC_ACTIVE:
                return "exec_not_active"
            existing = self._conn.execute(
                "SELECT state FROM hal_dispatch_receipts WHERE exec_id=? AND stage=?",
                (exec_id, stage),
            ).fetchone()
            if existing is not None and str(existing["state"]) in (
                RECEIPT_DISPATCHED,
                RECEIPT_FAILED,
            ):
                return "already_terminal"
            self._conn.execute(
                "INSERT INTO hal_dispatch_receipts (exec_id, stage, idempotency_key, "
                "kind, target, payload, state, detail, attempts, next_retry_at, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,0,NULL,?,?) "
                "ON CONFLICT(exec_id, stage) DO UPDATE SET "
                "state=excluded.state, detail=excluded.detail, updated_at=excluded.updated_at",
                (
                    exec_id,
                    stage,
                    idempotency_key,
                    kind,
                    target,
                    payload,
                    pre_state,
                    detail,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return "admitted"

    def get_receipt(self, exec_id: str, stage: int) -> dict[str, Any] | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM hal_dispatch_receipts WHERE exec_id=? AND stage=?",
                (exec_id, stage),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_receipts(self, exec_id: str) -> list[dict[str, Any]]:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM hal_dispatch_receipts WHERE exec_id=? ORDER BY stage ASC",
                (exec_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_exec_event_consumed(self, exec_id: str, event_id: str) -> bool:
        """False when the event was already consumed (idempotent advancement)."""
        with _LOCK:
            try:
                self._conn.execute(
                    "INSERT INTO hal_exec_events (exec_id, event_id) VALUES (?,?)",
                    (exec_id, event_id),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    # --- feed cursor ------------------------------------------------------

    def get_cursor(self, owner_id: str) -> int:
        with _LOCK:
            row = self._conn.execute(
                "SELECT cursor FROM hal_feed_cursor WHERE owner_id=?", (owner_id,)
            ).fetchone()
        return int(row["cursor"]) if row is not None else -1

    def set_cursor(self, owner_id: str, cursor: int) -> None:
        with _LOCK:
            self._conn.execute(
                "INSERT INTO hal_feed_cursor (owner_id, cursor) VALUES (?,?) "
                "ON CONFLICT(owner_id) DO UPDATE SET cursor=excluded.cursor",
                (owner_id, int(cursor)),
            )
            self._conn.commit()

def exec_summary(exec_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "exec_id": exec_row["exec_id"],
        "rule_id": exec_row["rule_id"],
        "rule_name": exec_row["rule_name"],
        "rule_revision": exec_row["rule_revision"],
        "stage": exec_row["stage"],
        "state": exec_row["state"],
        "trigger_event_id": exec_row["trigger_event_id"],
        "trigger_agent": exec_row["trigger_agent"],
        "trigger_kind": exec_row["trigger_kind"],
        "detail": exec_row["detail"],
        "created_at": exec_row["created_at"],
        "updated_at": exec_row["updated_at"],
    }
# HASHI-Fingerprint: hfp_690761c2879a481191a1026339b51141
