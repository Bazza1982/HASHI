"""PAO's shared capture metadata, owned by SessionStore transactions.

This module never opens a database, device, socket, model or media buffer.
A grant is a short permission for a qualified client, not proof that a physical
track has stopped. HTTP callers cannot create a Standby grant.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets

LEASE_SECONDS = 15
RENEW_SECONDS = 5
MAX_CAPTURE_SECONDS = 8 * 3600
ATTEMPT_RETENTION_SECONDS = 120
MAX_RECENT_ATTEMPTS = 256
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,96}\Z")
_SCOPE = ("client_id", "agent_id", "session_id", "context_generation")


class CaptureError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CaptureError("capture_identifier_invalid")
    return value


def integer(value, minimum=1):
    if type(value) is not int or not minimum <= value <= 2**31-1:
        raise CaptureError("capture_number_invalid")
    return value


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise CaptureError("capture_time_invalid")
    return value


def scope(value):
    if not isinstance(value, dict) or set(value) != set(_SCOPE):
        raise CaptureError("capture_scope_invalid")
    return {key:integer(value[key]) if key == "context_generation"
            else identifier(value[key]) for key in _SCOPE}


def install_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS media_capture_leases (
        instance_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        client_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        session_id TEXT NOT NULL, context_generation INTEGER NOT NULL,
        capture_id TEXT NOT NULL, purpose TEXT NOT NULL, lease_token TEXT NOT NULL,
        epoch INTEGER NOT NULL, fence_epoch INTEGER NOT NULL, state TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL, last_seen REAL NOT NULL,
        renew_sequence INTEGER NOT NULL, standby_id TEXT, standby_epoch INTEGER,
        PRIMARY KEY(instance_id,owner_id)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS media_capture_attempts (
        instance_id TEXT NOT NULL, owner_id TEXT NOT NULL, capture_id TEXT NOT NULL,
        request_digest TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(instance_id,owner_id,capture_id)
    )""")


def _row(connection, instance, owner):
    return connection.execute("""SELECT * FROM media_capture_leases
        WHERE instance_id=? AND owner_id=?""", (instance, str(owner))).fetchone()


def _session(connection, owner, binding):
    row = connection.execute("""SELECT session_kind,status,context_generation FROM sessions
        WHERE session_id=? AND owner_id=? AND agent_id=?""",
        (binding["session_id"], str(owner), binding["agent_id"])).fetchone()
    if (row is None or row["session_kind"] != "conversation" or row["status"] != "active"
            or row["context_generation"] != binding["context_generation"]):
        raise CaptureError("capture_scope_changed", 409)


def _standby_current(connection, row):
    if row["purpose"] != "standby":
        return True
    control = connection.execute("""SELECT lifecycle,epoch,disposal FROM standby_controls
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND session_id=?
        AND context_generation=? AND standby_id=?""",
        (row["instance_id"],row["owner_id"],row["agent_id"],row["session_id"],
         row["context_generation"],row["standby_id"])).fetchone()
    return (control is not None and control["lifecycle"] == "ACTIVE"
            and control["epoch"] == row["standby_epoch"] and control["disposal"] == "LIVE")


def _authority_current(connection, row):
    try:
        _session(connection,row["owner_id"],row)
    except CaptureError:
        return False
    return _standby_current(connection,row)


def revoke_standby(connection, *, instance_id, owner_id, binding_value, standby_id, now):
    """Called inside the control owner's transaction, never a local callback."""
    connection.execute("""UPDATE media_capture_leases SET state='REVOKING',
        fence_epoch=fence_epoch+1,last_seen=max(last_seen,?)
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND session_id=?
        AND context_generation=? AND purpose='standby' AND standby_id=? AND state='ACTIVE'""",
        (now,instance_id,str(owner_id),binding_value["agent_id"],binding_value["session_id"],
         binding_value["context_generation"],standby_id))


def _closed(reason="capture_lease_changed"):
    return {"granted": False, "state": "CLOSED", "reason": reason}


def _view(row):
    return {
        "granted": row["state"] == "ACTIVE",
        "capture_id": row["capture_id"], "purpose": row["purpose"],
        "lease_token": row["lease_token"], "epoch": row["epoch"],
        "fence_epoch": row["fence_epoch"], "state": row["state"],
        "expires_at": row["expires_at"], "renew_after_seconds": RENEW_SECONDS,
        "lease_seconds": LEASE_SECONDS, "sequence": row["renew_sequence"],
        "reason": "" if row["state"] == "ACTIVE" else "capture_lease_inactive",
    }


