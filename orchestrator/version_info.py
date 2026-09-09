"""Single read-only source of structured HASHI version facts."""

from __future__ import annotations

import json
import os
import platform
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.build_provenance import (
    PROVENANCE_SCHEMA_VERSION,
    inspect_source_checkout,
    product_version,
)
from remote.runtime_identity import pid_is_alive, read_runtime_claim


VERSION_PAYLOAD_SCHEMA = 1
_MAX_ARTIFACT_METADATA_BYTES = 2 * 1024 * 1024
_PROVENANCE_FIELDS = frozenset(
    {
        "schema_version",
        "product_version",
        "artifact_kind",
        "release_channel",
        "commit",
        "branch",
        "tag",
        "commit_time",
        "build_time",
        "dirty_at_build",
        "change_count_at_build",
        "generation_id",
        "verifiable",
        "build_id",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _environment_kind() -> str:
    system = platform.system().casefold()
    if system == "linux":
        try:
            marker = Path("/proc/version").read_text(
                encoding="utf-8", errors="ignore"
            ).casefold()
        except OSError:
            marker = ""
        return "wsl" if "microsoft" in marker or "wsl" in marker else "linux"
    if system == "darwin":
        return "macos"
    return system or "unknown"


def _generation_metadata_path(bridge_home: Path, generation_id: str) -> Path | None:
    value = str(generation_id or "").strip()
    if not value.startswith("sha256:"):
        return None
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return None
    return bridge_home / "state" / "function_generations" / digest / "function-generation.json"


def generation_provenance(
    bridge_home: Path | str,
    generation_id: str | None,
) -> dict[str, Any] | None:
    """Load provenance only from the exact immutable generation requested."""

    if not generation_id:
        return None
    path = _generation_metadata_path(Path(bridge_home).resolve(), generation_id)
    if path is None:
        return None
    try:
        if not path.is_file() or path.stat().st_size > _MAX_ARTIFACT_METADATA_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("generation_id") != generation_id:
        return None
    provenance = value.get("provenance")
    if not isinstance(provenance, dict):
        # An artifact created before provenance support is deliberately legacy;
        # never backfill it from the checkout currently on disk.
        return None
    if provenance.get("schema_version") != PROVENANCE_SCHEMA_VERSION:
        return None
    if provenance.get("generation_id") != generation_id:
        return None
    return {
        str(key): item
        for key, item in provenance.items()
        if key in _PROVENANCE_FIELDS
    }


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _fingerprint_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _mapping(to_dict())
        except Exception:
            return {}
    return _mapping(value)


def _worker_row(value: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _mapping(value.get("metadata"))
    return {
        "agent": str(
            value.get("agent")
            or value.get("name")
            or value.get("id")
            or metadata.get("name")
            or metadata.get("id")
            or ""
        ),
        "pid": int(
            value.get("pid")
            or value.get("worker_pid")
            or metadata.get("worker_pid")
            or 0
        ),
        "generation_id": str(
            value.get("generation_id") or metadata.get("generation_id") or ""
        ),
        "adopted_at": (
            value.get("adopted_at") or metadata.get("worker_adopted_at") or None
        ),
        "phase": str(value.get("phase") or metadata.get("worker_phase") or ""),
        "accepting": bool(
            value.get("accepting", metadata.get("worker_accepting", False))
        ),
    }


def _live_worker_rows(owner: Any, topology: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    runtimes = getattr(owner, "runtimes", None)
    if isinstance(runtimes, list):
        for runtime in runtimes:
            if not getattr(runtime, "is_function_worker_proxy", False):
                continue
            rows.append(
                _worker_row(
                    {
                        "agent": getattr(runtime, "name", ""),
                        "pid": getattr(runtime, "worker_pid", 0),
                        "generation_id": getattr(runtime, "generation_id", ""),
                        "metadata": getattr(runtime, "metadata", {}),
                    }
                )
            )
    if rows:
        return rows
    values = topology.get("function_workers") or ()
    return [_worker_row(item) for item in values if isinstance(item, Mapping)]


def _subject_context(
    subject: Any,
    *,
    agent_name: str | None,
) -> tuple[Any, Any | None, str | None, dict[str, Any]]:
    runtime = None
    owner = subject
    if getattr(subject, "orchestrator", None) is not None and not getattr(
        subject, "function_workers", None
    ):
        runtime = subject
        owner = subject.orchestrator
        agent_name = agent_name or str(getattr(subject, "name", "") or "") or None
    topology_reader = getattr(owner, "version_topology", None)
    if callable(topology_reader):
        topology = _mapping(topology_reader())
    else:
        topology = _mapping(getattr(owner, "_topology", {}))
    return owner, runtime, agent_name, topology


def _remote_fact(bridge_home: Path, instance_id: str) -> dict[str, Any]:
    claim = read_runtime_claim(bridge_home)
    if not isinstance(claim, dict):
        return {"status": "offline", "pid": None, "started_at": None, "provenance": None}
    pid = int(claim.get("pid") or 0)
    if (
        not pid_is_alive(pid)
        or str(claim.get("instance_id") or "").strip().upper()
        not in {"", instance_id.upper()}
    ):
        return {"status": "offline", "pid": pid or None, "started_at": None, "provenance": None}
    started = claim.get("started_at")
    if isinstance(started, (int, float)):
        started = (
            datetime.fromtimestamp(float(started), tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    provenance = claim.get("provenance")
    if not isinstance(provenance, dict):
        provenance = None
    else:
        provenance = {
            str(key): value
            for key, value in provenance.items()
            if key in _PROVENANCE_FIELDS
        }
    return {
        "status": "active" if provenance else "legacy",
        "pid": pid,
        "started_at": started,
        "generation_id": (
            str(provenance.get("build_id") or "") if provenance else None
        ),
        "product_version": (
            str(provenance.get("product_version") or "") if provenance else None
        ),
        "provenance": provenance,
    }


def collect_version_payload(
    subject: Any,
    *,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Collect the payload used by Workbench, API, Telegram and Remote."""

    owner, runtime, selected_agent, topology = _subject_context(
        subject, agent_name=agent_name
    )
    paths = getattr(owner, "paths", None)
    if paths is None:
        raise ValueError("HASHI runtime paths are unavailable")
    code_root = Path(getattr(paths, "code_root")).resolve()
    bridge_home = Path(getattr(paths, "bridge_home")).resolve()
    global_cfg = getattr(owner, "global_cfg", None) or getattr(
        owner, "global_config", None
    )
    instance_id = str(getattr(global_cfg, "instance_id", None) or "HASHI").upper()
    display_name = str(getattr(global_cfg, "display_name", None) or instance_id)

    shared = _mapping(topology.get("shared_functions"))
    if not shared:
        shared = {
            "pid": os.getpid(),
            "generation_id": getattr(owner, "shared_generation_id", None),
            "adopted_at": getattr(owner, "shared_adopted_at", None),
        }
    workers = _live_worker_rows(owner, topology)
    if runtime is not None:
        own = _mapping(getattr(runtime, "_worker_version_metadata", {}))
        if own:
            own.setdefault("agent", selected_agent)
            row = _worker_row(own)
            workers = [item for item in workers if item["agent"] != row["agent"]]
            workers.append(row)
    workers.sort(key=lambda item: item["agent"].casefold())
    current_worker = next(
        (item for item in workers if item["agent"] == selected_agent), None
    )

    provenance_cache: dict[str, dict[str, Any] | None] = {}

    def provenance_for(generation_id: Any) -> dict[str, Any] | None:
        key = str(generation_id or "")
        if key not in provenance_cache:
            provenance_cache[key] = generation_provenance(bridge_home, key)
        return provenance_cache[key]

    shared["generation_id"] = str(shared.get("generation_id") or "")
    shared["pid"] = int(shared.get("pid") or 0) or None
    shared["adopted_at"] = shared.get("adopted_at") or None
    shared["provenance"] = provenance_for(shared["generation_id"])
    for worker in workers:
        worker["provenance"] = provenance_for(worker["generation_id"])

    primary = (
        current_worker.get("provenance") if current_worker else None
    ) or shared.get("provenance")
    primary = _mapping(primary)
    running_commit = str(primary.get("commit") or "") or None
    source = inspect_source_checkout(code_root, running_commit=running_commit)
    remote = _remote_fact(bridge_home, instance_id)

    generations = {
        str(value)
        for value in [shared.get("generation_id"), *(row.get("generation_id") for row in workers)]
        if value
    }
    reasons: list[str] = []
    mixed = len(generations) > 1
    if mixed:
        reasons.append("function_worker_generations_differ")
    remote_commit = str(
        ((_mapping(remote.get("provenance"))).get("commit") or "")
    )
    if running_commit and remote_commit and running_commit != remote_commit:
        mixed = True
        reasons.append("remote_commit_differs")

    if mixed:
        state_code = "mixed_generations"
    elif not running_commit:
        state_code = "provenance_unavailable"
        reasons.append("running_commit_unavailable")
    elif source.get("available"):
        relation = str(source.get("relation") or "unknown")
        if relation == "same" and source.get("dirty"):
            state_code = "source_dirty"
            reasons.append("source_has_uncommitted_changes")
        elif relation == "same":
            state_code = "running_matches_source"
        elif relation in {"source_ahead", "running_ahead", "diverged"}:
            state_code = relation
            reasons.append(f"commit_relation_{relation}")
        else:
            state_code = "provenance_unavailable"
            reasons.append("commit_relation_unknown")
    else:
        state_code = "packaged_release"

    fingerprint = _fingerprint_mapping(
        getattr(owner, "runtime_fingerprint", None)
        or getattr(runtime, "_runtime_fingerprint", None)
        or topology.get("runtime")
    )
    product = str(primary.get("product_version") or product_version(code_root))
    adopted_at = (
        current_worker.get("adopted_at") if current_worker else None
    ) or shared.get("adopted_at")
    return {
        "ok": True,
        "schema_version": VERSION_PAYLOAD_SCHEMA,
        "collected_at": _utc_now(),
        "product": {"name": "HASHI", "version": product},
        "instance": {
            "id": instance_id,
            "display_name": display_name,
            "environment": _environment_kind(),
            "python": fingerprint.get("python") or platform.python_version(),
        },
        "running": {
            "branch": primary.get("branch"),
            "tag": primary.get("tag"),
            "commit": running_commit,
            "commit_time": primary.get("commit_time"),
            "build_id": primary.get("build_id"),
            "build_time": primary.get("build_time"),
            "release_channel": primary.get("release_channel"),
            "dirty_at_build": primary.get("dirty_at_build"),
            "adopted_at": adopted_at,
            "functions": shared,
            "worker": current_worker,
            "workers": workers,
            "mixed_workers": len(generations) > 1,
            "remote": remote,
        },
        "source": source,
        "compatibility": {
            "core_api": fingerprint.get("core_api"),
            "function_api": fingerprint.get("function_api"),
            "worker_protocol": fingerprint.get("worker_protocol"),
            "generation_schema": fingerprint.get("generation_schema"),
            "python": fingerprint.get("python") or platform.python_version(),
            "platform_abi": fingerprint.get("platform_abi"),
            "runtime_id": fingerprint.get("runtime_id") or fingerprint.get("id"),
        },
        "state": {"code": state_code, "reasons": reasons},
    }
