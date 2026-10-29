"""Transactional PAO permission authority for explicitly enabled L1.

The caller supplies a verifier belonging to a trusted approval ingress. Merely
constructing a DecisionReceipt, providing a claimed actor, or answering an
ordinary RunQuestion is not proof. There is deliberately no permissive default
verifier and this module does not install a transport or enable the feature.

The ledger serializes decisions, reservation, start and revocation with SQLite
transactions. A runner must still bind the real resource and make its start
barrier enforce the committed claim; a database row alone is not OS isolation.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import hashlib
import inspect
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Callable, Iterable, Protocol
from uuid import uuid4

from orchestrator.permissions.models import (
    ExecutionContext,
    Scope,
    canonical_scope_set,
    scope_covers,
    scope_digest,
)


class PermissionErrorBase(ValueError):
    pass


class PermissionDenied(PermissionErrorBase):
    pass


class InvalidDecision(PermissionErrorBase):
    pass


class RevisionConflict(PermissionErrorBase):
    pass


class OperationConflict(PermissionErrorBase):
    pass


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    """Claims returned only after a trusted ingress verifies a private reference."""

    nonce: str
    request_id: str
    request_revision: int
    requested_digest: str
    approved_digest: str
    instance_id: str
    task_id: str
    run_id: str
    execution_epoch: str
    operation_id: str | None
    arguments_digest: str | None
    policy_revision: int
    owner_id: str
    agent_id: str
    session_id: str
    context_generation: str
    principal_id: str
    choice: str
    lifetime: str
    duration_seconds: int
    idempotency_key: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class DecisionAction:
    """A server-issued snapshot, not a human approval or bearer capability."""

    action_id: str
    nonce: str
    request_id: str
    request_revision: int
    requested_digest: str
    approved_scopes: tuple[Scope, ...]
    approved_digest: str
    operation_id: str | None
    arguments_digest: str | None
    choice: str
    lifetime: str
    duration_seconds: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class ResourceStopReceipt:
    """Verified supervisor evidence that a specific execution resource stopped."""

    nonce: str
    claim_id: str
    instance_id: str
    task_id: str
    execution_epoch: str
    observer_id: str
    evidence_digest: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class OperationOutcomeReceipt:
    """Verified supervisor evidence of an interrupted operation's final outcome."""

    nonce: str
    claim_id: str
    operation_id: str
    arguments_digest: str
    instance_id: str
    task_id: str
    execution_epoch: str
    observer_id: str
    outcome: str
    result_digest: str
    expires_at: float


class ReceiptVerifier(Protocol):
    def verify(self, reference: str) -> DecisionReceipt | None:
        """Verify private provenance and return the exact immutable decision."""
        ...


@dataclass(frozen=True, slots=True)
class AccessRequest:
    request_id: str
    task_id: str
    revision: int
    scopes: tuple[Scope, ...]
    scope_digest: str
    operation_id: str | None
    state: str
    expires_at: float
    purpose: str = ""
    suggested_lifetime: str = "task"
    suggested_seconds: int = 4 * 60 * 60
    request_key: str | None = None
    is_current_revision: bool = True


@dataclass(frozen=True, slots=True)
class PermissionLease:
    lease_id: str
    task_id: str
    scopes: tuple[Scope, ...]
    lifetime: str
    operation_id: str | None
    run_id: str | None
    expires_at: float
    status: str


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    claim_id: str
    operation_id: str
    state: str


@dataclass(frozen=True, slots=True)
class ExecutionStatus:
    task_state: str
    binding_state: str


@dataclass(frozen=True, slots=True)
class OperationStatus:
    state: str
    arguments_digest: str
    scope_digest: str
    execution_epoch: str
    result_digest: str | None
    resource_state: str | None = None


@dataclass(frozen=True, slots=True)
class PermissionEvent:
    sequence: int
    task_id: str
    event: str
    reference_id: str
    created_at: float


_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA_VERSION = 6
_MAX_LIVE_DECISION_ACTIONS = 1024
_REQUIRED_COLUMNS = {
    "authority_metadata": {"key", "value"},
    "permission_tasks": {"task_id", "instance_id", "owner_id", "agent_id", "session_id",
                         "state", "revision", "created_at", "ended_at"},
    "execution_bindings": {"execution_epoch", "task_id", "run_id", "worker_generation",
                           "context_generation", "backend_id", "executor_id", "policy_revision", "state"},
    "access_requests": {"request_id", "task_id", "run_id", "session_id", "context_generation",
                        "execution_epoch", "policy_revision", "operation_id", "arguments_digest",
                        "scopes_json", "scope_digest", "purpose", "revision", "state",
                        "created_at", "expires_at"},
    "approval_decisions": {"decision_id", "request_id", "request_revision", "nonce",
                           "idempotency_key", "proof_hash", "choice", "approved_scopes",
                           "principal_id", "created_at"},
    "permission_leases": {"lease_id", "decision_id", "task_id", "scopes_json", "lifetime",
                          "operation_id", "run_id", "policy_revision", "expires_at",
                          "status", "reserved_claim"},
    "pending_operations": {"operation_id", "task_id", "run_id", "arguments_digest",
                           "scope_digest", "execution_epoch", "state"},
    "execution_claims": {"claim_id", "operation_id", "task_id", "execution_epoch",
                         "lease_ids_json", "scopes_json", "state", "result_digest", "created_at"},
    "execution_resources": {"claim_id", "state"},
    "resource_stop_receipts": {"nonce", "claim_id", "proof_hash", "recorded_at"},
    "permission_events_outbox": {"sequence", "task_id", "event", "reference_id", "created_at"},
    "outbox_cursors": {"task_id", "consumer_id", "sequence"},
}

_REQUEST_VERSIONS_SCHEMA = """CREATE TABLE access_request_versions (
    request_id TEXT NOT NULL REFERENCES access_requests(request_id),
    revision INTEGER NOT NULL,
    scopes_json TEXT NOT NULL,
    scope_digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    context_generation TEXT NOT NULL,
    execution_epoch TEXT NOT NULL,
    policy_revision INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    arguments_digest TEXT,
    purpose TEXT NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY(request_id, revision)
)"""

_DECISION_ACTIONS_SCHEMA = """CREATE TABLE decision_actions (
    action_id TEXT PRIMARY KEY,
    nonce TEXT NOT NULL UNIQUE,
    request_id TEXT NOT NULL,
    request_revision INTEGER NOT NULL,
    requested_digest TEXT NOT NULL,
    approved_scopes TEXT NOT NULL,
    approved_digest TEXT NOT NULL,
    operation_id TEXT,
    arguments_digest TEXT,
    choice TEXT NOT NULL CHECK(choice IN ('approve','deny')),
    lifetime TEXT NOT NULL CHECK(lifetime IN ('once','run','task','ttl')),
    duration_seconds INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY(request_id,request_revision)
        REFERENCES access_request_versions(request_id,revision)
)"""

_DECISION_PRESENTATIONS_SCHEMA = """CREATE TABLE decision_action_presentations (
    presentation_id TEXT NOT NULL,
    presentation_slot TEXT NOT NULL,
    action_id TEXT NOT NULL UNIQUE REFERENCES decision_actions(action_id),
    PRIMARY KEY(presentation_id,presentation_slot)
)"""