def _pause_standby(connection, row, now):
    if row["purpose"] != "standby" or not row["standby_id"]:
        return
    connection.execute("""UPDATE standby_controls
        SET lifecycle='PAUSED',epoch=epoch+1,revision=revision+1,updated_at=?
        WHERE instance_id=? AND owner_id=? AND agent_id=? AND session_id=?
        AND context_generation=? AND standby_id=? AND lifecycle='ACTIVE'""",
        (now,row["instance_id"],row["owner_id"],row["agent_id"],row["session_id"],
         row["context_generation"],row["standby_id"]))


def _state(connection, row, state, now):
    if row["state"] in ("RELEASED","EXPIRED") and state == "REVOKING":
        return row
    if row["state"] != state:
        _pause_standby(connection, row, now)
        connection.execute("""UPDATE media_capture_leases SET state=?,
            fence_epoch=fence_epoch+1,last_seen=max(last_seen,?)
            WHERE instance_id=? AND owner_id=?""",
            (state,now,row["instance_id"],row["owner_id"]))
    return _row(connection,row["instance_id"],row["owner_id"])


def _expire(connection, row, now):
    if row is None:
        return None
    if row["state"] in ("ACTIVE","REVOKING"):
        if now < row["last_seen"]:
            return _state(connection,row,"REVOKING",row["last_seen"])
        if now >= row["expires_at"]:
            return _state(connection,row,"EXPIRED",now)
    if row["state"] in ("ACTIVE","REVOKING") and now > row["last_seen"]:
        connection.execute("""UPDATE media_capture_leases SET last_seen=?
            WHERE instance_id=? AND owner_id=?""",
            (now,row["instance_id"],row["owner_id"]))
        row = _row(connection,row["instance_id"],row["owner_id"])
    return row


def _authenticated(connection, *, instance_id, owner_id, binding,
                   capture_id, lease_token, epoch):
    binding = scope(binding)
    identifier(capture_id)
    integer(epoch)
    if not isinstance(lease_token,str) or not _TOKEN.fullmatch(lease_token):
        raise CaptureError("capture_lease_invalid",403)
    row = _row(connection,instance_id,owner_id)
    if row is None:
        raise CaptureError("capture_scope_changed",404)
    if row["capture_id"] != capture_id:
        # Old acknowledgements have no authority over a replacement grant.
        return None, binding
    if any(row[key] != binding[key] for key in _SCOPE):
        raise CaptureError("capture_scope_changed",409)
    if row["epoch"] != epoch or not hmac.compare_digest(row["lease_token"],lease_token):
        raise CaptureError("capture_lease_invalid",403)
    return row, binding


