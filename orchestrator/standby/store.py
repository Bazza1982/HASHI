"""SessionStore-owned control extension. Never stores environmental content.

Only SessionStore opens connections. Control receipts contain IDs and state,
not request bodies, prompts, media, tools, models or a second chat history.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from .contract import (
    CONTROL_RETENTION_SECONDS, StandbyError, binding, identifier, integer,
    keys, operation_time, revision,
)

_COMMON = ("operation", "operation_id", "issued_at", "binding")
_MUTATE = ("standby_id", "expected_revision", "epoch")
MAX_OPERATION_RECEIPTS = 4096


def install_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS standby_controls (
        standby_id TEXT PRIMARY KEY,
        instance_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        session_id TEXT NOT NULL, context_generation INTEGER NOT NULL,
        profile_id TEXT NOT NULL, profile_revision TEXT NOT NULL,
        revision INTEGER NOT NULL, epoch INTEGER NOT NULL,
        lifecycle TEXT NOT NULL, mode TEXT NOT NULL, disposal TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
    )""")
    connection.execute("""CREATE INDEX IF NOT EXISTS standby_control_scope
        ON standby_controls(instance_id,owner_id,agent_id,session_id,updated_at)""")
    connection.execute("""CREATE TABLE IF NOT EXISTS standby_control_operations (
        instance_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        operation_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        result_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(instance_id,owner_id,agent_id,operation_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS standby_safety_operations (
        instance_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        standby_id TEXT NOT NULL, operation_id TEXT NOT NULL,
        request_digest TEXT NOT NULL, result_json TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(instance_id,owner_id,agent_id,standby_id),
        UNIQUE(instance_id,owner_id,agent_id,operation_id),
        FOREIGN KEY(standby_id) REFERENCES standby_controls(standby_id) ON DELETE CASCADE
    )""")


def _where(instance, owner, scope):
    return (instance, str(owner), scope["agent_id"], scope["session_id"], scope["context_generation"])


def _view(row):
    result = {key: row[key] for key in (
        "standby_id", "agent_id", "session_id", "context_generation", "profile_id",
        "profile_revision", "revision", "epoch", "lifecycle", "mode", "disposal",
        "created_at", "updated_at",
    )}
    result["capture_started"] = False
    if result["disposal"] == "PURGED_LOCAL":
        result["cleanup"] = {"control_only": True, "external_copies": "not_created"}
    return result


def snapshot(connection, *, instance_id, owner_id, binding_value, standby_id):
    scope = binding(binding_value)
    row = connection.execute("""SELECT * FROM standby_controls
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND session_id=?
        AND context_generation=? AND standby_id=?""",
        (*_where(instance_id, owner_id, scope), identifier(standby_id))).fetchone()
    if row is None:
        raise StandbyError("standby_scope_changed", 404)
    return _view(row)


def recent(connection, *, instance_id, owner_id, binding_value, now):
    scope = binding(binding_value)
    rows = connection.execute("""SELECT * FROM standby_controls
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND session_id=?
        AND context_generation=? AND disposal!='PURGED_LOCAL' AND updated_at>?
        ORDER BY updated_at DESC,standby_id LIMIT 32""",
        (*_where(instance_id, owner_id, scope), now - CONTROL_RETENTION_SECONDS)).fetchall()
    return [_view(row) for row in rows]


