"""Persistent Ingress Confirmation Service for delayed / stale messages.

Functional owner: PAO
Engineering layer: Functions
Decision: Pre-admission gate persists delayed requests and enforces exactly-once confirmation.
"""
from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from orchestrator.message_freshness import parse_rfc3339_utc

logger = logging.getLogger("HASHI.IngressConfirmation")

# A short lease separates an in-flight decision from a process that died after
# persisting ``confirming``.  Run admission is idempotent, so a later claimant
# may safely retry after this lease; before then duplicate clicks only observe
# the in-flight state.
CONFIRMATION_EXECUTION_LEASE_SECONDS = 30


class IngressConfirmationError(Exception):
    """Base error for ingress confirmations."""


class ConfirmationNotFoundError(IngressConfirmationError):
    """Confirmation record was not found."""

    code = "ingress_confirmation_not_found"


class ConfirmationConflictError(IngressConfirmationError):
    """Payload or idempotency conflict."""

    code = "ingress_confirmation_conflict"


class ConfirmationExpiredError(IngressConfirmationError):
    """Confirmation expired past the 15-minute window."""

    code = "ingress_confirmation_expired"


class ConfirmationSupersededError(IngressConfirmationError):
    """Confirmation superseded by an advanced session context generation."""

    code = "ingress_confirmation_superseded"


