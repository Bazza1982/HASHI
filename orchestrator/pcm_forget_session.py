"""PAO-owned redaction of forgotten conversation projections.

Selected records are retained in a separate audit table; public/session
projections no longer carry their contents.  This module handles one local
SessionStore transaction, not the entire /forget operation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping
from uuid import uuid4

from orchestrator.pcm_forget_memory import validate_window
from orchestrator.timezone_policy import parse_absolute_timestamp


class ForgetBusy(RuntimeError):
    pass


class ForgetUnsupported(RuntimeError):
    pass


def install_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_windows (
            instance_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            start_epoch REAL NOT NULL,
            end_epoch REAL NOT NULL,
            naive_timezone TEXT,
            PRIMARY KEY(instance_id, owner_id, agent_id, start_epoch, end_epoch)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_audit (
            instance_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(instance_id, owner_id, agent_id, source_kind, source_id)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_messages (
            session_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            text_digest TEXT NOT NULL,
            PRIMARY KEY(session_id, message_id, text_digest)
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_live_calls (
            call_id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            session_id TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS pcm_forget_operations (
            operation_id TEXT PRIMARY KEY,
            instance_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            start_epoch REAL NOT NULL,
            end_epoch REAL NOT NULL,
            state TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS pcm_forget_one_pending
           ON pcm_forget_operations(instance_id, owner_id, agent_id)
           WHERE state='pending'"""
    )
    # A forget applies to the Agent, across historical owner identities.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS pcm_forget_agent_phone_guard
           BEFORE INSERT ON live_call_attempts
           WHEN EXISTS (
               SELECT 1 FROM pcm_forget_operations f
               WHERE f.instance_id=NEW.instance_id
                 AND f.agent_id=NEW.agent_id AND f.state='pending'
           )
           BEGIN SELECT RAISE(ABORT, 'pcm_forget_in_progress'); END"""
    )
    # Phone admission and forget admission are serialized by SQLite, including
    # separate workers which do not share the SessionStore thread lock.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS pcm_forget_phone_reservation_guard
           BEFORE INSERT ON live_call_attempts
           WHEN EXISTS (
               SELECT 1 FROM pcm_forget_operations f
               WHERE f.instance_id=NEW.instance_id AND f.owner_id=NEW.owner_id
                 AND f.agent_id=NEW.agent_id AND f.state='pending'
           )
           BEGIN SELECT RAISE(ABORT, 'pcm_forget_in_progress'); END"""
    )


