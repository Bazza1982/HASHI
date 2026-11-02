"""Small, immutable PAO artifact-provenance contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Mapping

SCHEMA = 1
POLICY_KEY = "fingerprint_policy"


def canonical_json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class FingerprintPolicy:
    enabled: bool = False
    marking: str = "auto"
    export_content: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.export_content) is not bool:
            raise ValueError("fingerprint switches must be booleans")
        if self.marking != "auto":
            raise ValueError("only automatic fingerprint marking is supported")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) - {"enabled", "marking", "export_content"}:
            raise ValueError("fingerprint policy must contain supported settings only")
        return cls(**value)

    @property
    def version(self) -> str:
        return hashlib.sha256(canonical_json(asdict(self))).hexdigest()

    def snapshot(self) -> dict:
        return {"schema": SCHEMA, **asdict(self), "version": self.version}

    @classmethod
    def from_snapshot(cls, value):
        # Old persisted Runs have no feature snapshot and must remain OFF.
        if value == {} or value is None:
            return cls()
        if not isinstance(value, dict) or set(value) != {"schema", "enabled", "marking", "export_content", "version"}:
            raise ValueError("invalid frozen fingerprint policy")
        policy = cls.from_mapping({key: value[key] for key in ("enabled", "marking", "export_content")})
        if type(value["schema"]) is not int or value["schema"] != SCHEMA or value["version"] != policy.version:
            raise ValueError("fingerprint policy version mismatch")
        return policy


@dataclass(frozen=True)
class PolicySnapshot:
    policy: FingerprintPolicy
    revision: str


@dataclass(frozen=True)
class RunIdentity:
    instance_id: str
    agent_id: str
    session_id: str
    run_id: str
    request_id: str

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in asdict(self).values()):
            raise ValueError("complete trusted HASHI Run identity is required")


@dataclass(frozen=True)
class FileVersion:
    state: str = "unknown"
    sha256: str | None = None
    size: int | None = None

    def __post_init__(self):
        if self.state not in {"present", "absent", "unknown", "conflict"}:
            raise ValueError("invalid file observation state")
        if self.sha256 is not None and (len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256)):
            raise ValueError("invalid exact-byte SHA256")
        if self.state == "present" and self.sha256 is None:
            raise ValueError("present version requires its exact hash")
        if self.state == "absent" and self.sha256 is not None:
            raise ValueError("absent file cannot have a hash")
        if self.size is not None and (type(self.size) is not int or self.size < 0):
            raise ValueError("invalid byte count")


def relative_target(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("relative artifact path required")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or PureWindowsPath(value).drive or any(p in {"", ".", ".."} for p in normalized.split("/")):
        raise ValueError("artifact path must stay within its declared root")
    return path.as_posix()
