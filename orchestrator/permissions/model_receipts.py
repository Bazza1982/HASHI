"""Private durable observations of physical model calls; never a permission grant.

The intent is committed before transport. A lost response stays unknown; restart
reads the same observation and never sends another HTTP request to reconcile it.
Request bodies, credentials and response text are not stored here.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _owner(binding):
    context = binding.context
    return _encode({name: getattr(context, name) for name in (
        "instance_id", "owner_id", "agent_id", "session_id", "task_id", "run_id", "turn_id",
    )})


class ModelReceiptJournal:
    def __init__(self, path):
        self.path = Path(path)
        with self._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS model_receipts(
                id TEXT PRIMARY KEY, owner BLOB NOT NULL, binding BLOB NOT NULL,
                payload BLOB NOT NULL, digest TEXT NOT NULL
            )""")
            connection.execute("""CREATE TABLE IF NOT EXISTS model_projections(
                id TEXT PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL
            )""")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def reserve(self, binding, receipt):
        data = _encode(receipt)
        if receipt["status"] != "unknown":
            raise ValueError("model transport requires an unknown initial intent")
        with self._connection() as connection:
            connection.execute("INSERT INTO model_receipts VALUES(?,?,?,?,?)", (
                receipt["id"], _owner(binding), _encode(asdict(binding)), data, sha256(data).hexdigest(),
            ))

    def complete(self, binding, receipt):
        data = _encode(receipt)
        if receipt["status"] != "completed":
            raise ValueError("only an observed response may complete a model receipt")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM model_receipts WHERE id=?", (receipt["id"],)).fetchone()
            if (row is None or row["binding"] != _encode(asdict(binding))
                    or sha256(row["payload"]).hexdigest() != row["digest"]):
                raise ValueError("model transport observation ownership changed")
            old = json.loads(row["payload"])
            if old["status"] != "unknown":
                if old == receipt:
                    return
                raise ValueError("a completed physical model observation cannot change")
            if any(old.get(key) != receipt.get(key) for key in ("id", "engine", "model", "origin")):
                raise ValueError("model invocation identity changed")
            connection.execute("UPDATE model_receipts SET payload=?,digest=? WHERE id=?",
                               (data, sha256(data).hexdigest(), receipt["id"]))

    def commit_projections(self, binding, projections):
        from dataclasses import fields
        from tools.meter_cost import PerCallUsageLineItem

        names = {field.name for field in fields(PerCallUsageLineItem)}
        with self._connection() as connection:
            for identity, projection in projections.items():
                row = connection.execute("SELECT * FROM model_receipts WHERE id=? AND owner=?",
                                         (identity, _owner(binding))).fetchone()
                if row is None or sha256(row["payload"]).hexdigest() != row["digest"]:
                    raise ValueError("no physical model observation belongs to this projection")
                receipt = json.loads(row["payload"])
                if not isinstance(projection, dict) or set(projection) != names:
                    raise ValueError("invalid canonical model usage projection")
                if (projection["request_id"] != "permission-provider:" + identity
                        or projection["engine"] != receipt["engine"] or projection["model"] != receipt["model"]
                        or any(projection[name] != receipt[name] for name in (
                            "input_tokens", "output_tokens", "thinking_tokens", "token_source"))):
                    raise ValueError("usage projection changed physical model facts")
                data = _encode(projection)
                previous = connection.execute("SELECT * FROM model_projections WHERE id=?", (identity,)).fetchone()
                if previous is not None:
                    if previous["payload"] != data or sha256(data).hexdigest() != previous["digest"]:
                        raise ValueError("canonical model usage projection changed")
                else:
                    connection.execute("INSERT INTO model_projections VALUES(?,?,?)",
                                       (identity, data, sha256(data).hexdigest()))

    def observations(self, binding, *, origin=None):
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM model_receipts WHERE owner=? ORDER BY rowid",
                                      (_owner(binding),)).fetchall()
        result = []
        for row in rows:
            if sha256(row["payload"]).hexdigest() != row["digest"]:
                raise ValueError("model usage observations are damaged")
            receipt = json.loads(row["payload"])
            if row["id"] != receipt["id"]:
                raise ValueError("model usage invocation identity changed")
            if origin is None or receipt.get("origin", "host") == origin:
                with self._connection() as connection:
                    projection = connection.execute("SELECT * FROM model_projections WHERE id=?", (row["id"],)).fetchone()
                if projection is not None:
                    if sha256(projection["payload"]).hexdigest() != projection["digest"]:
                        raise ValueError("canonical model usage projection is damaged")
                    receipt["projection"] = json.loads(projection["payload"])
                result.append(receipt)
        return result
