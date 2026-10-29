"""PAO-owned durable routing and parking; these rows never grant permission.

Optional tables are created only on an enabled managed Run. L0 reads and
startup do not create a permission store. Provider context stays in the existing
private HER store; this table keeps its exact reference and original queue item.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, fields
from hashlib import sha256
import json
import time
from uuid import uuid4

from orchestrator.permissions.models import RuntimeExecutionBinding
from orchestrator.session_store import SessionConflict, SessionStore


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _claims(binding):
    context = binding.context
    return dict(run_id=context.run_id, owner_id=context.owner_id, agent_id=context.agent_id,
                session_id=context.session_id, request_id=binding.request_id,
                context_generation=int(context.context_generation),
                fencing_token=binding.fencing_token, worker_id=binding.worker_id)


class PermissionParkingStore:
    def __init__(self, store: SessionStore):
        if not isinstance(store, SessionStore):
            raise ValueError("the canonical PAO store is required")
        self.store = store

    @staticmethod
    def _ensure(connection):
        # These optional PAO routing tables contain no leases, approvals or
        # policy writers; PermissionAuthority remains the only authorization DB.
        connection.execute("""CREATE TABLE IF NOT EXISTS permission_run_routes(
            run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
            task_id TEXT NOT NULL, execution_binding_json TEXT NOT NULL,
            binding_digest TEXT NOT NULL
        )""")
        connection.execute("""CREATE TABLE IF NOT EXISTS permission_run_parks(
            run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
            producer_fence INTEGER NOT NULL CHECK(producer_fence>0),
            resume_fence INTEGER,
            state TEXT NOT NULL CHECK(state IN ('parked','resuming','finished')),
            payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL,
            resume_token TEXT, updated_at REAL NOT NULL
        )""")

    def record_binding(self, binding: RuntimeExecutionBinding):
        if not isinstance(binding, RuntimeExecutionBinding):
            raise TypeError("a canonical execution binding is required")
        payload = asdict(binding)
        with self.store.hold_active_run(**_claims(binding)) as connection:
            self._ensure(connection)
            row = connection.execute(
                "SELECT * FROM permission_run_routes WHERE run_id=?", (binding.context.run_id,),
            ).fetchone()
            if row is not None:
                old = json.loads(row["execution_binding_json"])
                if (row["binding_digest"] != _digest(old) or row["task_id"] != binding.context.task_id
                        or old["context"]["owner_id"] != binding.context.owner_id
                        or old["context"]["session_id"] != binding.context.session_id):
                    raise SessionConflict("permission Run route changed")
            connection.execute(
                "INSERT INTO permission_run_routes VALUES(?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET execution_binding_json=excluded.execution_binding_json,"
                "binding_digest=excluded.binding_digest",
                (binding.context.run_id, binding.context.task_id, _json(payload), _digest(payload)),
            )

    def route(self, run_id, *, owner_id):
        run = self.store.get_run(run_id, owner_id=owner_id)
        with self.store._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='permission_run_routes' AND type='table'",
            ).fetchone() is None:
                return None
            row = connection.execute(
                "SELECT * FROM permission_run_routes WHERE run_id=?", (run_id,),
            ).fetchone()
            if row is None:
                return None
            value = json.loads(row["execution_binding_json"])
            if row["binding_digest"] != _digest(value) or value["context"]["run_id"] != run["run_id"]:
                raise SessionConflict("permission route integrity check failed")
            return value

    def park(self, binding, item, *, checkpoint_reference, request_id, operation_id, state):
        from orchestrator.runtime_common import QueuedRequest

        if not isinstance(binding, RuntimeExecutionBinding) or not isinstance(item, QueuedRequest):
            raise ValueError("canonical binding and queue item are required")
        if not isinstance(checkpoint_reference, dict) or not checkpoint_reference.get("digest"):
            raise ValueError("an exact durable provider checkpoint reference is required")
        if state not in {"waiting", "paused", "recovering", "unknown", "fenced", "permission_changed", "unavailable"}:
            raise ValueError("this control state cannot be parked")
        context = binding.context
        if (item.run_id != context.run_id or item.request_id != binding.request_id
                or item.session_id != context.session_id or item.owner_id != context.owner_id):
            raise SessionConflict("queue item does not own the permission Run")
        saved = asdict(item)
        saved.pop("private_authorization_evidence", None)
        saved.pop("queued_monotonic", None)
        payload = {
            "format": "hashi-permission-park-v1", "binding": asdict(binding),
            "item": saved, "checkpoint": checkpoint_reference,
            "request_id": request_id, "operation_id": operation_id, "reason": state,
        }
        encoded, digest = _json(payload), _digest(payload)
        if len(encoded.encode("utf-8")) > 16 * 1024 * 1024:
            raise ValueError("parked continuation exceeds the private storage limit")
        with self.store.hold_active_run(**_claims(binding)) as connection:
            self._ensure(connection)
            previous = connection.execute(
                "SELECT * FROM permission_run_parks WHERE run_id=?", (context.run_id,),
            ).fetchone()
            if (previous is not None and previous["state"] == "parked"
                    and (previous["producer_fence"] != binding.fencing_token or previous["payload_digest"] != digest)):
                raise SessionConflict("parked continuation already binds different facts")
            connection.execute(
                "INSERT INTO permission_run_parks VALUES(?,?,NULL,'parked',?,?,NULL,?) "
                "ON CONFLICT(run_id) DO UPDATE SET producer_fence=excluded.producer_fence,resume_fence=NULL,"
                "state='parked',payload_json=excluded.payload_json,payload_digest=excluded.payload_digest,"
                "resume_token=NULL,updated_at=excluded.updated_at",
                (context.run_id, binding.fencing_token, encoded, digest, time.time()),
            )
            self.store._append_event(
                connection, session_id=context.session_id, run_id=context.run_id,
                kind="run.progress", status="running", phase="waiting_permission",
                summary="Waiting for permission", detail={"request_id": request_id},
            )
        return digest

    @staticmethod
    def _payload(row):
        try:
            value = json.loads(row["payload_json"])
            if value.get("format") != "hashi-permission-park-v1" or _digest(value) != row["payload_digest"]:
                raise ValueError("park digest changed")
            return value
        except (TypeError, ValueError, KeyError) as exc:
            raise SessionConflict("parked continuation is unavailable or damaged") from exc

    @classmethod
    def recoverable(cls, connection, run):
        row = connection.execute(
            "SELECT * FROM permission_run_parks WHERE run_id=?", (run["run_id"],),
        ).fetchone()
        if row is None or row["state"] == "finished":
            return False
        payload = cls._payload(row)
        context = payload["binding"]["context"]
        expected_fence = row["producer_fence"] if row["state"] == "parked" else row["resume_fence"]
        session = connection.execute(
            "SELECT * FROM sessions WHERE session_id=?", (run["session_id"],),
        ).fetchone()
        return bool(
            expected_fence == run["fencing_token"] and session is not None
            and session["status"] == "active"
            and session["context_generation"] == run["context_generation"]
            and context["session_id"] == run["session_id"] and context["run_id"] == run["run_id"]
            and context["owner_id"] == session["owner_id"] and context["agent_id"] == run["agent_id"]
            and int(context["context_generation"]) == run["context_generation"]
            and run["state"] in {"running", "queued"}
        )

    def list_parked(self, *, agent_id):
        with self.store._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='permission_run_parks' AND type='table'",
            ).fetchone() is None:
                return ()
            rows = connection.execute(
                "SELECT r.* FROM permission_run_parks p JOIN runs r ON r.run_id=p.run_id "
                "WHERE r.agent_id=? AND p.state IN ('parked','resuming') AND r.state IN ('running','queued')",
                (agent_id,),
            ).fetchall()
            return tuple(self._payload(connection.execute(
                "SELECT * FROM permission_run_parks WHERE run_id=?", (row["run_id"],),
            ).fetchone()) for row in rows if self.recoverable(connection, row))

    def claim_resume(self, binding):
        from orchestrator.runtime_common import QueuedRequest

        with self.store.hold_active_run(**_claims(binding)) as connection:
            row = connection.execute(
                "SELECT * FROM permission_run_parks WHERE run_id=?", (binding.context.run_id,),
            ).fetchone()
            if row is None or row["state"] != "parked" or row["producer_fence"] != binding.fencing_token:
                raise SessionConflict("parked Run was already claimed or changed")
            payload = self._payload(row)
            if payload["binding"] != asdict(binding):
                raise SessionConflict("parked Run binding changed")
            saved = payload["item"]
            accepted = {field.name for field in fields(QueuedRequest)}
            if set(saved) - accepted:
                raise SessionConflict("parked queue item fields changed")
            try:
                item = QueuedRequest(**{**saved, "fencing_token": None, "queued_monotonic": time.monotonic()})
            except (TypeError, ValueError) as exc:
                raise SessionConflict("parked queue item is invalid") from exc
            if (item.run_id != binding.context.run_id or item.request_id != binding.request_id
                    or item.session_id != binding.context.session_id or item.owner_id != binding.context.owner_id):
                raise SessionConflict("parked queue item binding changed")
            token, next_fence = uuid4().hex, binding.fencing_token + 1
            connection.execute(
                "UPDATE permission_run_parks SET state='resuming',resume_token=?,resume_fence=?,updated_at=? "
                "WHERE run_id=?",
                (token, next_fence, time.time(), binding.context.run_id),
            )
            connection.execute(
                "UPDATE run_attempts SET state='interrupted',finished_at=COALESCE(finished_at,?) "
                "WHERE run_id=? AND fencing_token=? AND state='running'",
                (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), binding.context.run_id, binding.fencing_token),
            )
            connection.execute(
                "UPDATE runs SET state='queued',fencing_token=?,worker_id=NULL WHERE run_id=?",
                (next_fence, binding.context.run_id),
            )
        return item, token

    @contextmanager
    def hold_resume(self, binding, token):
        with self.store.hold_active_run(**_claims(binding)) as connection:
            row = connection.execute(
                "SELECT * FROM permission_run_parks WHERE run_id=?", (binding.context.run_id,),
            ).fetchone()
            if (row is None or row["state"] != "resuming" or row["resume_token"] != token
                    or binding.fencing_token <= row["producer_fence"]
                    or binding.context.run_id != self._payload(row)["binding"]["context"]["run_id"]
                    or binding.context.task_id != self._payload(row)["binding"]["context"]["task_id"]
                    or binding.context.context_generation != self._payload(row)["binding"]["context"]["context_generation"]):
                raise SessionConflict("permission continuation was superseded")
            payload = self._payload(row)
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (binding.context.run_id,)).fetchone()
            payload["_current_run_claim"] = {
                "run_id": run["run_id"], "request_id": run["request_id"], "attempt": run["attempt"],
                "attempt_id": f"{run['run_id']}:attempt:{run['attempt']}",
                "state_revision": run["fencing_token"], "input_digest": run["request_digest"],
                "state": run["state"], "parent_run_id": run["parent_run_id"],
            }
            yield payload

    def resume_ticket(self, binding):
        with self.store.hold_active_run(**_claims(binding)) as connection:
            row = connection.execute(
                "SELECT * FROM permission_run_parks WHERE run_id=?", (binding.context.run_id,),
            ).fetchone()
            if row is None or row["state"] != "resuming" or not row["resume_token"]:
                raise SessionConflict("no current permission continuation exists")
            return PermissionResumeTicket(self, binding, row["resume_token"])

    def finish(self, binding):
        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute("SELECT fencing_token FROM runs WHERE run_id=?", (binding.context.run_id,)).fetchone()
            if run is None or run["fencing_token"] != binding.fencing_token:
                return False
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='permission_run_parks' AND type='table'",
            ).fetchone() is not None:
                connection.execute(
                    "UPDATE permission_run_parks SET state='finished',updated_at=? WHERE run_id=?",
                    (time.time(), binding.context.run_id),
                )


class PermissionResumeTicket:
    """Private PAO continuation reference; never put it in public request metadata."""

    __slots__ = ("_parking", "binding", "_token")

    def __init__(self, parking, binding, token):
        if not isinstance(parking, PermissionParkingStore) or not isinstance(binding, RuntimeExecutionBinding):
            raise ValueError("a canonical PAO continuation is required")
        self._parking, self.binding, self._token = parking, binding, token

    def hold(self):
        return self._parking.hold_resume(self.binding, self._token)

    def __repr__(self):
        return "PermissionResumeTicket(<private canonical continuation>)"
