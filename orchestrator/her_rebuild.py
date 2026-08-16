from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_PACKAGE = "rusty-claude-cli"
DEFAULT_BINARY_NAME = "claw"
DEFAULT_PROFILE = "hashi-dev"
DEFAULT_BUILD_TIMEOUT_SECONDS = 30 * 60
DEFAULT_MAX_LOG_BYTES = 20 * 1024 * 1024
DEFAULT_SUMMARY_BYTES = 96 * 1024

_SOURCE_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".idea",
        ".vscode",
        "candidates",
        "logs",
        "target",
        "targets",
    }
)
_SOURCE_EXCLUDED_SUFFIXES = (
    ".bak",
    ".orig",
    ".pyc",
    ".swp",
    ".tmp",
    "~",
)
_ENV_ALLOWLIST = frozenset(
    {
        "AR",
        "CARGO_HOME",
        "CC",
        "COMSPEC",
        "CXX",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "PATH",
        "PATHEXT",
        "PKG_CONFIG_PATH",
        "PROCESSOR_ARCHITECTURE",
        "RUSTUP_HOME",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
    }
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:token|secret|password|passwd|api[_-]?key|authorization)[a-z0-9_-]*)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_ACTIONABLE_LINE_RE = re.compile(
    r"(?i)(^error(?:\[[^]]+\])?:|^error:|failed to|caused by:|linking with .* failed|could not compile)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RebuildStage(str, Enum):
    ACCEPTED = "accepted"
    SOURCE_PREFLIGHT = "source_preflight"
    WAITING_FOR_BUILD_LOCK = "waiting_for_build_lock"
    BUILDING = "building"
    VERIFYING = "verifying"
    CANDIDATE_READY = "candidate_ready"
    WAITING_FOR_AGENT_IDLE = "waiting_for_agent_idle"
    ACTIVATING = "activating"
    REBOOT_REQUESTED = "reboot_requested"
    ADOPTING = "adopting"
    POSTCHECK = "postcheck"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ACTIVATION_DEFERRED = "activation_deferred"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


class FailureKind(str, Enum):
    SOURCE_MISSING = "source_missing"
    SOURCE_INVALID = "source_invalid"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    TOOLCHAIN_MISSING = "toolchain_missing"
    TOOLCHAIN_MISMATCH = "toolchain_mismatch"
    BUILD_LOCK_BUSY = "build_lock_busy"
    STALE_LOCK_UNRECOVERABLE = "stale_lock_unrecoverable"
    FINGERPRINT_FAILED = "fingerprint_failed"
    CARGO_TIMEOUT = "cargo_timeout"
    CARGO_FAILED = "cargo_failed"
    CANDIDATE_MISSING = "candidate_missing"
    CANDIDATE_DIGEST_MISMATCH = "candidate_digest_mismatch"
    CANDIDATE_IDENTITY_MISMATCH = "candidate_identity_mismatch"
    QUICK_TEST_FAILED = "quick_test_failed"
    VERSION_PROBE_FAILED = "version_probe_failed"
    STREAM_JSON_PROBE_FAILED = "stream_json_probe_failed"
    SESSION_PROBE_FAILED = "session_probe_failed"
    GATEWAY_PROBE_FAILED = "gateway_probe_failed"
    ACTIVATION_DEFERRED = "activation_deferred"
    SELECTION_WRITE_FAILED = "selection_write_failed"
    REBOOT_REJECTED = "reboot_rejected"
    AGENT_RESTART_FAILED = "agent_restart_failed"
    ADAPTER_INITIALIZATION_FAILED = "adapter_initialization_failed"
    POSTCHECK_IDENTITY_MISMATCH = "postcheck_identity_mismatch"
    POSTCHECK_HEALTH_FAILED = "postcheck_health_failed"
    ROLLBACK_SELECTION_FAILED = "rollback_selection_failed"
    ROLLBACK_RESTART_FAILED = "rollback_restart_failed"
    NOTIFICATION_FAILED = "notification_failed"
    INTERNAL_ERROR = "internal_error"


class HERRebuildError(RuntimeError):
    def __init__(
        self,
        failure_kind: FailureKind,
        stage: RebuildStage,
        message: str,
        *,
        exit_code: int | None = None,
        diagnostics: str | None = None,
    ):
        super().__init__(message)
        self.failure_kind = failure_kind
        self.stage = stage
        self.exit_code = exit_code
        self.diagnostics = diagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_kind": self.failure_kind.value,
            "stage": self.stage.value,
            "message": str(self),
            "exit_code": self.exit_code,
            "diagnostics": self.diagnostics,
        }


