"""One-time, owner-scoped previews for destructive PCM forgetting."""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from orchestrator.pcm_forget_session import ForgetBusy, ForgetUnsupported, forget_window


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_previews (
            token_digest TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
            instance_id TEXT NOT NULL, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL,
            session_id TEXT NOT NULL, context_generation INTEGER NOT NULL,
            start_epoch REAL NOT NULL, end_epoch REAL NOT NULL, expires_epoch REAL NOT NULL,
            naive_timezone TEXT, fingerprint TEXT NOT NULL, state TEXT NOT NULL
        )"""
    )


def _fingerprint(connection: sqlite3.Connection, *, instance: str, owner: str, agent: str) -> str:
    digest = hashlib.sha256()
    queries = (
        """SELECT s.session_id, s.status, s.context_generation FROM sessions s
           WHERE s.instance_id=? AND s.owner_id=? AND s.agent_id=? ORDER BY s.session_id""",
        """SELECT m.message_id, m.session_id, m.run_id, m.role, m.visibility,
                  m.history_eligible, m.created_at, m.text, m.display_text
           FROM messages m JOIN sessions s ON s.session_id=m.session_id
           WHERE s.instance_id=? AND s.owner_id=? AND s.agent_id=?
             AND m.history_eligible=1 ORDER BY m.message_id""",
        """SELECT r.run_id, r.state FROM runs r JOIN sessions s ON s.session_id=r.session_id
           WHERE s.instance_id=? AND s.owner_id=? AND s.agent_id=? ORDER BY r.run_id""",
        """SELECT attempt_id, state FROM live_call_attempts
           WHERE instance_id=? AND owner_id=? AND agent_id=? ORDER BY attempt_id""",
        """SELECT call_id, phase FROM live_calls
           WHERE instance_id=? AND owner_id=? AND agent_id=? ORDER BY call_id""",
    )
    for query in queries:
        for row in connection.execute(query, (instance, owner, agent)):
            digest.update(json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
        digest.update(b"\0")
    return digest.hexdigest()


def create(
    connection: sqlite3.Connection, *, instance_id: str, owner_id: str, agent_id: str,
    session_id: str, duration: timedelta, now: datetime, naive_timezone: str | None = None,
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("preview time requires timezone")
    seconds = duration.total_seconds()
    if seconds <= 0 or seconds > 3650 * 86400:
        raise ValueError("forget duration must be between zero and ten years")
    owner, agent = str(owner_id), str(agent_id).lower()
    row = connection.execute(
        """SELECT context_generation FROM sessions
           WHERE instance_id=? AND owner_id=? AND agent_id=? AND session_id=? AND status!='deleted'""",
        (instance_id, owner, agent, str(session_id)),
    ).fetchone()
    if row is None:
        raise ForgetUnsupported("forget session is unavailable")
    if connection.execute(
        """SELECT 1 FROM pcm_forget_operations WHERE instance_id=? AND owner_id=?
           AND agent_id=? AND state='pending'""", (instance_id, owner, agent),
    ).fetchone():
        raise ForgetBusy("an earlier forget operation is incomplete")
    token = secrets.token_urlsafe(18)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    operation = "forget_" + uuid4().hex
    end = now.astimezone(timezone.utc)
    start = end - duration
    expires = end + timedelta(minutes=5)
    connection.execute(
        """INSERT INTO pcm_forget_previews (
            token_digest, operation_id, instance_id, owner_id, agent_id,
            session_id, context_generation, start_epoch, end_epoch, expires_epoch,
            naive_timezone, fingerprint, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'previewed')""",
        (digest, operation, instance_id, owner, agent, str(session_id), int(row[0]),
         start.timestamp(), end.timestamp(), expires.timestamp(), naive_timezone,
         _fingerprint(connection, instance=instance_id, owner=owner, agent=agent)),
    )
    return {"token": token, "operation_id": operation, "start": start.isoformat(),
            "end": end.isoformat(), "expires": expires.isoformat()}


def confirm(
    connection: sqlite3.Connection, *, instance_id: str, owner_id: str, agent_id: str,
    session_id: str, token: str, now: datetime, now_text: str,
) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 128 or not token.isascii():
        raise ForgetUnsupported("forget preview is invalid")
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    owner, agent = str(owner_id), str(agent_id).lower()
    row = connection.execute(
        """SELECT * FROM pcm_forget_previews WHERE token_digest=? AND instance_id=?
           AND owner_id=? AND agent_id=? AND session_id=?""",
        (digest, instance_id, owner, agent, str(session_id)),
    ).fetchone()
    if row is None:
        raise ForgetUnsupported("forget preview is invalid")
    if str(row["state"]) == "previewed":
        if now.tzinfo is None or now.utcoffset() is None or now.timestamp() > float(row["expires_epoch"]):
            raise ForgetUnsupported("forget preview expired")
        current = _fingerprint(connection, instance=instance_id, owner=owner, agent=agent)
        if not secrets.compare_digest(current, str(row["fingerprint"])):
            raise ForgetUnsupported("forget preview changed; create a new preview")
    elif str(row["state"]) != "confirmed":
        raise ForgetUnsupported("forget preview is invalid")
    result = forget_window(
        connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
        start=datetime.fromtimestamp(float(row["start_epoch"]), timezone.utc),
        end=datetime.fromtimestamp(float(row["end_epoch"]), timezone.utc),
        naive_timezone=row["naive_timezone"], operation_id=str(row["operation_id"]), now=now_text,
    )
    connection.execute(
        "UPDATE pcm_forget_previews SET state='confirmed' WHERE token_digest=?", (digest,),
    )
    return result
