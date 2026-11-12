"""Read-only recent-file selection for the planned /getfile command.

This is a derived UI projection, not an audit ledger or an access grant.
PAO must supply trusted, owner/Agent-scoped events and re-authorise the file
at delivery time. No filesystem scan or Fingerprint setting is involved here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
from typing import Any, Iterable, Mapping

_ALLOWED_ACTIONS = frozenset({"created", "modified", "touched"})
_ALLOWED_EVIDENCE = frozenset({"verified", "engine_reported", "interval_observation"})
_ROOT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_CANDIDATES = 10000


def _normalised_candidate(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    root_id = raw.get("root_id")
    original = raw.get("relative_path")
    action = raw.get("action")
    evidence = raw.get("evidence")
    when = raw.get("observed_at")
    event_id = raw.get("source_event_id")
    if not isinstance(root_id, str) or not _ROOT_ID.fullmatch(root_id):
        return None
    if not isinstance(original, str) or not original or len(original) > 4096:
        return None
    if "\\x00" in original or "\\" in original or original.startswith("/"):
        return None
    if any(part in {"", ".", ".."} for part in original.split("/")):
        return None
    relative = PurePosixPath(original).as_posix()
    if not relative or relative == "." or relative.startswith("../"):
        return None
    if action not in _ALLOWED_ACTIONS or evidence not in _ALLOWED_EVIDENCE:
        return None
    # Provider reports/interval observations cannot prove create vs modify.
    if evidence != "verified" and action != "touched":
        return None
    if not isinstance(when, str) or not isinstance(event_id, str) or not event_id:
        return None
    try:
        timestamp = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        return None
    return {
        "root_id": root_id,
        "relative_path": relative,
        "action": action,
        "evidence": evidence,
        "observed_at": timestamp.astimezone(timezone.utc).isoformat(),
        "source_event_id": event_id[:160],
    }


def select_recent_agent_files(
    records: Iterable[Mapping[str, Any]], *, limit: int = 10,
    case_sensitive: bool = True,
) -> list[dict[str, Any]]:
    """Return newest distinct, supported file observations.

    Input is already scoped by PAO to one Instance/Agent. Paths are relative
    to trusted root identities: do not present these values as authority.
    Callers must still verify existence, roots, current owner and file bytes.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    if type(case_sensitive) is not bool:
        raise ValueError("case_sensitive must be boolean")
    candidates = []
    for index, raw in enumerate(records):
        if index >= _MAX_CANDIDATES:
            raise ValueError("too many recent-file candidates")
        normalised = _normalised_candidate(raw)
        if normalised is not None:
            candidates.append((normalised["observed_at"], index, normalised))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    result = []
    seen = set()
    for _timestamp, _index, item in candidates:
        path_key = item["relative_path"] if case_sensitive else item["relative_path"].casefold()
        key = (item["root_id"], path_key)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) == limit:
            break
    return result
