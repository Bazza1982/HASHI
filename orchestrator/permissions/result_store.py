"""Encrypted operation result copies for a trusted permission service.

This store does not authenticate callers, supply its own key, install an ACL,
or replace the authority's result and resource checks. The parent directory and
the key must be controlled by the trusted service. A managed agent must not
receive this object or its key. Only the explicit service installation supplies
those protected objects.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from orchestrator.permissions.descriptors import ToolDescriptor
from orchestrator.permissions.execution_gate import SupervisedResult
from orchestrator.permissions.models import ExecutionContext, scope_digest


_SCHEMA = """CREATE TABLE encrypted_operation_results (
    result_key TEXT PRIMARY KEY,
    nonce BLOB NOT NULL UNIQUE CHECK(length(nonce)=12),
    ciphertext BLOB NOT NULL CHECK(length(ciphertext)>=16)
)"""
_VERSION = 1
_DOMAIN = b"hashi-permission-result-v1" + bytes([0])
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_PAYLOAD = 1024 * 1024
_MAX_CIPHERTEXT = 6 * _MAX_PAYLOAD + 8192


class ResultConflict(ValueError):
    """A committed operation already has a different result."""


class ResultUnavailable(ValueError):
    """The stored result cannot be authenticated or decoded."""


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class EncryptedResultStore:
    """Append-only authenticated ciphertext indexed by the original operation."""

    def __init__(self, path: str | Path, *, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise ValueError("a trusted 32-byte result key is required")
        self.path = Path(path)
        if not self.path.parent.is_dir() or self.path.is_symlink():
            raise ValueError("a trusted existing result directory is required")
        self._cipher = AESGCM(key)
        with self._transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if tables:
                    raise ValueError("refusing to initialize an unknown result database")
                connection.execute(_SCHEMA)
                connection.execute(f"PRAGMA user_version={_VERSION}")
            elif version != _VERSION:
                raise ValueError("unsupported result database version")
            self._check_schema(connection)

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != _VERSION:
            raise ValueError("unsupported result database version")
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='encrypted_operation_results'"
        ).fetchone()
        if schema is None or not isinstance(schema[0], str) or (
            "".join(schema[0].split()).casefold() != "".join(_SCHEMA.split()).casefold()
        ):
            raise ValueError("result database constraints are incomplete")

    @staticmethod
    def _fetch(connection: sqlite3.Connection, result_key: str) -> tuple[bytes, bytes] | None:
        size = connection.execute(
            "SELECT length(nonce),length(ciphertext) FROM encrypted_operation_results WHERE result_key=?",
            (result_key,),
        ).fetchone()
        if size is None:
            return None
        if (type(size[0]) is not int or size[0] != 12
                or type(size[1]) is not int or not 16 <= size[1] <= _MAX_CIPHERTEXT):
            raise ResultUnavailable("stored result size is invalid")
        row = connection.execute(
            "SELECT nonce,ciphertext FROM encrypted_operation_results WHERE result_key=?", (result_key,)
        ).fetchone()
        if row is None:
            raise ResultUnavailable("stored result is unavailable")
        return row

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _binding(context: ExecutionContext, operation_id: str, descriptor: ToolDescriptor) -> tuple[str, bytes, dict]:
        if not isinstance(context, ExecutionContext) or not isinstance(descriptor, ToolDescriptor):
            raise ValueError("trusted result context and descriptor are required")
        if (not isinstance(operation_id, str) or not operation_id.strip()
                or len(operation_id) > 512 or chr(0) in operation_id):
            raise ValueError("invalid operation identity")
        if (not isinstance(descriptor.tool_name, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", descriptor.tool_name) is None
                or not isinstance(descriptor.arguments_digest, str)
                or _DIGEST.fullmatch(descriptor.arguments_digest) is None):
            raise ValueError("invalid operation descriptor")
        scopes = scope_digest(descriptor.scopes)
        identity = {
            "instance": context.instance_id, "owner": context.owner_id, "agent": context.agent_id,
            "task": context.task_id, "run": context.run_id, "session": context.session_id,
            "operation": operation_id,
        }
        facts = {
            "operation": operation_id, "tool": descriptor.tool_name,
            "arguments_digest": descriptor.arguments_digest, "scope_digest": scopes,
        }
        result_key = sha256(_json(identity)).hexdigest()
        aad = _DOMAIN + _json({"identity": identity, "facts": facts})
        return result_key, aad, facts

    @staticmethod
    def _payload(facts: dict, outcome: str, payload: str) -> bytes:
        if not isinstance(outcome, str) or outcome not in {"succeeded", "failed"}:
            raise ValueError("only final operation results can be stored")
        if not isinstance(payload, str):
            raise ValueError("result payload must be text")
        try:
            encoded = payload.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("result payload is not valid text") from exc
        if len(encoded) > _MAX_PAYLOAD:
            raise ValueError("result payload is too large")
        return _json({"version": 1, **facts, "outcome": outcome, "payload": payload})

    def _decode(self, nonce: bytes, ciphertext: bytes, aad: bytes, facts: dict) -> SupervisedResult:
        try:
            if not isinstance(nonce, bytes) or len(nonce) != 12 or not isinstance(ciphertext, bytes):
                raise ValueError("invalid encrypted result")
            raw = self._cipher.decrypt(nonce, ciphertext, aad)
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {
                "version", "operation", "tool", "arguments_digest", "scope_digest", "outcome", "payload",
            }:
                raise ValueError("invalid result shape")
            if type(data["version"]) is not int or data["version"] != 1:
                raise ValueError("invalid result version")
            if any(data[name] != expected for name, expected in facts.items()):
                raise ValueError("result facts do not match")
            self._payload(facts, data["outcome"], data["payload"])
            digest = sha256(data["payload"].encode("utf-8")).hexdigest()
            return SupervisedResult(facts["operation"], facts["arguments_digest"], data["outcome"], digest, data["payload"])
        except (InvalidTag, ValueError, TypeError, UnicodeError, KeyError) as exc:
            raise ResultUnavailable("result could not be authenticated") from exc

    def put(
        self, context: ExecutionContext, operation_id: str, descriptor: ToolDescriptor, *, outcome: str, payload: str,
    ) -> SupervisedResult:
        result_key, aad, facts = self._binding(context, operation_id, descriptor)
        raw = self._payload(facts, outcome, payload)
        with self._transaction() as connection:
            self._check_schema(connection)
            row = self._fetch(connection, result_key)
            if row is not None:
                previous = self._decode(row[0], row[1], aad, facts)
                if previous.outcome != outcome or previous.payload != payload:
                    raise ResultConflict("operation already has a different result")
                return previous
            nonce = os.urandom(12)
            if (type(nonce) is not bytes or len(nonce) != 12
                    or connection.execute(
                        "SELECT 1 FROM encrypted_operation_results WHERE nonce=?", (nonce,)
                    ).fetchone() is not None):
                raise ResultUnavailable("a safe result nonce is unavailable")
            ciphertext = self._cipher.encrypt(nonce, raw, aad)
            try:
                connection.execute(
                    "INSERT INTO encrypted_operation_results VALUES(?,?,?)", (result_key, nonce, ciphertext)
                )
            except sqlite3.IntegrityError as exc:
                raise ResultUnavailable("result could not be stored safely") from exc
            return SupervisedResult(
                operation_id, descriptor.arguments_digest, outcome, sha256(payload.encode("utf-8")).hexdigest(), payload,
            )

    def get(
        self, context: ExecutionContext, operation_id: str, descriptor: ToolDescriptor,
    ) -> SupervisedResult | None:
        result_key, aad, facts = self._binding(context, operation_id, descriptor)
        with self._transaction() as connection:
            self._check_schema(connection)
            row = self._fetch(connection, result_key)
            if row is None:
                return None
            return self._decode(row[0], row[1], aad, facts)