@dataclass(frozen=True)
class HERSourceLayout:
    source_root: Path
    rust_root: Path
    license_path: Path
    provenance_path: Path
    cargo_manifest: Path
    cargo_lock: Path
    package: str = DEFAULT_PACKAGE
    binary_name: str = DEFAULT_BINARY_NAME
    profile: str = DEFAULT_PROFILE
    features: tuple[str, ...] = ()

    @classmethod
    def from_code_root(
        cls,
        code_root: Path,
        *,
        package: str = DEFAULT_PACKAGE,
        binary_name: str = DEFAULT_BINARY_NAME,
        profile: str = DEFAULT_PROFILE,
        features: Sequence[str] = (),
    ) -> HERSourceLayout:
        resolved_code_root = Path(code_root).resolve()
        source_root = (resolved_code_root / "native" / "her").resolve()
        if not source_root.is_relative_to(resolved_code_root):
            raise HERRebuildError(
                FailureKind.SOURCE_INVALID,
                RebuildStage.SOURCE_PREFLIGHT,
                "Canonical HER source root escapes the HASHI code root.",
            )
        rust_root = source_root / "rust"
        return cls(
            source_root=source_root,
            rust_root=rust_root,
            license_path=source_root / "LICENSE",
            provenance_path=source_root / "UPSTREAM_SOURCE.json",
            cargo_manifest=rust_root / "Cargo.toml",
            cargo_lock=rust_root / "Cargo.lock",
            package=package,
            binary_name=binary_name,
            profile=profile,
            features=tuple(sorted(set(features))),
        )


@dataclass(frozen=True)
class ToolchainIdentity:
    cargo_path: str
    cargo_version: str
    rustc_path: str
    rustc_version: str


@dataclass(frozen=True)
class GitSourceState:
    head: str | None
    dirty: bool


@dataclass(frozen=True)
class SourceFingerprint:
    digest: str
    git_head: str | None
    dirty: bool
    file_count: int
    source_bytes: int
    target: str
    profile: str
    features: tuple[str, ...]
    cargo_version: str
    rustc_version: str

    @property
    def short_identity(self) -> str:
        head = (self.git_head or "no-git")[:12]
        dirty = "-dirty" if self.dirty else ""
        return f"{head}{dirty}-{self.digest[:16]}-{self.target}-{self.profile}"


@dataclass(frozen=True)
class BuildArtifact:
    job_id: str
    fingerprint: SourceFingerprint
    binary_path: Path
    build_log_path: Path
    build_started_at: str
    build_finished_at: str
    build_duration_seconds: float
    cargo_argv: tuple[str, ...]
    diagnostics: str
    log_truncated: bool


@dataclass(frozen=True)
class CandidateMetadata:
    schema_version: int
    candidate_id: str
    job_id: str
    development_build: bool
    production_certified: bool
    source_fingerprint: str
    source_git_head: str | None
    source_dirty: bool
    target: str
    profile: str
    features: tuple[str, ...]
    cargo_version: str
    rustc_version: str
    build_started_at: str
    build_finished_at: str
    build_duration_seconds: float
    binary_name: str
    binary_sha256: str
    binary_size: int
    candidate_dir: str
    binary_path: str
    build_log_path: str
    quick_verification: Mapping[str, Any]
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["features"] = list(self.features)
        payload["quick_verification"] = dict(self.quick_verification)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CandidateMetadata:
        return cls(
            schema_version=int(payload["schema_version"]),
            candidate_id=str(payload["candidate_id"]),
            job_id=str(payload["job_id"]),
            development_build=bool(payload["development_build"]),
            production_certified=bool(payload["production_certified"]),
            source_fingerprint=str(payload["source_fingerprint"]),
            source_git_head=(
                str(payload["source_git_head"])
                if payload.get("source_git_head") is not None
                else None
            ),
            source_dirty=bool(payload["source_dirty"]),
            target=str(payload["target"]),
            profile=str(payload["profile"]),
            features=tuple(str(item) for item in payload.get("features", [])),
            cargo_version=str(payload["cargo_version"]),
            rustc_version=str(payload["rustc_version"]),
            build_started_at=str(payload["build_started_at"]),
            build_finished_at=str(payload["build_finished_at"]),
            build_duration_seconds=float(payload["build_duration_seconds"]),
            binary_name=str(payload["binary_name"]),
            binary_sha256=str(payload["binary_sha256"]),
            binary_size=int(payload["binary_size"]),
            candidate_dir=str(payload["candidate_dir"]),
            binary_path=str(payload["binary_path"]),
            build_log_path=str(payload["build_log_path"]),
            quick_verification=dict(payload.get("quick_verification", {})),
            created_at=str(payload["created_at"]),
        )


