"""PAO-owned durable attachment discovery for authenticated Frontend Connectors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

_HAS_ATTACHMENT = """EXISTS (
    SELECT 1 FROM json_each(NEW.content_json) AS part
    WHERE lower(json_extract(part.value, '$.type')) IN ('attachment','media','audio')
      AND coalesce(json_extract(part.value, '$.attachment_id'), '') != ''
)"""


def install_schema(connection):
    missing = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachment_catalogue_entries'"
        ).fetchone()
        is None
    )
    connection.execute("""CREATE TABLE IF NOT EXISTS attachment_catalogue_entries (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id TEXT NOT NULL UNIQUE,
        FOREIGN KEY(message_id) REFERENCES messages(message_id) ON DELETE CASCADE
    )""")
    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS attachment_catalogue_insert
        AFTER INSERT ON messages WHEN NEW.visibility='visible'
          AND NEW.role IN ('user','assistant') AND {_HAS_ATTACHMENT}
        BEGIN INSERT OR REPLACE INTO attachment_catalogue_entries(message_id) VALUES(NEW.message_id); END""")
    connection.execute(f"""CREATE TRIGGER IF NOT EXISTS attachment_catalogue_update
        AFTER UPDATE OF content_json,visibility,role ON messages
        WHEN OLD.content_json != NEW.content_json OR OLD.visibility != NEW.visibility OR OLD.role != NEW.role
        BEGIN
          DELETE FROM attachment_catalogue_entries WHERE message_id=NEW.message_id;
          INSERT INTO attachment_catalogue_entries(message_id)
            SELECT NEW.message_id WHERE NEW.visibility='visible'
              AND NEW.role IN ('user','assistant') AND {_HAS_ATTACHMENT};
        END""")
    if missing:
        connection.execute("""INSERT OR IGNORE INTO attachment_catalogue_entries(message_id)
            SELECT message_id FROM messages AS m WHERE m.visibility='visible'
              AND m.role IN ('user','assistant') AND EXISTS (
                SELECT 1 FROM json_each(m.content_json) AS part
                WHERE lower(json_extract(part.value, '$.type')) IN ('attachment','media','audio')
                  AND coalesce(json_extract(part.value, '$.attachment_id'), '') != ''
              ) ORDER BY m.created_at,m.session_id,m.ordinal""")


def read_page(connection, *, instance_id, owner_id, agent_ids, after=0, limit=50):
    if (
        not isinstance(after, int)
        or isinstance(after, bool)
        or not 0 <= after <= 2**53 - 1
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= 100
        or not isinstance(owner_id, str)
        or not owner_id
        or not isinstance(agent_ids, (list, tuple))
        or len(agent_ids) > 256
        or any(not isinstance(agent, str) or not agent for agent in agent_ids)
    ):
        raise ValueError("invalid attachment catalogue request")
    connection.execute("BEGIN")
    row = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='attachment_catalogue_entries'"
    ).fetchone()
    upper = max(after, int(row["seq"]) if row else 0)
    agents = sorted(set(agent_ids))
    if not agents:
        return {"messages": [], "next_after": upper, "complete": True}
    placeholders = ",".join("?" for _ in agents)
    rows = connection.execute(
        f"""
        SELECT m.*, s.agent_id, e.sequence AS catalogue_sequence,
               r.request_id, r.state AS run_state, r.final_message_id AS run_final_message_id
        FROM attachment_catalogue_entries AS e
        JOIN messages AS m ON m.message_id=e.message_id
        JOIN sessions AS s ON s.session_id=m.session_id
        LEFT JOIN runs AS r ON r.run_id=m.run_id
        WHERE e.sequence>? AND e.sequence<=?
          AND s.instance_id=? AND s.owner_id=?
          AND s.agent_id IN ({placeholders})
          AND s.session_kind='conversation' AND s.status!='deleted'
          AND m.visibility='visible' AND m.role IN ('user','assistant')
        ORDER BY e.sequence LIMIT ?
    """,
        [after, upper, instance_id, owner_id, *agents, limit + 1],
    ).fetchall()
    more = len(rows) > limit
    selected = rows[:limit]
    return {
        "messages": selected,
        "next_after": int(selected[-1]["catalogue_sequence"]) if more else upper,
        "complete": not more,
    }


def read_source(
    connection,
    *,
    instance_id,
    owner_id,
    agent_ids,
    session_id,
    message_id,
    attachment_id,
):
    if any(
        not isinstance(value, str) or not value or len(value) > 512
        for value in (owner_id, session_id, message_id, attachment_id)
    ):
        raise ValueError("invalid attachment source")
    connection.execute("BEGIN")
    target = connection.execute(
        """
        SELECT m.*, s.agent_id FROM messages AS m JOIN sessions AS s ON s.session_id=m.session_id
        WHERE s.instance_id=? AND s.owner_id=? AND s.session_kind='conversation'
          AND s.status!='deleted' AND m.session_id=? AND m.message_id=?
          AND m.visibility='visible' AND m.role IN ('user','assistant')
    """,
        (instance_id, owner_id, session_id, message_id),
    ).fetchone()
    if target is None or target["agent_id"] not in set(agent_ids):
        raise LookupError("attachment source unavailable")
    parts = json.loads(target["content_json"])
    if not any(
        isinstance(part, dict)
        and str(part.get("attachment_id", "")) == attachment_id
        and str(part.get("type", "")).lower() in {"attachment", "media", "audio"}
        for part in parts
    ):
        raise LookupError("attachment source unavailable")
    rows = connection.execute(
        """
        SELECT m.*, r.request_id, r.state AS run_state, r.final_message_id AS run_final_message_id
        FROM messages AS m LEFT JOIN runs AS r ON r.run_id=m.run_id
        WHERE m.session_id=? AND m.context_generation=? AND m.visibility='visible'
          AND m.role IN ('user','assistant') AND m.ordinal>=? AND m.ordinal<=?
        ORDER BY m.ordinal LIMIT 25
    """,
        (
            session_id,
            target["context_generation"],
            max(0, target["ordinal"] - 12),
            target["ordinal"] + 12,
        ),
    ).fetchall()
    return {
        "agent_id": target["agent_id"],
        "session_id": session_id,
        "context_generation": target["context_generation"],
        "target_message_id": message_id,
        "messages": rows,
    }


def _scope(instance, owner, agents):
    return [
        str(instance),
        str(owner),
        hashlib.sha256(
            json.dumps(
                sorted(set(agents)), ensure_ascii=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
    ]


def encode_cursor(after, *, instance, owner, agents, key):
    payload = json.dumps(
        ["attachment-catalogue-v1", *_scope(instance, owner, agents), after],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return body + "." + signature


def decode_cursor(cursor, *, instance, owner, agents, key):
    if not isinstance(cursor, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,1024}\.[a-f0-9]{64}", cursor
    ):
        raise ValueError("invalid attachment catalogue cursor")
    body, signature = cursor.split(".")
    expected = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid attachment catalogue cursor")
    try:
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        value = json.loads(decoded)
        if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != body:
            raise ValueError()
    except Exception as error:
        raise ValueError("invalid attachment catalogue cursor") from error
    if (
        not isinstance(value, list)
        or len(value) != 5
        or value[:4] != ["attachment-catalogue-v1", *_scope(instance, owner, agents)]
        or not isinstance(value[4], int)
        or isinstance(value[4], bool)
        or not 0 <= value[4] <= 2**53 - 1
    ):
        raise ValueError("attachment catalogue scope changed")
    return value[4]
