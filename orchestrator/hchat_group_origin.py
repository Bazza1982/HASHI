"""Durable, Run-owned group fanout and per-recipient dispatch receipts."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from orchestrator.hchat_delivery_status import group_member_message_id
from orchestrator.hchat_origin_binding import HChatOriginConflict, _ensure_schema
from orchestrator.hchat_submit_result import HChatSubmission
from orchestrator.operation_failures import OperationFailure, OperationRejected, operation_failure
from remote.internet_address import GroupAddress, LocalAddress, PublicAddress, parse_hchat_address


def _ensure_group_schema(connection: Any) -> None:
    _ensure_schema(connection)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS hchat_group_origins (
               parent_message_id TEXT PRIMARY KEY,
               instance_id TEXT NOT NULL,
               owner_id TEXT NOT NULL,
               agent_id TEXT NOT NULL,
               session_id TEXT NOT NULL,
               run_id TEXT NOT NULL,
               request_id TEXT NOT NULL,
               context_generation INTEGER NOT NULL,
               fencing_token INTEGER NOT NULL,
               target TEXT NOT NULL,
               content_sha256 TEXT NOT NULL,
               attachments_sha256 TEXT NOT NULL,
               originated_at TEXT NOT NULL,
               FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
               FOREIGN KEY(run_id) REFERENCES runs(run_id)
           )"""
    )
    group_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(hchat_group_origins)").fetchall()
    }
    if "attachments_sha256" not in group_columns:
        # Earlier text-only group records all had an empty attachment set.
        empty_manifest = hashlib.sha256(b"[]").hexdigest()
        connection.execute(
            "ALTER TABLE hchat_group_origins ADD COLUMN attachments_sha256 TEXT NOT NULL DEFAULT '"
            + empty_manifest + "'"
        )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS hchat_group_recipients (
               parent_message_id TEXT NOT NULL,
               recipient TEXT NOT NULL,
               message_id TEXT NOT NULL,
               ordinal INTEGER NOT NULL,
               state TEXT NOT NULL,
               result_json TEXT,
               PRIMARY KEY(parent_message_id, recipient),
               UNIQUE(parent_message_id, ordinal),
               UNIQUE(message_id),
               FOREIGN KEY(parent_message_id) REFERENCES hchat_group_origins(parent_message_id)
                   ON DELETE CASCADE
           )"""
    )


class HChatGroupOrigin:
    """Bind a frozen group and its outcomes to one authenticated local Run."""

    def __init__(
        self, store: Any, *, parent_message_id: str, owner_id: str, agent_id: str,
        session_id: str, run_id: str, request_id: str, context_generation: int,
        fencing_token: int, target: str, text: str,
    ):
        parsed = parse_hchat_address(target)
        if not isinstance(parsed, GroupAddress):
            raise HChatOriginConflict("HChat group target is invalid")
        self.store = store
        self.identity = {
            "parent_message_id": str(parent_message_id).strip(),
            "instance_id": str(store.instance_id).strip().upper(),
            "owner_id": str(owner_id).strip(),
            "agent_id": str(agent_id).strip().casefold(),
            "session_id": str(session_id).strip(),
            "run_id": str(run_id).strip(),
            "request_id": str(request_id).strip(),
            "context_generation": int(context_generation),
            "fencing_token": int(fencing_token),
            "target": parsed.canonical,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        if any(value in ("", None) for value in self.identity.values()):
            raise HChatOriginConflict("HChat group source identity is incomplete")
        if self.identity["context_generation"] < 1:
            raise HChatOriginConflict("HChat group context is invalid")

    def assert_request(
        self, *, parent_message_id: str, source_instance: str, from_agent: str,
        target: str, text: str,
    ) -> None:
        expected = {
            "parent_message_id": str(parent_message_id).strip(),
            "instance_id": str(source_instance).strip().upper(),
            "agent_id": str(from_agent).strip().casefold(),
            "target": parse_hchat_address(target).canonical,
            "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        if any(self.identity[key] != value for key, value in expected.items()):
            raise HChatOriginConflict("HChat group request does not match its source binding")

    def _check_live_run(self, connection: Any) -> None:
        identity = self.identity
        run = connection.execute(
            """SELECT r.request_id, r.context_generation, r.fencing_token, r.state,
                      r.agent_id AS run_agent, s.owner_id, s.agent_id, s.session_id,
                      s.status, s.context_generation AS session_generation
               FROM runs r JOIN sessions s ON s.session_id = r.session_id
               WHERE r.run_id=? AND s.instance_id=?""",
            (identity["run_id"], identity["instance_id"]),
        ).fetchone()
        if run is None or any((
            run["request_id"] != identity["request_id"],
            run["owner_id"] != identity["owner_id"],
            run["agent_id"] != identity["agent_id"],
            run["run_agent"] != identity["agent_id"],
            run["session_id"] != identity["session_id"],
            run["context_generation"] != identity["context_generation"],
            run["session_generation"] != identity["context_generation"],
            run["fencing_token"] != identity["fencing_token"],
            run["state"] != "running",
            run["status"] != "active",
        )):
            raise HChatOriginConflict("HChat group source Run is no longer current")

    def _group(self, connection: Any) -> Any:
        row = connection.execute(
            "SELECT * FROM hchat_group_origins WHERE parent_message_id=?",
            (self.identity["parent_message_id"],),
        ).fetchone()
        if row is not None and any(row[key] != value for key, value in self.identity.items()):
            raise HChatOriginConflict("HChat group is already bound to another request or content")
        return row

    def _snapshot(self, connection: Any, row: Any) -> list[dict[str, str]]:
        entries = connection.execute(
            """SELECT recipient, message_id FROM hchat_group_recipients
               WHERE parent_message_id=? ORDER BY ordinal""",
            (self.identity["parent_message_id"],),
        ).fetchall()
        return [
            {"recipient": item["recipient"], "message_id": item["message_id"],
             "originated_at": row["originated_at"]}
            for item in entries
        ]

    def prepare(
        self, members: list[str] | None, *, attachment_manifest: list[dict] | None = None,
    ) -> list[dict[str, str]] | None:
        """Return an existing frozen membership, or atomically create it."""
        manifest = list(attachment_manifest or [])
        manifest_digest = hashlib.sha256(json.dumps(
            manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_group_schema(connection)
            self._check_live_run(connection)
            row = self._group(connection)
            if row is not None:
                if row["attachments_sha256"] != manifest_digest:
                    raise HChatOriginConflict("HChat group attachment content changed")
                return self._snapshot(connection, row)
            if members is None:
                return None
            if not members:
                raise HChatOriginConflict("HChat group has no recipients")
            normalized = []
            seen = set()
            for member in members:
                parsed = parse_hchat_address(member, allow_group=False)
                if not isinstance(parsed, (LocalAddress, PublicAddress)):
                    raise HChatOriginConflict("HChat group recipient is invalid")
                if isinstance(parsed, LocalAddress) and not parsed.instance_id:
                    raise HChatOriginConflict("HChat group recipient has no instance")
                canonical = parsed.canonical
                if canonical.casefold() in seen:
                    raise HChatOriginConflict("HChat group recipient is duplicated")
                seen.add(canonical.casefold())
                normalized.append(canonical)
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            columns = tuple(self.identity)
            connection.execute(
                "INSERT INTO hchat_group_origins (" + ", ".join(columns)
                + ", attachments_sha256, originated_at) VALUES ("
                + ", ".join("?" for _ in columns) + ", ?, ?)",
                (*self.identity.values(), manifest_digest, timestamp),
            )
            for ordinal, member in enumerate(normalized):
                child_id = group_member_message_id(
                    self.identity["parent_message_id"], self.identity["instance_id"],
                    self.identity["agent_id"], member,
                )
                expected = {
                    "message_id": child_id,
                    "instance_id": self.identity["instance_id"],
                    "owner_id": self.identity["owner_id"],
                    "agent_id": self.identity["agent_id"],
                    "session_id": self.identity["session_id"],
                    "run_id": self.identity["run_id"],
                    "request_id": self.identity["request_id"],
                    "context_generation": self.identity["context_generation"],
                    "target": member.casefold(),
                }
                existing = connection.execute(
                    "SELECT * FROM hchat_outbound_origins WHERE message_id=?", (child_id,),
                ).fetchone()
                if existing is not None:
                    if any(existing[key] != value for key, value in expected.items()):
                        raise HChatOriginConflict("HChat recipient message already has another origin")
                    if existing["originated_at"] != timestamp:
                        raise HChatOriginConflict("HChat recipient timestamp conflicts with group")
                else:
                    fields = tuple(expected)
                    connection.execute(
                        "INSERT INTO hchat_outbound_origins (" + ", ".join(fields)
                        + ", originated_at) VALUES (" + ", ".join("?" for _ in fields) + ", ?)",
                        (*expected.values(), timestamp),
                    )
                connection.execute(
                    """INSERT INTO hchat_group_recipients
                       (parent_message_id, recipient, message_id, ordinal, state)
                       VALUES (?, ?, ?, ?, 'pending')""",
                    (self.identity["parent_message_id"], member, child_id, ordinal),
                )
            row = self._group(connection)
            return self._snapshot(connection, row)

    def begin(self, recipient: str) -> HChatSubmission | None:
        """Claim a recipient once; an interrupted claim remains unconfirmed."""
        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_group_schema(connection)
            self._check_live_run(connection)
            if self._group(connection) is None:
                raise HChatOriginConflict("HChat group origin is unavailable")
            row = connection.execute(
                """SELECT * FROM hchat_group_recipients
                   WHERE parent_message_id=? AND recipient=?""",
                (self.identity["parent_message_id"], recipient),
            ).fetchone()
            if row is None:
                raise HChatOriginConflict("HChat recipient is not in the frozen group")
            if row["state"] == "dispatching":
                failure = operation_failure(
                    OperationRejected("response_invalid"), operation="hchat",
                    phase="dispatch", unconfirmed=True,
                )
                return HChatSubmission(False, "unconfirmed", None, failure)
            if row["state"] == "finished":
                result = json.loads(row["result_json"])
                failure = OperationFailure(**result["failure"]) if result.get("failure") else None
                prior = HChatSubmission(
                    bool(result["handled"]), result["status"], result["accepted"], failure,
                )
                if prior.handled or prior.accepted is None:
                    return prior
            elif row["state"] != "pending":
                raise HChatOriginConflict("HChat recipient state is invalid")
            connection.execute(
                """UPDATE hchat_group_recipients SET state='dispatching', result_json=NULL
                   WHERE parent_message_id=? AND recipient=?""",
                (self.identity["parent_message_id"], recipient),
            )
            return None

    def finish(self, recipient: str, result: HChatSubmission) -> None:
        """Persist the observed result, even if the Run ended during the send."""
        payload = {
            "handled": result.handled, "status": result.status, "accepted": result.accepted,
            "failure": result.failure.diagnostic() if result.failure else None,
        }
        with self.store._lock, self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_group_schema(connection)
            if self._group(connection) is None:
                raise HChatOriginConflict("HChat group origin is unavailable")
            cursor = connection.execute(
                """UPDATE hchat_group_recipients SET state='finished', result_json=?
                   WHERE parent_message_id=? AND recipient=? AND state='dispatching'""",
                (json.dumps(payload, ensure_ascii=True), self.identity["parent_message_id"], recipient),
            )
            if cursor.rowcount != 1:
                raise HChatOriginConflict("HChat recipient dispatch is no longer current")
