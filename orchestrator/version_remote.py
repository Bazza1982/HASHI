"""Authenticated cross-instance transport and stale-cache policy for /version."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from orchestrator.agent_move.remote_client import (
    AgentMoveRemoteError,
    candidate_remote_base_urls,
    request_authenticated_json,
)


VERSION_CACHE_SCHEMA = 1


class VersionQueryError(RuntimeError):
    """A trusted instance name or authenticated version query failed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_instance_entry(
    instances: Mapping[str, Any],
    query: str,
) -> tuple[str, dict[str, Any]]:
    """Resolve exact ID/display aliases and fail closed on ambiguity."""

    wanted = str(query or "").strip().removeprefix("@").casefold()
    if not wanted:
        raise VersionQueryError("unknown_instance", "instance name is empty")
    matches: dict[str, dict[str, Any]] = {}
    for key, raw in instances.items():
        if not isinstance(raw, Mapping):
            continue
        entry = dict(raw)
        instance_id = str(entry.get("instance_id") or key).strip().upper()
        aliases = {
            str(key).strip().removeprefix("@").casefold(),
            instance_id.casefold(),
            str(entry.get("display_name") or "").strip().casefold(),
            str(entry.get("display_handle") or "").strip().removeprefix("@").casefold(),
        }
        if wanted in aliases:
            matches[instance_id] = entry
    if not matches:
        raise VersionQueryError("unknown_instance", f"unknown instance: {query}")
    if len(matches) != 1:
        raise VersionQueryError("ambiguous_instance", f"ambiguous instance: {query}")
    return next(iter(matches.items()))


def query_remote_version(
    entry: Mapping[str, Any],
    *,
    target_instance: str,
    source_instance: str,
    shared_token: str,
    timeout: int = 8,
) -> dict[str, Any]:
    """Fetch one payload whose request and response are mutually authenticated."""

    target = str(target_instance).strip().upper()
    source = str(source_instance).strip().upper()
    if not shared_token:
        raise VersionQueryError(
            "pairing_required", "HASHI Remote shared-token pairing is required"
        )
    errors: list[str] = []
    try:
        candidates = candidate_remote_base_urls(entry)
    except AgentMoveRemoteError as exc:
        raise VersionQueryError("invalid_route", str(exc)) from exc
    for base_url in candidates:
        try:
            payload = request_authenticated_json(
                f"{base_url}/version/v1",
                shared_token=shared_token,
                from_instance=source,
                timeout=timeout,
            )
        except (AgentMoveRemoteError, OSError, TimeoutError) as exc:
            errors.append(str(exc))
            continue
        identity = str((payload.get("instance") or {}).get("id") or "").upper()
        authenticated = str(payload.get("authenticated_instance") or "").upper()
        if identity != target:
            errors.append(f"authenticated endpoint answered as {identity or 'unknown'}")
            continue
        if authenticated != source:
            errors.append(
                f"receiver authenticated requester as {authenticated or 'unknown'}"
            )
            continue
        if int(payload.get("schema_version") or 0) != 1:
            errors.append("unsupported version payload schema")
            continue
        payload.pop("authenticated_instance", None)
        payload.pop("authenticated_response_proof", None)
        return payload
    detail = "; ".join(errors[-3:]) or "no usable authenticated endpoint"
    raise VersionQueryError("unreachable", f"{target} version query failed: {detail}")


def _cache_path(bridge_home: Path | str) -> Path:
    return Path(bridge_home).resolve() / "state" / "version_cache.json"


def load_version_cache(bridge_home: Path | str) -> dict[str, dict[str, Any]]:
    path = _cache_path(bridge_home)
    try:
        if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("schema_version") != VERSION_CACHE_SCHEMA:
        return {}
    instances = value.get("instances")
    if not isinstance(instances, dict):
        return {}
    return {
        str(key).upper(): dict(item)
        for key, item in instances.items()
        if isinstance(item, Mapping)
    }


def save_version_cache(
    bridge_home: Path | str,
    instances: Mapping[str, Mapping[str, Any]],
) -> None:
    """Atomically retain last authenticated facts; callers always label them stale."""

    path = _cache_path(bridge_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    payload = {
        "schema_version": VERSION_CACHE_SCHEMA,
        "instances": {
            str(key).upper(): dict(value) for key, value in instances.items()
        },
    }
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
