"""Lossless, append-only canonical audit evidence for HASHI PCM.

Operational logs remain sanitised derivatives.  This store deliberately keeps
complete payloads, moves large/binary values to content-addressed artifacts,
has no expiry path, and lives outside mutable Agent workspaces.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable
from uuid import uuid4


AUDIT_SCHEMA_VERSION: Final = 1
DEFAULT_ARTIFACT_THRESHOLD: Final = 64 * 1024
TAIL_READ_CHUNK_SIZE: Final = 64 * 1024


class CanonicalAuditAccessError(PermissionError):
    pass


class CanonicalAuditConfigurationError(RuntimeError):
    pass


def _safe_component(value: str, fallback: str) -> str:
    cleaned = "".join(
        character
        for character in str(value or "").strip()
        if character.isalnum() or character in {"-", "_", "."}
    )
    return fallback if cleaned in {"", ".", ".."} else cleaned


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class AuditAuthorization:
    actor: str
    purpose: str
    allow_raw_audit: bool

    @classmethod
    def parse(cls, value: Any) -> "AuditAuthorization":
        raw = value if isinstance(value, dict) else {}
        return cls(
            actor=str(raw.get("actor") or "").strip(),
            purpose=str(raw.get("purpose") or "").strip(),
            allow_raw_audit=raw.get("allow_raw_audit") is True,
        )

    def require(self) -> None:
        if not self.allow_raw_audit or not self.actor or not self.purpose:
            raise CanonicalAuditAccessError(
                "canonical raw audit requires explicit capability, actor, and purpose"
            )


class CanonicalAuditStore:
    """One Agent's permanent canonical evidence chain."""

    def __init__(
        self,
        bridge_home: str | Path,
        *,
        instance_id: str,
        agent_id: str,
        config: dict[str, Any] | None = None,
    ):
        self.bridge_home = Path(bridge_home)
        self.instance_id = _safe_component(instance_id, "HASHI")
        self.agent_id = _safe_component(agent_id, "agent")
        self.config = dict(config or {})
        configured_root = self.config.get("root")
        if configured_root:
            configured_path = Path(str(configured_root)).expanduser()
            base = (
                configured_path
                if configured_path.is_absolute()
                else self.bridge_home / configured_path
            )
        else:
            base = self.bridge_home / "state" / "canonical_audit"
        self.base = base
        self.instance_root = self.base / self.instance_id
        self.root = self.instance_root / self.agent_id
        self.artifacts_root = self.root / "artifacts"
        self.artifact_dir = self.root / "artifacts" / "sha256"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / ".chain.lock"
        self.artifact_threshold = max(
            1024,
            int(self.config.get("artifact_threshold_bytes") or DEFAULT_ARTIFACT_THRESHOLD),
        )
        self._lock = threading.RLock()
        self._key = self._load_encryption_key()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        # ``mkdir(parents=True)`` applies the process umask to intermediate
        # directories, which can leave an existing canonical root or the
        # instance/artifact namespace world-readable.  Tighten every
        # audit-owned directory on every open, including directories created
        # by an older HASHI build.  Do not chmod ``bridge_home`` or ``state``:
        # they contain unrelated operator-managed data.
        for private_directory in (
            self.base,
            self.instance_root,
            self.root,
            self.artifacts_root,
            self.artifact_dir,
        ):
            os.chmod(private_directory, 0o700)
        if self.events_path.exists():
            os.chmod(self.events_path, 0o600)

    @contextmanager
    def _chain_guard(self, *, exclusive: bool):
        """Serialise the digest chain across runtime and MCP subprocesses."""

        import fcntl

        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
            )
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @property
    def encrypted(self) -> bool:
        return self._key is not None

    def descriptor(self) -> dict[str, Any]:
        """Serializable gateway context without copying the encryption key."""

        return {
            "bridge_home": str(self.bridge_home),
            "instance_id": self.instance_id,
            "agent_id": self.agent_id,
            "config": dict(self.config),
        }

    def _load_encryption_key(self) -> bytes | None:
        key_env = str(
            self.config.get("encryption_key_env") or "HASHI_CANONICAL_AUDIT_KEY"
        ).strip()
        raw = os.environ.get(key_env, "").strip()
        required = self.config.get("encryption_required") is True
        if not raw:
            if required:
                raise CanonicalAuditConfigurationError(
                    f"canonical audit encryption key is required in {key_env}"
                )
            return None
        try:
            decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except Exception:
            decoded = b""
        return decoded if len(decoded) == 32 else hashlib.sha256(raw.encode("utf-8")).digest()

    def _aesgcm(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise CanonicalAuditConfigurationError(
                "cryptography is required when canonical audit encryption is configured"
            ) from exc
        return AESGCM(self._key)

    def _store_artifact(self, content: bytes, *, media_type: str) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        target_name = digest + (".aesgcm" if self._key is not None else "")
        target = self.artifact_dir / digest[:2] / target_name
        artifact_metadata: dict[str, Any] = {
            "algorithm": "sha256",
            "digest": digest,
            "size_bytes": len(content),
            "media_type": media_type,
            "relative_path": str(target.relative_to(self.root)),
        }
        stored_content = content
        if self._key is not None:
            nonce = hmac.new(
                self._key,
                b"HASHI-CANONICAL-ARTIFACT-v1\0" + bytes.fromhex(digest),
                hashlib.sha256,
            ).digest()[:12]
            stored_content = self._aesgcm().encrypt(
                nonce,
                content,
                b"HASHI-CANONICAL-ARTIFACT-v1",
            )
            artifact_metadata.update(
                {
                    "storage_encoding": "aes-256-gcm",
                    "nonce": base64.b64encode(nonce).decode("ascii"),
                }
            )
        else:
            artifact_metadata["storage_encoding"] = "raw"
        if not target.exists():
            _atomic_write_bytes(target, stored_content)
        return {"$artifact": artifact_metadata}

    def _externalize(self, value: Any) -> Any:
        if is_dataclass(value) and not isinstance(value, type):
            return self._externalize(asdict(value))
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bytes):
            return self._store_artifact(value, media_type="application/octet-stream")
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) > self.artifact_threshold:
                return self._store_artifact(encoded, media_type="text/plain; charset=utf-8")
            return value
        if isinstance(value, dict):
            return {str(key): self._externalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._externalize(item) for item in value]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return repr(value)

    def _last_digest(self) -> str:
        if not self.events_path.exists():
            return ""
        try:
            with self.events_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                position = handle.tell()
                tail = b""
                # A valid append always ends with a newline.  Walk backwards
                # only far enough to load that final record; the chain lock
                # keeps the tail stable while it is verified and extended.
                while position > 0:
                    read_size = min(TAIL_READ_CHUNK_SIZE, position)
                    position -= read_size
                    handle.seek(position)
                    tail = handle.read(read_size) + tail
                    candidate = tail.rstrip()
                    if not candidate:
                        continue
                    if b"\n" not in tail[len(candidate) :]:
                        raise CanonicalAuditConfigurationError(
                            "canonical audit chain tail is unreadable"
                        )
                    line_start = candidate.rfind(b"\n")
                    if line_start >= 0:
                        tail = candidate[line_start + 1 :]
                        break
                    if position == 0:
                        tail = candidate
                        break
                else:
                    return ""
            wrapper = json.loads(tail.decode("utf-8"))
            self._decode_record(wrapper)
            return str(wrapper.get("record_digest") or "")
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise CanonicalAuditConfigurationError(
                "canonical audit chain tail is unreadable"
            )

    def _encode_record(self, event: dict[str, Any]) -> dict[str, Any]:
        clear = json.dumps(
            event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if self._key is None:
            payload = {"encoding": "json", "event": event}
        else:
            nonce = os.urandom(12)
            ciphertext = self._aesgcm().encrypt(nonce, clear, b"HASHI-CANONICAL-AUDIT-v1")
            payload = {
                "encoding": "aes-256-gcm",
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            }
        payload["record_digest"] = hashlib.sha256(clear).hexdigest()
        return payload

    def _decode_record(self, wrapper: dict[str, Any]) -> dict[str, Any]:
        if wrapper.get("encoding") == "json":
            event = wrapper.get("event")
            if not isinstance(event, dict):
                raise CanonicalAuditConfigurationError("invalid canonical audit event")
        else:
            if wrapper.get("encoding") != "aes-256-gcm" or self._key is None:
                raise CanonicalAuditConfigurationError(
                    "canonical audit encryption key is unavailable or mismatched"
                )
            nonce = base64.b64decode(str(wrapper.get("nonce") or ""))
            ciphertext = base64.b64decode(str(wrapper.get("ciphertext") or ""))
            clear = self._aesgcm().decrypt(
                nonce, ciphertext, b"HASHI-CANONICAL-AUDIT-v1"
            )
            event = json.loads(clear.decode("utf-8"))
            if not isinstance(event, dict):
                raise CanonicalAuditConfigurationError("invalid canonical audit event")
        clear = json.dumps(
            event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        expected = hashlib.sha256(clear).hexdigest()
        actual = str(wrapper.get("record_digest") or "")
        if not actual or not hmac.compare_digest(actual, expected):
            raise CanonicalAuditConfigurationError(
                "canonical audit record digest mismatch"
            )
        return event

    def record(
        self,
        event_type: str,
        payload: Any,
        *,
        request_id: str = "",
        provenance: dict[str, Any] | None = None,
    ) -> str:
        event_id = str(uuid4())
        with self._lock:
            with self._chain_guard(exclusive=True):
                event = {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "event_id": event_id,
                    "event_type": str(event_type),
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "instance_id": self.instance_id,
                    "agent_id": self.agent_id,
                    "request_id": str(request_id or ""),
                    "previous_record_digest": self._last_digest(),
                    "provenance": self._externalize(dict(provenance or {})),
                    "payload": self._externalize(payload),
                }
                self._append_event_unlocked(event)
        return event_id

    def _append_event_unlocked(self, event: dict[str, Any]) -> None:
        wrapper = self._encode_record(event)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("ab") as handle:
            os.chmod(self.events_path, 0o600)
            line = json.dumps(
                wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_events_unlocked(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        events = []
        previous_digest = ""
        for line in self.events_path.read_bytes().splitlines():
            if line.strip():
                wrapper = json.loads(line.decode("utf-8"))
                event = self._decode_record(wrapper)
                if str(event.get("previous_record_digest") or "") != previous_digest:
                    raise CanonicalAuditConfigurationError(
                        "canonical audit chain linkage mismatch"
                    )
                events.append(event)
                previous_digest = str(wrapper.get("record_digest") or "")
        return events

    def read_events(self, authorization: Any) -> list[dict[str, Any]]:
        AuditAuthorization.parse(authorization).require()
        with self._lock:
            with self._chain_guard(exclusive=False):
                return self._read_events_unlocked()

    def read_artifact(self, reference: Any, authorization: Any) -> bytes:
        """Read and verify one authorised content-addressed artifact."""

        AuditAuthorization.parse(authorization).require()
        metadata = (
            reference.get("$artifact")
            if isinstance(reference, dict) and isinstance(reference.get("$artifact"), dict)
            else reference
        )
        if not isinstance(metadata, dict):
            raise CanonicalAuditAccessError("invalid canonical audit artifact reference")
        relative_path = str(metadata.get("relative_path") or "")
        candidate = (self.root / relative_path).resolve(strict=False)
        try:
            candidate.relative_to(self.artifact_dir.resolve(strict=False))
        except ValueError as exc:
            raise CanonicalAuditAccessError("artifact reference escapes canonical store") from exc
        stored = candidate.read_bytes()
        encoding = str(metadata.get("storage_encoding") or "raw")
        if encoding == "aes-256-gcm":
            if self._key is None:
                raise CanonicalAuditConfigurationError(
                    "canonical artifact encryption key is unavailable"
                )
            nonce = base64.b64decode(str(metadata.get("nonce") or ""))
            content = self._aesgcm().decrypt(
                nonce,
                stored,
                b"HASHI-CANONICAL-ARTIFACT-v1",
            )
        elif encoding == "raw":
            content = stored
        else:
            raise CanonicalAuditConfigurationError(
                f"unsupported canonical artifact encoding: {encoding}"
            )
        if hashlib.sha256(content).hexdigest() != str(metadata.get("digest") or ""):
            raise CanonicalAuditConfigurationError("canonical artifact digest mismatch")
        return content

    @staticmethod
    def _artifact_paths(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            metadata = value.get("$artifact")
            if isinstance(metadata, dict) and metadata.get("relative_path"):
                found.add(str(metadata["relative_path"]))
            for item in value.values():
                found.update(CanonicalAuditStore._artifact_paths(item))
        elif isinstance(value, list):
            for item in value:
                found.update(CanonicalAuditStore._artifact_paths(item))
        return found

    def audit_wipe(
        self,
        *,
        authorization: Any,
        confirmation: str,
        event_ids: Iterable[str] | None = None,
    ) -> int:
        auth = AuditAuthorization.parse(authorization)
        auth.require()
        expected = f"DELETE CANONICAL AUDIT {self.instance_id}/{self.agent_id}"
        if confirmation != expected:
            raise CanonicalAuditAccessError("canonical audit wipe confirmation mismatch")
        selected = {str(value) for value in (event_ids or []) if str(value)}
        with self._lock:
            with self._chain_guard(exclusive=True):
                events = self._read_events_unlocked()
                if selected:
                    removed = [event for event in events if event.get("event_id") in selected]
                    kept = [event for event in events if event.get("event_id") not in selected]
                else:
                    removed, kept = events, []

                wrappers = []
                previous_digest = ""
                for event in kept:
                    event["previous_record_digest"] = previous_digest
                    wrapper = self._encode_record(event)
                    wrappers.append(wrapper)
                    previous_digest = str(wrapper["record_digest"])
                content = b"".join(
                    json.dumps(
                        wrapper,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                    for wrapper in wrappers
                )
                _atomic_write_bytes(self.events_path, content)

                removed_artifacts = set().union(
                    *(self._artifact_paths(event) for event in removed)
                ) if removed else set()
                kept_artifacts = set().union(
                    *(self._artifact_paths(event) for event in kept)
                ) if kept else set()
                for relative_path in sorted(removed_artifacts - kept_artifacts):
                    candidate = (self.root / relative_path).resolve(strict=False)
                    try:
                        candidate.relative_to(self.artifact_dir.resolve(strict=False))
                    except ValueError:
                        continue
                    candidate.unlink(missing_ok=True)
                    try:
                        candidate.parent.rmdir()
                    except OSError:
                        pass

                wipe_event = {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "event_id": str(uuid4()),
                    "event_type": "audit_wipe",
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "instance_id": self.instance_id,
                    "agent_id": self.agent_id,
                    "request_id": "",
                    "previous_record_digest": self._last_digest(),
                    "provenance": {"explicit_confirmation": True},
                    "payload": {
                        "deleted_event_ids": [event.get("event_id") for event in removed],
                        "deleted_count": len(removed),
                        "actor": auth.actor,
                        "purpose": auth.purpose,
                    },
                }
                self._append_event_unlocked(wipe_event)
                return len(removed)