def _audit(
    connection: sqlite3.Connection, *, instance_id: str, owner_id: str,
    agent_id: str, kind: str, identity: str, row: Mapping[str, Any],
) -> None:
    from orchestrator.pcm_forget_audit import protect

    scope = (instance_id, owner_id, agent_id, kind, identity)
    if connection.execute(
        """SELECT 1 FROM pcm_forget_audit WHERE instance_id=? AND owner_id=?
           AND agent_id=? AND source_kind=? AND source_id=?""", scope,
    ).fetchone():
        return
    evidence = protect(kind, identity, dict(row))
    connection.execute(
        """INSERT INTO pcm_forget_audit
           (instance_id, owner_id, agent_id, source_kind, source_id, payload_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (*scope, json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))),
    )


def _epoch(value: object, naive_timezone: str | None) -> float | None:
    parsed = parse_absolute_timestamp(value, naive_timezone=naive_timezone)
    return None if parsed is None else parsed.timestamp()


def forget_window(
    connection: sqlite3.Connection,
    *,
    instance_id: str,
    owner_id: str,
    agent_id: str,
    start: datetime,
    end: datetime,
    now: str,
    naive_timezone: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    lower, upper = validate_window(start, end)
    owner, agent = str(owner_id).strip(), str(agent_id).strip().lower()
    if not owner or not agent:
        raise ValueError("forget requires an exact owner and Agent")
    operation = str(operation_id or f"forget_{uuid4().hex}").strip()
    if not operation or len(operation) > 80 or any(
        not (character.isalnum() or character in "_-") for character in operation
    ):
        raise ValueError("invalid forget operation id")
    existing = connection.execute(
        "SELECT * FROM pcm_forget_operations WHERE operation_id=?", (operation,)
    ).fetchone()
    if existing is not None:
        if (str(existing["instance_id"]), str(existing["owner_id"]), str(existing["agent_id"]),
            float(existing["start_epoch"]), float(existing["end_epoch"])) != (
            instance_id, owner, agent, lower, upper
        ):
            raise ValueError("forget operation id was reused for another scope")
        return json.loads(str(existing["result_json"]))
    if connection.execute(
        """SELECT 1 FROM pcm_forget_operations WHERE instance_id=?
           AND agent_id=? AND state='pending'""", (instance_id, agent),
    ).fetchone():
        raise ForgetBusy("a forget operation is already incomplete")
    if naive_timezone:
        from orchestrator.timezone_policy import canonical_timezone_name
        naive_timezone = canonical_timezone_name(naive_timezone)

    if connection.execute(
        """SELECT 1 FROM live_call_attempts
           WHERE instance_id=? AND agent_id=?
             AND state IN ('reserved','provider_created','unknown') LIMIT 1""",
        (instance_id, agent),
    ).fetchone():
        raise ForgetBusy("Agent has an active or uncertain Phone reservation")

    active = connection.execute(
        """SELECT 1 FROM runs r JOIN sessions s ON s.session_id=r.session_id
           WHERE s.instance_id=? AND s.agent_id=?
             AND r.state NOT IN ('completed','failed','stopped','superseded','interrupted')
           LIMIT 1""", (instance_id, agent),
    ).fetchone()
    if active:
        raise ForgetBusy("Agent has active or queued work")
    live = connection.execute(
        """SELECT * FROM live_calls
           WHERE instance_id=? AND agent_id=?""",
        (instance_id, agent),
    ).fetchall()
    selected_calls: list[sqlite3.Row] = []
    phone_run_ids: set[str] = set()
    for call in live:
        if str(call["phase"]) not in {"ended", "failed", "interrupted"}:
            raise ForgetBusy("Agent has an active Phone call")
        started = _epoch(call["started_at"], naive_timezone)
        ended = _epoch(call["ended_at"], naive_timezone) if call["ended_at"] else None
        # A Phone call is indivisible here. Preserve crossing or undated calls
        # rather than removing content whose time cannot be established.
        if started is not None and ended is not None and lower <= started <= ended <= upper:
            selected_calls.append(call)
            for row in connection.execute(
                "SELECT run_id FROM live_actions WHERE call_id=? AND run_id IS NOT NULL",
                (str(call["call_id"]),),
            ):
                phone_run_ids.add(str(row["run_id"]))
            for row in connection.execute(
                """SELECT accepted_run_id FROM live_delegations
                   WHERE call_id=? AND accepted_run_id IS NOT NULL""",
                (str(call["call_id"]),),
            ):
                phone_run_ids.add(str(row["accepted_run_id"]))

    rows = connection.execute(
        """SELECT m.* FROM messages m
           JOIN sessions s ON s.session_id=m.session_id
           WHERE s.instance_id=? AND s.agent_id=?
             AND m.visibility='visible'""",
        (instance_id, agent),
    ).fetchall()
    by_run: dict[str, list[sqlite3.Row]] = defaultdict(list)
    single: list[sqlite3.Row] = []
    for row in rows:
        run_id = str(row["run_id"] or "")
        (by_run[run_id] if run_id else single).append(row)

    selected: list[sqlite3.Row] = []
    selected_runs: set[str] = set()
    unverified = 0
    for run_id, members in by_run.items():
        times = [_epoch(row["created_at"], naive_timezone) for row in members]
        unverified += sum(value is None for value in times)
        selected.extend(row for row, instant in zip(members, times)
                        if instant is not None and lower <= instant <= upper)
        # A run-level projection can contain both sides of a boundary.
        # Redact it only when its entire visible message set is in the window.
        if run_id in phone_run_ids or (times and all(
            value is not None and lower <= value <= upper for value in times
        )):
            selected_runs.add(run_id)
    for row in single:
        instant = _epoch(row["created_at"], naive_timezone)
        unverified += int(instant is None)
        if instant is not None and lower <= instant <= upper:
            selected.append(row)

    from orchestrator.session_titles import automatic_session_title

    affected_sessions: set[str] = set()
    forgotten_titles: dict[str, set[str]] = defaultdict(set)
    for row in selected:
        session_id, message_id = str(row["session_id"]), str(row["message_id"])
        affected_sessions.add(session_id)
        if str(row["role"]) == "user":
            title = automatic_session_title(row["display_text"] or row["text"])
            if title:
                forgotten_titles[session_id].add(title)
        _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
               kind="message", identity=message_id, row=dict(row))
        for text in {str(row["text"] or ""), str(row["display_text"] or "")} - {""}:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            connection.execute(
                """INSERT OR IGNORE INTO pcm_forget_messages(session_id, message_id, text_digest)
                   VALUES (?, ?, ?)""", (session_id, message_id, digest),
            )
        connection.execute(
            """UPDATE messages SET text='', display_text=NULL, content_json='[]',
                   message_context_json='{}', visibility='forgotten', history_eligible=0
               WHERE message_id=?""", (message_id,),
        )

    for session_id, titles in forgotten_titles.items():
        row = connection.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is not None and str(row["title_source"]) == "maintenance" and str(row["title"]) in titles:
            _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                   kind="session_title", identity=operation + ":" + session_id, row=dict(row))
            connection.execute(
                "UPDATE sessions SET title='New session', title_source='system' WHERE session_id=?",
                (session_id,),
            )

    # Command receipts and non-run events are not model memory, but can expose
    # the same conversation in ordinary Session projections or replay.
    command_rows = connection.execute(
        """SELECT c.* FROM frontend_command_invocations c
           JOIN sessions s ON s.session_id=c.session_id
           WHERE s.instance_id=? AND s.agent_id=?""",
        (instance_id, agent),
    ).fetchall()
    for row in command_rows:
        instant = _epoch(row["created_at"], naive_timezone)
        if instant is None or not lower <= instant <= upper:
            continue
        if str(row["state"]) != "completed":
            raise ForgetBusy("Agent has an unfinished command")
        identity = ":".join((str(row["session_id"]), str(row["client_id"]), str(row["request_id"])))
        _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
               kind="command_invocation", identity=identity, row=dict(row))
        connection.execute(
            """UPDATE frontend_command_invocations SET invocation_json='{}',
               response_json='{"ok":false,"error_code":"forgotten_command_result"}'
               WHERE session_id=? AND client_id=? AND request_id=?""",
            (str(row["session_id"]), str(row["client_id"]), str(row["request_id"])),
        )
    event_rows = connection.execute(
        """SELECT e.* FROM run_events e JOIN sessions s ON s.session_id=e.session_id
           WHERE s.instance_id=? AND s.agent_id=? AND e.run_id IS NULL
             AND NOT EXISTS (SELECT 1 FROM live_fragments f WHERE f.event_id=e.event_id)""",
        (instance_id, agent),
    ).fetchall()
    for event in event_rows:
        instant = _epoch(event["created_at"], naive_timezone)
        if instant is not None and lower <= instant <= upper:
            _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                   kind="event", identity=str(event["event_id"]), row=dict(event))
            connection.execute(
                "UPDATE run_events SET summary='Conversation forgotten', detail_json='{}' WHERE event_id=?",
                (str(event["event_id"]),),
            )

    for call in selected_calls:
        call_id = str(call["call_id"])
        affected_sessions.add(str(call["session_id"]))
        connection.execute(
            """INSERT OR IGNORE INTO pcm_forget_live_calls
               (call_id, owner_id, agent_id, session_id) VALUES (?, ?, ?, ?)""",
            (call_id, str(call["owner_id"]), agent, str(call["session_id"])),
        )
        event_rows = connection.execute(
            """SELECT e.* FROM run_events e JOIN live_fragments f ON f.event_id=e.event_id
               WHERE f.call_id=?""", (call_id,),
        ).fetchall()
        for event in event_rows:
            _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                   kind="live_event", identity=str(event["event_id"]), row=dict(event))
            connection.execute(
                "UPDATE run_events SET summary='Phone conversation forgotten', detail_json='{}' WHERE event_id=?",
                (str(event["event_id"]),),
            )
        for table, kind, id_field in (
            ("phone_context_handoffs", "phone_handoff", "handoff_id"),
            ("live_actions", "phone_action", "action_id"),
            ("live_delegations", "phone_delegation", "delegation_id"),
        ):
            for row in connection.execute(f"SELECT * FROM {table} WHERE call_id=?", (call_id,)):
                identity = call_id + ":" + str(row[id_field])
                _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                       kind=kind, identity=identity, row=dict(row))
        connection.execute(
            "UPDATE phone_context_handoffs SET payload_json='{}' WHERE call_id=?", (call_id,)
        )
        connection.execute(
            "UPDATE live_actions SET request='', receipt='', evidence_json='[]' WHERE call_id=?",
            (call_id,),
        )
        connection.execute(
            "UPDATE live_delegations SET proposal_text='' WHERE call_id=?", (call_id,)
        )
        for table in ("live_provider_fragment_inbox", "live_provider_event_inbox"):
            for row in connection.execute(f"SELECT * FROM {table} WHERE call_id=?", (call_id,)):
                identity = call_id + ":" + str(row["provider_event_id"])
                _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                       kind=table, identity=identity, row=dict(row))
            connection.execute(f"DELETE FROM {table} WHERE call_id=?", (call_id,))

    for run_id in selected_runs:
        projection = connection.execute(
            "SELECT * FROM run_projection_records WHERE run_id=?", (run_id,)
        ).fetchone()
        if projection is not None:
            _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                   kind="run_projection", identity=run_id, row=dict(projection))
            connection.execute("DELETE FROM run_projection_records WHERE run_id=?", (run_id,))
        run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is not None:
            _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                   kind="run", identity=run_id, row=dict(run))
            connection.execute("UPDATE runs SET error_text=NULL WHERE run_id=?", (run_id,))
        event_rows = connection.execute(
            "SELECT * FROM run_events WHERE run_id=?", (run_id,)
        ).fetchall()
        for event in event_rows:
            _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                   kind="event", identity=str(event["event_id"]), row=dict(event))
        connection.execute(
            """UPDATE run_events SET summary='Conversation forgotten', detail_json='{}'
               WHERE run_id=?""", (run_id,),
        )
        voice_rows = connection.execute(
            "SELECT * FROM voice_transcripts WHERE run_id=?", (run_id,)
        ).fetchall()
        for voice in voice_rows:
            _audit(connection, instance_id=instance_id, owner_id=owner, agent_id=agent,
                   kind="voice", identity=str(voice["transcript_id"]), row=dict(voice))
        connection.execute("UPDATE voice_transcripts SET text='' WHERE run_id=?", (run_id,))

    all_sessions = connection.execute(
        """SELECT session_id, owner_id, context_generation, status FROM sessions
           WHERE instance_id=? AND agent_id=?""",
        (instance_id, agent),
    ).fetchall()
    owners = {str(session["owner_id"]) for session in all_sessions} | {owner}
    connection.executemany(
        """INSERT OR IGNORE INTO pcm_forget_windows
           (instance_id, owner_id, agent_id, start_epoch, end_epoch, naive_timezone)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ((instance_id, item, agent, lower, upper, naive_timezone) for item in owners),
    )
    scoped_sessions = [session for session in all_sessions if session["status"] != "deleted"]
    previous_generations = []
    for session in scoped_sessions:
        session_id = str(session["session_id"])
        previous = int(session["context_generation"])
        generation = previous + 1
        previous_generations.append({"session_id": session_id, "generation": previous})
        connection.execute(
            """INSERT INTO session_context_generations(session_id, generation, reason, created_at)
               VALUES (?, ?, 'forget', ?)""", (session_id, generation, now),
        )
        connection.execute(
            """UPDATE sessions SET context_generation=?, history_generation=history_generation+1,
                   revision=revision+1, updated_at=? WHERE session_id=?""",
            (generation, now, session_id),
        )
    result = {
        "messages": len(selected), "runs": len(selected_runs),
        "sessions": len(scoped_sessions), "unverified": unverified,
        "phone_calls": len(selected_calls), "operation_id": operation,
        "naive_timezone": naive_timezone,
        "run_ids": sorted(selected_runs),
        "session_ids": sorted(str(row["session_id"]) for row in scoped_sessions),
        "all_session_ids": sorted(str(row["session_id"]) for row in all_sessions),
        "previous_generations": previous_generations,
    }
    connection.execute(
        """INSERT INTO pcm_forget_operations(
               operation_id, instance_id, owner_id, agent_id, start_epoch,
               end_epoch, state, steps_json, result_json, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (operation, instance_id, owner, agent, lower, upper,
         json.dumps(["session"]), json.dumps(result, separators=(",", ":")), now),
    )
    return result


def assert_continuity_safe(
    connection: sqlite3.Connection, *, instance_id: str, owner_id: str,
    agent_id: str, sessions: list[Mapping[str, Any]],
) -> None:
    """Prevent an old transfer from recreating an erased PCM interval."""
    scope = (instance_id, str(owner_id), str(agent_id).lower())
    if connection.execute(
        """SELECT 1 FROM pcm_forget_operations WHERE instance_id=?
           AND agent_id=? AND state='pending'""", (scope[0], scope[2]),
    ).fetchone():
        raise ForgetBusy("forget operation is incomplete")
    windows = connection.execute(
        """SELECT DISTINCT start_epoch, end_epoch, naive_timezone FROM pcm_forget_windows
           WHERE instance_id=? AND agent_id=?""", (scope[0], scope[2]),
    ).fetchall()
    if not windows:
        return
    for session in sessions:
        for message in session.get("messages", []):
            for lower, upper, naive_timezone in windows:
                instant = _epoch(message.get("source_created_at"), naive_timezone)
                if instant is not None and float(lower) <= instant <= float(upper):
                    raise ForgetUnsupported("continuity contains a forgotten interval")


def projection_policy(
    connection: sqlite3.Connection, *, instance_id: str,
    owner_id: str, agent_id: str, session_id: str,
) -> dict[str, Any]:
    windows = connection.execute(
        """SELECT DISTINCT start_epoch, end_epoch, naive_timezone FROM pcm_forget_windows
           WHERE instance_id=? AND agent_id=?""",
        (instance_id, str(agent_id).lower()),
    ).fetchall()
    messages = connection.execute(
        "SELECT message_id, text_digest FROM pcm_forget_messages WHERE session_id=?",
        (str(session_id),),
    ).fetchall()
    return {
        "windows": [(float(row[0]), float(row[1]), row[2]) for row in windows],
        "message_ids": {str(row[0]) for row in messages},
        "digests": {str(row[1]) for row in messages},
    }


def transcript_blocked(item: Mapping[str, Any], policy: Mapping[str, Any]) -> bool:
    windows = policy.get("windows") or ()
    if not windows:
        return False
    if str(item.get("message_ref") or item.get("message_id") or "") in policy["message_ids"]:
        return True
    for lower, upper, naive_timezone in windows:
        instant = _epoch(item.get("ts"), naive_timezone)
        if instant is not None and lower <= instant <= upper:
            return True
    return False