def control(connection, *, instance_id, owner_id, body, now):
    if not isinstance(body, dict):
        raise StandbyError("standby_request_invalid")
    operation = body.get("operation")
    if not isinstance(operation, str):
        raise StandbyError("standby_request_invalid")
    if operation == "configure":
        keys(body, required=(*_COMMON, "profile_id", "profile_revision"))
    elif operation in {"pause", "end", "delete_data"}:
        keys(body, required=(*_COMMON, *_MUTATE))
    else:
        raise StandbyError("standby_request_invalid")
    scope = binding(body["binding"])
    operation_id = identifier(body["operation_id"])
    issued = operation_time(body["issued_at"])
    identity = (instance_id, str(owner_id), scope["agent_id"], operation_id)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    previous = connection.execute("""SELECT request_digest,result_json FROM standby_control_operations
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND operation_id=?
        UNION ALL SELECT request_digest,result_json FROM standby_safety_operations
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND operation_id=?
        LIMIT 1""", (*identity,*identity)).fetchone()
    if previous:
        if previous["request_digest"] != digest:
            raise StandbyError("standby_operation_conflict", 409)
        return json.loads(previous["result_json"])
    if now - issued > 300 or issued - now > 30:
        raise StandbyError("standby_operation_expired", 409)
    # Old request IDs cannot run anew after their bounded receipt window.
    connection.execute("DELETE FROM standby_control_operations WHERE created_at<?",
                       (now - CONTROL_RETENTION_SECONDS,))
    connection.execute("DELETE FROM standby_safety_operations WHERE created_at<?",
                       (now - CONTROL_RETENTION_SECONDS,))
    if operation == "configure":
        profile_id = identifier(body["profile_id"], maximum=64)
        profile_revision = revision(body["profile_revision"])
        session = connection.execute("""SELECT session_kind,status,context_generation FROM sessions
            WHERE session_id=? AND owner_id=? AND agent_id=?""",
            (scope["session_id"], str(owner_id), scope["agent_id"])).fetchone()
        if (session is None or session["session_kind"] != "conversation"
                or session["status"] != "active"
                or session["context_generation"] != scope["context_generation"]):
            raise StandbyError("standby_scope_changed", 409)
        connection.execute("""DELETE FROM standby_controls WHERE updated_at<?
            AND lifecycle IN (\'STOPPED\',\'EXPIRED\')""", (now - CONTROL_RETENTION_SECONDS,))
        count = connection.execute("""SELECT count(*) FROM standby_controls
            WHERE instance_id=? AND owner_id=? AND agent_id=?""",
            (instance_id, str(owner_id), scope["agent_id"])).fetchone()[0]
        if count >= 256:
            raise StandbyError("standby_capacity_reached", 429)
        standby_id = "sb-" + uuid.uuid4().hex
        connection.execute("""INSERT INTO standby_controls VALUES
            (?,?,?,?,?,?,?,?,1,1,'CONFIGURED','SHADOW','LIVE',?,?)""",
            (standby_id, instance_id, str(owner_id), scope["agent_id"], scope["session_id"],
             scope["context_generation"], profile_id, profile_revision, now, now))
    else:
        standby_id = identifier(body["standby_id"])
        current = snapshot(connection, instance_id=instance_id, owner_id=owner_id,
                           binding_value=scope, standby_id=standby_id)
        expected = integer(body["expected_revision"], 1, 2**31 - 1)
        epoch = integer(body["epoch"], 1, 2**31 - 1)
        if current["revision"] != expected or current["epoch"] != epoch:
            raise StandbyError("standby_revision_conflict", 409)
        # Even control-only rows advance the durable fence before any future
        # asynchronous cleanup. Nothing here calls a model or cancels other Runs.
        lifecycle = "PAUSED" if operation == "pause" else "STOPPED"
        if operation == "pause" and current["lifecycle"] in {"STOPPED", "EXPIRED"}:
            raise StandbyError("standby_session_ended", 409)
        disposal = "PURGED_LOCAL" if operation == "delete_data" else current["disposal"]
        if current["disposal"] == "PURGED_LOCAL":
            lifecycle = "STOPPED"
        connection.execute("""UPDATE standby_controls SET revision=revision+1,
            epoch=epoch+1,lifecycle=?,disposal=?,updated_at=? WHERE standby_id=?""",
            (lifecycle, disposal, now, standby_id))
        from orchestrator.frontend_capture import revoke_standby
        revoke_standby(connection, instance_id=instance_id, owner_id=owner_id,
            binding_value=scope, standby_id=standby_id, now=now)
    result = snapshot(connection, instance_id=instance_id, owner_id=owner_id,
                      binding_value=scope, standby_id=standby_id)
    count = connection.execute("""SELECT count(*) FROM standby_control_operations
        WHERE instance_id=? AND owner_id=? AND agent_id=?""", identity[:3]).fetchone()[0]
    serialized = json.dumps(result, separators=(",", ":"), allow_nan=False)
    if count >= MAX_OPERATION_RECEIPTS:
        if operation == "configure":
            raise StandbyError("standby_capacity_reached", 429)
        # One emergency receipt per existing handle. Admission cannot exhaust
        # stopping capacity; superseded emergency IDs remain fenced by CAS.
        connection.execute("""INSERT INTO standby_safety_operations VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(instance_id,owner_id,agent_id,standby_id) DO UPDATE SET
            operation_id=excluded.operation_id,request_digest=excluded.request_digest,
            result_json=excluded.result_json,created_at=excluded.created_at""",
            (*identity[:3],standby_id,operation_id,digest,serialized,now))
    else:
        connection.execute("INSERT INTO standby_control_operations VALUES (?,?,?,?,?,?,?)",
            (*identity, digest, serialized, now))
    return result


def operation_status(connection, *, instance_id, owner_id, agent_id, operation_id):
    identity = (instance_id, str(owner_id), identifier(agent_id, maximum=64), identifier(operation_id))
    row = connection.execute("""SELECT result_json FROM standby_control_operations
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND operation_id=?
        UNION ALL SELECT result_json FROM standby_safety_operations
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND operation_id=?
        LIMIT 1""", (*identity,*identity)).fetchone()
    return {"state": "committed", "session": json.loads(row[0])} if row else {"state": "not_found"}
