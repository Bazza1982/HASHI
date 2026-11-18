"""Run-bound, disk-backed retention for oversized tool results.

References are derived from already-authorized results; they do not grant any
new filesystem or network authority. Agent-owned stores survive Function Worker
replacement while the Run remains active. Only a canonically terminal Run is
eligible for automatic cleanup. Registries without an Agent home use a private
process-local temporary store.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from orchestrator.config_json import (
    ConfigDurabilityError,
    file_write_lock,
    read_config_json,
    write_config_json,
)

_CHUNK_CHARS = 64 * 1024
_FRAME_RECORD = struct.Struct(">qqqqqq")
_FRAME_RECORD_SIZE = _FRAME_RECORD.size + 32
_REF = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_MARKER = {"type": "hashi.tool-output-retention", "version": 1}


def run_binding(context: Mapping[str, Any]) -> tuple[str, ...] | None:
    run_id = str(context.get("hashi_run_id") or context.get("run_id") or "")
    request_id = str(context.get("request_id") or "")
    if not run_id and not request_id:
        return None
    return tuple(
        str(context.get(key) or "")
        for key in ("instance_id", "agent_name", "owner_id", "hashi_session_id")
    ) + (run_id, request_id)


def run_state_from_store(store: Any, binding: tuple[str, ...]) -> str | None:
    """Read the existing PAO Run, requiring the complete stored identity."""
    if store is None or len(binding) != 6 or not all(binding):
        return None
    instance, agent, owner, session_id, run_id, request_id = binding
    if str(getattr(store, "instance_id", "")) != instance:
        return None
    try:
        run = store.get_run(run_id, owner_id=owner)
    except Exception:
        return None
    if (
        str(run.get("session_id") or "") != session_id
        or str(run.get("request_id") or "") != request_id
        or str(run.get("agent_id") or "").casefold() != agent.casefold()
    ):
        return None
    return str(run.get("state") or "") or None


def run_state_from_context(context: Mapping[str, Any], binding: tuple[str, ...]) -> str | None:
    """Use only the existing local Run owner or its read-only gateway descriptor."""
    runtime = context.get("_runtime")
    store = getattr(runtime, "session_store", None)
    if store is not None:
        return run_state_from_store(store, binding)
    descriptor = context.get("session_store_descriptor")
    if not isinstance(descriptor, Mapping) or len(binding) != 6 or not all(binding):
        return None
    instance, agent, owner, session_id, run_id, request_id = binding
    if str(descriptor.get("instance_id") or "") != instance or not descriptor.get("db_path"):
        return None
    try:
        path = Path(str(descriptor["db_path"])).expanduser().resolve()
        if not path.is_file():
            return None
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2) as connection:
            row = connection.execute(
                """SELECT r.session_id, r.request_id, r.agent_id, r.state
                   FROM runs r JOIN sessions s ON s.session_id=r.session_id
                   WHERE r.run_id=? AND s.instance_id=? AND s.owner_id=?""",
                (run_id, instance, owner),
            ).fetchone()
        if not row or row[0] != session_id or row[1] != request_id or str(row[2]).casefold() != agent.casefold():
            return None
        return str(row[3] or "") or None
    except (OSError, ValueError, sqlite3.Error):
        return None


def _binding_key(binding: tuple[str, ...]) -> str:
    payload = json.dumps(binding, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _private_file(path: Path):
    """Create and seal an empty output file before writing sensitive bytes."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        from tools.private_files import protect_private_file

        protect_private_file(path)
        return os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise


def _read_exact(path: Path, start: int, size: int) -> bytes:
    if path.is_symlink():
        raise ValueError("retained output is corrupt")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise ValueError("retained output is corrupt")
            stream.seek(start)
            data = stream.read(size)
    except (OSError, ValueError) as exc:
        raise ValueError("retained output is corrupt or unavailable") from exc
    if len(data) != size:
        raise ValueError("retained output is corrupt or unavailable")
    return data


@dataclass(frozen=True)
class RetainedOutput:
    binding: tuple[str, ...]
    source_tool: str
    digest: str
    kind: str
    characters: int
    reference: str
    image_outcomes: dict[int, bool] = field(default_factory=dict, compare=False)
    frame_count: int = 0