_SCHEMA = (
    """CREATE TABLE authority_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE permission_tasks (
        task_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active','paused','completed','cancelled','failed_terminal')),
        revision INTEGER NOT NULL,
        created_at REAL NOT NULL,
        ended_at REAL
    )""",
    """CREATE TABLE execution_bindings (
        execution_epoch TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES permission_tasks(task_id),
        run_id TEXT NOT NULL,
        worker_generation TEXT NOT NULL,
        context_generation TEXT NOT NULL,
        backend_id TEXT NOT NULL,
        executor_id TEXT NOT NULL,
        policy_revision INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('active','fenced'))
    )""",
    """CREATE TABLE access_requests (
        request_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES permission_tasks(task_id),
        run_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        context_generation TEXT NOT NULL,
        execution_epoch TEXT NOT NULL REFERENCES execution_bindings(execution_epoch),
        policy_revision INTEGER NOT NULL,
        operation_id TEXT NOT NULL,
        arguments_digest TEXT,
        scopes_json TEXT NOT NULL,
        scope_digest TEXT NOT NULL,
        purpose TEXT NOT NULL,
        revision INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','decided','denied','expired','withdrawn')),
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )""",
    _REQUEST_VERSIONS_SCHEMA,
    """CREATE UNIQUE INDEX one_pending_request
       ON access_requests(task_id, operation_id, scope_digest, policy_revision) WHERE state='pending'""",
    """CREATE TABLE approval_decisions (
        decision_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES access_requests(request_id),
        request_revision INTEGER NOT NULL,
        nonce TEXT NOT NULL UNIQUE,
        idempotency_key TEXT NOT NULL UNIQUE,
        proof_hash TEXT NOT NULL,
        choice TEXT NOT NULL,
        approved_scopes TEXT NOT NULL,
        principal_id TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE(request_id, request_revision)
    )""",
    """CREATE TABLE permission_leases (
        lease_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL UNIQUE REFERENCES approval_decisions(decision_id),
        task_id TEXT NOT NULL REFERENCES permission_tasks(task_id),
        scopes_json TEXT NOT NULL,
        lifetime TEXT NOT NULL CHECK (lifetime IN ('once','run','task','ttl')),
        operation_id TEXT,
        run_id TEXT,
        policy_revision INTEGER NOT NULL,
        expires_at REAL NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('active','reserved','consumed','revoked')),
        reserved_claim TEXT
    )""",
    """CREATE TABLE pending_operations (
        operation_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES permission_tasks(task_id),
        run_id TEXT NOT NULL,
        arguments_digest TEXT NOT NULL,
        scope_digest TEXT NOT NULL,
        execution_epoch TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN
            ('waiting','prepared','started','succeeded','failed','unknown','cancelled'))
    )""",
    """CREATE TABLE execution_claims (
        claim_id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL REFERENCES pending_operations(operation_id),
        task_id TEXT NOT NULL REFERENCES permission_tasks(task_id),
        execution_epoch TEXT NOT NULL,
        lease_ids_json TEXT NOT NULL,
        scopes_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('prepared','started','succeeded','failed','unknown','cancelled')),
        result_digest TEXT,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE execution_resources (
        claim_id TEXT PRIMARY KEY REFERENCES execution_claims(claim_id),
        state TEXT NOT NULL CHECK (state IN ('registered','running','stopping','stopped','unknown'))
    )""",
    """CREATE TABLE resource_stop_receipts (
        nonce TEXT PRIMARY KEY,
        claim_id TEXT NOT NULL REFERENCES execution_claims(claim_id),
        proof_hash TEXT NOT NULL,
        recorded_at REAL NOT NULL
    )""",
    """CREATE TABLE permission_events_outbox (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL,
        event TEXT NOT NULL,
        reference_id TEXT NOT NULL,
        created_at REAL NOT NULL
    )""",
    """CREATE TABLE outbox_cursors (
        task_id TEXT NOT NULL REFERENCES permission_tasks(task_id),
        consumer_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        PRIMARY KEY(task_id, consumer_id)
    )""",
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scope_json(scopes: Iterable[Scope]) -> str:
    return _json([asdict(scope) for scope in canonical_scope_set(scopes)])


def _load_scopes(value: str) -> tuple[Scope, ...]:
    data = json.loads(value)
    if not isinstance(data, list):
        raise ValueError("damaged scope data")
    return canonical_scope_set(Scope(**entry) for entry in data)


def _request_from(row: sqlite3.Row) -> AccessRequest:
    return AccessRequest(
        request_id=row["request_id"], task_id=row["task_id"], revision=row["revision"],
        scopes=_load_scopes(row["scopes_json"]), scope_digest=row["scope_digest"],
        operation_id=row["operation_id"] or None, state=row["state"],
        expires_at=row["expires_at"], purpose=row["purpose"],
        suggested_lifetime=row["suggested_lifetime"], suggested_seconds=row["suggested_seconds"],
        request_key=row["request_key"],
    )


def _action_from(row: sqlite3.Row) -> DecisionAction:
    return DecisionAction(
        action_id=row["action_id"], nonce=row["nonce"], request_id=row["request_id"],
        request_revision=row["request_revision"], requested_digest=row["requested_digest"],
        approved_scopes=_load_scopes(row["approved_scopes"]), approved_digest=row["approved_digest"],
        operation_id=row["operation_id"], arguments_digest=row["arguments_digest"],
        choice=row["choice"], lifetime=row["lifetime"], duration_seconds=row["duration_seconds"],
        expires_at=row["expires_at"],
    )


def _lease_from(row: sqlite3.Row) -> PermissionLease:
    return PermissionLease(
        lease_id=row["lease_id"], task_id=row["task_id"], scopes=_load_scopes(row["scopes_json"]),
        lifetime=row["lifetime"], operation_id=row["operation_id"], run_id=row["run_id"],
        expires_at=row["expires_at"], status=row["status"],
    )


def _nonempty(value: object, name: str, *, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"invalid {name}")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or not _HEX_DIGEST.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


class PermissionAuthority:
    """One explicit ledger, not constructed on the legacy L0 execution path."""

    def __init__(
        self,
        path: Path,
        *,
        verifier: ReceiptVerifier,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        request_ttl_seconds: int = 24 * 60 * 60,
        max_lease_seconds: int = 4 * 60 * 60,
    ) -> None:
        if not callable(getattr(verifier, "verify", None)):
            raise ValueError("trusted receipt verifier is required")
        for name, value in (("request_ttl_seconds", request_ttl_seconds), ("max_lease_seconds", max_lease_seconds)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._verifier = verifier
        self._clock = clock
        self._monotonic = monotonic
        self._request_ttl = request_ttl_seconds
        self._max_lease = max_lease_seconds
        self._wall_anchor = float(clock())
        self._monotonic_anchor = float(monotonic())
        self._clock_lock = threading.Lock()
        self._clock_observations: dict[int, tuple[float, float]] = {}
        self._service_generation: str | None = None
        if not math.isfinite(self._wall_anchor) or not math.isfinite(self._monotonic_anchor):
            raise ValueError("invalid clock")
        self._initialize()
        self._begin_service()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if self._service_generation is not None:
                active = connection.execute(
                    "SELECT value FROM authority_metadata WHERE key='service_generation'"
                ).fetchone()
                if active is None or active["value"] != self._service_generation:
                    raise PermissionDenied("permission service generation is fenced")
            yield connection
            connection.commit()
        except BaseException:
            # Roll back business state, but do not forget a later clock observed
            # by the rejected transaction: a restart must not revive a lease.
            with self._clock_lock:
                observation = self._clock_observations.pop(id(connection), None)
            connection.rollback()
            if observation is not None:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    self._store_time_floor(connection, "wall_high_water", observation[0])
                    self._store_time_floor(connection, "effective_high_water", observation[1])
                    connection.commit()
                except BaseException as exc:
                    connection.rollback()
                    raise PermissionDenied("trusted time could not be persisted") from exc
            raise
        finally:
            with self._clock_lock:
                self._clock_observations.pop(id(connection), None)
            connection.close()

    def _begin_service(self) -> None:
        """Fence old executions before a newly started authority can accept work."""
        generation = uuid4().hex
        with self._transaction() as connection:
            now = self._now(connection)
            epochs = connection.execute(
                "SELECT execution_epoch,task_id FROM execution_bindings WHERE state='active'"
            ).fetchall()
            for row in epochs:
                epoch = row["execution_epoch"]
                connection.execute(
                    "UPDATE execution_bindings SET state='fenced' WHERE execution_epoch=?", (epoch,),
                )
                self._stop_epoch_resources(connection, epoch)
                connection.execute(
                    "UPDATE pending_operations SET state='unknown' WHERE state='started' "
                    "AND operation_id IN (SELECT operation_id FROM execution_claims "
                    "WHERE execution_epoch=? AND state='started')", (epoch,),
                )
                connection.execute(
                    "UPDATE execution_claims SET state='unknown' WHERE execution_epoch=? AND state='started'",
                    (epoch,),
                )
                self._event(connection, row["task_id"], "execution_fenced", epoch, now)
            connection.execute(
                "INSERT INTO authority_metadata(key,value) VALUES('service_generation',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (generation,),
            )
        self._service_generation = generation

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection, version: int) -> None:
        required = {table: set(columns) for table, columns in _REQUIRED_COLUMNS.items()}
        if version >= 2:
            required["access_request_versions"] = {
                "request_id", "revision", "scopes_json", "scope_digest", "run_id",
                "session_id", "context_generation", "execution_epoch", "policy_revision",
                "operation_id", "arguments_digest", "purpose", "recorded_at",
            }
        if version >= 3:
            for table in ("access_requests", "access_request_versions"):
                required[table].update({"suggested_lifetime", "suggested_seconds", "request_key"})
            required["request_idempotency"] = {"task_id", "request_key", "fingerprint", "request_id", "outcome"}
        if version >= 4:
            required["operation_outcome_receipts"] = {"nonce", "claim_id", "proof_hash", "recorded_at"}
        if version >= 5:
            required["decision_actions"] = {
                "action_id", "nonce", "request_id", "request_revision", "requested_digest",
                "approved_scopes", "approved_digest", "operation_id", "arguments_digest",
                "choice", "lifetime", "duration_seconds", "created_at", "expires_at",
            }
        if version >= 6:
            required["decision_action_presentations"] = {"presentation_id", "presentation_slot", "action_id"}
        primary = {
            "authority_metadata": ("key",), "permission_tasks": ("task_id",),
            "execution_bindings": ("execution_epoch",), "access_requests": ("request_id",),
            "approval_decisions": ("decision_id",), "permission_leases": ("lease_id",),
            "pending_operations": ("operation_id",), "execution_claims": ("claim_id",),
            "execution_resources": ("claim_id",), "resource_stop_receipts": ("nonce",),
            "permission_events_outbox": ("sequence",), "outbox_cursors": ("task_id", "consumer_id"),
        }
        if version >= 2:
            primary["access_request_versions"] = ("request_id", "revision")
        if version >= 3:
            primary["request_idempotency"] = ("task_id", "request_key")
        if version >= 4:
            primary["operation_outcome_receipts"] = ("nonce",)
        if version >= 5:
            primary["decision_actions"] = ("action_id",)
        if version >= 6:
            primary["decision_action_presentations"] = ("presentation_id", "presentation_slot")
        for table, columns in required.items():
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual = {row["name"] for row in rows}
            keys = tuple(row["name"] for row in sorted(rows, key=lambda row: row["pk"]) if row["pk"])
            if not columns.issubset(actual) or keys != primary[table]:
                raise ValueError("permission ledger schema is incomplete")

        unique_constraints = [
            ("approval_decisions", ("nonce",)),
            ("approval_decisions", ("idempotency_key",)),
            ("approval_decisions", ("request_id", "request_revision")),
            ("permission_leases", ("decision_id",)),
        ]
        if version >= 5:
            unique_constraints.append(("decision_actions", ("nonce",)))
        if version >= 6:
            unique_constraints.append(("decision_action_presentations", ("action_id",)))
        for table, columns in unique_constraints:
            valid = False
            for index in connection.execute(f"PRAGMA index_list({table})"):
                if not index["unique"] or index["partial"]:
                    continue
                members = tuple(row["name"] for row in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index["name"],)
                ))
                if members == columns:
                    valid = True
                    break
            if not valid:
                raise ValueError("permission ledger uniqueness is incomplete")
        if version >= 5:
            table = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='decision_actions'"
            ).fetchone()
            if table is None or not isinstance(table["sql"], str) or (
                "".join(table["sql"].split()).casefold()
                != "".join(_DECISION_ACTIONS_SCHEMA.split()).casefold()
            ):
                raise ValueError("permission ledger decision action constraints are incomplete")
        if version >= 6:
            table = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='decision_action_presentations'"
            ).fetchone()
            if table is None or not isinstance(table["sql"], str) or (
                "".join(table["sql"].split()).casefold()
                != "".join(_DECISION_PRESENTATIONS_SCHEMA.split()).casefold()
            ):
                raise ValueError("permission ledger presentation constraints are incomplete")
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='one_pending_request'"
        ).fetchone()
        expected = (
            "createuniqueindexone_pending_requestonaccess_requests"
            "(task_id,operation_id,scope_digest,policy_revision)wherestate='pending'"
        )
        if index is None or not isinstance(index["sql"], str) or (
            "".join(index["sql"].split()).casefold() != expected
        ):
            raise ValueError("permission ledger pending uniqueness is incomplete")

    def _initialize(self) -> None:
        with self._transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version in {1, 2, 3, 4, 5, _SCHEMA_VERSION}:
                self._validate_schema(connection, version)
            if version == _SCHEMA_VERSION:
                return
            if version == 0:
                existing = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if existing:
                    raise ValueError("refusing to initialize a nonempty unknown database")
                for statement in _SCHEMA:
                    connection.execute(statement)
                version = 2
            elif version == 1:
                # Preserve existing scopes and bindings before adding suggestions.
                connection.execute(_REQUEST_VERSIONS_SCHEMA)
                connection.execute(
                    "INSERT INTO access_request_versions "
                    "(request_id,revision,scopes_json,scope_digest,run_id,session_id,"
                    "context_generation,execution_epoch,policy_revision,operation_id,"
                    "arguments_digest,purpose,recorded_at) "
                    "SELECT request_id,revision,scopes_json,scope_digest,run_id,session_id,"
                    "context_generation,execution_epoch,policy_revision,operation_id,"
                    "arguments_digest,purpose,created_at FROM access_requests"
                )
                version = 2
            # Every supported migration is one transaction; unknown or
            # conflicting tables roll the entire upgrade back.
            if version == 2:
                for table in ("access_requests", "access_request_versions"):
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN suggested_lifetime TEXT NOT NULL DEFAULT 'task'"
                    )
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN suggested_seconds INTEGER NOT NULL DEFAULT 14400"
                    )
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN request_key TEXT")
                connection.execute(
                    "CREATE TABLE request_idempotency ("
                    "task_id TEXT NOT NULL REFERENCES permission_tasks(task_id),"
                    "request_key TEXT NOT NULL, fingerprint TEXT NOT NULL,"
                    "request_id TEXT REFERENCES access_requests(request_id),"
                    "outcome TEXT NOT NULL CHECK(outcome IN ('request','granted_existing')),"
                    "PRIMARY KEY(task_id,request_key),"
                    "CHECK ((outcome='request' AND request_id IS NOT NULL) OR "
                    "(outcome='granted_existing' AND request_id IS NULL)))"
                )
                version = 3
            if version == 3:
                connection.execute(
                    "CREATE TABLE operation_outcome_receipts ("
                    "nonce TEXT PRIMARY KEY, "
                    "claim_id TEXT NOT NULL REFERENCES execution_claims(claim_id), "
                    "proof_hash TEXT NOT NULL, recorded_at REAL NOT NULL)"
                )
                version = 4
            if version == 4:
                connection.execute(_DECISION_ACTIONS_SCHEMA)
                version = 5
            if version == 5:
                connection.execute(_DECISION_PRESENTATIONS_SCHEMA)
                version = 6
            if version != _SCHEMA_VERSION:
                raise ValueError("unsupported permission ledger schema")
            self._validate_schema(connection, _SCHEMA_VERSION)
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    @staticmethod
    def _time_floor(connection: sqlite3.Connection, key: str) -> float | None:
        row = connection.execute(
            "SELECT value FROM authority_metadata WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            value = float(row["value"])
        except (TypeError, ValueError) as exc:
            raise PermissionDenied("trusted time record is invalid") from exc
        if not math.isfinite(value):
            raise PermissionDenied("trusted time record is invalid")
        return value

    @classmethod
    def _store_time_floor(
        cls, connection: sqlite3.Connection, key: str, value: float
    ) -> None:
        old = cls._time_floor(connection, key)
        if old is not None:
            value = max(value, old)
        connection.execute(
            "INSERT INTO authority_metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, repr(value)),
        )

    def _now(self, connection: sqlite3.Connection) -> float:
        wall = float(self._clock())
        monotonic = float(self._monotonic())
        if not math.isfinite(wall) or not math.isfinite(monotonic):
            raise PermissionDenied("trusted time unavailable")
        previous_wall = self._time_floor(connection, "wall_high_water")
        if previous_wall is not None and wall < previous_wall:
            raise PermissionDenied("clock rollback detected")
        previous_effective = self._time_floor(connection, "effective_high_water")
        with self._clock_lock:
            elapsed = monotonic - self._monotonic_anchor
            if elapsed < 0:
                raise PermissionDenied("monotonic clock moved backwards")
            effective = max(wall, self._wall_anchor + elapsed)
            if previous_effective is not None and effective < previous_effective:
                # A reopened service may see a stalled wall clock. Re-anchor
                # monotonic time to the durable floor, so time keeps advancing.
                effective = previous_effective
                self._wall_anchor = effective
                self._monotonic_anchor = monotonic
            self._clock_observations[id(connection)] = (wall, effective)
        self._store_time_floor(connection, "wall_high_water", wall)
        self._store_time_floor(connection, "effective_high_water", effective)
        return effective

    @staticmethod
    def _event(connection: sqlite3.Connection, task_id: str, event: str, ref: str, now: float) -> None:
        connection.execute(
            "INSERT INTO permission_events_outbox(task_id,event,reference_id,created_at) VALUES(?,?,?,?)",
            (task_id, event, ref, now),
        )

    @staticmethod
    def _task(connection: sqlite3.Connection, context: ExecutionContext, *, active: bool = True) -> sqlite3.Row:
        if not isinstance(context, ExecutionContext):
            raise PermissionDenied("trusted execution context is required")
        row = connection.execute(
            "SELECT * FROM permission_tasks WHERE task_id=?", (context.task_id,)
        ).fetchone()
        if row is None or any((
            row["instance_id"] != context.instance_id,
            row["owner_id"] != context.owner_id,
            row["agent_id"] != context.agent_id,
            row["session_id"] != context.session_id,
        )):
            raise PermissionDenied("task identity mismatch")
        if active and row["state"] != "active":
            raise PermissionDenied("task is not active")
        return row

    @staticmethod
    def _binding(connection: sqlite3.Connection, context: ExecutionContext, *, active: bool = True) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM execution_bindings WHERE execution_epoch=?", (context.execution_epoch,)
        ).fetchone()
        if row is None or any((
            row["task_id"] != context.task_id,
            row["run_id"] != context.run_id,
            row["worker_generation"] != context.worker_generation,
            row["context_generation"] != context.context_generation,
            row["backend_id"] != context.backend_id,
            row["executor_id"] != context.executor_id,
            row["policy_revision"] != context.policy_revision,
        )):
            raise PermissionDenied("execution binding mismatch")
        if active and row["state"] != "active":
            raise PermissionDenied("execution is fenced")
        return row

    @classmethod
    def _context(cls, connection: sqlite3.Connection, context: ExecutionContext, *, active: bool = True) -> None:
        cls._task(connection, context, active=active)
        cls._binding(connection, context, active=active)

    @staticmethod
    def _require_prior_execution_fenced(
        connection: sqlite3.Connection, operation: sqlite3.Row, context: ExecutionContext
    ) -> None:
        if operation["execution_epoch"] == context.execution_epoch:
            return
        prior = connection.execute(
            "SELECT task_id,run_id,state FROM execution_bindings WHERE execution_epoch=?",
            (operation["execution_epoch"],),
        ).fetchone()
        if (
            prior is None or prior["state"] != "fenced"
            or prior["task_id"] != operation["task_id"] or prior["run_id"] != operation["run_id"]
        ):
            raise OperationConflict("operation belongs to another active execution")

    @staticmethod
    def _insert_binding(connection: sqlite3.Connection, context: ExecutionContext) -> None:
        row = connection.execute(
            "SELECT * FROM execution_bindings WHERE execution_epoch=?", (context.execution_epoch,)
        ).fetchone()
        if row is not None:
            PermissionAuthority._binding(connection, context)
            return
        connection.execute(
            "INSERT INTO execution_bindings VALUES(?,?,?,?,?,?,?,?,?)",
            (context.execution_epoch, context.task_id, context.run_id, context.worker_generation,
             context.context_generation, context.backend_id, context.executor_id,
             context.policy_revision, "active"),
        )

    def open_task(self, context: ExecutionContext) -> None:
        if not isinstance(context, ExecutionContext):
            raise PermissionDenied("trusted execution context is required")
        with self._transaction() as connection:
            now = self._now(connection)
            row = connection.execute(
                "SELECT * FROM permission_tasks WHERE task_id=?", (context.task_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO permission_tasks VALUES(?,?,?,?,?,?,?,?,?)",
                    (context.task_id, context.instance_id, context.owner_id, context.agent_id,
                     context.session_id, "active", 1, now, None),
                )
                self._event(connection, context.task_id, "task_opened", context.task_id, now)
            else:
                self._task(connection, context)
            self._insert_binding(connection, context)

    def bind_execution(self, context: ExecutionContext) -> None:
        with self._transaction() as connection:
            self._now(connection)
            task = self._task(connection, context, active=False)
            if task["state"] not in {"active", "paused"}:
                raise PermissionDenied("task is terminal")
            self._insert_binding(connection, context)

    @staticmethod
    def _stop_epoch_resources(connection: sqlite3.Connection, epoch: str) -> None:
        # A prepared runner may still exist. Its reservation is retained until
        # trusted supervisor evidence confirms that it has actually stopped.
        connection.execute(
            "UPDATE execution_resources SET state='stopping' WHERE claim_id IN "
            "(SELECT claim_id FROM execution_claims WHERE execution_epoch=? "
            "AND state IN ('prepared','started')) AND state!='stopped'", (epoch,),
        )
        connection.execute(
            "UPDATE execution_claims SET state='cancelled' "
            "WHERE execution_epoch=? AND state='prepared'", (epoch,),
        )

    def pause_task(self, context: ExecutionContext) -> None:
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context)
            connection.execute(
                "UPDATE permission_tasks SET state='paused',revision=revision+1 WHERE task_id=?",
                (context.task_id,),
            )
            epochs = connection.execute(
                "SELECT execution_epoch FROM execution_bindings WHERE task_id=? AND state='active'",
                (context.task_id,),
            ).fetchall()
            for row in epochs:
                self._stop_epoch_resources(connection, row["execution_epoch"])
            self._event(connection, context.task_id, "task_paused", context.task_id, now)

    def resume_task(self, context: ExecutionContext) -> None:
        with self._transaction() as connection:
            now = self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            if task["state"] != "paused" or binding["state"] != "active":
                raise PermissionDenied("task or execution cannot resume")
            connection.execute(
                "UPDATE permission_tasks SET state='active',revision=revision+1 WHERE task_id=?",
                (context.task_id,),
            )
            self._event(connection, context.task_id, "task_resumed", context.task_id, now)

    def fence_execution(self, context: ExecutionContext) -> None:
        with self._transaction() as connection:
            now = self._now(connection)
            self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            if binding["state"] == "fenced":
                return
            connection.execute(
                "UPDATE execution_bindings SET state='fenced' WHERE execution_epoch=?",
                (context.execution_epoch,),
            )
            self._stop_epoch_resources(connection, context.execution_epoch)
            self._event(connection, context.task_id, "execution_fenced", context.execution_epoch, now)

    def end_run(self, context: ExecutionContext) -> None:
        """Fence a completed Run while preserving task and timed leases."""
        with self._transaction() as connection:
            now = self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            if task["state"] not in {"active", "paused"} or binding["state"] != "active":
                raise PermissionDenied("run cannot end from this execution")
            epochs = connection.execute(
                "SELECT execution_epoch FROM execution_bindings "
                "WHERE task_id=? AND run_id=? AND state='active'",
                (context.task_id, context.run_id),
            ).fetchall()
            for row in epochs:
                self._stop_epoch_resources(connection, row["execution_epoch"])
            connection.execute(
                "UPDATE execution_bindings SET state='fenced' WHERE task_id=? AND run_id=?",
                (context.task_id, context.run_id),
            )
            connection.execute(
                "UPDATE permission_leases SET status='revoked' WHERE task_id=? AND run_id=? "
                "AND lifetime IN ('once','run') AND status!='revoked'",
                (context.task_id, context.run_id),
            )
            connection.execute(
                "UPDATE access_requests SET state='withdrawn' WHERE task_id=? AND run_id=? AND state='pending'",
                (context.task_id, context.run_id),
            )
            connection.execute(
                "UPDATE pending_operations SET state='cancelled' WHERE task_id=? AND run_id=? "
                "AND state IN ('waiting','prepared')",
                (context.task_id, context.run_id),
            )
            self._event(connection, context.task_id, "run_ended", context.run_id, now)

    @staticmethod
    def _record_request_key(
        connection: sqlite3.Connection, task_id: str, request_key: str | None,
        fingerprint: str | None, request_id: str | None,
    ) -> None:
        if request_key is not None:
            connection.execute(
                "INSERT INTO request_idempotency VALUES(?,?,?,?,?)",
                (task_id, request_key, fingerprint, request_id,
                 "request" if request_id is not None else "granted_existing"),
            )

    def request_access(
        self,
        context: ExecutionContext,
        scopes: Iterable[Scope],
        *,
        purpose: str,
        operation_id: str | None = None,
        arguments_digest: str | None = None,
        suggested_lifetime: str = "task",
        suggested_seconds: int | None = None,
        request_key: str | None = None,
    ) -> AccessRequest:
        if not isinstance(context, ExecutionContext):
            raise PermissionDenied("trusted execution context is required")
        requested = canonical_scope_set(scopes)
        operation_digest = scope_digest(requested)
        purpose = _nonempty(purpose, "purpose", limit=2000)
        if not isinstance(suggested_lifetime, str) or suggested_lifetime not in {"once", "run", "task", "ttl"}:
            raise ValueError("invalid suggested lifetime")
        if suggested_seconds is None:
            suggested_seconds = self._max_lease
        if type(suggested_seconds) is not int or not 1 <= suggested_seconds <= self._max_lease:
            raise ValueError("invalid suggested duration")
        if suggested_lifetime == "once" and operation_id is None:
            raise ValueError("once suggestion requires a bound operation")
        if request_key is not None and (
            not isinstance(request_key, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", request_key) is None
        ):
            raise ValueError("invalid request key")
        if (operation_id is None) != (arguments_digest is None):
            raise ValueError("operation identity and arguments digest must be provided together")
        if operation_id is not None:
            _nonempty(operation_id, "operation_id")
            _digest(arguments_digest, "arguments_digest")
        op_key = operation_id or ""
        fingerprint = None
        if request_key is not None:
            fingerprint = hashlib.sha256(_json({
                "instance": context.instance_id, "owner": context.owner_id,
                "agent": context.agent_id, "task": context.task_id, "run": context.run_id,
                "session": context.session_id, "policy": context.policy_revision,
                "scope": operation_digest, "operation": operation_id, "arguments": arguments_digest,
                "purpose": purpose, "suggested_lifetime": suggested_lifetime,
                "suggested_seconds": suggested_seconds,
            }).encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context)
            if request_key is not None:
                prior_key = connection.execute(
                    "SELECT * FROM request_idempotency WHERE task_id=? AND request_key=?",
                    (context.task_id, request_key),
                ).fetchone()
                if prior_key is not None:
                    if prior_key["fingerprint"] != fingerprint:
                        raise OperationConflict("request key already binds different facts")
                    if prior_key["request_id"] is not None:
                        prior = self._request_for_task(
                            connection, context.task_id, prior_key["request_id"]
                        )
                        if prior["state"] == "pending" and prior["execution_epoch"] != context.execution_epoch:
                            raise OperationConflict("request key belongs to another execution")
                        return self._visible_request(prior, now)
                    still_covered = self._covering_leases(
                        connection, context, now, requested, operation_id=operation_id
                    )
                    if still_covered is None:
                        raise OperationConflict("the prior grant is no longer active")
                    expires = min(connection.execute(
                        "SELECT expires_at FROM permission_leases WHERE lease_id=?", (lease_id,)
                    ).fetchone()[0] for lease_id in still_covered)
                    return AccessRequest("", context.task_id, 0, requested, operation_digest,
                                         operation_id, "granted_existing", expires, purpose,
                                         suggested_lifetime, suggested_seconds, request_key)
            operation = None
            if operation_id:
                operation = connection.execute(
                    "SELECT * FROM pending_operations WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if operation is not None and any((
                    operation["task_id"] != context.task_id,
                    operation["run_id"] != context.run_id,
                    operation["arguments_digest"] != arguments_digest,
                    operation["scope_digest"] != operation_digest,
                    operation["state"] != "waiting",
                )):
                    raise OperationConflict("operation identity already bound")
                if operation is not None:
                    self._require_prior_execution_fenced(connection, operation, context)

            covering = self._covering_leases(
                connection, context, now, requested, operation_id=operation_id
            )
            if covering:
                # Another approved lease may satisfy a waiting operation. Close
                # obsolete prompts so an old action cannot grant a duplicate.
                if operation_id:
                    pending = connection.execute(
                        "SELECT request_id,expires_at FROM access_requests "
                        "WHERE task_id=? AND operation_id=? AND state='pending'",
                        (context.task_id, operation_id),
                    ).fetchall()
                    for item in pending:
                        state = "expired" if item["expires_at"] <= now else "withdrawn"
                        connection.execute(
                            "UPDATE access_requests SET state=? WHERE request_id=?",
                            (state, item["request_id"]),
                        )
                        self._event(connection, context.task_id, "request_" + state,
                                    item["request_id"], now)
                expires = min(connection.execute(
                    "SELECT expires_at FROM permission_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()[0] for lease_id in covering)
                self._record_request_key(connection, context.task_id, request_key, fingerprint, None)
                return AccessRequest("", context.task_id, 0, requested, operation_digest,
                                     operation_id, "granted_existing", expires, purpose,
                                     suggested_lifetime, suggested_seconds, request_key)

            # Request only actions that existing leases do not already cover.
            # Keep the pending operation bound to the complete original scope.
            missing = []
            for required in requested:
                actions = []
                for action in required.actions:
                    single = Scope(required.kind, required.resource, (action,),
                                   required.normalization_version)
                    if self._covering_leases(
                        connection, context, now, (single,), operation_id=operation_id
                    ) is None:
                        actions.append(action)
                if actions:
                    missing.append(Scope(required.kind, required.resource, tuple(actions),
                                         required.normalization_version))
            normalized = canonical_scope_set(missing)
            digest = scope_digest(normalized)

            if operation_id:
                existing = connection.execute(
                    "SELECT * FROM access_requests WHERE task_id=? AND operation_id=? "
                    "AND policy_revision=? AND state='pending' ORDER BY rowid DESC LIMIT 1",
                    (context.task_id, op_key, context.policy_revision),
                ).fetchone()
            else:
                existing = connection.execute(
                    "SELECT * FROM access_requests WHERE task_id=? AND operation_id='' "
                    "AND scope_digest=? AND policy_revision=? AND state='pending'",
                    (context.task_id, digest, context.policy_revision),
                ).fetchone()
            if existing is not None and existing["expires_at"] <= now:
                connection.execute(
                    "UPDATE access_requests SET state='expired' WHERE request_id=?",
                    (existing["request_id"],),
                )
                self._event(connection, context.task_id, "request_expired", existing["request_id"], now)
                existing = None
            if existing is not None:
                if request_key is not None and (
                    existing["suggested_lifetime"] != suggested_lifetime
                    or existing["suggested_seconds"] != suggested_seconds
                ):
                    raise OperationConflict("equivalent pending request has different suggestions")
                if existing["execution_epoch"] == context.execution_epoch:
                    self._record_request_key(
                        connection, context.task_id, request_key, fingerprint, existing["request_id"]
                    )
                    return _request_from(existing)
                old_binding = connection.execute(
                    "SELECT state FROM execution_bindings WHERE execution_epoch=?",
                    (existing["execution_epoch"],),
                ).fetchone()
                if old_binding is None or old_binding["state"] != "fenced":
                    raise OperationConflict("request belongs to another active execution")
                if existing["run_id"] == context.run_id:
                    revision = existing["revision"] + 1
                    connection.execute(
                        "UPDATE access_requests SET execution_epoch=?,context_generation=?,revision=? "
                        "WHERE request_id=?",
                        (context.execution_epoch, context.context_generation,
                         revision, existing["request_id"]),
                    )
                    self._snapshot_request(connection, existing["request_id"], now)
                    if operation_id:
                        connection.execute(
                            "UPDATE pending_operations SET execution_epoch=? WHERE operation_id=?",
                            (context.execution_epoch, operation_id),
                        )
                    self._event(connection, context.task_id, "request_rebound", existing["request_id"], now)
                    self._record_request_key(
                        connection, context.task_id, request_key, fingerprint, existing["request_id"]
                    )
                    return _request_from(connection.execute(
                        "SELECT * FROM access_requests WHERE request_id=?", (existing["request_id"],)
                    ).fetchone())
                connection.execute(
                    "UPDATE access_requests SET state='withdrawn' WHERE request_id=?",
                    (existing["request_id"],),
                )
                self._event(connection, context.task_id, "request_withdrawn", existing["request_id"], now)

            prior = connection.execute(
                "SELECT * FROM access_requests WHERE task_id=? AND operation_id=? AND scope_digest=? "
                "AND policy_revision=? ORDER BY rowid DESC LIMIT 1",
                (context.task_id, op_key, digest, context.policy_revision),
            ).fetchone()
            if prior is not None and prior["state"] == "denied":
                self._record_request_key(
                    connection, context.task_id, request_key, fingerprint, prior["request_id"]
                )
                return _request_from(prior)
            if operation_id:
                if operation is None:
                    connection.execute(
                        "INSERT INTO pending_operations VALUES(?,?,?,?,?,?,?)",
                        (operation_id, context.task_id, context.run_id, arguments_digest,
                         operation_digest, context.execution_epoch, "waiting"),
                    )
                elif operation["execution_epoch"] != context.execution_epoch:
                    previous = connection.execute(
                        "SELECT state FROM execution_bindings WHERE execution_epoch=?",
                        (operation["execution_epoch"],),
                    ).fetchone()
                    if previous is None or previous["state"] != "fenced":
                        raise OperationConflict("operation belongs to another active execution")
                    connection.execute(
                        "UPDATE pending_operations SET execution_epoch=? WHERE operation_id=?",
                        (context.execution_epoch, operation_id),
                    )
            request_id = uuid4().hex
            connection.execute(
                "INSERT INTO access_requests "
                "(request_id,task_id,run_id,session_id,context_generation,execution_epoch,"
                "policy_revision,operation_id,arguments_digest,scopes_json,scope_digest,purpose,"
                "revision,state,created_at,expires_at,suggested_lifetime,suggested_seconds,request_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, context.task_id, context.run_id, context.session_id,
                 context.context_generation, context.execution_epoch, context.policy_revision,
                 op_key, arguments_digest, _scope_json(normalized), digest, purpose, 1, "pending",
                 now, now + self._request_ttl, suggested_lifetime, suggested_seconds, request_key),
            )
            self._snapshot_request(connection, request_id, now)
            self._record_request_key(connection, context.task_id, request_key, fingerprint, request_id)
            self._event(connection, context.task_id, "request_created", request_id, now)
            return _request_from(connection.execute(
                "SELECT * FROM access_requests WHERE request_id=?", (request_id,)
            ).fetchone())

    @staticmethod
    def _snapshot_request(
        connection: sqlite3.Connection, request_id: str, now: float
    ) -> None:
        connection.execute(
            "INSERT INTO access_request_versions "
            "(request_id,revision,scopes_json,scope_digest,run_id,session_id,"
            "context_generation,execution_epoch,policy_revision,operation_id,"
            "arguments_digest,purpose,recorded_at,suggested_lifetime,suggested_seconds,request_key) "
            "SELECT request_id,revision,scopes_json,scope_digest,run_id,session_id,"
            "context_generation,execution_epoch,policy_revision,operation_id,"
            "arguments_digest,purpose,?,suggested_lifetime,suggested_seconds,request_key "
            "FROM access_requests WHERE request_id=?",
            (now, request_id),
        )

    @staticmethod
    def _request_for_task(
        connection: sqlite3.Connection, task_id: str, request_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM access_requests WHERE request_id=? AND task_id=?",
            (request_id, task_id),
        ).fetchone()
        if row is None:
            raise PermissionDenied("request not found for this task")
        return row

    @staticmethod
    def _visible_request(row: sqlite3.Row, now: float) -> AccessRequest:
        request = _request_from(row)
        if request.state == "pending" and request.expires_at <= now:
            return replace(request, state="expired")
        return request

    def get_request(self, context: ExecutionContext, request_id: str) -> AccessRequest:
        _nonempty(request_id, "request_id")
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context, active=False)
            row = self._request_for_task(connection, context.task_id, request_id)
            return self._visible_request(row, now)

    def list_requests(
        self, context: ExecutionContext, *, limit: int = 100
    ) -> tuple[AccessRequest, ...]:
        if type(limit) is not int or not 1 <= limit <= 250:
            raise ValueError("limit must be between 1 and 250")
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context, active=False)
            rows = connection.execute(
                "SELECT * FROM access_requests WHERE task_id=? "
                "ORDER BY rowid DESC LIMIT ?", (context.task_id, limit),
            ).fetchall()
            return tuple(self._visible_request(row, now) for row in rows)

    def get_request_version(
        self, context: ExecutionContext, request_id: str, revision: int
    ) -> AccessRequest:
        _nonempty(request_id, "request_id")
        if type(revision) is not int or revision < 1:
            raise ValueError("revision must be a positive integer")
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context, active=False)
            row = self._request_for_task(connection, context.task_id, request_id)
            snapshot = connection.execute(
                "SELECT * FROM access_request_versions WHERE request_id=? AND revision=?",
                (request_id, revision),
            ).fetchone()
            if snapshot is None:
                raise PermissionDenied("request revision not found")
            return replace(
                self._visible_request(row, now),
                revision=revision,
                scopes=_load_scopes(snapshot["scopes_json"]),
                scope_digest=snapshot["scope_digest"],
                purpose=snapshot["purpose"],
                suggested_lifetime=snapshot["suggested_lifetime"],
                suggested_seconds=snapshot["suggested_seconds"],
                request_key=snapshot["request_key"],
                is_current_revision=(revision == row["revision"]),
            )

    def narrow_request(
        self, context: ExecutionContext, request_id: str, expected_revision: int,
        scopes: Iterable[Scope],
    ) -> AccessRequest:
        _nonempty(request_id, "request_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("revision must be a positive integer")
        normalized = canonical_scope_set(scopes)
        digest = scope_digest(normalized)
        with self._transaction() as connection:
            now = self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            if task["state"] not in {"active", "paused"} or binding["state"] != "active":
                raise PermissionDenied("task cannot change requests")
            row = self._request_for_task(connection, context.task_id, request_id)
            if row["revision"] != expected_revision:
                raise RevisionConflict("request revision changed")
            if row["state"] != "pending" or row["expires_at"] <= now:
                raise InvalidDecision("request is no longer pending")
            if row["execution_epoch"] != context.execution_epoch:
                raise PermissionDenied("request belongs to a different execution")
            original = _load_scopes(row["scopes_json"])
            if not all(any(scope_covers(parent, child) for parent in original) for child in normalized):
                raise InvalidDecision("new scope exceeds the request")
            if digest == row["scope_digest"]:
                return _request_from(row)
            revision = expected_revision + 1
            try:
                connection.execute(
                    "UPDATE access_requests SET scopes_json=?,scope_digest=?,revision=? "
                    "WHERE request_id=?",
                    (_scope_json(normalized), digest, revision, request_id),
                )
                self._snapshot_request(connection, request_id, now)
            except sqlite3.IntegrityError as exc:
                raise RevisionConflict("equivalent request is already pending") from exc
            self._event(connection, context.task_id, "request_narrowed", request_id, now)
            return _request_from(self._request_for_task(connection, context.task_id, request_id))

    def withdraw_request(
        self, context: ExecutionContext, request_id: str, expected_revision: int
    ) -> AccessRequest:
        _nonempty(request_id, "request_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("revision must be a positive integer")
        with self._transaction() as connection:
            now = self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            row = self._request_for_task(connection, context.task_id, request_id)
            if row["revision"] != expected_revision:
                raise RevisionConflict("request revision changed")
            if row["state"] != "pending" or row["expires_at"] <= now:
                return self._visible_request(row, now)
            if task["state"] not in {"active", "paused"} or binding["state"] != "active":
                raise PermissionDenied("task cannot withdraw requests")
            connection.execute(
                "UPDATE access_requests SET state='withdrawn' WHERE request_id=?", (request_id,),
            )
            if row["operation_id"]:
                connection.execute(
                    "UPDATE pending_operations SET state='cancelled' "
                    "WHERE operation_id=? AND task_id=? AND state='waiting' "
                    "AND NOT EXISTS(SELECT 1 FROM access_requests WHERE task_id=? "
                    "AND operation_id=? AND state='pending' AND expires_at>?)",
                    (row["operation_id"], context.task_id, context.task_id,
                     row["operation_id"], now),
                )
            self._event(connection, context.task_id, "request_withdrawn", request_id, now)
            return _request_from(self._request_for_task(connection, context.task_id, request_id))

    @staticmethod
    def _check_action_request(row: sqlite3.Row, context: ExecutionContext, now: float) -> None:
        if (row["state"] != "pending" or row["expires_at"] <= now
                or row["task_id"] != context.task_id or row["run_id"] != context.run_id
                or row["session_id"] != context.session_id
                or row["context_generation"] != context.context_generation
                or row["execution_epoch"] != context.execution_epoch
                or row["policy_revision"] != context.policy_revision):
            raise InvalidDecision("request is no longer valid for this execution")

    def issue_decision_action(
        self, context: ExecutionContext, request_id: str, expected_revision: int, *,
        approved_scopes: Iterable[Scope], choice: str, lifetime: str,
        duration_seconds: int, action_ttl_seconds: int = 15 * 60,
        presentation_id: str | None = None, presentation_slot: str | None = None,
    ) -> DecisionAction:
        """Register an exact UI choice; this does not grant or authenticate it.

        Only a trusted presentation service may expose these snapshots. It may
        supply its own server-issued presentation and stable slot to retry the
        same rendering without creating another action; these identifiers do
        not authenticate a user or replace the frontend's menu binding checks.
        A later action still requires an independently verified decision receipt.
        """
        _nonempty(request_id, "request_id")
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("revision must be a positive integer")
        if not isinstance(choice, str) or choice not in {"approve", "deny"}:
            raise ValueError("invalid decision choice")
        if not isinstance(lifetime, str) or lifetime not in {"once", "run", "task", "ttl"}:
            raise ValueError("invalid decision lifetime")
        if type(duration_seconds) is not int or not 1 <= duration_seconds <= self._max_lease:
            raise ValueError("invalid decision duration")
        if type(action_ttl_seconds) is not int or not 1 <= action_ttl_seconds <= 15 * 60:
            raise ValueError("invalid action lifetime")
        if (presentation_id is None) != (presentation_slot is None):
            raise ValueError("presentation and slot must be supplied together")
        if presentation_id is not None:
            for value in (presentation_id, presentation_slot):
                if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
                    raise ValueError("invalid trusted presentation binding")
        normalized = canonical_scope_set(approved_scopes)
        approved_digest = scope_digest(normalized)
        with self._transaction() as connection:
            now = self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            if task["state"] not in {"active", "paused"} or binding["state"] != "active":
                raise PermissionDenied("task cannot issue decision actions")
            row = self._request_for_task(connection, context.task_id, request_id)
            if row["revision"] != expected_revision:
                raise RevisionConflict("request revision changed")
            self._check_action_request(row, context, now)
            requested = _load_scopes(row["scopes_json"])
            if scope_digest(requested) != row["scope_digest"]:
                raise InvalidDecision("request scope is damaged")
            if not all(any(scope_covers(parent, child) for parent in requested) for child in normalized):
                raise InvalidDecision("action scope exceeds the request")
            if choice == "deny" and approved_digest != row["scope_digest"]:
                raise InvalidDecision("denial must bind the entire displayed request")
            if lifetime == "once" and not row["operation_id"]:
                raise InvalidDecision("once requires an operation")
            if presentation_id is not None:
                existing = connection.execute(
                    "SELECT a.* FROM decision_action_presentations p "
                    "LEFT JOIN decision_actions a ON a.action_id=p.action_id "
                    "WHERE p.presentation_id=? AND p.presentation_slot=?",
                    (presentation_id, presentation_slot),
                ).fetchone()
                if existing is not None:
                    if existing["action_id"] is None:
                        raise InvalidDecision("presentation action is unavailable")
                    if existing["expires_at"] <= now:
                        raise InvalidDecision("presentation action has expired")
                    if any((
                        existing["request_id"] != request_id,
                        existing["request_revision"] != expected_revision,
                        existing["requested_digest"] != row["scope_digest"],
                        existing["approved_digest"] != approved_digest,
                        _load_scopes(existing["approved_scopes"]) != normalized,
                        existing["operation_id"] != (row["operation_id"] or None),
                        existing["arguments_digest"] != row["arguments_digest"],
                        existing["choice"] != choice,
                        existing["lifetime"] != lifetime,
                        existing["duration_seconds"] != duration_seconds,
                        existing["expires_at"] > min(row["expires_at"], now + action_ttl_seconds),
                    )):
                        raise RevisionConflict("presentation slot already binds different facts")
                    return _action_from(existing)
            count = connection.execute(
                "SELECT count(*) FROM decision_actions WHERE request_id=? "
                "AND request_revision=? AND expires_at>?", (request_id, expected_revision, now),
            ).fetchone()[0]
            if count >= _MAX_LIVE_DECISION_ACTIONS:
                raise PermissionDenied("too many live decision actions")
            action_id, nonce = uuid4().hex, uuid4().hex
            connection.execute(
                "INSERT INTO decision_actions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, nonce, request_id, expected_revision, row["scope_digest"],
                 _scope_json(normalized), approved_digest, row["operation_id"] or None,
                 row["arguments_digest"], choice, lifetime, duration_seconds, now,
                 min(row["expires_at"], now + action_ttl_seconds)),
            )
            if presentation_id is not None:
                connection.execute(
                    "INSERT INTO decision_action_presentations VALUES(?,?,?)",
                    (presentation_id, presentation_slot, action_id),
                )
            return _action_from(connection.execute(
                "SELECT * FROM decision_actions WHERE action_id=?", (action_id,)
            ).fetchone())

    def get_decision_action(self, context: ExecutionContext, action_id: str) -> DecisionAction:
        """Return an issued snapshot only while the original request is current."""
        _nonempty(action_id, "action_id")
        with self._transaction() as connection:
            now = self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            if task["state"] not in {"active", "paused"} or binding["state"] != "active":
                raise PermissionDenied("task cannot use decision actions")
            action = connection.execute(
                "SELECT * FROM decision_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if action is None:
                raise PermissionDenied("decision action not found")
            row = self._request_for_task(connection, context.task_id, action["request_id"])
            if row["revision"] != action["request_revision"]:
                raise RevisionConflict("request revision changed")
            self._check_action_request(row, context, now)
            if scope_digest(_load_scopes(row["scopes_json"])) != row["scope_digest"]:
                raise InvalidDecision("request scope is damaged")
            if (action["expires_at"] <= now or action["requested_digest"] != row["scope_digest"]
                    or action["operation_id"] != (row["operation_id"] or None)
                    or action["arguments_digest"] != row["arguments_digest"]):
                raise InvalidDecision("decision action is no longer current")
            result = _action_from(action)
            if scope_digest(result.approved_scopes) != result.approved_digest:
                raise InvalidDecision("decision action scope is damaged")
            return result

    def get_committed_decision_action(
        self, context: ExecutionContext, action_id: str,
    ) -> DecisionAction:
        """Retrieve only an already-committed action for authenticated replay."""
        _nonempty(action_id, "action_id")
        with self._transaction() as connection:
            self._now(connection)
            self._context(connection, context, active=False)
            action = connection.execute(
                "SELECT * FROM decision_actions WHERE action_id=?", (action_id,)
            ).fetchone()
            if action is None:
                raise PermissionDenied("decision action not found")
            self._request_for_task(connection, context.task_id, action["request_id"])
            snapshot = connection.execute(
                "SELECT * FROM access_request_versions WHERE request_id=? AND revision=?",
                (action["request_id"], action["request_revision"]),
            ).fetchone()
            decision = connection.execute(
                "SELECT * FROM approval_decisions WHERE idempotency_key=?", (action_id,)
            ).fetchone()
            if snapshot is None or decision is None or any((
                snapshot["run_id"] != context.run_id,
                snapshot["session_id"] != context.session_id,
                snapshot["context_generation"] != context.context_generation,
                snapshot["execution_epoch"] != context.execution_epoch,
                snapshot["policy_revision"] != context.policy_revision,
                snapshot["scope_digest"] != action["requested_digest"],
                (snapshot["operation_id"] or None) != action["operation_id"],
                snapshot["arguments_digest"] != action["arguments_digest"],
                decision["request_id"] != action["request_id"],
                decision["request_revision"] != action["request_revision"],
                decision["nonce"] != action["nonce"],
                decision["choice"] != action["choice"],
                decision["approved_scopes"] != action["approved_scopes"],
            )):
                raise InvalidDecision("committed decision action does not match")
            result = _action_from(action)
            if scope_digest(result.approved_scopes) != result.approved_digest:
                raise InvalidDecision("decision action scope is damaged")
            return result

    def get_committed_decision_result(
        self, context: ExecutionContext, action_id: str, *, principal_expires_at: float,
    ) -> PermissionLease | None:
        """Read the current outcome without creating another decision."""
        if isinstance(principal_expires_at, bool) or not isinstance(principal_expires_at, (int, float)):
            raise ValueError("invalid principal expiry")
        try:
            finite_expiry = math.isfinite(principal_expires_at)
        except OverflowError as exc:
            raise ValueError("invalid principal expiry") from exc
        if not finite_expiry:
            raise ValueError("invalid principal expiry")
        action = self.get_committed_decision_action(context, action_id)
        with self._transaction() as connection:
            now = self._now(connection)
            if principal_expires_at <= now:
                raise PermissionDenied("principal session expired")
            self._context(connection, context, active=False)
            decision = connection.execute(
                "SELECT * FROM approval_decisions WHERE idempotency_key=? AND nonce=? "
                "AND request_id=? AND request_revision=?",
                (action.action_id, action.nonce, action.request_id, action.request_revision),
            ).fetchone()
            if decision is None or decision["choice"] != action.choice:
                raise InvalidDecision("committed decision action does not match")
            row = connection.execute(
                "SELECT * FROM permission_leases WHERE decision_id=?", (decision["decision_id"],)
            ).fetchone()
            if action.choice == "deny":
                if row is not None:
                    raise InvalidDecision("denied decision has an unexpected lease")
                return None
            if row is None:
                raise InvalidDecision("approved decision has no lease")
            lease = _lease_from(row)
            if lease.status in {"active", "reserved"} and lease.expires_at <= now:
                return replace(lease, status="expired")
            return lease

    def decide(
        self, reference: str, *, approved_scopes: Iterable[Scope], action_id: str | None = None,
    ) -> PermissionLease | None:
        try:
            _nonempty(reference, "decision receipt reference", limit=16 * 1024)
            if action_id is not None:
                _nonempty(action_id, "action_id")
        except ValueError as exc:
            raise InvalidDecision("invalid decision receipt reference") from exc
        proof = self._verifier.verify(reference)
        if not isinstance(proof, DecisionReceipt):
            raise InvalidDecision("trusted decision receipt unavailable")
        normalized = canonical_scope_set(approved_scopes)
        approved_digest = scope_digest(normalized)
        try:
            for key in ("nonce", "request_id", "requested_digest", "approved_digest", "instance_id",
                        "task_id", "run_id", "execution_epoch", "owner_id", "agent_id", "session_id",
                        "context_generation", "principal_id", "idempotency_key"):
                _nonempty(getattr(proof, key), key)
            _digest(proof.requested_digest, "requested_digest")
            _digest(proof.approved_digest, "approved_digest")
            if proof.operation_id is None:
                if proof.arguments_digest is not None:
                    raise ValueError("unbound arguments digest")
            else:
                _nonempty(proof.operation_id, "operation_id")
                _digest(proof.arguments_digest, "arguments_digest")
        except ValueError as exc:
            raise InvalidDecision("invalid receipt") from exc
        if (
            type(proof.request_revision) is not int or proof.request_revision < 1
            or type(proof.policy_revision) is not int or proof.policy_revision < 0
            or type(proof.duration_seconds) is not int or proof.duration_seconds <= 0
            or proof.duration_seconds > self._max_lease
            or proof.choice not in {"approve", "deny"}
            or proof.lifetime not in {"once", "run", "task", "ttl"}
            or isinstance(proof.expires_at, bool)
            or not isinstance(proof.expires_at, (int, float))
            or not math.isfinite(proof.expires_at)
            or proof.approved_digest != approved_digest
        ):
            raise InvalidDecision("invalid decision facts")
        import hashlib
        proof_hash = hashlib.sha256(_json(asdict(proof)).encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            now = self._now(connection)
            related = connection.execute(
                "SELECT action_id FROM decision_actions WHERE action_id=? OR nonce=? LIMIT 1",
                (proof.idempotency_key, proof.nonce),
            ).fetchone()
            if action_id is None and related is not None:
                raise InvalidDecision("issued decision action must be verified")
            action = None
            if action_id is not None:
                action = connection.execute(
                    "SELECT * FROM decision_actions WHERE action_id=?", (action_id,)
                ).fetchone()
                if action is None or any((
                    action["action_id"] != proof.idempotency_key,
                    action["nonce"] != proof.nonce,
                    action["request_id"] != proof.request_id,
                    action["request_revision"] != proof.request_revision,
                    action["requested_digest"] != proof.requested_digest,
                    action["approved_digest"] != approved_digest,
                    action["approved_scopes"] != _scope_json(normalized),
                    action["operation_id"] != proof.operation_id,
                    action["arguments_digest"] != proof.arguments_digest,
                    action["choice"] != proof.choice,
                    action["lifetime"] != proof.lifetime,
                    action["duration_seconds"] != proof.duration_seconds,
                    proof.expires_at > action["expires_at"],
                    proof.expires_at <= action["created_at"],
                )):
                    raise InvalidDecision("receipt does not bind the issued action")
            prior = connection.execute(
                "SELECT * FROM approval_decisions WHERE idempotency_key=?", (proof.idempotency_key,)
            ).fetchone()
            if prior is not None:
                if prior["proof_hash"] != proof_hash or prior["nonce"] != proof.nonce:
                    raise InvalidDecision("decision idempotency conflict")
                lease = connection.execute(
                    "SELECT * FROM permission_leases WHERE decision_id=?", (prior["decision_id"],)
                ).fetchone()
                return _lease_from(lease) if lease is not None else None
            if proof.expires_at <= now or (action is not None and action["expires_at"] <= now):
                raise InvalidDecision("decision receipt or action expired")
            request = connection.execute(
                "SELECT r.*,t.instance_id,t.owner_id,t.agent_id,t.state AS task_state "
                "FROM access_requests r JOIN permission_tasks t ON t.task_id=r.task_id "
                "WHERE r.request_id=?", (proof.request_id,),
            ).fetchone()
            if request is None:
                raise InvalidDecision("request not found")
            if proof.request_revision != request["revision"]:
                raise RevisionConflict("request revision changed")
            if any((
                request["state"] != "pending", request["expires_at"] <= now,
                request["task_state"] not in {"active", "paused"},
                request["instance_id"] != proof.instance_id,
                request["task_id"] != proof.task_id,
                request["run_id"] != proof.run_id,
                request["execution_epoch"] != proof.execution_epoch,
                request["operation_id"] != (proof.operation_id or ""),
                request["arguments_digest"] != proof.arguments_digest,
                request["policy_revision"] != proof.policy_revision,
                request["scope_digest"] != proof.requested_digest,
                request["owner_id"] != proof.owner_id,
                request["owner_id"] != proof.principal_id,
                request["agent_id"] != proof.agent_id,
                request["session_id"] != proof.session_id,
                request["context_generation"] != proof.context_generation,
            )):
                raise InvalidDecision("receipt does not bind the current request")
            try:
                requested = _load_scopes(request["scopes_json"])
                if scope_digest(requested) != request["scope_digest"]:
                    raise ValueError("request scope digest changed")
            except (TypeError, ValueError, OSError, RuntimeError) as exc:
                raise InvalidDecision("request scope is unavailable or changed") from exc
            if not all(any(scope_covers(parent, child) for parent in requested) for child in normalized):
                raise InvalidDecision("approved scope exceeds the request")
            if proof.lifetime == "once" and not request["operation_id"]:
                raise InvalidDecision("once requires an operation")
            decision_id = uuid4().hex
            try:
                connection.execute(
                    "INSERT INTO approval_decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (decision_id, proof.request_id, proof.request_revision, proof.nonce,
                     proof.idempotency_key, proof_hash, proof.choice, _scope_json(normalized),
                     proof.principal_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidDecision("decision receipt already consumed") from exc
            state = "decided" if proof.choice == "approve" else "denied"
            connection.execute(
                "UPDATE access_requests SET state=? WHERE request_id=?", (state, proof.request_id)
            )
            self._event(connection, request["task_id"], "request_" + state, proof.request_id, now)
            if proof.choice == "deny":
                return None
            binding = connection.execute(
                "SELECT policy_revision FROM execution_bindings WHERE task_id=? AND run_id=? "
                "AND context_generation=? AND execution_epoch=? AND policy_revision=? AND state='active'",
                (request["task_id"], request["run_id"], request["context_generation"],
                 request["execution_epoch"], request["policy_revision"]),
            ).fetchone()
            if binding is None:
                raise InvalidDecision("execution binding is no longer active")
            lease_id = uuid4().hex
            connection.execute(
                "INSERT INTO permission_leases VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (lease_id, decision_id, request["task_id"], _scope_json(normalized), proof.lifetime,
                 request["operation_id"] if proof.lifetime == "once" else None,
                 request["run_id"] if proof.lifetime in {"once", "run"} else None,
                 binding["policy_revision"], now + proof.duration_seconds, "active", None),
            )
            self._event(connection, request["task_id"], "lease_created", lease_id, now)
            return _lease_from(connection.execute(
                "SELECT * FROM permission_leases WHERE lease_id=?", (lease_id,)
            ).fetchone())

    @staticmethod
    def _valid_leases(
        connection: sqlite3.Connection,
        context: ExecutionContext,
        now: float,
        *,
        operation_id: str | None,
        claim_id: str | None = None,
    ) -> list[sqlite3.Row]:
        rows = connection.execute(
            "SELECT * FROM permission_leases WHERE task_id=? AND expires_at>? "
            "AND policy_revision=? AND status IN ('active','reserved') "
            "ORDER BY CASE lifetime WHEN 'once' THEN 1 ELSE 0 END, lease_id",
            (context.task_id, now, context.policy_revision),
        ).fetchall()
        return [
            row for row in rows
            if (row["lifetime"] not in {"once", "run"} or row["run_id"] == context.run_id)
            and (row["lifetime"] != "once" or row["operation_id"] == operation_id)
            and (row["status"] != "reserved" or row["reserved_claim"] == claim_id)
        ]

    @classmethod
    def _covering_leases(
        cls,
        connection: sqlite3.Connection,
        context: ExecutionContext,
        now: float,
        scopes: tuple[Scope, ...],
        *,
        operation_id: str | None,
        claim_id: str | None = None,
    ) -> tuple[str, ...] | None:
        rows = cls._valid_leases(connection, context, now, operation_id=operation_id, claim_id=claim_id)
        selected: set[str] = set()
        for required in scopes:
            for action in required.actions:
                single = Scope(required.kind, required.resource, (action,), required.normalization_version)
                match = next((row for row in rows if any(
                    scope_covers(grant, single) for grant in _load_scopes(row["scopes_json"])
                )), None)
                if match is None:
                    return None
                selected.add(match["lease_id"])
        return tuple(sorted(selected))

    def evaluate(self, context: ExecutionContext, scopes: Iterable[Scope], *, operation_id: str | None = None) -> bool:
        normalized = canonical_scope_set(scopes)
        scope_digest(normalized)
        with self._transaction() as connection:
            now = self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            if task["state"] != "active" or binding["state"] != "active":
                return False
            return self._covering_leases(
                connection, context, now, normalized, operation_id=operation_id
            ) is not None

    def prepare(
        self,
        context: ExecutionContext,
        operation_id: str,
        arguments_digest: str,
        scopes: Iterable[Scope],
    ) -> ExecutionClaim:
        _nonempty(operation_id, "operation_id")
        _digest(arguments_digest, "arguments_digest")
        normalized = canonical_scope_set(scopes)
        digest = scope_digest(normalized)
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context)
            operation = connection.execute(
                "SELECT * FROM pending_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if operation is not None and any((
                operation["task_id"] != context.task_id, operation["run_id"] != context.run_id,
                operation["arguments_digest"] != arguments_digest, operation["scope_digest"] != digest,
                operation["state"] != "waiting",
            )):
                raise OperationConflict("operation is already bound or has begun")
            if operation is not None:
                self._require_prior_execution_fenced(connection, operation, context)
            leases = self._covering_leases(
                connection, context, now, normalized, operation_id=operation_id
            )
            if leases is None:
                raise PermissionDenied("required scope is not currently granted")
            claim_id = uuid4().hex
            for lease_id in leases:
                connection.execute(
                    "UPDATE permission_leases SET status='reserved',reserved_claim=? "
                    "WHERE lease_id=? AND lifetime='once' AND status='active'",
                    (claim_id, lease_id),
                )
            if operation is None:
                connection.execute(
                    "INSERT INTO pending_operations VALUES(?,?,?,?,?,?,?)",
                    (operation_id, context.task_id, context.run_id, arguments_digest, digest,
                     context.execution_epoch, "prepared"),
                )
            else:
                connection.execute(
                    "UPDATE pending_operations SET state='prepared',execution_epoch=? WHERE operation_id=?",
                    (context.execution_epoch, operation_id),
                )
            connection.execute(
                "INSERT INTO execution_claims VALUES(?,?,?,?,?,?,?,?,?)",
                (claim_id, operation_id, context.task_id, context.execution_epoch, _json(leases),
                 _scope_json(normalized), "prepared", None, now),
            )
            connection.execute("INSERT INTO execution_resources VALUES(?,?)", (claim_id, "registered"))
            self._event(connection, context.task_id, "claim_prepared", claim_id, now)
            return ExecutionClaim(claim_id, operation_id, "prepared")

    @staticmethod
    def _claim(connection: sqlite3.Connection, context: ExecutionContext, claim_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM execution_claims WHERE claim_id=?", (claim_id,)
        ).fetchone()
        if row is None or row["task_id"] != context.task_id or row["execution_epoch"] != context.execution_epoch:
            raise PermissionDenied("claim binding mismatch")
        return row

    def cancel_prepared(self, context: ExecutionContext, claim_id: str) -> bool:
        """Fence a prepared claim; only verified cleanup can release its reservation."""
        _nonempty(claim_id, "claim_id")
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context, active=False)
            claim = self._claim(connection, context, claim_id)
            if claim["state"] == "cancelled":
                return True
            if claim["state"] != "prepared":
                return False
            operation = connection.execute(
                "SELECT state FROM pending_operations WHERE operation_id=?", (claim["operation_id"],)
            ).fetchone()
            if operation is None or operation["state"] != "prepared":
                raise OperationConflict("prepared claim and operation disagree")
            connection.execute("UPDATE execution_claims SET state='cancelled' WHERE claim_id=?", (claim_id,))
            connection.execute(
                "UPDATE execution_resources SET state='stopping' WHERE claim_id=? AND state!='stopped'",
                (claim_id,),
            )
            self._event(connection, context.task_id, "claim_cancelled", claim_id, now)
            return True

    def start(self, context: ExecutionContext, claim_id: str) -> ExecutionClaim:
        _nonempty(claim_id, "claim_id")
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context)
            claim = self._claim(connection, context, claim_id)
            if claim["state"] == "cancelled":
                raise PermissionDenied("claim was cancelled before start")
            if claim["state"] != "prepared":
                raise OperationConflict("claim is no longer prepared")
            operation = connection.execute(
                "SELECT * FROM pending_operations WHERE operation_id=?", (claim["operation_id"],)
            ).fetchone()
            if operation is None or any((
                operation["task_id"] != context.task_id, operation["run_id"] != context.run_id,
                operation["execution_epoch"] != context.execution_epoch, operation["state"] != "prepared",
                operation["scope_digest"] != scope_digest(_load_scopes(claim["scopes_json"])),
            )):
                raise OperationConflict("prepared claim and operation disagree")
            original = set(json.loads(claim["lease_ids_json"]))
            valid = self._covering_leases(
                connection, context, now, _load_scopes(claim["scopes_json"]),
                operation_id=claim["operation_id"], claim_id=claim_id,
            )
            if valid is None or set(valid) != original:
                raise PermissionDenied("permission changed before start")
            connection.execute(
                "UPDATE execution_claims SET state='started' WHERE claim_id=?", (claim_id,)
            )
            connection.execute(
                "UPDATE pending_operations SET state='started' WHERE operation_id=?", (claim["operation_id"],)
            )
            connection.execute(
                "UPDATE execution_resources SET state='running' WHERE claim_id=?", (claim_id,)
            )
            connection.execute(
                "UPDATE permission_leases SET status='consumed' WHERE reserved_claim=? AND status='reserved'",
                (claim_id,),
            )
            self._event(connection, context.task_id, "claim_started", claim_id, now)
            return ExecutionClaim(claim_id, claim["operation_id"], "started")

    def dispatch_guarded(
        self, context: ExecutionContext, claim_id: str, *, operation_id: str,
        arguments_digest: str, scopes: Iterable[Scope], effect: Callable[[], object],
    ) -> object:
        """Run one short, trusted local effect under the authority write barrier.

        The caller must already hold the actual resource and bind it to these
        exact facts. The callback is trusted service code, never model input.
        It must not call the authority, wait for a user, perform network I/O
        or start a long-running operation. A blocked local call can delay
        revocation. This is not an OS sandbox or an L2 service. A callback
        failure leaves the started operation uncertain and never retries it.
        """
        _nonempty(claim_id, "claim_id")
        _nonempty(operation_id, "operation_id")
        _digest(arguments_digest, "arguments_digest")
        deferred = (inspect.iscoroutinefunction, inspect.isgeneratorfunction, inspect.isasyncgenfunction)
        if (not callable(effect)
                or any(check(effect) or check(getattr(effect, "__call__", None)) for check in deferred)):
            raise ValueError("a synchronous trusted effect is required")
        normalized = canonical_scope_set(scopes)
        required_digest = scope_digest(normalized)

        def bound_claim(connection: sqlite3.Connection, state: str) -> sqlite3.Row:
            self._context(connection, context)
            claim = self._claim(connection, context, claim_id)
            operation = connection.execute(
                "SELECT * FROM pending_operations WHERE operation_id=?", (claim["operation_id"],)
            ).fetchone()
            if operation is None or any((
                claim["operation_id"] != operation_id,
                operation["task_id"] != context.task_id,
                operation["run_id"] != context.run_id,
                operation["execution_epoch"] != context.execution_epoch,
                operation["arguments_digest"] != arguments_digest,
                operation["scope_digest"] != required_digest,
                _load_scopes(claim["scopes_json"]) != normalized,
            )):
                raise OperationConflict("claim does not bind these operation facts")
            if claim["state"] == "cancelled":
                raise PermissionDenied("claim was cancelled")
            if claim["state"] != state or operation["state"] != state:
                raise OperationConflict("claim is no longer in the expected state")
            return claim

        # Refuse mismatched facts without consuming an otherwise valid claim.
        with self._transaction() as connection:
            self._now(connection)
            bound_claim(connection, "prepared")

        # This durable transition is never repeated. A crash or rejected
        # barrier after it stays uncertain until an independent reconciliation.
        self.start(context, claim_id)

        with self._transaction() as connection:
            now = self._now(connection)
            claim = bound_claim(connection, "started")
            resource = connection.execute(
                "SELECT state FROM execution_resources WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if resource is None or resource["state"] != "running":
                raise PermissionDenied("execution resource is no longer active")
            try:
                lease_ids = json.loads(claim["lease_ids_json"])
            except (TypeError, ValueError) as exc:
                raise PermissionDenied("claim lease bindings are invalid") from exc
            if (not isinstance(lease_ids, list) or not lease_ids
                    or any(not isinstance(item, str) or not item for item in lease_ids)
                    or len(lease_ids) != len(set(lease_ids))):
                raise PermissionDenied("claim lease bindings are invalid")
            granted = []
            for lease_id in lease_ids:
                lease = connection.execute(
                    "SELECT * FROM permission_leases WHERE lease_id=?", (lease_id,)
                ).fetchone()
                if lease is None or any((
                    lease["task_id"] != context.task_id,
                    lease["policy_revision"] != context.policy_revision,
                    lease["expires_at"] <= now,
                    lease["lifetime"] in {"once", "run"} and lease["run_id"] != context.run_id,
                    lease["lifetime"] == "once" and lease["operation_id"] != operation_id,
                    lease["lifetime"] == "once" and (
                        lease["status"] != "consumed" or lease["reserved_claim"] != claim_id
                    ),
                    lease["lifetime"] != "once" and lease["status"] != "active",
                )):
                    raise PermissionDenied("permission changed before dispatch")
                granted.extend(_load_scopes(lease["scopes_json"]))
            for required in normalized:
                for action in required.actions:
                    single = Scope(required.kind, required.resource, (action,), required.normalization_version)
                    if not any(scope_covers(grant, single) for grant in granted):
                        raise PermissionDenied("claim no longer covers the requested action")
            result = effect()
            # A deferred object could run only after the authority lock closes.
            if inspect.iscoroutine(result) or inspect.isgenerator(result):
                result.close()
            if type(result) not in (type(None), bool, int, float, str, bytes):
                raise TypeError("guarded effect must return an immediate scalar")
            if isinstance(result, float) and not math.isfinite(result):
                raise TypeError("guarded effect result must be finite")
            return result

    def finish(
        self,
        context: ExecutionContext,
        claim_id: str,
        *,
        outcome: str,
        result_digest: str | None,
    ) -> None:
        if outcome not in {"succeeded", "failed", "unknown"}:
            raise ValueError("invalid outcome")
        if result_digest is not None:
            _digest(result_digest, "result_digest")
        with self._transaction() as connection:
            now = self._now(connection)
            # Fenced/ended executions may report their existing result, but cannot start.
            self._context(connection, context, active=False)
            claim = self._claim(connection, context, claim_id)
            if claim["state"] in {"succeeded", "failed", "unknown"}:
                if claim["state"] != outcome or claim["result_digest"] != result_digest:
                    raise OperationConflict("conflicting operation receipt")
                return
            if claim["state"] != "started":
                raise OperationConflict("operation has not started")
            connection.execute(
                "UPDATE execution_claims SET state=?,result_digest=? WHERE claim_id=?",
                (outcome, result_digest, claim_id),
            )
            connection.execute(
                "UPDATE pending_operations SET state=? WHERE operation_id=?", (outcome, claim["operation_id"])
            )
            # A result is not proof that every OS resource has stopped.
            connection.execute("UPDATE execution_resources SET state='unknown' WHERE claim_id=? AND state!='stopped'", (claim_id,))
            self._event(connection, context.task_id, "operation_" + outcome, claim["operation_id"], now)

    def reconcile_outcome(
        self, context: ExecutionContext, claim_id: str, *, receipt_reference: str
    ) -> None:
        """Resolve an unknown operation only with verified supervisor evidence."""
        try:
            _nonempty(claim_id, "claim_id")
            _nonempty(receipt_reference, "operation outcome receipt", limit=16 * 1024)
        except ValueError as exc:
            raise PermissionDenied("invalid operation outcome reference") from exc
        verifier = getattr(self._verifier, "verify_outcome", None)
        receipt = verifier(receipt_reference) if callable(verifier) else None
        if not isinstance(receipt, OperationOutcomeReceipt):
            raise PermissionDenied("trusted operation outcome evidence is required")
        try:
            for key in ("nonce", "claim_id", "operation_id", "instance_id",
                        "task_id", "execution_epoch", "observer_id"):
                _nonempty(getattr(receipt, key), key)
            _digest(receipt.arguments_digest, "arguments_digest")
            _digest(receipt.result_digest, "result_digest")
        except ValueError as exc:
            raise PermissionDenied("invalid operation outcome evidence") from exc
        if (
            not isinstance(receipt.outcome, str)
            or receipt.outcome not in {"succeeded", "failed"}
            or isinstance(receipt.expires_at, bool)
            or not isinstance(receipt.expires_at, (int, float))
            or not math.isfinite(receipt.expires_at)
        ):
            raise PermissionDenied("invalid operation outcome evidence")
        proof_hash = hashlib.sha256(_json(asdict(receipt)).encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context, active=False)
            claim = self._claim(connection, context, claim_id)
            operation = connection.execute(
                "SELECT * FROM pending_operations WHERE operation_id=?", (claim["operation_id"],)
            ).fetchone()
            if operation is None or any((
                receipt.claim_id != claim_id, receipt.operation_id != claim["operation_id"],
                receipt.arguments_digest != operation["arguments_digest"],
                receipt.instance_id != context.instance_id, receipt.task_id != context.task_id,
                receipt.execution_epoch != context.execution_epoch,
                receipt.observer_id != context.executor_id,
            )):
                raise PermissionDenied("operation outcome evidence does not match the claim")
            prior = connection.execute(
                "SELECT claim_id,proof_hash FROM operation_outcome_receipts WHERE nonce=?",
                (receipt.nonce,),
            ).fetchone()
            if prior is not None:
                if prior["claim_id"] != claim_id or prior["proof_hash"] != proof_hash:
                    raise PermissionDenied("operation outcome receipt replayed")
                return
            if receipt.expires_at <= now:
                raise PermissionDenied("operation outcome evidence expired")
            if claim["state"] != "unknown" or operation["state"] != "unknown":
                raise OperationConflict("operation outcome is not awaiting reconciliation")
            connection.execute(
                "INSERT INTO operation_outcome_receipts VALUES(?,?,?,?)",
                (receipt.nonce, claim_id, proof_hash, now),
            )
            connection.execute(
                "UPDATE execution_claims SET state=?,result_digest=? WHERE claim_id=?",
                (receipt.outcome, receipt.result_digest, claim_id),
            )
            connection.execute(
                "UPDATE pending_operations SET state=? WHERE operation_id=?",
                (receipt.outcome, claim["operation_id"]),
            )
            # An outcome receipt is not evidence that all OS resources stopped.
            self._event(connection, context.task_id, "operation_reconciled", claim["operation_id"], now)

    def revoke(self, context: ExecutionContext, lease_id: str) -> None:
        _nonempty(lease_id, "lease_id")
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context)
            lease = connection.execute(
                "SELECT * FROM permission_leases WHERE lease_id=? AND task_id=?",
                (lease_id, context.task_id),
            ).fetchone()
            if lease is None:
                raise PermissionDenied("lease not found for this task")
            if lease["status"] != "revoked":
                connection.execute(
                    "UPDATE permission_leases SET status='revoked' WHERE lease_id=?", (lease_id,)
                )
                for claim in connection.execute(
                    "SELECT claim_id,lease_ids_json,state FROM execution_claims "
                    "WHERE task_id=? AND state IN ('prepared','started')", (context.task_id,)
                ).fetchall():
                    if lease_id in json.loads(claim["lease_ids_json"]):
                        connection.execute(
                            "UPDATE execution_resources SET state='stopping' WHERE claim_id=? AND state!='stopped'",
                            (claim["claim_id"],),
                        )
                        if claim["state"] == "prepared":
                            connection.execute(
                                "UPDATE execution_claims SET state='cancelled' WHERE claim_id=?",
                                (claim["claim_id"],),
                            )
                self._event(connection, context.task_id, "lease_revoked", lease_id, now)

    def end_task(self, context: ExecutionContext, *, outcome: str) -> None:
        if outcome not in {"completed", "cancelled", "failed_terminal"}:
            raise ValueError("invalid task outcome")
        with self._transaction() as connection:
            now = self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            if task["state"] not in {"active", "paused"} or binding["state"] != "active":
                raise PermissionDenied("task cannot be ended from this execution")
            connection.execute(
                "UPDATE permission_tasks SET state=?,revision=revision+1,ended_at=? WHERE task_id=?",
                (outcome, now, context.task_id),
            )
            connection.execute(
                "UPDATE execution_bindings SET state='fenced' WHERE task_id=?", (context.task_id,)
            )
            connection.execute(
                "UPDATE permission_leases SET status='revoked' WHERE task_id=? AND status!='revoked'",
                (context.task_id,),
            )
            connection.execute(
                "UPDATE access_requests SET state='withdrawn' WHERE task_id=? AND state='pending'",
                (context.task_id,),
            )
            connection.execute(
                "UPDATE execution_resources SET state='stopping' WHERE claim_id IN "
                "(SELECT claim_id FROM execution_claims WHERE task_id=? AND state='prepared') "
                "AND state!='stopped'", (context.task_id,),
            )
            connection.execute(
                "UPDATE execution_claims SET state='cancelled' WHERE task_id=? AND state='prepared'",
                (context.task_id,),
            )
            connection.execute(
                "UPDATE pending_operations SET state='cancelled' WHERE task_id=? AND state IN ('waiting','prepared')",
                (context.task_id,),
            )
            connection.execute(
                "UPDATE execution_resources SET state='stopping' WHERE claim_id IN "
                "(SELECT claim_id FROM execution_claims WHERE task_id=? AND state='started') "
                "AND state!='stopped'",
                (context.task_id,),
            )
            self._event(connection, context.task_id, "task_" + outcome, context.task_id, now)

    def confirm_resource_stopped(
        self, context: ExecutionContext, claim_id: str, *, receipt_reference: str
    ) -> None:
        """Record cleanup only on verified, claim-bound supervisor evidence."""
        verifier = getattr(self._verifier, "verify_stop", None)
        receipt = verifier(receipt_reference) if callable(verifier) else None
        if not isinstance(receipt, ResourceStopReceipt):
            raise PermissionDenied("trusted resource-stop evidence is required")
        try:
            for key in ("nonce", "claim_id", "instance_id", "task_id", "execution_epoch", "observer_id"):
                _nonempty(getattr(receipt, key), key)
            _digest(receipt.evidence_digest, "evidence_digest")
        except ValueError as exc:
            raise PermissionDenied("invalid resource-stop evidence") from exc
        if (
            isinstance(receipt.expires_at, bool)
            or not isinstance(receipt.expires_at, (int, float))
            or not math.isfinite(receipt.expires_at)
        ):
            raise PermissionDenied("invalid resource-stop evidence")
        import hashlib
        proof_hash = hashlib.sha256(_json(asdict(receipt)).encode("utf-8")).hexdigest()
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context, active=False)
            claim = self._claim(connection, context, claim_id)
            if any((
                receipt.claim_id != claim_id, receipt.instance_id != context.instance_id,
                receipt.task_id != context.task_id, receipt.execution_epoch != context.execution_epoch,
                receipt.observer_id != context.executor_id,
            )):
                raise PermissionDenied("resource-stop evidence does not match the claim")
            prior = connection.execute(
                "SELECT * FROM resource_stop_receipts WHERE nonce=?", (receipt.nonce,)
            ).fetchone()
            if prior is not None:
                if prior["claim_id"] != claim_id or prior["proof_hash"] != proof_hash:
                    raise PermissionDenied("resource-stop receipt replayed")
                return
            if receipt.expires_at <= now:
                raise PermissionDenied("resource-stop evidence expired")
            resource = connection.execute(
                "SELECT state FROM execution_resources WHERE claim_id=?", (claim_id,)
            ).fetchone()
            if resource is None:
                raise PermissionDenied("execution resource is unavailable")
            connection.execute(
                "INSERT INTO resource_stop_receipts VALUES(?,?,?,?)",
                (receipt.nonce, claim_id, proof_hash, now),
            )
            if resource["state"] == "stopped":
                # Record the distinct nonce, but never reapply old cleanup to a
                # later claim of the same operation.
                return
            connection.execute(
                "UPDATE execution_resources SET state='stopped' WHERE claim_id=?", (claim_id,)
            )
            if claim["state"] == "cancelled":
                # Only a never-started prepared claim can be retried; the
                # receipt has proved its associated prepared resource stopped.
                connection.execute(
                    "UPDATE pending_operations SET state='waiting' "
                    "WHERE operation_id=? AND state='prepared' AND NOT EXISTS "
                    "(SELECT 1 FROM execution_claims c WHERE c.operation_id=? "
                    "AND c.claim_id!=? AND c.state IN ('prepared','started'))",
                    (claim["operation_id"], claim["operation_id"], claim_id),
                )
                connection.execute(
                    "UPDATE permission_leases SET status='active',reserved_claim=NULL "
                    "WHERE reserved_claim=? AND status='reserved'", (claim_id,),
                )
            self._event(connection, context.task_id, "resource_stopped", claim_id, now)

    def resource_state(self, claim_id: str) -> str | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM execution_resources WHERE claim_id=?", (claim_id,)
            ).fetchone()
            return None if row is None else row["state"]

    def pending_events(
        self, context: ExecutionContext, *, consumer_id: str, limit: int = 100
    ) -> tuple[PermissionEvent, ...]:
        """Read an at-least-once event batch without changing its cursor."""
        _nonempty(consumer_id, "consumer_id")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("event limit must be between 1 and 500")
        with self._transaction() as connection:
            self._context(connection, context, active=False)
            cursor = connection.execute(
                "SELECT sequence FROM outbox_cursors WHERE task_id=? AND consumer_id=?",
                (context.task_id, consumer_id),
            ).fetchone()
            after = cursor["sequence"] if cursor is not None else 0
            rows = connection.execute(
                "SELECT * FROM permission_events_outbox WHERE task_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?", (context.task_id, after, limit),
            ).fetchall()
            return tuple(PermissionEvent(
                row["sequence"], row["task_id"], row["event"], row["reference_id"], row["created_at"]
            ) for row in rows)

    def ack_events(
        self, context: ExecutionContext, *, consumer_id: str, expected: int, through: int
    ) -> int:
        """CAS a consumer cursor only after its caller has delivered the batch."""
        _nonempty(consumer_id, "consumer_id")
        if type(expected) is not int or type(through) is not int or expected < 0 or through < expected:
            raise ValueError("invalid event cursor")
        with self._transaction() as connection:
            self._context(connection, context, active=False)
            row = connection.execute(
                "SELECT sequence FROM outbox_cursors WHERE task_id=? AND consumer_id=?",
                (context.task_id, consumer_id),
            ).fetchone()
            current = row["sequence"] if row is not None else 0
            if current == through:
                return current
            if current != expected:
                raise RevisionConflict("event cursor changed")
            target = connection.execute(
                "SELECT sequence FROM permission_events_outbox WHERE task_id=? AND sequence=?",
                (context.task_id, through),
            ).fetchone()
            if target is None:
                raise ValueError("event cursor does not identify this task's event")
            connection.execute(
                "INSERT INTO outbox_cursors(task_id,consumer_id,sequence) VALUES(?,?,?) "
                "ON CONFLICT(task_id,consumer_id) DO UPDATE SET sequence=excluded.sequence",
                (context.task_id, consumer_id, through),
            )
            return through

    def get_lease(self, lease_id: str) -> PermissionLease:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM permission_leases WHERE lease_id=?", (lease_id,)).fetchone()
            if row is None:
                raise PermissionDenied("lease not found")
            return _lease_from(row)

    def execution_status(self, context: ExecutionContext) -> ExecutionStatus:
        with self._transaction() as connection:
            self._now(connection)
            task = self._task(connection, context, active=False)
            binding = self._binding(connection, context, active=False)
            return ExecutionStatus(task["state"], binding["state"])

    def find_operation_for_task(
        self, context: ExecutionContext, operation_id: str
    ) -> OperationStatus | None:
        """Read an operation for an active execution, including a fenced predecessor."""
        _nonempty(operation_id, "operation_id")
        with self._transaction() as connection:
            self._now(connection)
            self._context(connection, context)
            row = connection.execute(
                "SELECT p.*,(SELECT c.result_digest FROM execution_claims c "
                "WHERE c.operation_id=p.operation_id AND c.state=p.state "
                "ORDER BY c.rowid DESC LIMIT 1) AS result_digest,"
                "(SELECT r.state FROM execution_claims c JOIN execution_resources r ON r.claim_id=c.claim_id "
                "WHERE c.operation_id=p.operation_id AND c.state=p.state "
                "ORDER BY c.rowid DESC LIMIT 1) AS resource_state "
                "FROM pending_operations p WHERE p.operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                return None
            if row["task_id"] != context.task_id or row["run_id"] != context.run_id:
                raise OperationConflict("operation belongs to another task or run")
            self._require_prior_execution_fenced(connection, row, context)
            return OperationStatus(
                row["state"], row["arguments_digest"], row["scope_digest"],
                row["execution_epoch"], row["result_digest"], row["resource_state"],
            )

    def operation_status_for_task(
        self, context: ExecutionContext, operation_id: str
    ) -> OperationStatus:
        _nonempty(operation_id, "operation_id")
        with self._transaction() as connection:
            self._now(connection)
            self._context(connection, context, active=False)
            row = connection.execute(
                "SELECT p.state,p.arguments_digest,p.scope_digest,p.execution_epoch,"
                "(SELECT c.result_digest FROM execution_claims c WHERE c.operation_id=p.operation_id "
                "AND c.state=p.state ORDER BY c.rowid DESC LIMIT 1) AS result_digest,"
                "(SELECT r.state FROM execution_claims c JOIN execution_resources r ON r.claim_id=c.claim_id "
                "WHERE c.operation_id=p.operation_id AND c.state=p.state "
                "ORDER BY c.rowid DESC LIMIT 1) AS resource_state "
                "FROM pending_operations p WHERE p.operation_id=? AND p.task_id=? AND p.run_id=?",
                (operation_id, context.task_id, context.run_id),
            ).fetchone()
            if row is None:
                raise PermissionDenied("operation not found for this task and run")
            return OperationStatus(
                row["state"], row["arguments_digest"], row["scope_digest"],
                row["execution_epoch"], row["result_digest"], row["resource_state"],
            )

    def get_lease_for_task(
        self, context: ExecutionContext, lease_id: str
    ) -> PermissionLease:
        _nonempty(lease_id, "lease_id")
        with self._transaction() as connection:
            now = self._now(connection)
            self._context(connection, context, active=False)
            row = connection.execute(
                "SELECT * FROM permission_leases WHERE lease_id=? AND task_id=?",
                (lease_id, context.task_id),
            ).fetchone()
            if row is None:
                raise PermissionDenied("lease not found for this task")
            lease = _lease_from(row)
            if lease.status in {"active", "reserved"} and lease.expires_at <= now:
                return replace(lease, status="expired")
            return lease

    def operation_state(self, operation_id: str) -> str | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM pending_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            return None if row is None else row["state"]

    def decision_count(self, request_id: str) -> int:
        with self._transaction() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM approval_decisions WHERE request_id=?", (request_id,)
            ).fetchone()[0]