def acquire(connection, *, instance_id, owner_id, binding, capture_id, purpose,
            issued_at, now, standby_id=None, standby_epoch=None):
    binding, capture_id = scope(binding), identifier(capture_id)
    timestamp(issued_at)
    timestamp(now)
    if not owner_id:
        raise CaptureError("capture_scope_changed",403)
    if purpose not in ("call","phone","standby"):
        raise CaptureError("capture_purpose_invalid")
    if purpose == "standby":
        identifier(standby_id)
        integer(standby_epoch)
    elif standby_id is not None or standby_epoch is not None:
        raise CaptureError("capture_scope_invalid")
    claim = {"binding":binding,"capture_id":capture_id,"purpose":purpose,
             "issued_at":issued_at,"standby_id":standby_id,"standby_epoch":standby_epoch}
    digest = hashlib.sha256(json.dumps(claim,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    row = _expire(connection,_row(connection,instance_id,owner_id),now)
    prior = connection.execute("""SELECT request_digest FROM media_capture_attempts
        WHERE instance_id=? AND owner_id=? AND capture_id=?""",
        (instance_id,str(owner_id),capture_id)).fetchone()
    if prior:
        if row is None or row["capture_id"] != capture_id:
            return _closed("capture_attempt_closed")
        if prior["request_digest"] != digest:
            if row["state"] == "ACTIVE":
                raise CaptureError("capture_idempotency_conflict",409)
            return _closed("capture_attempt_closed")
        if row["state"] == "ACTIVE" and not _authority_current(connection,row):
            row = _state(connection,row,"REVOKING",now)
        # Recover the same grant after a lost acknowledgement, even when it
        # expired. This does not renew it and lets its client finish stopping.
        return _view(row)
    if issued_at-now > 30 or now-issued_at > 30:
        raise CaptureError("capture_request_expired",409)
    _session(connection,owner_id,binding)
    if row is not None and now < row["last_seen"]:
        return _closed("capture_clock_changed")
    # Permission expiry revokes use; it is not physical release evidence.
    # A qualified native timer may stop the device and acknowledge release.
    # An unresponsive browser must not be unlocked by a server clock alone.
    if row is not None and row["state"] != "RELEASED":
        if row["purpose"] == "standby" and purpose in ("call","phone"):
            # This transaction commits the Standby epoch fence before the
            # caller may observe STOP_PENDING. Raising here would roll it back.
            row = _state(connection,row,"REVOKING",now)
            return {"granted":False,"state":"STOP_PENDING","reason":"capture_stop_pending",
                    "retry_at":row["expires_at"]}
        return {"granted":False,"state":"BUSY","reason":"capture_media_busy",
                "retry_at":row["expires_at"]}
    if purpose == "standby":
        if not _standby_current(connection,{"instance_id":instance_id,
                "owner_id":str(owner_id),**binding,"purpose":purpose,
                "standby_id":standby_id,"standby_epoch":standby_epoch}):
            raise CaptureError("capture_standby_scope_changed",409)
    connection.execute("""DELETE FROM media_capture_attempts
        WHERE instance_id=? AND owner_id=? AND created_at<?""",
        (instance_id,str(owner_id),now-ATTEMPT_RETENTION_SECONDS))
    count = connection.execute("""SELECT count(*) FROM media_capture_attempts
        WHERE instance_id=? AND owner_id=?""",(instance_id,str(owner_id))).fetchone()[0]
    if count >= MAX_RECENT_ATTEMPTS:
        raise CaptureError("capture_capacity_reached",429)
    epoch = row["fence_epoch"]+1 if row is not None else 1
    token = secrets.token_urlsafe(32)
    connection.execute("""INSERT OR REPLACE INTO media_capture_leases VALUES
        (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (instance_id,str(owner_id),*(binding[key] for key in _SCOPE),capture_id,purpose,
         token,epoch,epoch,"ACTIVE",now,now+LEASE_SECONDS,now,0,standby_id,standby_epoch))
    connection.execute("""INSERT INTO media_capture_attempts VALUES (?,?,?,?,?)""",
        (instance_id,str(owner_id),capture_id,digest,now))
    return _view(_row(connection,instance_id,owner_id))


def operate(connection, *, operation, instance_id, owner_id, binding, capture_id,
            lease_token, epoch, now, sequence=None, stopped=None):
    timestamp(now)
    if operation not in ("status","renew","release"):
        raise CaptureError("capture_operation_invalid")
    if operation == "renew":
        integer(sequence)
    if operation == "release" and type(stopped) is not bool:
        raise CaptureError("capture_stop_receipt_invalid")
    row, binding = _authenticated(connection,instance_id=instance_id,owner_id=owner_id,
        binding=binding,capture_id=capture_id,lease_token=lease_token,epoch=epoch)
    if row is None:
        return _closed()
    row = _expire(connection,row,now)
    if operation == "release":
        # A release without the client's stop receipt revokes input/output
        # immediately, but cannot pretend its tracks have been released.
        return _view(_state(connection,row,"RELEASED" if stopped else "REVOKING",now))
    if row["state"] != "ACTIVE":
        return _view(row)
    if not _authority_current(connection,row):
        # Both the Conversation and current Standby epoch are authoritative.
        # This survives another Worker's mutation even without a callback.
        return _view(_state(connection,row,"REVOKING",now))
    if operation == "status":
        return _view(row)
    if sequence == row["renew_sequence"]:
        return _view(row)
    if sequence != row["renew_sequence"]+1:
        raise CaptureError("capture_sequence_invalid",409)
    expires = min(now+LEASE_SECONDS,row["created_at"]+MAX_CAPTURE_SECONDS)
    if expires <= now:
        return _view(_state(connection,row,"EXPIRED",now))
    connection.execute("""UPDATE media_capture_leases
        SET expires_at=?,last_seen=?,renew_sequence=?
        WHERE instance_id=? AND owner_id=?""",
        (expires,now,sequence,instance_id,str(owner_id)))
    return _view(_row(connection,instance_id,owner_id))
