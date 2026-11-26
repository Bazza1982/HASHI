"""Durable record of the work one forced ``/stop`` actually removes.

A forced reboot may only restore what the stop really took away.  Inferring that
from a before/after queue difference cannot work: a request that *finished*
during the stop disappears from the queue without being removed, a request that
was enqueued after the snapshot and then removed never appears in it, and the
same request can be seen both as queued work and as an interrupted turn.

So the record is written where the removal happens, by the removal itself:

* :func:`arm` attaches a capture to the Agent runtime for one reboot operation.
* ``runtime_pending.recall_pending`` records the exact ready requests it drains
  and the exact delayed messages the scheduler cancels, and refuses to remove
  anything it could not first persist.
* ``runtime_control`` records the interrupted-turn snapshots ``/stop`` just wrote.

Each record is flushed to disk before the destructive step, so a crash mid-stop
cannot leave work that is removed but not written down.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

logger = logging.getLogger("BridgeU.RebootCapture")

CAPTURE_VERSION = 1
# A forced reboot stops and cut over within a couple of minutes; an arm older
# than this belongs to an operation that never came back and must not keep
# steering later stops.
CAPTURE_MAX_AGE_SECONDS = 900.0
STATE_DIRECTORY = ("state", "reboot_resume")
_CAPTURE_ATTRIBUTE = "_reboot_work_capture"
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_name(value: Any) -> str:
    cleaned = _UNSAFE_NAME.sub("-", str(value or "").strip())
    return cleaned.strip(".-") or "unknown"


_safe_name = safe_name


def bridge_home_for(runtime: Any) -> Path | None:
    config = getattr(runtime, "global_config", None) or getattr(runtime, "config", None)
    home = getattr(config, "bridge_home", None)
    return Path(home) if home else None


def capture_directory(bridge_home: Any) -> Path | None:
    if not bridge_home:
        return None
    return Path(bridge_home).joinpath(*STATE_DIRECTORY)


def capture_path(bridge_home: Any, operation_id: str, agent_name: str) -> Path | None:
    directory = capture_directory(bridge_home)
    if directory is None:
        return None
    return directory / (
        f"capture-{_safe_name(operation_id)}-{_safe_name(agent_name)}.json"
    )


def ready_entry(item: Any) -> dict[str, Any] | None:
    """Map one ready QueuedRequest onto the existing admission parameters."""
    chat_id = getattr(item, "chat_id", None)
    prompt = getattr(item, "prompt", None)
    if chat_id is None or not prompt:
        return None
    return {
        "request_id": str(getattr(item, "request_id", "") or ""),
        "session_id": str(getattr(item, "session_id", "") or "") or None,
        "context_generation": getattr(item, "context_generation", None),
        "chat_id": int(chat_id),
        "prompt": str(prompt),
        "source": str(getattr(item, "source", "") or "api"),
        "summary": str(getattr(item, "summary", "") or ""),
        "silent": bool(getattr(item, "silent", False)),
        "is_retry": bool(getattr(item, "is_retry", False)),
        "deliver_to_telegram": bool(getattr(item, "deliver_to_telegram", True)),
        "skip_memory_injection": bool(
            getattr(item, "skip_memory_injection", False)
        ),
        "habit_learning_eligible": bool(
            getattr(item, "habit_learning_eligible", True)
        ),
        "skill_id": getattr(item, "skill_id", None),
        "scheduler_context": getattr(item, "scheduler_context", None),
        "request_metadata": getattr(item, "request_metadata", None),
        "request_content": getattr(item, "request_content", None),
        # The queued request knows its own delivery binding; keep it so a resumed
        # admission is checked against the route the work was accepted for.
        "owner_id": getattr(item, "owner_id", None),
        "session_surface": getattr(item, "session_surface", None),
        "session_channel_key": getattr(item, "session_channel_key", None),
    }


def delayed_entry(record: Any) -> dict[str, Any] | None:
    """Keep a cancelled delayed message verbatim so it can be put back as-was."""
    if not isinstance(record, Mapping):
        return None
    prompt = str(record.get("prompt") or "").strip()
    if not prompt:
        return None
    try:
        due_at = float(record.get("due_at") or 0.0)
    except (TypeError, ValueError):
        due_at = 0.0
    return {
        "id": str(record.get("id") or ""),
        "chat_id": record.get("chat_id"),
        "prompt": prompt,
        "due_at": due_at,
        "deliver_to_telegram": bool(record.get("deliver_to_telegram", True)),
        "record": dict(record),
    }


def interrupted_entry(snapshot: Any) -> dict[str, Any] | None:
    """One interrupted turn, exactly as the stop recorded it."""
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    as_dict = getattr(snapshot, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
        return dict(payload) if isinstance(payload, Mapping) else None
    if is_dataclass(snapshot):
        # ``InterruptedTaskSnapshot`` is a plain frozen dataclass; the resume
        # path needs its fields, not the object.
        return dict(asdict(snapshot))
    return None


def _identity(kind: str, entry: Mapping[str, Any]) -> str:
    if kind == "interrupted":
        return "interrupted|{0}|{1}".format(
            str(entry.get("session_id") or ""),
            str(entry.get("request_id") or ""),
        )
    return "{0}|{1}".format(kind, str(entry.get("request_id") or entry.get("id") or ""))


class RebootWorkCapture:
    """What the stop of one reboot operation really removed, flushed as it goes."""

    def __init__(
        self,
        operation_id: str,
        agent_name: str,
        *,
        path: Path | None = None,
    ) -> None:
        self.operation_id = str(operation_id)
        self.agent_name = str(agent_name)
        self.path = path
        self.armed_at = time.time()
        self.broken = False
        # Only a finished record is evidence: an armed capture writes nothing
        # until the stop has something to remember.
        self.completed = False
        self.pending: list[dict[str, Any]] = []
        self.delayed: list[dict[str, Any]] = []
        self.interrupted: list[dict[str, Any]] = []
        self.unconfirmed: list[dict[str, Any]] = []
        self._identities: set[str] = set()
        self._request_ids: set[str] = set()

    # ------------------------------------------------------------------ recording

    def record_ready(self, items: Iterable[Any]) -> None:
        entries = [self._unique("pending", ready_entry(item)) for item in items]
        self._extend("pending", [entry for entry in entries if entry])

    def record_delayed(self, records: Iterable[Any]) -> None:
        entries = [self._unique("delayed", delayed_entry(record)) for record in records]
        self._extend("delayed", [entry for entry in entries if entry])

    def confirm_delayed(self, records: Iterable[Any]) -> None:
        """Replace the planned cancellations with the ones that really happened."""
        confirmed: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records or ():
            entry = delayed_entry(record)
            if not entry or entry["id"] in seen:
                continue
            seen.add(entry["id"])
            confirmed.append(entry)
        dropped = [entry for entry in self.delayed if entry["id"] not in seen]
        self.delayed = confirmed
        if dropped:
            # Cancelled nothing after all: keep the record honest rather than
            # rebuilding a queue entry that still exists.
            self.unconfirmed.extend(
                {
                    "kind": "delayed",
                    "entry": entry,
                    "reason": "cancel_not_confirmed",
                }
                for entry in dropped
            )
        try:
            self._flush()
        except OSError:
            self.broken = True
            logger.warning(
                "Reboot capture: confirmed cancellations not stored | "
                "operation=%s agent=%s",
                self.operation_id,
                self.agent_name,
            )

    def record_interrupted(self, snapshots: Iterable[Any]) -> None:
        entries = [
            self._unique("interrupted", interrupted_entry(snapshot))
            for snapshot in snapshots or ()
        ]
        self._extend("interrupted", [entry for entry in entries if entry])

    def mark_unconfirmed(self, kind: str, entry: Any, reason: str) -> None:
        payload = dict(entry) if isinstance(entry, Mapping) else {"value": str(entry)}
        self.unconfirmed.append({"kind": kind, "entry": payload, "reason": reason})
        try:
            self._flush()
        except OSError:
            self.broken = True

    # -------------------------------------------------------------------- reading

    def payload(self) -> dict[str, Any]:
        return {
            "pending": [dict(entry) for entry in self.pending],
            "delayed": [dict(entry) for entry in self.delayed],
            "interrupted": [dict(entry) for entry in self.interrupted],
            "unconfirmed": [dict(entry) for entry in self.unconfirmed],
            "broken": self.broken,
            "phase": self.phase,
        }

    @property
    def phase(self) -> str:
        if self.broken:
            return "incomplete"
        return "stopped" if self.completed else "recording"

    def complete(self) -> None:
        """Finish the record: from here the file says whether it can be used."""
        if self.completed:
            return
        self.completed = True
        try:
            self._flush()
        except OSError:
            self.broken = True

    # ------------------------------------------------------------------ internals

    def _unique(self, kind: str, entry: dict[str, Any] | None) -> dict[str, Any] | None:
        """One original request is recorded once, as queued work or as a turn."""
        if not entry:
            return None
        marker = _identity(kind, entry)
        request_id = str(entry.get("request_id") or "")
        if marker in self._identities:
            return None
        if request_id and request_id in self._request_ids:
            return None
        self._identities.add(marker)
        if request_id:
            self._request_ids.add(request_id)
        return entry

    def _extend(self, kind: str, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        bucket: list[dict[str, Any]] = getattr(self, kind)
        bucket.extend(entries)
        try:
            # Durable before the caller removes anything.
            self._flush()
        except OSError:
            del bucket[-len(entries) :]
            for entry in entries:
                self._identities.discard(_identity(kind, entry))
                request_id = str(entry.get("request_id") or "")
                if request_id:
                    self._request_ids.discard(request_id)
            self.broken = True
            raise

    def _flush(self) -> None:
        if self.path is None:
            return
        payload = {
            "version": CAPTURE_VERSION,
            "operation_id": self.operation_id,
            "agent": self.agent_name,
            "recorded_at": time.time(),
            "pending": self.pending,
            "delayed": self.delayed,
            "interrupted": self.interrupted,
            "unconfirmed": self.unconfirmed,
            "phase": self.phase,
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise OSError(f"reboot capture is not serialisable: {exc}") from exc
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = None
        temporary = None
        try:
            handle, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=str(self.path.parent)
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                handle = None
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if handle is not None:
                os.close(handle)
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass


def arm(runtime: Any, operation_id: str) -> RebootWorkCapture:
    """Attach the capture this reboot operation will record its stop into."""
    name = str(getattr(runtime, "name", "") or "")
    path = capture_path(bridge_home_for(runtime), operation_id, name)
    capture = RebootWorkCapture(operation_id, name, path=path)
    setattr(runtime, _CAPTURE_ATTRIBUTE, capture)
    return capture


def _detach(runtime: Any, operation_id: str | None = None) -> None:
    """Drop the armed capture without consulting it (never recurses)."""
    capture = getattr(runtime, _CAPTURE_ATTRIBUTE, None)
    if not isinstance(capture, RebootWorkCapture):
        return
    if operation_id is not None and capture.operation_id != str(operation_id):
        return
    try:
        delattr(runtime, _CAPTURE_ATTRIBUTE)
    except AttributeError:
        pass


def armed(runtime: Any, operation_id: str | None = None) -> RebootWorkCapture | None:
    capture = getattr(runtime, _CAPTURE_ATTRIBUTE, None)
    if not isinstance(capture, RebootWorkCapture):
        return None
    if capture.completed or capture.armed_at + CAPTURE_MAX_AGE_SECONDS < time.time():
        # A finished or abandoned operation must not keep arming later stops.
        _detach(runtime, capture.operation_id)
        return None
    if operation_id is not None and capture.operation_id != str(operation_id):
        return None
    return capture


def disarm(runtime: Any, operation_id: str | None = None) -> None:
    _detach(runtime, operation_id)


def complete(runtime: Any, capture: RebootWorkCapture | None = None) -> None:
    """Finish and detach this operation's capture; a normal /stop has none."""
    if not isinstance(capture, RebootWorkCapture):
        capture = armed(runtime)
    if capture is None:
        return
    try:
        capture.complete()
    finally:
        _detach(runtime, capture.operation_id)


def read_capture(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def is_complete(payload: Mapping[str, Any] | None) -> bool:
    """A record the stop really finished writing, and could write at all."""
    if not isinstance(payload, Mapping):
        return False
    return str(payload.get("phase") or "") == "stopped" and not payload.get("broken")


__all__ = [
    "RebootWorkCapture",
    "arm",
    "armed",
    "complete",
    "is_complete",
    "safe_name",
    "capture_directory",
    "capture_path",
    "delayed_entry",
    "disarm",
    "interrupted_entry",
    "ready_entry",
    "read_capture",
]