def detect_host_target(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    system_name = (system or platform.system()).strip().lower()
    machine_name = (machine or platform.machine()).strip().lower()
    is_x64 = machine_name in {"amd64", "x86_64", "x64"}
    if system_name == "linux" and is_x64:
        return "x86_64-unknown-linux-gnu"
    if system_name == "windows" and is_x64:
        return "x86_64-pc-windows-msvc"
    raise HERRebuildError(
        FailureKind.UNSUPPORTED_PLATFORM,
        RebuildStage.SOURCE_PREFLIGHT,
        f"HER development rebuild is unavailable on {system_name or '?'} / {machine_name or '?'}.",
    )


def preflight_source(layout: HERSourceLayout) -> None:
    required_files = {
        "MIT license": layout.license_path,
        "source provenance": layout.provenance_path,
        "Cargo workspace manifest": layout.cargo_manifest,
        "Cargo lockfile": layout.cargo_lock,
    }
    missing = []
    if not layout.source_root.is_dir():
        missing.append("integrated source root")
    missing.extend(
        label for label, path in required_files.items() if not path.is_file()
    )
    if missing:
        kind = (
            FailureKind.SOURCE_MISSING
            if not layout.source_root.exists()
            else FailureKind.SOURCE_INVALID
        )
        raise HERRebuildError(
            kind,
            RebuildStage.SOURCE_PREFLIGHT,
            "Integrated HER source is unavailable or incomplete: " + ", ".join(missing) + ".",
        )

    try:
        provenance = json.loads(layout.provenance_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HERRebuildError(
            FailureKind.SOURCE_INVALID,
            RebuildStage.SOURCE_PREFLIGHT,
            "HER source provenance is not valid JSON.",
        ) from exc
    if not isinstance(provenance, dict) or not provenance:
        raise HERRebuildError(
            FailureKind.SOURCE_INVALID,
            RebuildStage.SOURCE_PREFLIGHT,
            "HER source provenance must be a non-empty JSON object.",
        )

    try:
        cargo_text = layout.cargo_manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise HERRebuildError(
            FailureKind.SOURCE_INVALID,
            RebuildStage.SOURCE_PREFLIGHT,
            "HER Cargo workspace manifest could not be read.",
        ) from exc
    if "[workspace]" not in cargo_text:
        raise HERRebuildError(
            FailureKind.SOURCE_INVALID,
            RebuildStage.SOURCE_PREFLIGHT,
            "HER Cargo.toml is not a workspace manifest.",
        )
    if f"[profile.{layout.profile}]" not in cargo_text:
        raise HERRebuildError(
            FailureKind.SOURCE_INVALID,
            RebuildStage.SOURCE_PREFLIGHT,
            f"HER Cargo.toml does not define the required {layout.profile!r} development profile.",
        )

    source_root = layout.source_root.resolve()
    for current_root, dir_names, file_names in os.walk(source_root):
        current = Path(current_root)
        relevant_dirs = [
            name
            for name in dir_names
            if name not in _SOURCE_EXCLUDED_DIRS and not name.startswith(".tmp-")
        ]
        relevant_files = [
            name
            for name in file_names
            if not name.endswith(_SOURCE_EXCLUDED_SUFFIXES)
            and not name.startswith(".tmp-")
        ]
        for name in (*relevant_dirs, *relevant_files):
            path = current / name
            if path.is_symlink():
                raise HERRebuildError(
                    FailureKind.SOURCE_INVALID,
                    RebuildStage.SOURCE_PREFLIGHT,
                    "HER integrated source contains a symbolic link: "
                    f"{path.relative_to(source_root).as_posix()}.",
                )
        dir_names[:] = sorted(relevant_dirs)


def _iter_source_files(layout: HERSourceLayout) -> Iterable[Path]:
    root = layout.source_root.resolve()
    if not root.exists():
        return
    for current_root, dir_names, file_names in os.walk(root):
        dir_names[:] = sorted(
            name
            for name in dir_names
            if name not in _SOURCE_EXCLUDED_DIRS and not name.startswith(".tmp-")
        )
        current = Path(current_root)
        for file_name in sorted(file_names):
            if file_name.endswith(_SOURCE_EXCLUDED_SUFFIXES) or file_name.startswith(".tmp-"):
                continue
            yield current / file_name


def discover_git_source_state(source_root: Path) -> GitSourceState:
    root = Path(source_root).resolve()
    try:
        head_run = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        status_run = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--", "."],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return GitSourceState(head=None, dirty=True)
    head = head_run.stdout.strip() if head_run.returncode == 0 else None
    dirty = status_run.returncode != 0 or bool(status_run.stdout.strip())
    return GitSourceState(head=head or None, dirty=dirty)


def compute_source_fingerprint(
    layout: HERSourceLayout,
    *,
    toolchain: ToolchainIdentity,
    target: str,
    git_state: GitSourceState | None = None,
) -> SourceFingerprint:
    preflight_source(layout)
    source_root = layout.source_root.resolve()
    git = git_state or discover_git_source_state(source_root)
    digest = hashlib.sha256()
    header = {
        "schema_version": 1,
        "git_head": git.head,
        "git_dirty": git.dirty,
        "target": target,
        "profile": layout.profile,
        "features": list(layout.features),
        "cargo_version": toolchain.cargo_version,
        "rustc_version": toolchain.rustc_version,
    }
    digest.update(json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\0")

    file_count = 0
    source_bytes = 0
    try:
        for path in _iter_source_files(layout):
            relative = path.relative_to(source_root).as_posix()
            data = path.read_bytes()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(len(data)).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
            file_count += 1
            source_bytes += len(data)
    except (OSError, ValueError) as exc:
        raise HERRebuildError(
            FailureKind.FINGERPRINT_FAILED,
            RebuildStage.SOURCE_PREFLIGHT,
            f"Could not fingerprint integrated HER source: {type(exc).__name__}: {exc}",
        ) from exc

    return SourceFingerprint(
        digest=digest.hexdigest(),
        git_head=git.head,
        dirty=git.dirty,
        file_count=file_count,
        source_bytes=source_bytes,
        target=target,
        profile=layout.profile,
        features=layout.features,
        cargo_version=toolchain.cargo_version,
        rustc_version=toolchain.rustc_version,
    )


def build_cargo_environment(
    base_environment: Mapping[str, str] | None = None,
    *,
    cargo_target_dir: Path,
) -> dict[str, str]:
    clean = _allowlisted_environment(base_environment)
    clean["CARGO_TARGET_DIR"] = str(Path(cargo_target_dir).resolve())
    clean["CARGO_INCREMENTAL"] = "1"
    clean["RUST_BACKTRACE"] = "1"
    return clean


def _allowlisted_environment(
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = base_environment if base_environment is not None else os.environ
    allowed = {key.upper() for key in _ENV_ALLOWLIST}
    return {
        key.upper(): str(value)
        for key, value in source.items()
        if key.upper() in allowed and str(value)
    }


def cargo_build_argv(
    layout: HERSourceLayout,
    *,
    cargo_executable: str,
    target: str,
) -> tuple[str, ...]:
    argv = [
        cargo_executable,
        "build",
        "--locked",
        "--profile",
        layout.profile,
        "--package",
        layout.package,
        "--target",
        target,
    ]
    if layout.features:
        argv.extend(["--features", ",".join(layout.features)])
    return tuple(argv)


def redact_diagnostics(text: str, *, limit: int = 4000) -> str:
    cleaned = text.replace("\x00", "")
    cleaned = _BEARER_TOKEN_RE.sub("Bearer <redacted>", cleaned)
    cleaned = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines() if line.strip()]
    actionable = [line for line in lines if _ACTIONABLE_LINE_RE.search(line.strip())]
    selected = actionable[:16] if actionable else lines[-24:]
    bounded = "\n".join(selected).strip()
    if len(bounded) <= limit:
        return bounded
    head = max(1, limit // 2)
    tail = max(1, limit - head - 42)
    return bounded[:head].rstrip() + "\n…[build diagnostics truncated]…\n" + bounded[-tail:].lstrip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            temporary.chmod(mode)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            os.close(descriptor)


async def _run_version_command(
    executable: str,
    *,
    environment: Mapping[str, str],
    timeout_seconds: float = 10,
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=dict(environment),
        )
    except FileNotFoundError as exc:
        raise HERRebuildError(
            FailureKind.TOOLCHAIN_MISSING,
            RebuildStage.SOURCE_PREFLIGHT,
            f"Required Rust toolchain executable is unavailable: {executable}.",
        ) from exc
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise HERRebuildError(
            FailureKind.TOOLCHAIN_MISMATCH,
            RebuildStage.SOURCE_PREFLIGHT,
            f"Rust toolchain probe timed out: {executable}.",
        ) from exc
    rendered = output.decode("utf-8", errors="replace").strip()
    if process.returncode != 0 or not rendered:
        raise HERRebuildError(
            FailureKind.TOOLCHAIN_MISMATCH,
            RebuildStage.SOURCE_PREFLIGHT,
            f"Rust toolchain probe failed: {executable}.",
            exit_code=process.returncode,
            diagnostics=redact_diagnostics(rendered),
        )
    return rendered.splitlines()[0].strip()


async def inspect_toolchain(
    *,
    environment: Mapping[str, str] | None = None,
    cargo_executable: str = "cargo",
    rustc_executable: str = "rustc",
) -> ToolchainIdentity:
    clean_environment = _allowlisted_environment(environment)
    search_path = clean_environment.get("PATH")
    cargo_path = shutil.which(cargo_executable, path=search_path)
    rustc_path = shutil.which(rustc_executable, path=search_path)
    if not cargo_path or not rustc_path:
        missing = []
        if not cargo_path:
            missing.append("cargo")
        if not rustc_path:
            missing.append("rustc")
        raise HERRebuildError(
            FailureKind.TOOLCHAIN_MISSING,
            RebuildStage.SOURCE_PREFLIGHT,
            "Required Rust toolchain executable(s) unavailable: " + ", ".join(missing) + ".",
        )
    cargo_version, rustc_version = await asyncio.gather(
        _run_version_command(cargo_path, environment=clean_environment),
        _run_version_command(rustc_path, environment=clean_environment),
    )
    return ToolchainIdentity(
        cargo_path=cargo_path,
        cargo_version=cargo_version,
        rustc_path=rustc_path,
        rustc_version=rustc_version,
    )


class HERBuildController:
    def __init__(
        self,
        layout: HERSourceLayout,
        *,
        state_root: Path,
        build_timeout_seconds: float = DEFAULT_BUILD_TIMEOUT_SECONDS,
        max_log_bytes: int = DEFAULT_MAX_LOG_BYTES,
        summary_bytes: int = DEFAULT_SUMMARY_BYTES,
    ):
        self.layout = layout
        self.state_root = Path(state_root).resolve()
        self.build_timeout_seconds = max(1.0, float(build_timeout_seconds))
        self.max_log_bytes = max(1024, int(max_log_bytes))
        self.summary_bytes = max(1024, int(summary_bytes))

    @property
    def cargo_target_dir(self) -> Path:
        return self.state_root / "cargo-target"

    @property
    def logs_dir(self) -> Path:
        return self.state_root / "logs"

    def expected_binary_path(self, *, target: str) -> Path:
        suffix = ".exe" if target.endswith("windows-msvc") else ""
        return self.cargo_target_dir / target / self.layout.profile / f"{self.layout.binary_name}{suffix}"

    async def build(
        self,
        *,
        job_id: str,
        fingerprint: SourceFingerprint,
        toolchain: ToolchainIdentity,
        environment: Mapping[str, str] | None = None,
    ) -> BuildArtifact:
        preflight_source(self.layout)
        before_build = compute_source_fingerprint(
            self.layout,
            toolchain=toolchain,
            target=fingerprint.target,
        )
        if before_build.digest != fingerprint.digest:
            raise HERRebuildError(
                FailureKind.FINGERPRINT_FAILED,
                RebuildStage.BUILDING,
                "HER source changed after the rebuild fingerprint was accepted; submit a new rebuild.",
            )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.cargo_target_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"{job_id}.build.log"
        argv = cargo_build_argv(
            self.layout,
            cargo_executable=toolchain.cargo_path,
            target=fingerprint.target,
        )
        cargo_environment = build_cargo_environment(
            environment,
            cargo_target_dir=self.cargo_target_dir,
        )
        start_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            start_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            start_kwargs["start_new_session"] = True

        started_wall = utc_now()
        started_monotonic = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(self.layout.rust_root),
                env=cargo_environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **start_kwargs,
            )
        except FileNotFoundError as exc:
            raise HERRebuildError(
                FailureKind.TOOLCHAIN_MISSING,
                RebuildStage.BUILDING,
                f"Cargo executable is unavailable: {toolchain.cargo_path}.",
            ) from exc
        except OSError as exc:
            raise HERRebuildError(
                FailureKind.CARGO_FAILED,
                RebuildStage.BUILDING,
                f"Cargo could not be started: {type(exc).__name__}: {exc}",
            ) from exc

        tail = bytearray()
        log_truncated = False
        stored_bytes = 0

        async def _drain_output() -> None:
            nonlocal log_truncated, stored_bytes
            assert process.stdout is not None
            with log_path.open("wb") as log_handle:
                while True:
                    chunk = await process.stdout.read(64 * 1024)
                    if not chunk:
                        break
                    tail.extend(chunk)
                    if len(tail) > self.summary_bytes:
                        del tail[: len(tail) - self.summary_bytes]
                    remaining = self.max_log_bytes - stored_bytes
                    if remaining > 0:
                        written = chunk[:remaining]
                        log_handle.write(written)
                        stored_bytes += len(written)
                    if len(chunk) > max(0, remaining):
                        log_truncated = True
                if log_truncated:
                    marker = b"\n[HASHI: build log reached configured size ceiling]\n"
                    if stored_bytes + len(marker) <= self.max_log_bytes:
                        log_handle.write(marker)
                log_handle.flush()
                os.fsync(log_handle.fileno())

        reader_task = asyncio.create_task(_drain_output())
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout=self.build_timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate_process_tree(process)
        except asyncio.CancelledError:
            await self._terminate_process_tree(process)
            await reader_task
            raise
        finally:
            if not reader_task.done():
                await reader_task

        finished_wall = utc_now()
        duration = max(0.0, time.monotonic() - started_monotonic)
        diagnostics = redact_diagnostics(tail.decode("utf-8", errors="replace"))
        if timed_out:
            raise HERRebuildError(
                FailureKind.CARGO_TIMEOUT,
                RebuildStage.BUILDING,
                f"Cargo build exceeded the {self.build_timeout_seconds:g}-second timeout.",
                exit_code=process.returncode,
                diagnostics=diagnostics,
            )
        if process.returncode != 0:
            raise HERRebuildError(
                FailureKind.CARGO_FAILED,
                RebuildStage.BUILDING,
                f"Cargo build failed with exit code {process.returncode}.",
                exit_code=process.returncode,
                diagnostics=diagnostics,
            )

        after_build = compute_source_fingerprint(
            self.layout,
            toolchain=toolchain,
            target=fingerprint.target,
        )
        if after_build.digest != fingerprint.digest:
            raise HERRebuildError(
                FailureKind.FINGERPRINT_FAILED,
                RebuildStage.BUILDING,
                "HER source changed while Cargo was building; the uncorrelated output was rejected.",
                diagnostics=diagnostics,
            )

        binary_path = self.expected_binary_path(target=fingerprint.target)
        if not binary_path.is_file():
            raise HERRebuildError(
                FailureKind.CANDIDATE_MISSING,
                RebuildStage.BUILDING,
                "Cargo reported success but the expected HER executable was not produced.",
                diagnostics=diagnostics,
            )
        return BuildArtifact(
            job_id=job_id,
            fingerprint=fingerprint,
            binary_path=binary_path,
            build_log_path=log_path,
            build_started_at=started_wall,
            build_finished_at=finished_wall,
            build_duration_seconds=duration,
            cargo_argv=argv,
            diagnostics=diagnostics,
            log_truncated=log_truncated,
        )

    async def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=3)
            return
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        if process.returncode is None:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


class CandidateStore:
    def __init__(self, candidates_root: Path):
        self.root = Path(candidates_root).resolve()

    def stage(
        self,
        artifact: BuildArtifact,
        *,
        toolchain: ToolchainIdentity,
        quick_verification: Mapping[str, Any],
    ) -> CandidateMetadata:
        if str(quick_verification.get("result", "")).strip().lower() != "passed":
            raise HERRebuildError(
                FailureKind.QUICK_TEST_FAILED,
                RebuildStage.VERIFYING,
                "HER candidate cannot be staged before mandatory quick verification passes.",
            )
        source_binary = artifact.binary_path
        if not source_binary.is_file():
            raise HERRebuildError(
                FailureKind.CANDIDATE_MISSING,
                RebuildStage.VERIFYING,
                "Built HER executable disappeared before candidate staging.",
            )
        binary_digest = sha256_file(source_binary)
        candidate_id = f"dev-{artifact.fingerprint.digest[:16]}-{binary_digest[:12]}"
        candidate_dir = self.root / candidate_id
        suffix = source_binary.suffix if source_binary.suffix.lower() == ".exe" else ""
        binary_name = f"claw{suffix}"
        final_binary = candidate_dir / binary_name
        final_build_log = candidate_dir / "build.log"

        if candidate_dir.exists():
            existing = self.read(candidate_id)
            if existing.binary_sha256 != binary_digest:
                raise HERRebuildError(
                    FailureKind.CANDIDATE_DIGEST_MISMATCH,
                    RebuildStage.VERIFYING,
                    "Existing immutable HER candidate has an unexpected digest.",
                )
            return existing

        self.root.mkdir(parents=True, exist_ok=True)
        temporary_dir = self.root / f".{candidate_id}.tmp-{os.getpid()}-{time.time_ns()}"
        temporary_dir.mkdir(mode=0o700)
        try:
            temporary_binary = temporary_dir / binary_name
            shutil.copyfile(source_binary, temporary_binary)
            if sha256_file(temporary_binary) != binary_digest:
                raise HERRebuildError(
                    FailureKind.CANDIDATE_DIGEST_MISMATCH,
                    RebuildStage.VERIFYING,
                    "HER candidate digest changed while copying from Cargo output.",
                )
            if os.name != "nt":
                temporary_binary.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
            if not artifact.build_log_path.is_file():
                raise HERRebuildError(
                    FailureKind.CANDIDATE_MISSING,
                    RebuildStage.VERIFYING,
                    "HER build log disappeared before candidate staging.",
                )
            shutil.copyfile(artifact.build_log_path, temporary_dir / "build.log")
            _atomic_write_json(
                temporary_dir / "quick-verification.json",
                dict(quick_verification),
                mode=0o440,
            )

            metadata = CandidateMetadata(
                schema_version=1,
                candidate_id=candidate_id,
                job_id=artifact.job_id,
                development_build=True,
                production_certified=False,
                source_fingerprint=artifact.fingerprint.digest,
                source_git_head=artifact.fingerprint.git_head,
                source_dirty=artifact.fingerprint.dirty,
                target=artifact.fingerprint.target,
                profile=artifact.fingerprint.profile,
                features=artifact.fingerprint.features,
                cargo_version=toolchain.cargo_version,
                rustc_version=toolchain.rustc_version,
                build_started_at=artifact.build_started_at,
                build_finished_at=artifact.build_finished_at,
                build_duration_seconds=artifact.build_duration_seconds,
                binary_name=binary_name,
                binary_sha256=binary_digest,
                binary_size=temporary_binary.stat().st_size,
                candidate_dir=str(candidate_dir),
                binary_path=str(final_binary),
                build_log_path=str(final_build_log),
                quick_verification=dict(quick_verification),
                created_at=utc_now(),
            )
            _atomic_write_json(
                temporary_dir / "candidate.json",
                metadata.to_dict(),
                mode=0o440,
            )
            if os.name != "nt":
                (temporary_dir / "build.log").chmod(0o440)
            try:
                os.replace(temporary_dir, candidate_dir)
                _fsync_directory(self.root)
            except FileExistsError:
                existing = self.read(candidate_id)
                if existing.binary_sha256 != binary_digest:
                    raise HERRebuildError(
                        FailureKind.CANDIDATE_DIGEST_MISMATCH,
                        RebuildStage.VERIFYING,
                        "Concurrent HER candidate staging produced a digest conflict.",
                    )
                return existing
            if os.name != "nt":
                candidate_dir.chmod(0o550)
            return metadata
        finally:
            if temporary_dir.exists():
                shutil.rmtree(temporary_dir, ignore_errors=True)

    def read(self, candidate_id: str) -> CandidateMetadata:
        candidate_dir = (self.root / candidate_id).resolve()
        if not candidate_dir.is_relative_to(self.root):
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.VERIFYING,
                "HER candidate identity escapes the approved candidate root.",
            )
        metadata_path = candidate_dir / "candidate.json"
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HERRebuildError(
                FailureKind.CANDIDATE_MISSING,
                RebuildStage.VERIFYING,
                f"HER candidate metadata is unavailable: {candidate_id}.",
            ) from exc
        metadata = CandidateMetadata.from_dict(payload)
        if metadata.candidate_id != candidate_id:
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.VERIFYING,
                "HER candidate metadata identity does not match its directory.",
            )
        if Path(metadata.candidate_dir).resolve() != candidate_dir:
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.VERIFYING,
                "HER candidate metadata directory does not match its immutable directory.",
            )
        if not metadata.development_build or metadata.production_certified:
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.VERIFYING,
                "HER development candidate has invalid certification identity metadata.",
            )
        binary_path = Path(metadata.binary_path).resolve()
        if not binary_path.is_relative_to(candidate_dir) or not binary_path.is_file():
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.VERIFYING,
                "HER candidate executable is outside its immutable directory or missing.",
            )
        if os.name != "nt" and not os.access(binary_path, os.X_OK):
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.VERIFYING,
                "HER candidate executable is not executable.",
            )
        if sha256_file(binary_path) != metadata.binary_sha256:
            raise HERRebuildError(
                FailureKind.CANDIDATE_DIGEST_MISMATCH,
                RebuildStage.VERIFYING,
                "HER candidate executable digest does not match candidate metadata.",
            )
        build_log_path = Path(metadata.build_log_path).resolve()
        if not build_log_path.is_relative_to(candidate_dir) or not build_log_path.is_file():
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.VERIFYING,
                "HER candidate build log is outside its immutable directory or missing.",
            )
        try:
            quick_payload = json.loads(
                (candidate_dir / "quick-verification.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise HERRebuildError(
                FailureKind.QUICK_TEST_FAILED,
                RebuildStage.VERIFYING,
                "HER candidate quick-verification evidence is missing or invalid.",
            ) from exc
        if quick_payload != dict(metadata.quick_verification) or str(
            quick_payload.get("result", "")
        ).lower() != "passed":
            raise HERRebuildError(
                FailureKind.QUICK_TEST_FAILED,
                RebuildStage.VERIFYING,
                "HER candidate quick-verification evidence does not match candidate metadata.",
            )
        return metadata


class DevelopmentSelectionStore:
    def __init__(self, selection_path: Path, *, candidates_root: Path):
        self.selection_path = Path(selection_path).resolve()
        self.candidates = CandidateStore(candidates_root)

    def read(self) -> dict[str, Any] | None:
        if not self.selection_path.exists():
            return None
        try:
            payload = json.loads(self.selection_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.ACTIVATING,
                "HER development selection record is unreadable.",
            ) from exc
        if not isinstance(payload, dict) or int(payload.get("schema_version", 0)) != 1:
            raise HERRebuildError(
                FailureKind.CANDIDATE_IDENTITY_MISMATCH,
                RebuildStage.ACTIVATING,
                "HER development selection record has an unsupported schema.",
            )
        return payload

    def select(self, candidate_id: str, *, job_id: str) -> dict[str, Any]:
        candidate = self.candidates.read(candidate_id)
        previous = self.read()
        previous_active = previous.get("active") if previous else None
        payload = {
            "schema_version": 1,
            "active": {
                "candidate_id": candidate.candidate_id,
                "candidate_path": candidate.candidate_dir,
                "binary_path": candidate.binary_path,
                "binary_sha256": candidate.binary_sha256,
                "source_fingerprint": candidate.source_fingerprint,
                "target": candidate.target,
                "profile": candidate.profile,
                "development_build": True,
                "production_certified": False,
            },
            "previous": previous_active,
            "selected_at": utc_now(),
            "selecting_job_id": job_id,
            "adoption_state": "selected_not_yet_adopted",
        }
        try:
            _atomic_write_json(self.selection_path, payload)
        except OSError as exc:
            raise HERRebuildError(
                FailureKind.SELECTION_WRITE_FAILED,
                RebuildStage.ACTIVATING,
                f"Could not atomically select HER development candidate: {type(exc).__name__}: {exc}",
            ) from exc
        return payload

    def restore_previous(self, *, job_id: str) -> dict[str, Any] | None:
        current = self.read()
        if current is None:
            return None
        previous = current.get("previous")
        if previous is not None:
            try:
                validated = self.candidates.read(str(previous["candidate_id"]))
            except (KeyError, HERRebuildError) as exc:
                raise HERRebuildError(
                    FailureKind.ROLLBACK_SELECTION_FAILED,
                    RebuildStage.ROLLING_BACK,
                    "The previous HER development candidate failed rollback validation.",
                ) from exc
            if (
                previous.get("binary_sha256") != validated.binary_sha256
                or previous.get("binary_path") != validated.binary_path
            ):
                raise HERRebuildError(
                    FailureKind.ROLLBACK_SELECTION_FAILED,
                    RebuildStage.ROLLING_BACK,
                    "The previous HER selection record does not match its immutable candidate.",
                )
        payload = {
            "schema_version": 1,
            "active": previous,
            "previous": current.get("active"),
            "selected_at": utc_now(),
            "selecting_job_id": job_id,
            "adoption_state": "rollback_selected",
        }
        try:
            _atomic_write_json(self.selection_path, payload)
        except OSError as exc:
            raise HERRebuildError(
                FailureKind.ROLLBACK_SELECTION_FAILED,
                RebuildStage.ROLLING_BACK,
                f"Could not restore the previous HER development selection: {type(exc).__name__}: {exc}",
            ) from exc
        return payload