class ToolOutputStore:
    def __init__(
        self,
        *,
        agent_home: Path | None = None,
        instance_home: Path | None = None,
        state_resolver: Callable[[tuple[str, ...]], str | None] | None = None,
        lock_timeout: float | None = None,
    ) -> None:
        configured = os.environ.get("HASHI_TOOL_OUTPUT_LOCK_TIMEOUT_SECONDS")
        if lock_timeout is None and configured:
            try:
                lock_timeout = float(configured)
            except ValueError as exc:
                raise ValueError("invalid tool output lock timeout") from exc
        if lock_timeout is not None and (not math.isfinite(lock_timeout) or lock_timeout < 0):
            raise ValueError("invalid tool output lock timeout")
        self._lock_timeout = lock_timeout
        self._home = Path(agent_home).expanduser().resolve() if agent_home is not None else None
        self._temporary: tempfile.TemporaryDirectory | None = None
        self._instance_home = Path(instance_home).expanduser().resolve() if instance_home is not None else None
        self._root = (self._instance_home / "state" / ".hashi-tool-output-v1"
                      if self._instance_home is not None else
                      self._home / ".hashi-tool-output-v1" if self._home is not None else None)
        self._state_resolver = state_resolver
        self._lock = threading.RLock()

    @property
    def retention(self) -> str:
        return "run_lifetime" if self._instance_home is not None or self._home is not None else "registry_lifetime"

    @contextmanager
    def _file_lock(self, root: Path) -> Iterator[None]:
        # The shared lock primitive waits at most 60s per attempt. Keep waiting
        # by default so a large concurrent write cannot arbitrarily disable a
        # continuation. Operators may set an explicit total wait budget.
        deadline = None if self._lock_timeout is None else time.monotonic() + self._lock_timeout
        with ExitStack() as locks:
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                try:
                    locks.enter_context(file_write_lock(root / "store", timeout=min(60.0, remaining)
                                                       if remaining is not None else 60.0))
                    break
                except TimeoutError:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise
            yield

    def _root_for_use(self, *, create: bool) -> Path | None:
        if self._root is None:
            if not create:
                return None
            self._temporary = tempfile.TemporaryDirectory(prefix="hashi-tool-output-")
            self._root = Path(self._temporary.name) / "store"
        root = self._root
        if not root.exists():
            if not create:
                return None
            if self._instance_home is not None:
                root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                root.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                created = False
            if created:
                from tools.private_files import protect_private_file

                protect_private_file(root)
        if root.is_symlink() or not root.is_dir():
            raise OSError("tool output retention directory is unavailable")
        with self._file_lock(root):
            marker = root / ".owner.json"
            if not marker.exists():
                if not create:
                    return None
                extra = [entry for entry in root.iterdir() if entry.name not in {".store.lock", "..owner.json.lock"}]
                if extra:
                    raise OSError("tool output retention directory is not owned by HASHI")
                # Another process may have made the directory but not yet
                # sealed it; never publish the marker or output first.
                from tools.private_files import protect_private_file

                protect_private_file(root)
                write_config_json(marker, _MARKER, expected_revision=None)
            if marker.is_symlink() or dict(read_config_json(marker)) != _MARKER:
                raise OSError("tool output retention directory is not owned by HASHI")
        return root

    @contextmanager
    def _locked(self, *, create: bool = False) -> Iterator[Path | None]:
        with self._lock:
            root = self._root_for_use(create=create)
            if root is None:
                yield None
                return
            with self._file_lock(root):
                yield root

    def _terminal(self, binding: tuple[str, ...]) -> bool:
        if self._state_resolver is None:
            return False
        try:
            state = self._state_resolver(binding)
        except Exception:
            return False
        from orchestrator.session_store import TERMINAL_RUN_STATES

        return state in TERMINAL_RUN_STATES

    @staticmethod
    def _run_dir(root: Path, binding: tuple[str, ...]) -> Path:
        return root / _binding_key(binding)

    @staticmethod
    def _verify_run(run_dir: Path, binding: tuple[str, ...]) -> None:
        marker = run_dir / ".run.json"
        if run_dir.is_symlink() or marker.is_symlink():
            raise ValueError("result reference is invalid or belongs to another Run")
        try:
            data = read_config_json(marker)
        except (OSError, ValueError) as exc:
            raise ValueError("result reference is invalid or belongs to another Run") from exc
        if data.get("binding") != list(binding):
            raise ValueError("result reference is invalid or belongs to another Run")

    @staticmethod
    def _read_item(root: Path, binding: tuple[str, ...], reference: Any) -> RetainedOutput:
        if (
            binding is None or not isinstance(reference, str)
            or _REF.fullmatch(reference) is None
        ):
            raise ValueError("result reference is invalid or belongs to another Run")
        run_dir = ToolOutputStore._run_dir(root, binding)
        ToolOutputStore._verify_run(run_dir, binding)
        metadata = run_dir / f"{reference}.json"
        if metadata.is_symlink():
            raise ValueError("result reference is invalid or belongs to another Run")
        try:
            value = read_config_json(metadata)
            characters = value["characters"]
            digest = value["digest"]
            kind = value["kind"]
            source = value["source_tool"]
            outcomes = value.get("image_outcomes") or {}
            frame_count = value.get("frame_count", 0)
            if (
                value.get("binding") != list(binding)
                or type(characters) is not int or characters < 0
                or not isinstance(digest, str) or _HEX.fullmatch(digest) is None
                or not isinstance(kind, str) or not isinstance(source, str)
                or not isinstance(outcomes, dict)
                or type(frame_count) is not int or frame_count < 0
            ):
                raise ValueError("invalid metadata")
            parsed_outcomes = {}
            for index, valid in outcomes.items():
                if not isinstance(index, str) or not index.isdigit() or type(valid) is not bool:
                    raise ValueError("invalid screenshot outcome")
                parsed_outcomes[int(index)] = valid
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("result reference is invalid or belongs to another Run") from exc
        return RetainedOutput(binding, source, digest, kind, characters, reference, parsed_outcomes, frame_count)

    def temporary_capture_file(self):
        """Open a delete-on-close spool inside the protected retention root."""
        with self._locked(create=True) as root:
            assert root is not None
            return tempfile.TemporaryFile(mode="w+b", dir=root)

    def retain(
        self, binding: tuple[str, ...] | None, source_tool: str, text: str, *,
        kind: str = "text", image_outcomes: Mapping[int, bool] | None = None,
    ) -> str | None:
        if not isinstance(text, str):
            raise ValueError("invalid result retention input")
        return self._retain_chunks(
            binding, source_tool,
            (text[start:start + _CHUNK_CHARS] for start in range(0, len(text), _CHUNK_CHARS)),
            kind=kind, image_outcomes=image_outcomes,
            screenshot_source=text if kind == "legacy_screenshot" else None,
        )

    def retain_text_chunks(
        self, binding: tuple[str, ...] | None, source_tool: str, chunks: Iterator[str],
    ) -> str | None:
        """Retain a text stream without materializing the complete result."""
        return self._retain_chunks(binding, source_tool, chunks)

    def _retain_chunks(
        self, binding: tuple[str, ...] | None, source_tool: str, chunks: Iterator[str], *,
        kind: str = "text", image_outcomes: Mapping[int, bool] | None = None,
        screenshot_source: str | None = None,
    ) -> str | None:
        if binding is None:
            return None
        if len(binding) != 6:
            raise ValueError("invalid result retention input")
        with self._locked(create=True) as root:
            assert root is not None
            if self._terminal(binding):
                return None
            run_dir = self._run_dir(root, binding)
            if not run_dir.exists():
                run_dir.mkdir(mode=0o700)
            if not (run_dir / ".run.json").exists():
                if run_dir.is_symlink() or any(entry.name != "..run.json.lock" for entry in run_dir.iterdir()):
                    raise OSError("incomplete Run retention directory has unknown contents")
                from tools.private_files import protect_private_file

                protect_private_file(run_dir)
                write_config_json(run_dir / ".run.json", {"binding": list(binding)}, expected_revision=None)
            self._verify_run(run_dir, binding)
            while True:
                reference = secrets.token_urlsafe(32)
                data_path, index_path = run_dir / f"{reference}.data", run_dir / f"{reference}.index"
                metadata_path = run_dir / f"{reference}.json"
                frames_path = run_dir / f"{reference}.frames"
                if not any(path.exists() or path.is_symlink()
                           for path in (data_path, index_path, metadata_path, frames_path)):
                    break
            digest = hashlib.sha256()
            characters = 0
            try:
                with _private_file(data_path) as data, _private_file(index_path) as index:
                    pending = ""
                    for piece in chunks:
                        if not isinstance(piece, str):
                            raise ValueError("invalid result retention input")
                        position = 0
                        while position < len(piece):
                            size = min(_CHUNK_CHARS - len(pending), len(piece) - position)
                            pending += piece[position:position + size]
                            position += size
                            if len(pending) == _CHUNK_CHARS:
                                digest.update(pending.encode("utf-8", errors="surrogatepass"))
                                encoded = pending.encode("utf-32-le", errors="surrogatepass")
                                data.write(encoded)
                                index.write(hashlib.sha256(encoded).digest())
                                characters += len(pending)
                                pending = ""
                    if pending:
                        digest.update(pending.encode("utf-8", errors="surrogatepass"))
                        encoded = pending.encode("utf-32-le", errors="surrogatepass")
                        data.write(encoded)
                        index.write(hashlib.sha256(encoded).digest())
                        characters += len(pending)
                    data.flush()
                    index.flush()
                    os.fsync(data.fileno())
                    os.fsync(index.fileno())
                frame_count = 0
                if kind == "legacy_screenshot":
                    if screenshot_source is None:
                        raise ValueError("invalid screenshot source")
                    from tools.legacy_screenshot import LEGACY_SCREENSHOT_PATTERN

                    with _private_file(frames_path) as frames:
                        for match in LEGACY_SCREENSHOT_PATTERN.finditer(screenshot_source):
                            payload = ("data_payload" if match.group("data_url") is not None
                                       else "label_payload")
                            packed = _FRAME_RECORD.pack(*match.span(), *match.span(payload), *match.span("mime"))
                            frames.write(packed)
                            frames.write(hashlib.sha256(packed).digest())
                            frame_count += 1
                        frames.flush()
                        os.fsync(frames.fileno())
                outcomes = dict(image_outcomes or {})
                if any(type(i) is not int or i < 0 or type(v) is not bool for i, v in outcomes.items()):
                    raise ValueError("invalid screenshot outcome")
                write_config_json(metadata_path, {
                    "binding": list(binding), "source_tool": str(source_tool), "kind": str(kind),
                    "characters": characters, "digest": digest.hexdigest(),
                    "frame_count": frame_count,
                    "image_outcomes": {str(i): value for i, value in outcomes.items()},
                }, expected_revision=None)
            except ConfigDurabilityError:
                # Publication may already have committed; retain its data for
                # later reconciliation rather than undoing an uncertain write.
                raise
            except BaseException:
                data_path.unlink(missing_ok=True)
                index_path.unlink(missing_ok=True)
                frames_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                raise
            return reference

    def lookup(self, binding: tuple[str, ...] | None, reference: Any) -> RetainedOutput:
        if binding is None:
            raise ValueError("result reference is invalid or belongs to another Run")
        with self._locked() as root:
            if root is None:
                raise ValueError("result reference is invalid or belongs to another Run")
            return self._read_item(root, binding, reference)

    def _read_range(self, root: Path, item: RetainedOutput, start: int, end: int) -> str:
        if start == end:
            return ""
        run_dir = self._run_dir(root, item.binding)
        data_path = run_dir / f"{item.reference}.data"
        index_path = run_dir / f"{item.reference}.index"
        parts = []
        first, last = start // _CHUNK_CHARS, (end - 1) // _CHUNK_CHARS
        for index in range(first, last + 1):
            chunk_start = index * _CHUNK_CHARS
            count = min(_CHUNK_CHARS, item.characters - chunk_start)
            data = _read_exact(data_path, chunk_start * 4, count * 4)
            expected = _read_exact(index_path, index * 32, 32)
            if hashlib.sha256(data).digest() != expected:
                raise ValueError("retained output is corrupt or unavailable")
            try:
                text = data.decode("utf-32-le", errors="surrogatepass")
            except UnicodeError as exc:
                raise ValueError("retained output is corrupt or unavailable") from exc
            parts.append(text[max(start - chunk_start, 0):min(end - chunk_start, count)])
        return "".join(parts)

    def screenshot_page(
        self, binding: tuple[str, ...] | None, reference: Any, offset: Any, limit: Any = None,
    ) -> dict[str, Any]:
        if binding is None:
            raise ValueError("result reference is invalid or belongs to another Run")
        with self._locked() as root:
            if root is None:
                raise ValueError("result reference is invalid or belongs to another Run")
            item = self._read_item(root, binding, reference)
            if item.kind != "legacy_screenshot" or item.frame_count == 0:
                raise ValueError("retained screenshot source is invalid")
            frames_path = self._run_dir(root, binding) / f"{reference}.frames"

            def frame_at(index: int) -> tuple[int, ...]:
                raw = _read_exact(frames_path, index * _FRAME_RECORD_SIZE, _FRAME_RECORD_SIZE)
                packed, digest = raw[:_FRAME_RECORD.size], raw[_FRAME_RECORD.size:]
                if hashlib.sha256(packed).digest() != digest:
                    raise ValueError("retained output is corrupt or unavailable")
                start, end, payload_start, payload_end, mime_start, mime_end = _FRAME_RECORD.unpack(packed)
                if (not 0 <= start <= payload_start <= payload_end <= end <= item.characters
                    or not ((mime_start == mime_end == -1)
                            or (start <= mime_start <= mime_end <= payload_start))):
                    raise ValueError("retained output is corrupt or unavailable")
                return start, end, payload_start, payload_end, mime_start, mime_end

            from tools.legacy_screenshot import indexed_screenshot_page

            return indexed_screenshot_page(
                item.frame_count, item.characters, frame_at,
                lambda start, end: self._read_range(root, item, start, end),
                offset=offset, limit=limit,
            )

    def record_image_outcomes(
        self, binding: tuple[str, ...] | None, reference: Any,
        outcomes: Mapping[int, bool], total: int,
    ) -> bool:
        if binding is None:
            raise ValueError("result reference is invalid or belongs to another Run")
        with self._locked() as root:
            if root is None:
                raise ValueError("result reference is invalid or belongs to another Run")
            item = self._read_item(root, binding, reference)
            if item.kind != "legacy_screenshot" or type(total) is not int or total < 0:
                raise ValueError("result reference is not a screenshot page")
            updated = dict(item.image_outcomes)
            for index, valid in outcomes.items():
                if type(index) is not int or index < 0 or index >= total or type(valid) is not bool:
                    raise ValueError("invalid screenshot page outcome")
                updated[index] = valid
            path = self._run_dir(root, binding) / f"{reference}.json"
            document = read_config_json(path)
            document["image_outcomes"] = {str(i): value for i, value in updated.items()}
            write_config_json(path, document)
            return len(updated) == total and all(updated.values())

    def has_bound(self, binding: tuple[str, ...] | None) -> bool:
        if binding is None:
            return False
        try:
            with self._locked() as root:
                if root is None:
                    return False
                run_dir = self._run_dir(root, binding)
                self._verify_run(run_dir, binding)
                return any(not entry.name.startswith(".") for entry in run_dir.glob("*.json"))
        except (OSError, TimeoutError, ValueError):
            return False

    def source_allowed(self, binding: tuple[str, ...] | None, reference: Any, allowed: set[str]) -> bool:
        try:
            return self.lookup(binding, reference).source_tool in allowed
        except (OSError, TimeoutError, ValueError):
            return False

    def page(self, binding: tuple[str, ...] | None, reference: Any, offset: Any, limit: Any = None) -> str:
        if binding is None:
            raise ValueError("result reference is invalid or belongs to another Run")
        with self._locked() as root:
            if root is None:
                raise ValueError("result reference is invalid or belongs to another Run")
            item = self._read_item(root, binding, reference)
            if type(offset) is not int or offset < 0 or offset > item.characters:
                raise ValueError("invalid result offset")
            if limit is not None and (type(limit) is not int or limit < 1):
                raise ValueError("invalid result page limit")
            end = min(item.characters, offset + min(limit if limit is not None else 10_000, 10_000))
            window = self._read_range(root, item, offset, end)

            def render(count: int) -> str:
                next_offset = offset + count
                complete = next_offset == item.characters
                return json.dumps({
                    "reference": reference, "offset": offset, "text": window[:count],
                    "next_offset": None if complete else next_offset, "complete": complete,
                    "sha256": item.digest, "retention": self.retention,
                }, ensure_ascii=False, separators=(",", ":"))

            low, high = 0, len(window)
            while low < high:
                middle = (low + high + 1) // 2
                if len(render(middle)) <= 15_000:
                    low = middle
                else:
                    high = middle - 1
            return render(low)

    def cleanup_if_terminal(self, binding: tuple[str, ...] | None) -> bool:
        if binding is None:
            return False
        with self._locked() as root:
            if root is None or not self._terminal(binding):
                return False
            run_dir = self._run_dir(root, binding)
            if not run_dir.exists():
                return False
            self._verify_run(run_dir, binding)
            shutil.rmtree(run_dir)
            return True

    def reconcile_terminal(self) -> int:
        if self._state_resolver is None:
            return 0
        removed = 0
        with self._locked() as root:
            if root is None:
                return 0
            for run_dir in root.iterdir():
                if not run_dir.is_dir() or run_dir.is_symlink() or _HEX.fullmatch(run_dir.name) is None:
                    continue
                try:
                    marker = run_dir / ".run.json"
                    if marker.is_symlink():
                        continue
                    value = read_config_json(marker).get("binding")
                    if not isinstance(value, list) or len(value) != 6 or not all(isinstance(v, str) for v in value):
                        continue
                    binding = tuple(value)
                    if _binding_key(binding) != run_dir.name or not self._terminal(binding):
                        continue
                    self._verify_run(run_dir, binding)
                    shutil.rmtree(run_dir)
                    removed += 1
                except (OSError, ValueError, TypeError):
                    continue
        return removed
# HASHI-Fingerprint: hfp_921ad73b3593472ab571155fe338d46b