def initialize_schema(connection: Any) -> None:
    """Create the ingress_confirmations table and indexes in SQLite."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingress_confirmations (
            confirmation_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            connector_id TEXT NOT NULL,
            transport TEXT NOT NULL,
            source_message_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            payload_digest TEXT NOT NULL,
            message_kind TEXT NOT NULL,
            context_generation INTEGER NOT NULL,
            originated_at TEXT,
            received_at TEXT NOT NULL,
            threshold_seconds INTEGER NOT NULL,
            reason TEXT NOT NULL,
            delay_seconds REAL NOT NULL,
            expires_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            display_metadata_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'awaiting_confirmation',
            decision TEXT,
            run_id TEXT,
            execution_token TEXT,
            execution_started_at TEXT,
            execution_error TEXT,
            decided_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(session_id)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ingress_confirmations_idempotency
            ON ingress_confirmations(session_id, owner_id, idempotency_key);
        CREATE INDEX IF NOT EXISTS idx_ingress_confirmations_session
            ON ingress_confirmations(session_id, owner_id, created_at);
        """
    )
    existing_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(ingress_confirmations)")
    }
    for column_name, declaration in (
        ("execution_token", "TEXT"),
        ("execution_started_at", "TEXT"),
        ("execution_error", "TEXT"),
    ):
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE ingress_confirmations ADD COLUMN {column_name} {declaration}"
            )
    # Source identities are unique inside their authenticated owner boundary;
    # one tenant must never discover or block another tenant's confirmation.
    connection.execute("DROP INDEX IF EXISTS idx_ingress_confirmations_source_message")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ingress_confirmations_source_message
        ON ingress_confirmations(connector_id, owner_id, source_message_id)
        """
    )


class IngressConfirmationService:
    """Manages persistent confirmation lifecycle before Run creation."""

    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], datetime | float] | None = None,
    ) -> None:
        self.store = store
        self._clock = clock

    def _now_dt(self) -> datetime:
        if self._clock is not None:
            val = self._clock()
            if isinstance(val, datetime):
                return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
            return datetime.fromtimestamp(float(val), tz=timezone.utc)
        return datetime.now(timezone.utc)

    def _now_iso(self) -> str:
        return self._now_dt().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        rec = dict(row)
        try:
            rec["payload"] = json.loads(rec.get("payload_json") or "{}")
        except Exception:
            rec["payload"] = {}
        try:
            rec["display_metadata"] = json.loads(rec.get("display_metadata_json") or "{}")
        except Exception:
            rec["display_metadata"] = {}
        return rec

    @staticmethod
    def public_record(record: dict[str, Any]) -> dict[str, Any]:
        """Return the owner-visible record without execution tokens or raw payload."""
        keys = (
            "confirmation_id", "session_id", "agent_id", "connector_id", "transport",
            "message_kind", "context_generation", "originated_at", "received_at",
            "threshold_seconds", "reason", "delay_seconds", "expires_at", "state",
            "decision", "run_id", "execution_error", "decided_at", "created_at", "updated_at",
        )
        result = {key: record.get(key) for key in keys}
        result["display_metadata"] = dict(record.get("display_metadata") or {})
        return result

    def register_pending(
        self,
        *,
        session_id: str,
        owner_id: str,
        agent_id: str,
        connector_id: str,
        transport: str,
        source_message_id: str,
        idempotency_key: str,
        payload_digest: str,
        message_kind: str,
        context_generation: int,
        originated_at: str | None,
        received_at: str,
        threshold_seconds: int,
        reason: str,
        delay_seconds: float,
        expires_at: str,
        payload: dict[str, Any],
        display_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """Register a pending confirmation record or return existing idempotent match."""
        now_str = self._now_iso()
        # Keep attachment references in the same compact JSON form used by
        # canonical message content so the existing bounded-reference checks
        # can reserve pending assets without parsing untrusted JSON paths.
        payload_json = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        display_json = json.dumps(
            display_metadata,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        if isinstance(received_at, datetime):
            received_at_str = received_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            received_at_str = str(received_at)

        if isinstance(originated_at, datetime):
            originated_at_str = originated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        elif originated_at is not None:
            originated_at_str = str(originated_at)
        else:
            originated_at_str = None

        if isinstance(expires_at, datetime):
            expires_at_str = expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        else:
            expires_at_str = str(expires_at)

        with self.store._lock, self.store._connection() as connection:
            # Serialize the lookup-and-create boundary across processes.  The
            # in-process store lock alone cannot prevent two workers from both
            # observing absence before one of them inserts the unique source
            # identity.
            connection.execute("BEGIN IMMEDIATE")
            # Check for existing record by idempotency_key or source_message_id
            cursor = connection.execute(
                """
                SELECT * FROM ingress_confirmations
                WHERE (session_id = ? AND owner_id = ? AND idempotency_key = ?)
                   OR (connector_id = ? AND owner_id = ? AND source_message_id = ?)
                """,
                (
                    session_id, owner_id, idempotency_key,
                    connector_id, owner_id, source_message_id,
                ),
            )
            rows = cursor.fetchall()
            if len(rows) > 1:
                raise ConfirmationConflictError(
                    "Ingress confirmation identities resolve to different records"
                )
            row = rows[0] if rows else None
            if row is not None:
                existing = self._row_to_dict(row)
                if (
                    existing["payload_digest"] != payload_digest
                    or existing["idempotency_key"] != idempotency_key
                    or existing["source_message_id"] != source_message_id
                ):
                    raise ConfirmationConflictError(
                        f"Ingress confirmation conflict for idempotency_key {idempotency_key!r}"
                    )
                # Check lazy update if needed
                return self._check_lazy_state_transition_locked(connection, existing)

            confirmation_id = f"conf-{uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO ingress_confirmations (
                    confirmation_id, session_id, owner_id, agent_id,
                    connector_id, transport, source_message_id, idempotency_key,
                    payload_digest, message_kind, context_generation,
                    originated_at, received_at, threshold_seconds, reason,
                    delay_seconds, expires_at, payload_json, display_metadata_json,
                    state, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    'awaiting_confirmation', ?, ?
                )
                """,
                (
                    confirmation_id,
                    session_id,
                    owner_id,
                    agent_id,
                    connector_id,
                    transport,
                    source_message_id,
                    idempotency_key,
                    payload_digest,
                    message_kind,
                    int(context_generation),
                    originated_at_str,
                    received_at_str,
                    int(threshold_seconds),
                    reason,
                    float(delay_seconds),
                    expires_at_str,
                    payload_json,
                    display_json,
                    now_str,
                    now_str,
                ),
            )
            cursor = connection.execute(
                "SELECT * FROM ingress_confirmations WHERE confirmation_id = ?",
                (confirmation_id,),
            )
            created_row = cursor.fetchone()
            return self._row_to_dict(created_row)

    def _check_lazy_state_transition_locked(
        self, connection: Any, record: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply lazy expiration and context generation advancement checks."""
        if record["state"] != "awaiting_confirmation":
            return record

        now_dt = self._now_dt()
        exp_dt = parse_rfc3339_utc(record.get("expires_at"))

        # Check expiration
        if exp_dt is not None and now_dt >= exp_dt:
            now_str = self._now_iso()
            connection.execute(
                """
                UPDATE ingress_confirmations
                SET state = 'expired', updated_at = ?
                WHERE confirmation_id = ?
                """,
                (now_str, record["confirmation_id"]),
            )
            record["state"] = "expired"
            record["updated_at"] = now_str
            return record

        # Check session context generation
        cursor = connection.execute(
            "SELECT context_generation FROM sessions WHERE session_id = ?",
            (record["session_id"],),
        )
        s_row = cursor.fetchone()
        if s_row is not None:
            current_gen = int(s_row[0])
            if current_gen != int(record["context_generation"]):
                now_str = self._now_iso()
                connection.execute(
                    """
                    UPDATE ingress_confirmations
                    SET state = 'superseded', updated_at = ?
                    WHERE confirmation_id = ?
                    """,
                    (now_str, record["confirmation_id"]),
                )
                record["state"] = "superseded"
                record["updated_at"] = now_str
                return record

        return record

    def get(
        self,
        confirmation_id: str,
        *,
        owner_id: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch confirmation record by ID, applying lazy expiration."""
        with self.store._lock, self.store._connection() as connection:
            query = "SELECT * FROM ingress_confirmations WHERE confirmation_id = ? AND owner_id = ?"
            params: list[Any] = [confirmation_id, owner_id]
            if session_id is not None:
                query += " AND session_id = ?"
                params.append(session_id)
            cursor = connection.execute(query, tuple(params))
            row = cursor.fetchone()
            if row is None:
                raise ConfirmationNotFoundError(
                    f"Confirmation {confirmation_id!r} not found for owner {owner_id!r}"
                )
            record = self._row_to_dict(row)
            return self._check_lazy_state_transition_locked(connection, record)

    def list(
        self,
        *,
        session_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List confirmation records for a session, applying lazy expiration."""
        with self.store._lock, self.store._connection() as connection:
            cursor = connection.execute(
                """
                SELECT * FROM ingress_confirmations
                WHERE session_id = ? AND owner_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (session_id, owner_id, max(1, min(int(limit), 200))),
            )
            rows = cursor.fetchall()
            results = []
            for row in rows:
                rec = self._row_to_dict(row)
                rec = self._check_lazy_state_transition_locked(connection, rec)
                results.append(rec)
            return results

    def list_for_connector(
        self,
        *,
        agent_id: str,
        connector_id: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Internal connector recovery view, with lazy terminal transitions.

        This is deliberately not exposed as a frontend API.  It lets a trusted
        connector restore its own user-visible pending cards after a worker
        restart without broadening any owner-facing read boundary.
        """
        with self.store._lock, self.store._connection() as connection:
            cursor = connection.execute(
                """
                SELECT * FROM ingress_confirmations
                WHERE agent_id = ? AND connector_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (agent_id, connector_id, max(1, min(int(limit), 2000))),
            )
            results: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                record = self._row_to_dict(row)
                results.append(
                    self._check_lazy_state_transition_locked(connection, record)
                )
            return results

    def list_hchat_status_candidates(
        self,
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return internal HChat decisions that may need a sender status notice."""

        with self.store._lock, self.store._connection() as connection:
            cursor = connection.execute(
                """
                SELECT * FROM ingress_confirmations
                WHERE message_kind IN ('hchat_request', 'hchat_reply')
                  AND payload_json LIKE '%"status_notification"%'
                  AND display_metadata_json NOT LIKE '%"sender_delivery_status_complete"%'
                  AND state IN (
                      'awaiting_confirmation', 'confirming', 'confirmed',
                      'discarded', 'expired', 'superseded'
                  )
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max(1, min(int(limit), 2000)),),
            )
            results: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                record = self._row_to_dict(row)
                results.append(
                    self._check_lazy_state_transition_locked(connection, record)
                )
            return results

    def update_display_metadata(
        self,
        *,
        confirmation_id: str,
        owner_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge connector projection metadata without changing the decision.

        Raw request payloads remain immutable.  Only presentation receipts such
        as a Telegram card message ID or its projected terminal state belong in
        this metadata.
        """
        if not isinstance(updates, dict):
            raise TypeError("display metadata updates must be a mapping")
        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM ingress_confirmations
                WHERE confirmation_id = ? AND owner_id = ?
                """,
                (confirmation_id, owner_id),
            ).fetchone()
            if row is None:
                raise ConfirmationNotFoundError(
                    f"Confirmation {confirmation_id!r} not found for owner {owner_id!r}"
                )
            record = self._row_to_dict(row)
            metadata = dict(record.get("display_metadata") or {})
            metadata.update(updates)
            now_str = self._now_iso()
            connection.execute(
                """
                UPDATE ingress_confirmations
                SET display_metadata_json = ?, updated_at = ?
                WHERE confirmation_id = ? AND owner_id = ?
                """,
                (
                    json.dumps(metadata, sort_keys=True, ensure_ascii=False),
                    now_str,
                    confirmation_id,
                    owner_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM ingress_confirmations WHERE confirmation_id = ?",
                (confirmation_id,),
            ).fetchone()
            return self._row_to_dict(row)

    @staticmethod
    def _existing_run_locked(connection: Any, record: dict[str, Any]) -> str | None:
        """Return the Run already admitted for this immutable message, if any."""
        row = connection.execute(
            """
            SELECT run_id FROM runs
            WHERE session_id = ? AND idempotency_key = ?
            """,
            (record["session_id"], record["idempotency_key"]),
        ).fetchone()
        if row is None:
            return None
        return str(row[0])

    def _mark_confirmed_locked(
        self,
        connection: Any,
        record: dict[str, Any],
        *,
        run_id: str | None,
        execution_token: str | None = None,
    ) -> dict[str, Any]:
        now_str = self._now_iso()
        where = "confirmation_id = ? AND state = 'confirming'"
        params: list[Any] = [run_id, now_str, now_str, record["confirmation_id"]]
        if execution_token is not None:
            where += " AND execution_token = ?"
            params.append(execution_token)
        connection.execute(
            f"""
            UPDATE ingress_confirmations
            SET state = 'confirmed', decision = 'confirm', run_id = ?,
                execution_error = NULL, decided_at = ?, updated_at = ?
            WHERE {where}
            """,
            tuple(params),
        )
        row = connection.execute(
            "SELECT * FROM ingress_confirmations WHERE confirmation_id = ?",
            (record["confirmation_id"],),
        ).fetchone()
        return self._row_to_dict(row)

    def _claim_confirmation(
        self,
        *,
        confirmation_id: str,
        decision: str,
        owner_id: str,
    ) -> tuple[dict[str, Any], str | None]:
        """Atomically apply a decision and, for confirm, acquire one execution lease.

        The returned token is non-null only for the caller that may invoke the
        bound operation.  All concurrent callers receive the current record.
        """
        decision_norm = str(decision or "").strip().lower()
        if decision_norm not in {"confirm", "discard"}:
            raise ValueError(
                f"Invalid decision {decision!r}; must be 'confirm' or 'discard'"
            )

        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ingress_confirmations WHERE confirmation_id = ? AND owner_id = ?",
                (confirmation_id, owner_id),
            ).fetchone()
            if row is None:
                raise ConfirmationNotFoundError(
                    f"Confirmation {confirmation_id!r} not found for owner {owner_id!r}"
                )
            record = self._check_lazy_state_transition_locked(
                connection, self._row_to_dict(row)
            )
            state = str(record["state"])
            if state == "expired":
                raise ConfirmationExpiredError("Confirmation has expired")
            if state == "superseded":
                raise ConfirmationSupersededError(
                    "Confirmation has been superseded by a context reset"
                )
            if state in {"confirmed", "discarded"}:
                prior = str(record.get("decision") or "")
                if prior and prior != decision_norm:
                    raise ConfirmationConflictError(
                        f"Confirmation was already decided as {prior!r}"
                    )
                return record, None

            if decision_norm == "discard":
                if state == "confirming":
                    raise ConfirmationConflictError(
                        "Confirmation is already being executed and cannot be discarded"
                    )
                now_str = self._now_iso()
                changed = connection.execute(
                    """
                    UPDATE ingress_confirmations
                    SET state = 'discarded', decision = 'discard',
                        decided_at = ?, updated_at = ?
                    WHERE confirmation_id = ? AND state = 'awaiting_confirmation'
                    """,
                    (now_str, now_str, confirmation_id),
                ).rowcount
                if changed != 1:
                    row = connection.execute(
                        "SELECT * FROM ingress_confirmations WHERE confirmation_id = ?",
                        (confirmation_id,),
                    ).fetchone()
                    return self._row_to_dict(row), None
                row = connection.execute(
                    "SELECT * FROM ingress_confirmations WHERE confirmation_id = ?",
                    (confirmation_id,),
                ).fetchone()
                return self._row_to_dict(row), None

            # A process may have died after admitting the Run but before
            # recording the terminal confirmation state.  Reconcile that Run
            # before considering any retry.
            if state == "confirming":
                existing_run_id = self._existing_run_locked(connection, record)
                if existing_run_id is not None:
                    return (
                        self._mark_confirmed_locked(
                            connection, record, run_id=existing_run_id
                        ),
                        None,
                    )
                started = parse_rfc3339_utc(record.get("execution_started_at"))
                lease_age = (
                    (self._now_dt() - started).total_seconds()
                    if started is not None
                    else CONFIRMATION_EXECUTION_LEASE_SECONDS
                )
                if lease_age < CONFIRMATION_EXECUTION_LEASE_SECONDS:
                    return record, None

            token = uuid4().hex
            now_str = self._now_iso()
            eligible_state = state
            changed = connection.execute(
                """
                UPDATE ingress_confirmations
                SET state = 'confirming', decision = 'confirm',
                    execution_token = ?, execution_started_at = ?,
                    execution_error = NULL, updated_at = ?
                WHERE confirmation_id = ? AND state = ?
                """,
                (token, now_str, now_str, confirmation_id, eligible_state),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM ingress_confirmations WHERE confirmation_id = ?",
                (confirmation_id,),
            ).fetchone()
            claimed = self._row_to_dict(row)
            return claimed, token if changed == 1 else None

    def _finish_execution(
        self,
        *,
        record: dict[str, Any],
        execution_token: str,
        callback_result: Any,
    ) -> dict[str, Any]:
        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_id = self._existing_run_locked(connection, record)
            if run_id is None and callback_result is not None:
                if isinstance(callback_result, dict):
                    candidate = callback_result.get("run_id")
                else:
                    candidate = callback_result
                run_id = str(candidate) if candidate else None
            return self._mark_confirmed_locked(
                connection,
                record,
                run_id=run_id,
                execution_token=execution_token,
            )

    def _release_failed_claim(
        self,
        *,
        confirmation_id: str,
        execution_token: str,
        error: BaseException,
    ) -> None:
        # The immutable idempotency key makes a retry safe.  Do not overwrite a
        # terminal state or a newer claimant's lease.
        error_text = f"{type(error).__name__}: {error}"[:1000]
        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ingress_confirmations WHERE confirmation_id = ?",
                (confirmation_id,),
            ).fetchone()
            if row is None:
                return
            record = self._row_to_dict(row)
            existing_run_id = self._existing_run_locked(connection, record)
            if existing_run_id is not None:
                self._mark_confirmed_locked(
                    connection,
                    record,
                    run_id=existing_run_id,
                    execution_token=execution_token,
                )
                return
            connection.execute(
                """
                UPDATE ingress_confirmations
                SET state = 'awaiting_confirmation', decision = NULL,
                    execution_token = NULL, execution_started_at = NULL,
                    execution_error = ?, updated_at = ?
                WHERE confirmation_id = ? AND state = 'confirming'
                  AND execution_token = ?
                """,
                (error_text, self._now_iso(), confirmation_id, execution_token),
            )

    def decide(
        self,
        *,
        confirmation_id: str,
        decision: str,
        owner_id: str,
        execute_fn: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> dict[str, Any]:
        """Atomically decide on a confirmation record."""
        record, token = self._claim_confirmation(
            confirmation_id=confirmation_id,
            decision=decision,
            owner_id=owner_id,
        )
        if token is None:
            return record
        try:
            result = execute_fn(record) if execute_fn is not None else None
            if inspect.isawaitable(result):
                raise TypeError("decide() execute_fn returned an awaitable; use decide_async()")
        except Exception as exc:
            self._release_failed_claim(
                confirmation_id=confirmation_id,
                execution_token=token,
                error=exc,
            )
            raise
        return self._finish_execution(
            record=record,
            execution_token=token,
            callback_result=result,
        )

    async def decide_async(
        self,
        *,
        confirmation_id: str,
        decision: str,
        owner_id: str,
        execute_fn: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically decide on a confirmation record with async execution support."""
        record, token = self._claim_confirmation(
            confirmation_id=confirmation_id,
            decision=decision,
            owner_id=owner_id,
        )
        if token is None:
            return record
        try:
            result = execute_fn(record) if execute_fn is not None else None
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self._release_failed_claim(
                confirmation_id=confirmation_id,
                execution_token=token,
                error=exc,
            )
            raise
        return self._finish_execution(
            record=record,
            execution_token=token,
            callback_result=result,
        )
