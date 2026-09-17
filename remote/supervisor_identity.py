"""Deterministic per-instance names for Hashi Remote supervisors."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


_UNSAFE_SLUG_CHARS = re.compile(r"[^a-z0-9_.-]+")
_MAX_SLUG_CHARS = 48


@dataclass(frozen=True)
class RemoteSupervisorIdentity:
    instance_id: str
    instance_slug: str
    systemd_service_name: str
    windows_task_name: str
    source: str
    remote_port: int | None


def _read_json_object(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _configured_instance_id(root: Path) -> str:
    path = root / "agents.json"
    data = _read_json_object(path)
    global_config = data.get("global") or {}
    if not isinstance(global_config, dict):
        return ""
    return str(global_config.get("instance_id") or "").strip()


def configured_remote_port(
    root: Path | str,
    *,
    instance_id: str | None = None,
) -> int | None:
    """Return the instance-owned Remote port, before YAML compatibility defaults."""

    resolved_root = Path(root).expanduser().resolve()
    agents = _read_json_object(resolved_root / "agents.json")
    global_config = agents.get("global") or {}
    if not isinstance(global_config, dict):
        global_config = {}
    effective_id = str(
        instance_id or global_config.get("instance_id") or ""
    ).strip()
    instances_data = _read_json_object(resolved_root / "instances.json")
    instances = instances_data.get("instances") or {}
    if not isinstance(instances, dict):
        instances = {}
    entry = instances.get(effective_id.casefold(), {}) if effective_id else {}
    if not isinstance(entry, dict):
        entry = {}
    value = entry.get("remote_port") or global_config.get("remote_port")
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def normalise_instance_slug(value: str) -> str:
    """Return a bounded name safe for systemd units and scheduled tasks."""

    raw = str(value or "").strip()
    slug = _UNSAFE_SLUG_CHARS.sub("-", raw.casefold()).strip("._-")
    if not slug:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        slug = f"instance-{digest}"
    if len(slug) > _MAX_SLUG_CHARS:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
        slug = f"{slug[: _MAX_SLUG_CHARS - 11].rstrip('._-')}-{digest}"
    return slug


def resolve_supervisor_identity(
    root: Path | str,
    *,
    instance_id: str | None = None,
) -> RemoteSupervisorIdentity:
    resolved_root = Path(root).expanduser().resolve()
    explicit = str(instance_id or "").strip()
    configured = _configured_instance_id(resolved_root)
    if explicit:
        effective_id = explicit
        source = "explicit"
    elif configured:
        effective_id = configured
        source = "agents_json"
    else:
        effective_id = resolved_root.name or "default"
        source = "root_name"
    slug = normalise_instance_slug(effective_id)
    return RemoteSupervisorIdentity(
        instance_id=effective_id,
        instance_slug=slug,
        systemd_service_name=f"hashi-remote-{slug}.service",
        windows_task_name=f"HashiRemote-{slug}",
        source=source,
        remote_port=configured_remote_port(
            resolved_root,
            instance_id=effective_id,
        ),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve per-instance Hashi Remote supervisor names"
    )
    parser.add_argument("--hashi-root", required=True, type=Path)
    parser.add_argument("--instance-id", default=None)
    parser.add_argument("--format", choices=("json", "lines"), default="json")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    identity = resolve_supervisor_identity(
        args.hashi_root,
        instance_id=args.instance_id,
    )
    if args.format == "lines":
        print(identity.instance_id)
        print(identity.instance_slug)
        print(identity.systemd_service_name)
        print(identity.windows_task_name)
        print(identity.source)
    else:
        print(json.dumps(asdict(identity), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
