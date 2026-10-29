"""PAO-owned waiting before a managed executor/Provider boundary exists.

This routing record grants nothing and invents no PermissionTask or epoch. The
original accepted Run/queue item is re-adopted only through its canonical fence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, fields
import json
import time
from uuid import uuid4

from orchestrator.permissions.parking import PermissionParkingStore, _digest, _json
from orchestrator.runtime_common import QueuedRequest
from orchestrator.session_store import SessionConflict


class PermissionStartWait(BaseException):
    def __init__(self, request_id):
        self.request_id = request_id
        super().__init__("waiting for the installed permission service")


class PermissionStartStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _ensure(connection):
        PermissionParkingStore._ensure(connection)
        connection.execute("""CREATE TABLE IF NOT EXISTS permission_start_waits(
            run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
            producer_fence INTEGER NOT NULL, resume_fence INTEGER,
            state TEXT NOT NULL CHECK(state IN ('parked','resuming','finished')),
            payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
            resume_token TEXT, updated_at REAL NOT NULL
        )""")

    @staticmethod
    def _exists(connection):
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='permission_start_waits'",
        ).fetchone() is not None

    @staticmethod
    def _payload(row):
        value = json.loads(row["payload_json"])
        if value.get("format") != "hashi-permission-start-wait-v1" or _digest(value) != row["payload_digest"]:
            raise SessionConflict("unstarted permission continuation changed")
        return value

    def park(self, claim, item, reference):
        if (not isinstance(item, QueuedRequest) or set(reference) != {
                "her_session_id", "turn_id", "execution_owner_id",
        } or any(not isinstance(value, str) or not value for value in reference.values())):
            raise ValueError("the real accepted HER Turn and PAO queue item are required")
        if any(getattr(item, name) != claim[name] for name in ("run_id", "request_id", "session_id", "owner_id")):
            raise SessionConflict("the waiting item belongs to another Run")
        saved = asdict(item)
        saved.pop("private_authorization_evidence", None)
        saved.pop("queued_monotonic", None)
        payload = {"format": "hashi-permission-start-wait-v1", "claim": dict(claim),
                   "item": saved, "reference": reference}
        with self.store.hold_active_run(**claim) as connection:
            self._ensure(connection)
            previous = connection.execute("SELECT * FROM permission_start_waits WHERE run_id=?",
                                          (claim["run_id"],)).fetchone()
            if previous is not None and previous["state"] == "parked":
                if self._payload(previous) != payload:
                    raise SessionConflict("another start continuation owns this Run")
                return
            connection.execute(
                "INSERT INTO permission_start_waits VALUES(?,?,NULL,'parked',?,?,NULL,?) "
                "ON CONFLICT(run_id) DO UPDATE SET producer_fence=excluded.producer_fence,"
                "resume_fence=NULL,state='parked',payload_json=excluded.payload_json,"
                "payload_digest=excluded.payload_digest,resume_token=NULL,updated_at=excluded.updated_at",
                (claim["run_id"], claim["fencing_token"], _json(payload), _digest(payload), time.time()),
            )
            self.store._append_event(
                connection, session_id=claim["session_id"], run_id=claim["run_id"],
                kind="run.progress", status="running", phase="waiting_permission_service",
                summary="Waiting for the permission service", detail={},
            )

    @classmethod
    def recoverable(cls, connection, run):
        if not cls._exists(connection):
            return False
        row = connection.execute("SELECT * FROM permission_start_waits WHERE run_id=?", (run["run_id"],)).fetchone()
        if row is None or row["state"] == "finished":
            return False
        payload = cls._payload(row)
        claim = payload["claim"]
        session = connection.execute("SELECT * FROM sessions WHERE session_id=?", (run["session_id"],)).fetchone()
        expected = row["producer_fence"] if row["state"] == "parked" else row["resume_fence"]
        return bool(
            session is not None and session["status"] == "active" and run["state"] in {"running", "queued"}
            and (run["fencing_token"] == expected or (row["state"] == "resuming"
                 and run["state"] == "running" and run["fencing_token"] > expected))
            and run["context_generation"] == session["context_generation"]
            and claim["run_id"] == run["run_id"] and claim["request_id"] == run["request_id"]
            and claim["agent_id"] == run["agent_id"] and claim["owner_id"] == session["owner_id"]
            and claim["session_id"] == run["session_id"]
            and claim["context_generation"] == run["context_generation"]
        )

    def list_waiting(self, agent_id):
        with self.store._connection() as connection:
            if not self._exists(connection):
                return ()
            rows = connection.execute(
                "SELECT r.* FROM permission_start_waits p JOIN runs r ON r.run_id=p.run_id "
                "WHERE r.agent_id=? AND p.state IN ('parked','resuming') AND r.state IN ('running','queued')",
                (agent_id,),
            ).fetchall()
            result = []
            for run in rows:
                if self.recoverable(connection, run):
                    row = connection.execute("SELECT * FROM permission_start_waits WHERE run_id=?",
                                             (run["run_id"],)).fetchone()
                    result.append({**self._payload(row), "_state": row["state"]})
            return tuple(result)

    def claim_resume(self, payload):
        claim = payload["claim"]
        with self.store.hold_active_run(**claim) as connection:
            row = connection.execute("SELECT * FROM permission_start_waits WHERE run_id=?", (claim["run_id"],)).fetchone()
            if row is None or row["state"] != "parked" or self._payload(row) != {
                key: value for key, value in payload.items() if key != "_state"
            }:
                raise SessionConflict("start continuation was already claimed")
            next_fence, token = claim["fencing_token"] + 1, uuid4().hex
            connection.execute(
                "UPDATE permission_start_waits SET state='resuming',resume_fence=?,resume_token=?,updated_at=? "
                "WHERE run_id=?", (next_fence, token, time.time(), claim["run_id"]),
            )
            connection.execute(
                "UPDATE run_attempts SET state='interrupted',finished_at=COALESCE(finished_at,?) "
                "WHERE run_id=? AND fencing_token=? AND state='running'",
                (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), claim["run_id"], claim["fencing_token"]),
            )
            connection.execute("UPDATE runs SET state='queued',fencing_token=?,worker_id=NULL WHERE run_id=?",
                               (next_fence, claim["run_id"]))
        return self._item(payload)

    @staticmethod
    def _item(payload):
        saved = payload["item"]
        if set(saved) - {field.name for field in fields(QueuedRequest)}:
            raise SessionConflict("waiting queue item fields changed")
        return QueuedRequest(**{**saved, "fencing_token": None, "queued_monotonic": time.monotonic()})

    def recover_queued(self, payload):
        with self.store._connection() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (payload["claim"]["run_id"],)).fetchone()
            if run is None or run["state"] != "queued" or not self.recoverable(connection, run):
                raise SessionConflict("queued start continuation changed")
            return self._item(payload)

    def ticket(self, binding):
        return PermissionStartTicket(self, binding)


class PermissionStartTicket:
    def __init__(self, owner, binding):
        self.owner, self.binding = owner, binding

    @contextmanager
    def hold(self):
        from orchestrator.permissions.parking import _claims

        with self.owner.store.hold_active_run(**_claims(self.binding)) as connection:
            if not self.owner._exists(connection):
                raise SessionConflict("no waiting start continuation exists")
            row = connection.execute("SELECT * FROM permission_start_waits WHERE run_id=?",
                                     (self.binding.context.run_id,)).fetchone()
            if (row is None or row["state"] != "resuming" or not row["resume_token"]
                    or row["resume_fence"] > self.binding.fencing_token):
                raise SessionConflict("start continuation was superseded")
            payload = self.owner._payload(row)
            current_claim = _claims(self.binding)
            if any(payload["claim"][key] != current_claim[key] for key in (
                "run_id", "owner_id", "agent_id", "session_id", "request_id", "context_generation",
            )):
                raise SessionConflict("start continuation belongs to another subject")
            if (payload["claim"]["fencing_token"] >= self.binding.fencing_token
                    or payload["reference"]["turn_id"] != self.binding.context.turn_id):
                raise SessionConflict("start continuation Turn changed")
            yield payload

    def finish(self):
        from orchestrator.permissions.parking import _claims

        with self.owner.store.hold_active_run(**_claims(self.binding)) as connection:
            connection.execute(
                "UPDATE permission_start_waits SET state='finished' "
                "WHERE run_id=? AND resume_fence<=? AND producer_fence<? AND state='resuming'",
                (self.binding.context.run_id, self.binding.fencing_token, self.binding.fencing_token),
            )
