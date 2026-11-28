"""Default-off, read-only LAN diagnostics log gate for HASHI Remote.

Purpose
-------
A remote trouble-shooter (another machine on the same LAN, or a diagnostics
Agent) must be able to read *all* instance logs without first setting up
pairing, tokens or TLS.  The only boundary is an explicit switch:

* off by default, always, on every Remote start;
* the switch can only be flipped *from the local machine* (loopback);
* while it is on, any loopback / RFC1918 client may list and read the full
  log tree, read-only, with no further authentication;
* it closes itself after a TTL (60 minutes by default) so a forgotten
  switch cannot stay open forever.

Deliberate non-goals
--------------------
No encryption, no extra access-control list, no login flow.  This mirrors the
operator's instruction: "æ‰“å¼€å³å®Œæ•´åªè¯»æš´éœ²å®žä¾‹å…¨éƒ¨æ—¥å¿—ï¼ˆä¸åŠ å¯†ã€ä¸æžå¤æ‚é‰´æƒï¼‰".

Safety bottom line
------------------
* Only GET/HEAD; nothing here writes, renames or deletes anything.
* Every path is canonicalised and must stay inside the declared log root, so
  ``..`` segments, absolute paths and symlinks/junctions that escape the root
  are rejected before any file is opened.

The module is import-light (fastapi only) so the gate and the path rules can
be unit tested without booting a Remote.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

#: TTL used when the operator does not pass one.  Sixty minutes is long enough
#: for a real debugging session and short enough to survive being forgotten.
DEFAULT_TTL_SECONDS = 60 * 60

#: Guard rails for silly values coming off a query string or JSON body.
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 24 * 60 * 60

#: Streaming chunk size for log bodies.  Large enough to be fast, small enough
#: to keep a multi-hundred-megabyte log from ever being buffered in memory.
CHUNK_SIZE = 64 * 1024

#: Upper bound on a single ``GET /diag/logs`` listing.
MAX_LISTED_FILES = 5000

#: RFC1918 private ranges.  Anything else (public, CGNAT, link-local,
#: documentation ranges) is *not* allowed to read logs, per the task card.
_RFC1918_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)


# ---------------------------------------------------------------------------
# Log roots
# ---------------------------------------------------------------------------

#: Roots the gate can serve.  ``instance`` is the instance's *effective* log
#: directory (``global.base_logs_dir``), which is where Core, Bridge, HER audit
#: and agent session logs actually land; ``remote`` is the Remote process' own
#: log directory, which sits outside that tree when ``--log-file`` points
#: somewhere else.
LOG_ROOT_INSTANCE = "instance"
LOG_ROOT_REMOTE = "remote"

#: Prefix ``orchestrator.pathing`` accepts for "relative to the bridge home".
HOME_PREFIX = "@home/"

#: A rotated copy of a log file: ``name.1``, ``name.2026-11-28``,
#: ``name.2026-11-28.1.gz`` and friends.  Anything else next to the log (a
#: ``config.json``, a lock file, an unrelated log) is never exposed.
_ROTATION_SUFFIX = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d+)(?:\.\d+)?(?:\.gz)?$")


@dataclass(frozen=True)
class LogFileSet:
    """A root that exposes named log files plus their rotated copies only.

    Used for the Remote's own ``--log-file``: handing its *directory* over
    exposed every unrelated file the operator happened to keep next to it.
    """

    directory: Path
    names: tuple[str, ...]

    def allows(self, name: str) -> bool:
        for base in self.names:
            if name == base:
                return True
            prefix = base + "."
            if name.startswith(prefix) and _ROTATION_SUFFIX.match(name[len(prefix):]):
                return True
        return False


def _expand_config_path(value: object, *, config_dir: Path, bridge_home: Path) -> Optional[Path]:
    """Resolve a configured path the same way ``orchestrator.pathing`` does.

    Returns ``None`` for a blank value, matching ``resolve_path_value``; the
    caller then applies ``orchestrator.config``'s own ``base_logs_dir``
    fallback.  (A blank value used to resolve to ``config_dir`` — the instance
    root — and that widened the read path to ``agents.json``/``secrets.json``.)
    """

    raw = os.path.expanduser(os.path.expandvars(str(value if value is not None else "").strip()))
    if not raw.strip():
        return None
    normalized = raw.replace("\\", "/")
    if normalized.startswith(HOME_PREFIX):
        return (bridge_home / Path(normalized[len(HOME_PREFIX):])).resolve()
    candidate = Path(normalized)
    if candidate.is_absolute():
        return candidate.resolve()
    return (config_dir / candidate).resolve()


def _reuse_official_resolution(
    value: object,
    *,
    config_dir: Path,
    bridge_home: Path,
) -> Optional[Path]:
    """Resolve through the Core's own resolver so both sides cannot drift."""

    try:
        from orchestrator.pathing import resolve_path_value
    except Exception:  # pragma: no cover - standalone import of this module
        return _expand_config_path(value, config_dir=config_dir, bridge_home=bridge_home)
    try:
        return resolve_path_value(value, config_dir=config_dir, bridge_home=bridge_home)
    except (OSError, ValueError):
        return _expand_config_path(value, config_dir=config_dir, bridge_home=bridge_home)


