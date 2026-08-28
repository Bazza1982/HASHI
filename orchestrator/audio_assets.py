"""Provider-neutral native audio asset storage.

Raw audio lives only in private files beneath a server-owned root.  Durable
Messages and Events carry the returned opaque metadata, never bytes, Base64,
or caller-controlled paths.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import threading
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


MIN_RETENTION_SECONDS = 60
DEFAULT_RETENTION_SECONDS = 3600
MAX_AUDIO_ASSET_BYTES = 64 * 1024 * 1024
SUPPORTED_AUDIO_FORMATS = frozenset(
    {"wav", "mp3", "ogg", "opus", "webm", "flac", "m4a", "mp4"}
)


class AudioAssetError(RuntimeError):
    code = "audio_asset_error"


class AudioAssetNotFound(AudioAssetError):
    code = "audio_asset_not_found"


class AudioAssetUnauthorized(AudioAssetError):
    code = "audio_asset_unauthorized"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def normalize_audio_format(value: Any, *, mime_type: str = "") -> str:
    aliases = {
        "wave": "wav",
        "x-wav": "wav",
        "mpeg": "mp3",
        "mpeg3": "mp3",
        "x-m4a": "m4a",
        "oga": "ogg",
    }
    normalized = str(value or "").strip().casefold().lstrip(".")
    if not normalized and "/" in str(mime_type or ""):
        normalized = str(mime_type).split(";", 1)[0].rsplit("/", 1)[-1]
    normalized = aliases.get(normalized, normalized)
    if normalized not in SUPPORTED_AUDIO_FORMATS:
        raise AudioAssetError(f"unsupported audio format {normalized!r}")
    return normalized


def validate_audio_signature(payload: bytes, audio_format: str) -> None:
    """Reject obvious MIME/format mismatches at the trusted byte boundary."""

    signatures = {
        "wav": lambda data: len(data) >= 12
        and data[:4] == b"RIFF"
        and data[8:12] == b"WAVE",
        "ogg": lambda data: data.startswith(b"OggS"),
        "opus": lambda data: data.startswith(b"OggS"),
        "flac": lambda data: data.startswith(b"fLaC"),
        "webm": lambda data: data.startswith(bytes.fromhex("1a45dfa3")),
        "mp3": lambda data: data.startswith(b"ID3")
        or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0),
        "m4a": lambda data: len(data) >= 12 and data[4:8] == b"ftyp",
        "mp4": lambda data: len(data) >= 12 and data[4:8] == b"ftyp",
    }
    if not signatures[audio_format](payload):
        raise AudioAssetError(
            f"audio bytes do not match declared {audio_format} format"
        )


def probe_audio_duration_ms(payload: bytes, audio_format: str) -> int | None:
    """Best-effort duration probe without persisting another media copy."""

    if audio_format == "wav":
        try:
            with wave.open(io.BytesIO(payload), "rb") as source:
                rate = int(source.getframerate() or 0)
                frames = int(source.getnframes() or 0)
            if rate > 0:
                return max(0, round(frames * 1000 / rate))
        except (EOFError, wave.Error):
            return None
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                "-i",
                "pipe:0",
            ],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
        seconds = float(completed.stdout.decode("ascii", errors="ignore").strip())
        if completed.returncode == 0 and seconds >= 0:
            return round(seconds * 1000)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None


class AudioAssetStore:
    """Private, restart-safe storage for input and generated audio assets."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.files_root = self.root / "files"
        self.metadata_root = self.root / "metadata"
        self.files_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.metadata_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(self.files_root, 0o700)
            os.chmod(self.metadata_root, 0o700)
        except OSError:
            pass
        self._lock = threading.RLock()

    @staticmethod
    def _safe_id(asset_id: str) -> str:
        value = str(asset_id or "").strip()
        if not value or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in value
        ):
            raise AudioAssetError("invalid audio asset id")
        return value

    def _metadata_path(self, asset_id: str) -> Path:
        return self.metadata_root / f"{self._safe_id(asset_id)}.json"

    def _read(self, asset_id: str) -> dict[str, Any]:
        path = self._metadata_path(asset_id)
        try:
            decoded = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            raise AudioAssetNotFound(str(asset_id)) from exc
        if not isinstance(decoded, Mapping):
            raise AudioAssetNotFound(str(asset_id))
        return dict(decoded)

    def _write(self, metadata: Mapping[str, Any]) -> None:
        asset_id = self._safe_id(str(metadata.get("asset_id") or ""))
        target = self._metadata_path(asset_id)
        partial = target.with_suffix(f".{uuid4().hex}.partial")
        partial.write_text(
            json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        try:
            os.chmod(partial, 0o600)
        except OSError:
            pass
        partial.replace(target)

    @staticmethod
    def _public(metadata: Mapping[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in metadata.items()
            if key not in {"storage_name", "owner_id"}
        }

    def create(
        self,
        payload: bytes,
        *,
        owner_id: str,
        session_id: str,
        direction: str,
        mime_type: str,
        audio_format: str,
        asset_id: str | None = None,
        filename: str = "",
        duration_ms: int | None = None,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        retention_indefinite: bool = False,
        correlation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, bytes) or not payload:
            raise AudioAssetError("audio payload must contain bytes")
        if len(payload) > MAX_AUDIO_ASSET_BYTES:
            raise AudioAssetError("audio payload exceeds the configured limit")
        normalized_format = normalize_audio_format(audio_format, mime_type=mime_type)
        validate_audio_signature(payload, normalized_format)
        if not str(mime_type or "").split(";", 1)[0].casefold().startswith("audio/"):
            raise AudioAssetError("audio assets require an audio MIME type")
        if direction not in {"input", "output", "normalized"}:
            raise AudioAssetError("invalid audio asset direction")
        if duration_ms is None:
            duration_ms = probe_audio_duration_ms(payload, normalized_format)
        if duration_ms is not None and int(duration_ms) < 0:
            raise AudioAssetError("duration_ms must be non-negative")
        if not retention_indefinite and int(retention_seconds) < MIN_RETENTION_SECONDS:
            raise AudioAssetError("audio retention must be at least 60 seconds")

        opaque_id = self._safe_id(asset_id or f"media_{uuid4().hex}")
        digest = hashlib.sha256(payload).hexdigest()
        created = _utc_now()
        storage_name = f"{opaque_id}.{normalized_format}"
        target = self.files_root / storage_name
        partial = target.with_suffix(f".{uuid4().hex}.partial")
        metadata = {
            "asset_id": opaque_id,
            "owner_id": str(owner_id),
            "session_id": str(session_id),
            "direction": direction,
            "filename": str(filename or storage_name),
            "mime_type": str(mime_type).split(";", 1)[0].strip().casefold(),
            "format": normalized_format,
            "duration_ms": int(duration_ms) if duration_ms is not None else None,
            "size_bytes": len(payload),
            "sha256": digest,
            "created_at": _iso(created),
            "retention_seconds": None
            if retention_indefinite
            else int(retention_seconds),
            "retention_expires_at": None
            if retention_indefinite
            else _iso(created + timedelta(seconds=int(retention_seconds))),
            "retention_indefinite": bool(retention_indefinite),
            "lease_count": 0,
            "state": "available",
            "storage_name": storage_name,
            "correlation": dict(correlation or {}),
        }
        with self._lock:
            if target.exists() or self._metadata_path(opaque_id).exists():
                raise AudioAssetError("audio asset id already exists")
            try:
                partial.write_bytes(payload)
                try:
                    os.chmod(partial, 0o600)
                except OSError:
                    pass
                partial.replace(target)
                self._write(metadata)
            except Exception:
                partial.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                self._metadata_path(opaque_id).unlink(missing_ok=True)
                raise
        return self._public(metadata)

    def _authorize(
        self, metadata: Mapping[str, Any], *, owner_id: str, session_id: str
    ) -> None:
        if (
            str(metadata.get("owner_id")) != str(owner_id)
            or str(metadata.get("session_id")) != str(session_id)
        ):
            raise AudioAssetUnauthorized(str(metadata.get("asset_id") or ""))
        if metadata.get("state") != "available":
            raise AudioAssetNotFound(str(metadata.get("asset_id") or ""))

    def describe(
        self, asset_id: str, *, owner_id: str, session_id: str
    ) -> dict[str, Any]:
        with self._lock:
            metadata = self._read(asset_id)
            self._authorize(metadata, owner_id=owner_id, session_id=session_id)
            return self._public(metadata)

    def read_bytes(
        self, asset_id: str, *, owner_id: str, session_id: str
    ) -> tuple[dict[str, Any], bytes]:
        with self._lock:
            metadata = self._read(asset_id)
            self._authorize(metadata, owner_id=owner_id, session_id=session_id)
            path = self.files_root / str(metadata["storage_name"])
            try:
                payload = path.read_bytes()
            except OSError as exc:
                raise AudioAssetNotFound(asset_id) from exc
            if (
                len(payload) != int(metadata["size_bytes"])
                or hashlib.sha256(payload).hexdigest() != metadata["sha256"]
            ):
                raise AudioAssetError("stored audio asset failed integrity validation")
            return self._public(metadata), payload

    def authorized_path(
        self, asset_id: str, *, owner_id: str, session_id: str
    ) -> tuple[dict[str, Any], Path]:
        """Resolve an internal provider path after owner/Session authorization."""

        with self._lock:
            metadata = self._read(asset_id)
            self._authorize(metadata, owner_id=owner_id, session_id=session_id)
            path = (self.files_root / str(metadata["storage_name"])).resolve()
            if path.parent != self.files_root or not path.is_file():
                raise AudioAssetNotFound(asset_id)
            return self._public(metadata), path

    def acquire(
        self, asset_id: str, *, owner_id: str, session_id: str
    ) -> dict[str, Any]:
        with self._lock:
            metadata = self._read(asset_id)
            self._authorize(metadata, owner_id=owner_id, session_id=session_id)
            metadata["lease_count"] = int(metadata.get("lease_count") or 0) + 1
            self._write(metadata)
            return self._public(metadata)

    def release(
        self, asset_id: str, *, owner_id: str, session_id: str
    ) -> dict[str, Any]:
        with self._lock:
            metadata = self._read(asset_id)
            self._authorize(metadata, owner_id=owner_id, session_id=session_id)
            metadata["lease_count"] = max(
                0, int(metadata.get("lease_count") or 0) - 1
            )
            self._write(metadata)
            return self._public(metadata)

    def set_indefinite(
        self, asset_id: str, *, owner_id: str, session_id: str
    ) -> dict[str, Any]:
        with self._lock:
            metadata = self._read(asset_id)
            self._authorize(metadata, owner_id=owner_id, session_id=session_id)
            metadata["retention_indefinite"] = True
            metadata["retention_seconds"] = None
            metadata["retention_expires_at"] = None
            self._write(metadata)
            return self._public(metadata)

    def claim(
        self,
        asset_id: str,
        *,
        owner_id: str,
        session_id: str,
        request_id: str = "",
    ) -> dict[str, Any]:
        """Bind a provider-created output asset to its authoritative Session."""

        with self._lock:
            metadata = self._read(asset_id)
            current_owner = str(metadata.get("owner_id") or "")
            current_session = str(metadata.get("session_id") or "")
            if current_owner or current_session:
                self._authorize(
                    metadata, owner_id=owner_id, session_id=session_id
                )
                return self._public(metadata)
            correlation = dict(metadata.get("correlation") or {})
            expected_request = str(correlation.get("request_id") or "")
            if request_id and expected_request and request_id != expected_request:
                raise AudioAssetUnauthorized(asset_id)
            metadata["owner_id"] = str(owner_id)
            metadata["session_id"] = str(session_id)
            self._write(metadata)
            return self._public(metadata)

    def cleanup(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        current = now or _utc_now()
        expired: list[dict[str, Any]] = []
        with self._lock:
            for metadata_path in sorted(self.metadata_root.glob("*.json")):
                try:
                    metadata = self._read(metadata_path.stem)
                except AudioAssetNotFound:
                    continue
                expiry = metadata.get("retention_expires_at")
                if (
                    metadata.get("state") != "available"
                    or metadata.get("retention_indefinite")
                    or int(metadata.get("lease_count") or 0) > 0
                    or not expiry
                    or _parse_iso(str(expiry)) > current
                ):
                    continue
                (self.files_root / str(metadata.get("storage_name") or "")).unlink(
                    missing_ok=True
                )
                metadata["state"] = "expired"
                metadata["expired_at"] = _iso(current)
                self._write(metadata)
                expired.append(self._public(metadata))
            for partial_root in (self.files_root, self.metadata_root):
                for partial in partial_root.glob("*.partial"):
                    try:
                        if datetime.fromtimestamp(
                            partial.stat().st_mtime, tz=timezone.utc
                        ) + timedelta(hours=1) <= current:
                            partial.unlink(missing_ok=True)
                    except OSError:
                        continue
        return expired


def asset_root_from_global_config(global_config: Any) -> Path:
    bridge_home = getattr(global_config, "bridge_home", None)
    if bridge_home:
        return Path(bridge_home) / "state" / "native_audio_assets"
    base_media = getattr(global_config, "base_media_dir", None)
    if base_media:
        return Path(base_media) / "native_audio_assets"
    project_root = getattr(global_config, "project_root", None)
    if project_root:
        return Path(project_root) / "state" / "native_audio_assets"
    raise AudioAssetError("native audio asset root is not configured")


__all__ = [
    "AudioAssetError",
    "AudioAssetNotFound",
    "AudioAssetStore",
    "AudioAssetUnauthorized",
    "DEFAULT_RETENTION_SECONDS",
    "MIN_RETENTION_SECONDS",
    "asset_root_from_global_config",
    "normalize_audio_format",
    "probe_audio_duration_ms",
    "validate_audio_signature",
]