def _is_unsafe_root(resolved: Path, instance_root: Path) -> bool:
    """Reject a log root that would expose the instance itself or its parents."""

    if resolved == instance_root:
        return True
    # A parent of the instance root (``C:\``, ``C:\Users\thene``, a bare ``..``)
    # would publish agents.json, secrets.json and every sibling project.
    try:
        instance_root.relative_to(resolved)
    except ValueError:
        return False
    return True


def resolve_effective_logs_dir(hashi_root: Optional[Path]) -> Optional[Path]:
    """Return the instance's effective log directory.

    Instances may point ``global.base_logs_dir`` at any directory; the Remote
    must serve the same directory the Core writes to, otherwise Bridge, HER
    audit and agent session logs stay invisible over the LAN even though the
    switch is open.  The value is resolved with the Core's own resolver and the
    same ``or ("<home>/logs")`` fallback ``orchestrator.config`` applies, so a
    missing, blank or unusable value can never widen the read path.
    """

    if not hashi_root:
        return None
    root = Path(hashi_root).resolve()
    configured: object = "logs"
    try:
        raw = json.loads((root / "agents.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raw = None
    if isinstance(raw, dict):
        global_section = raw.get("global")
        if isinstance(global_section, dict) and "base_logs_dir" in global_section:
            configured = global_section.get("base_logs_dir")
    default_dir = (root / "logs").resolve()
    try:
        resolved = _reuse_official_resolution(
            configured, config_dir=root, bridge_home=root
        )
    except (OSError, ValueError):
        resolved = None
    if resolved is None or _is_unsafe_root(resolved, root):
        resolved = default_dir
    # ``<instance>/logs`` is resolved through junctions and symlinks too.  An
    # instance whose ``logs`` entry is a junction pointing back at the instance
    # root (or at one of its parents) would otherwise be published verbatim as
    # the log root, exposing agents.json/secrets.json.  Validate the directory
    # that is actually returned, not just the configured value.
    if _is_unsafe_root(resolved, root):
        return None
    return resolved


def build_diag_log_roots(
    hashi_root: Optional[Path],
    *,
    remote_log_file: Optional[Path] = None,
) -> dict[str, Path | LogFileSet]:
    """Build the labelled roots the diagnostics gate may serve.

    The instance root follows the instance's own configuration.  The Remote's
    own log is added as a *file set* — that file and its rotated copies — so a
    ``--log-file`` pointing into a shared directory no longer publishes the
    whole directory.  It is skipped when it already sits under the instance
    log tree, which keeps the same file from being listed twice.
    """

    roots: dict[str, Path | LogFileSet] = {}
    instance_root = resolve_effective_logs_dir(hashi_root)
    if instance_root is not None:
        roots[LOG_ROOT_INSTANCE] = instance_root
    if remote_log_file:
        try:
            remote_path = Path(remote_log_file).expanduser().resolve()
        except OSError:
            return roots
        parent = remote_path.parent
        if instance_root is not None:
            try:
                parent.relative_to(instance_root)
            except ValueError:
                pass
            else:
                return roots
        roots[LOG_ROOT_REMOTE] = LogFileSet(directory=parent, names=(remote_path.name,))
    return roots


def _normalise_log_roots(
    log_root: Optional[Path],
    log_roots: Optional[Mapping[str, Optional[Path | LogFileSet]]],
) -> dict[str, Path | LogFileSet]:
    roots: dict[str, Path | LogFileSet] = {}
    if log_roots:
        for label, path in log_roots.items():
            if isinstance(path, LogFileSet):
                roots[str(label)] = path
            elif path:
                roots[str(label)] = Path(path)
    if not roots and log_root:
        roots[LOG_ROOT_INSTANCE] = Path(log_root)
    return roots


def _root_directory(root: Path | LogFileSet) -> Path:
    return root.directory if isinstance(root, LogFileSet) else Path(root)


def _root_text(root: Path | LogFileSet) -> str:
    return str(_root_directory(root))


class DiagLogError(Exception):
    """Raised when a requested log path or range is not allowed."""

    def __init__(self, reason: str, *, status_code: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Client scope
# ---------------------------------------------------------------------------


def _parse_ip(host: Optional[str]) -> Optional[ipaddress._BaseAddress]:
    text = str(host or "").strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    # Starlette reports the bare host, but tolerate "host:port" shapes that
    # some proxies produce rather than silently widening the scope.
    if text.count(":") == 1 and "." in text:
        text = text.split(":", 1)[0]
    text = text.split("%", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def classify_client_host(host: Optional[str]) -> str:
    """Classify a client address as ``loopback``, ``lan``, ``public`` or ``unknown``.

    Only ``loopback`` and ``lan`` (RFC1918) may read logs.  ``unknown`` means the
    address could not be parsed, and is treated as untrusted by the callers.
    """

    address = _parse_ip(host)
    if address is None:
        return "unknown"

    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped

    if isinstance(address, ipaddress.IPv4Address):
        if address in ipaddress.IPv4Network("127.0.0.0/8"):
            return "loopback"
        if any(address in network for network in _RFC1918_NETWORKS):
            return "lan"
        return "public"

    if address.is_loopback:
        return "loopback"
    return "public"


# ---------------------------------------------------------------------------
# The switch
# ---------------------------------------------------------------------------


class DiagLogsGate:
    """In-memory, default-off switch guarding the read-only log endpoints.

    The state is intentionally *not* persisted: every Remote start begins with
    the gate closed.  A diagnostics session therefore cannot survive a restart,
    which is the safe failure mode for an unauthenticated read path.
    """

    def __init__(
        self,
        log_root: Optional[Path] = None,
        *,
        log_roots: Optional[Mapping[str, Optional[Path]]] = None,
        instance_id: str = "",
        default_ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now=time.time,
    ):
        self._log_roots = _normalise_log_roots(log_root, log_roots)
        # ``instance`` is routinely absent: the instance log dir is refused
        # when it would expose the instance itself, leaving only the Remote's
        # own log.  Indexing it here aborted Remote start-up with KeyError,
        # turning a safety check into an outage.
        self._log_root = self._log_roots.get(LOG_ROOT_INSTANCE)
        self._instance_id = str(instance_id or "")
        self._default_ttl_seconds = int(default_ttl_seconds)
        self._now = now
        self._lock = threading.Lock()
        self._enabled = False
        self._opened_at: Optional[float] = None
        self._expires_at: Optional[float] = None
        self._ttl_seconds: Optional[int] = None
        self._last_close_reason: Optional[str] = None
        # Bumped on every open and every close.  A stream remembers the value
        # it started under, so closing and re-opening the switch cannot resume
        # a download that belonged to the previous window.
        self._generation = 0

    # -- state ---------------------------------------------------------

    @property
    def log_root(self) -> Optional[Path]:
        """Primary (instance) log directory, kept for single-root callers."""

        return self._log_root

    @property
    def log_roots(self) -> dict[str, Path | LogFileSet]:
        """Every root this gate may serve, in listing order."""

        return dict(self._log_roots)

    def _expire_locked(self) -> None:
        if not self._enabled or self._expires_at is None:
            return
        if self._now() >= self._expires_at:
            self._enabled = False
            self._last_close_reason = "ttl_expired"
            self._opened_at = None
            self._expires_at = None
            self._ttl_seconds = None
            self._generation += 1

    def _status_locked(self) -> dict:
        now = self._now()
        remaining = None
        if self._enabled and self._expires_at is not None:
            remaining = max(0, int(round(self._expires_at - now)))
        return {
            "enabled": bool(self._enabled),
            "instance_id": self._instance_id,
            "log_root": str(self._log_root) if self._log_root else None,
            "log_root_exists": bool(self._log_root and self._log_root.is_dir()),
            "log_roots": {
                label: _root_text(root)
                for label, root in self._log_roots.items()
            },
            "opened_at": self._opened_at,
            "expires_at": self._expires_at,
            "ttl_seconds": self._ttl_seconds,
            "remaining_seconds": remaining,
            "default_ttl_seconds": self._default_ttl_seconds,
            "last_close_reason": self._last_close_reason,
        }

    def status(self) -> dict:
        with self._lock:
            self._expire_locked()
            return self._status_locked()

    def is_open(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._enabled

    def open(self, ttl_seconds: Optional[int] = None) -> dict:
        ttl = self._default_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        ttl = max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, ttl))
        with self._lock:
            now = self._now()
            self._enabled = True
            self._opened_at = now
            self._expires_at = now + ttl
            self._ttl_seconds = ttl
            self._last_close_reason = None
            self._generation += 1
            return self._status_locked()

    def close(self, *, reason: str = "manual") -> dict:
        with self._lock:
            was_open = self._enabled
            self._enabled = False
            self._opened_at = None
            self._expires_at = None
            self._ttl_seconds = None
            self._generation += 1
            if was_open or self._last_close_reason is None:
                self._last_close_reason = reason
            return self._status_locked()

    # -- downloads -----------------------------------------------------

    def active_token(self) -> int:
        """Token identifying the window a download is starting under."""

        with self._lock:
            self._expire_locked()
            return self._generation if self._enabled else 0

    def token_is_alive(self, token: int) -> bool:
        """True only while the same uninterrupted window is still open.

        Checking ``is_open()`` alone let a download survive a close/re-open
        cycle: the switch was open again, so the stream kept going.  A stream
        now dies with the window it was born in.
        """

        with self._lock:
            self._expire_locked()
            return bool(token) and self._enabled and self._generation == token


# ---------------------------------------------------------------------------
# Path rules
# ---------------------------------------------------------------------------


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _stays_within(root: Path, candidate: Path) -> bool:
    """True when ``candidate`` still resolves inside ``root``.

    ``os.walk(followlinks=False)`` does not stop at a Windows junction — a
    junction is not reported as a symlink — so a link inside the log tree can
    pull a whole unrelated directory into the listing. Reads through such a path
    are already refused, but the names and sizes must not leak either.
    """

    try:
        return _is_within(candidate.resolve(), root)
    except (OSError, RuntimeError):
        return False


def normalise_relative_path(relative: str) -> str:
    """Validate a caller-supplied relative path and return a posix-style form."""

    raw = str(relative or "")
    if not raw.strip():
        raise DiagLogError("empty_path")
    if "\x00" in raw:
        raise DiagLogError("invalid_path")
    text = raw.replace("\\", "/").strip()
    if text.startswith("/"):
        raise DiagLogError("absolute_path")
    if len(text) > 1 and text[1] == ":":
        raise DiagLogError("absolute_path")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise DiagLogError("parent_traversal")
        parts.append(part)
    if not parts:
        raise DiagLogError("empty_path")
    return "/".join(parts)


def resolve_log_file(log_root: Optional[Path], relative: str) -> Path:
    """Resolve ``relative`` inside ``log_root``, refusing anything that escapes it.

    ``Path.resolve(strict=True)`` follows symlinks and Windows junctions, so a
    link pointing at ``secrets.json`` or ``C:\\Windows`` is rejected here even
    though the link itself lives under the log root.
    """

    if log_root is None:
        raise DiagLogError("log_root_unavailable", status_code=503)
    relative_path = normalise_relative_path(relative)
    root = Path(log_root).resolve()
    candidate = root.joinpath(*relative_path.split("/"))
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise DiagLogError("not_found", status_code=404) from None
    if not _is_within(resolved, root):
        raise DiagLogError("outside_log_root", status_code=403)
    if not resolved.is_file():
        raise DiagLogError("not_a_file", status_code=404)
    return resolved


def list_file_set(root: LogFileSet, *, max_files: int = MAX_LISTED_FILES) -> dict:
    """List the log files of a file-set root (the log and its rotations)."""

    directory = Path(root.directory)
    try:
        resolved_dir = directory.resolve()
    except OSError:
        raise DiagLogError("log_root_unavailable", status_code=503) from None
    if not resolved_dir.is_dir():
        raise DiagLogError("log_root_unavailable", status_code=503)

    files: list[dict] = []
    total_bytes = 0
    truncated = False
    for entry in sorted(resolved_dir.iterdir(), key=lambda item: item.name):
        if len(files) >= max_files:
            truncated = True
            break
        try:
            if not entry.is_file():
                continue
            name = entry.name
            if not root.allows(name):
                continue
            stat = entry.stat()
        except OSError:
            continue
        files.append({"path": entry.name, "size": stat.st_size, "mtime": stat.st_mtime})
        total_bytes += stat.st_size
    return {
        "log_root": str(resolved_dir),
        "file_count": len(files),
        "total_bytes": total_bytes,
        "truncated": truncated,
        "max_files": max_files,
        "files": files,
    }


def list_log_files(log_root: Optional[Path], *, max_files: int = MAX_LISTED_FILES) -> dict:
    """List every regular file under the log root with size and mtime."""

    if log_root is None:
        raise DiagLogError("log_root_unavailable", status_code=503)
    root = Path(log_root).resolve()
    if not root.is_dir():
        raise DiagLogError("log_root_unavailable", status_code=503)

    files: list[dict] = []
    total_bytes = 0
    truncated = False
    root_text = str(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        # Drop subdirectories that escape the root through a junction/symlink.
        dirnames[:] = [
            name for name in dirnames if _stays_within(root, Path(dirpath) / name)
        ]
        for name in sorted(filenames):
            candidate = Path(dirpath) / name
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if not candidate.is_file():
                continue
            if len(files) >= max_files:
                truncated = True
                break
            relative = candidate.relative_to(root).as_posix()
            files.append(
                {
                    "path": relative,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
            total_bytes += stat.st_size
        if truncated:
            break
    return {
        "log_root": root_text,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "truncated": truncated,
        "max_files": max_files,
        "files": files,
    }


def list_all_log_files(
    log_roots: Mapping[str, Path | LogFileSet],
    *,
    max_files: int = MAX_LISTED_FILES,
) -> dict:
    """List every regular file across all served roots, labelled per root."""

    files: list[dict] = []
    total_bytes = 0
    truncated = False
    served: dict[str, str] = {}
    for label, root in log_roots.items():
        remaining = max_files - len(files)
        if remaining <= 0:
            truncated = True
            break
        try:
            listing = (
                list_file_set(root, max_files=remaining)
                if isinstance(root, LogFileSet)
                else list_log_files(root, max_files=remaining)
            )
        except DiagLogError:
            continue
        served[label] = listing["log_root"]
        for entry in listing["files"]:
            files.append({"root": label, **entry})
            total_bytes += entry["size"]
        if listing["truncated"]:
            truncated = True
    return {
        "log_roots": served,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "truncated": truncated,
        "max_files": max_files,
        "files": files,
    }


def resolve_within_root(root: Path | LogFileSet, relative: str) -> Path:
    """Resolve ``relative`` inside one served root, honouring file-set limits."""

    directory = _root_directory(root)
    resolved = resolve_log_file(directory, relative)
    if isinstance(root, LogFileSet):
        # Only the log file itself and its rotated siblings exist here; a
        # subdirectory that happens to contain a same-named file is not part of
        # the set and must not be reachable.
        try:
            on_top_level = resolved.parent == directory.resolve()
        except OSError:
            on_top_level = False
        if not on_top_level or not root.allows(resolved.name):
            raise DiagLogError("not_found", status_code=404)
    return resolved


def resolve_requested_log(
    log_roots: Mapping[str, Path | LogFileSet],
    requested: str,
) -> tuple[str, str, Path]:
    """Resolve ``[<root>/]<relative path>`` to ``(label, relative, absolute)``.

    A leading ``instance``/``remote`` segment selects a root explicitly; that is
    the form the listing hands out so a caller can copy a path straight back.
    Anything else resolves against the instance root, which keeps the original
    single-root URLs working.
    """

    if not log_roots:
        raise DiagLogError("log_root_unavailable", status_code=503)
    relative = normalise_relative_path(requested)
    segments = relative.split("/")
    labelled = segments[0] in log_roots and len(segments) > 1
    label = segments[0] if labelled else LOG_ROOT_INSTANCE
    if label not in log_roots:
        raise DiagLogError("log_root_unavailable", status_code=503)
    inner = "/".join(segments[1:]) if labelled else relative
    return label, inner, resolve_within_root(log_roots[label], inner)


# ---------------------------------------------------------------------------
# Ranged / streamed reads
# ---------------------------------------------------------------------------


def compute_start_offset(size: int, *, offset: Optional[int] = None, tail: Optional[int] = None) -> int:
    if offset is not None and tail is not None:
        raise DiagLogError("conflicting_range")
    if offset is not None:
        if offset < 0:
            raise DiagLogError("invalid_offset")
        return min(int(offset), size)
    if tail is not None:
        if tail <= 0:
            raise DiagLogError("invalid_tail")
        return max(0, size - int(tail))
    return 0


def read_log_slice(
    path: Path,
    *,
    offset: Optional[int] = None,
    tail: Optional[int] = None,
    lines: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> tuple[int, bytes]:
    """Read a bounded slice in one go.  Used by unit tests and small files.

    Returns ``(start_offset, payload)``.
    """

    return _read_with_meta(
        path, offset=offset, tail=tail, lines=lines, max_bytes=max_bytes
    )


def _read_with_meta(
    path: Path,
    *,
    offset: Optional[int] = None,
    tail: Optional[int] = None,
    lines: Optional[int] = None,
    max_bytes: Optional[int] = None,
) -> tuple[int, bytes]:
    size = path.stat().st_size
    start = compute_start_offset(size, offset=offset, tail=tail)
    chunks = b"".join(
        iter_log_bytes(
            path,
            offset=start,
            lines=lines,
            max_bytes=max_bytes,
        )
    )
    return start, chunks


class DiagLogRevoked(DiagLogError):
    """Raised mid-stream when the switch closes or expires under a download."""


def iter_log_bytes(
    path: Path,
    *,
    offset: int = 0,
    lines: Optional[int] = None,
    max_bytes: Optional[int] = None,
    chunk_size: int = CHUNK_SIZE,
    keep_alive: Optional[Callable[[], bool]] = None,
) -> Iterator[bytes]:
    """Yield the file contents from ``offset``, optionally line-capped.

    The read is incremental, so a multi-gigabyte log never lands in memory.
    ``lines`` counts complete lines from the requested start position; the
    final partial line is included.

    ``keep_alive`` is re-checked before every chunk, so flipping the switch off
    (or letting the TTL expire) aborts a download that is already in progress
    instead of letting it run to completion.
    """

    if lines is not None and lines <= 0:
        raise DiagLogError("invalid_lines")
    if max_bytes is not None and max_bytes <= 0:
        raise DiagLogError("invalid_max_bytes")

    remaining = max_bytes
    seen_lines = 0
    with path.open("rb") as handle:
        handle.seek(max(0, int(offset)))
        while True:
            if keep_alive is not None and not keep_alive():
                raise DiagLogRevoked("gate_closed", status_code=403)
            want = chunk_size if remaining is None else min(chunk_size, remaining)
            if want <= 0:
                return
            chunk = handle.read(want)
            if not chunk:
                return
            if remaining is not None:
                remaining -= len(chunk)
            if lines is not None:
                emit = None
                for index, byte in enumerate(chunk):
                    if byte == 0x0A:
                        seen_lines += 1
                        if seen_lines >= lines:
                            emit = chunk[: index + 1]
                            break
                if emit is not None:
                    yield emit
                    return
            yield chunk


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


class DiagGateRequest(BaseModel):
    enabled: bool
    ttl_seconds: Optional[int] = None


#: Headers that mean "a proxy relayed this", so the socket address describes
#: the proxy (usually 127.0.0.1) rather than the operator.
_PROXY_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")


def _client_scope(request: Request) -> str:
    client = getattr(request, "client", None)
    return classify_client_host(getattr(client, "host", None))


def _looks_proxied(request: Request) -> bool:
    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    return any(str(headers.get(name) or "").strip() for name in _PROXY_HEADERS)


_SCOPE_ALLOWED = frozenset({"loopback", "lan"})


def _gate_denied(scope: str, gate: DiagLogsGate):
    """Common admission rule: gate must be open and the client must be local."""

    if not gate.is_open():
        # Hide the endpoints entirely while the switch is off.
        return JSONResponse(status_code=404, content={"ok": False, "error": "diagnostics_disabled"})
    if scope not in _SCOPE_ALLOWED:
        return JSONResponse(status_code=403, content={"ok": False, "error": "lan_required"})
    return None


def install_diag_logs_routes(app: FastAPI, *, gate: DiagLogsGate) -> None:
    """Attach the switch and the read-only log endpoints to a Remote app."""

    app.state.diag_logs_gate = gate

    @app.api_route("/diag/gate", methods=["GET", "HEAD"])
    async def diag_gate_status(request: Request):
        scope = _client_scope(request)
        if scope != "loopback":
            denied = _gate_denied(scope, gate)
            if denied is not None:
                return denied
        return {"ok": True, "client_scope": scope, **gate.status()}

    @app.post("/diag/gate")
    async def diag_gate_update(request: Request, payload: DiagGateRequest):
        scope = _client_scope(request)
        if _looks_proxied(request):
            # A relayed request arrives from the proxy's loopback address, so
            # the socket address no longer proves the operator is local.
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "proxy_not_allowed", "client_scope": scope},
            )
        if scope != "loopback":
            # Only the local machine may flip the switch; LAN readers cannot
            # extend or re-open a diagnostics window.
            return JSONResponse(
                status_code=403,
                content={"ok": False, "error": "loopback_required", "client_scope": scope},
            )
        if payload.enabled:
            state = gate.open(ttl_seconds=payload.ttl_seconds)
        else:
            state = gate.close(reason="manual")
        return {"ok": True, "client_scope": scope, **state}

    @app.api_route("/diag/logs", methods=["GET", "HEAD"])
    async def diag_logs_list(request: Request):
        scope = _client_scope(request)
        denied = _gate_denied(scope, gate)
        if denied is not None:
            return denied
        state = gate.status()
        if not gate.log_roots:
            return JSONResponse(
                status_code=503,
                content={"ok": False, "error": "log_root_unavailable"},
            )
        listing = list_all_log_files(gate.log_roots)
        return {
            "ok": True,
            "client_scope": scope,
            "instance_id": state["instance_id"],
            "gate_expires_at": state["expires_at"],
            "gate_remaining_seconds": state["remaining_seconds"],
            **listing,
        }

    @app.api_route("/diag/logs/{log_path:path}", methods=["GET", "HEAD"])
    async def diag_logs_read(
        request: Request,
        log_path: str,
        offset: Optional[int] = None,
        tail: Optional[int] = None,
        lines: Optional[int] = None,
        max_bytes: Optional[int] = None,
    ):
        scope = _client_scope(request)
        denied = _gate_denied(scope, gate)
        if denied is not None:
            return denied

        if offset is not None and tail is not None:
            return JSONResponse(status_code=400, content={"ok": False, "error": "conflicting_range"})
        if offset is not None and offset < 0:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_offset"})
        if tail is not None and tail <= 0:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_tail"})
        if lines is not None and lines <= 0:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_lines"})
        if max_bytes is not None and max_bytes <= 0:
            return JSONResponse(status_code=400, content={"ok": False, "error": "invalid_max_bytes"})

        try:
            root_label, relative, resolved = resolve_requested_log(gate.log_roots, log_path)
        except DiagLogError as exc:
            return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": exc.reason})

        size = resolved.stat().st_size
        start = compute_start_offset(size, offset=offset, tail=tail)
        state = gate.status()

        headers = {
            "X-Diag-Log-Root": root_label,
            "X-Diag-Log-Path": relative,
            "X-Diag-Log-Size": str(size),
            "X-Diag-Log-Start": str(start),
            "X-Diag-Log-Instance": str(state["instance_id"] or ""),
            "X-Diag-Gate-Expires-At": str(int(state["expires_at"] or 0)),
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{relative.rsplit("/", 1)[-1]}"',
        }

        # Remember which open window this download was granted under: closing
        # the switch (or letting the TTL run out) kills it even if the switch is
        # opened again a moment later.
        window = gate.active_token()
        body = iter_log_bytes(
            resolved,
            offset=start,
            lines=lines,
            max_bytes=max_bytes,
            keep_alive=lambda: gate.token_is_alive(window),
        )
        return StreamingResponse(
            body,
            media_type="text/plain; charset=utf-8",
            headers=headers,
        )
# HASHI-Fingerprint: hfp_0bb8c0ae543346949a507ac3f99967fa
