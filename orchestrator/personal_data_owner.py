"""Stable personal-instance data binding, independent of connector accounts.

This is a local storage lookup, never an authentication or transfer credential.
On first establishment it adopts only the existing configured data key; it
does not infer ownership of unrelated historical rows.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from orchestrator.config_json import ConfigConflictError, read_config_json, write_config_json
from orchestrator.telegram_identity import resolve_owner_scope_id


def read_personal_data_binding(root: Path | str) -> tuple[str, bool] | None:
    path = Path(root).expanduser().resolve() / "state" / "personal_data_owner.json"
    try:
        binding = read_config_json(path)
    except FileNotFoundError:
        return None
    owner = binding.get("data_owner_id")
    known = binding.get("legacy_owner_known", False)
    if (type(binding.get("version")) is not int or binding.get("version") != 1
            or not isinstance(owner, str)
            or not owner.strip() or type(known) is not bool):
        raise ValueError("personal data owner binding is invalid")
    return owner.strip(), known


def read_personal_data_owner(root: Path | str) -> str | None:
    binding = read_personal_data_binding(root)
    return binding[0] if binding is not None else None


def read_personal_data_owner_for_config(
    root: Path | str, global_config: Mapping[str, Any]
) -> str | None:
    from orchestrator.enterprise.profile import resolve_deployment_profile

    if resolve_deployment_profile(dict(global_config)).value != "personal":
        return None
    return read_personal_data_owner(root)


def project_legacy_data_owner(config_path: Path | str, owner: str) -> bool:
    """Publish only a missing rollback-era data key; never overwrite an existing key."""
    from orchestrator.agent_config_json import write_agent_config_json

    for _ in range(3):
        try:
            document = read_config_json(config_path)
            global_config = document.get("global")
            if not isinstance(global_config, dict):
                return False
            current = str(global_config.get("owner_scope_id") or "").strip()
            if current:
                return current == owner
            global_config["owner_scope_id"] = owner
            write_agent_config_json(config_path, document)
            return True
        except ConfigConflictError:
            continue
        except (OSError, ValueError):
            return False
    return False


def establish_handoff_data_binding(root: Path | str, value: Mapping[str, Any]) -> str:
    """Adopt the retiring process's local data key, never an external credential."""
    if not isinstance(value, Mapping):
        raise ValueError("personal data handoff binding is invalid")
    owner = value.get("data_owner_id")
    known = value.get("legacy_owner_known")
    if (type(value.get("version")) is not int or value.get("version") != 1
            or not isinstance(owner, str) or not owner.strip() or type(known) is not bool):
        raise ValueError("personal data handoff binding is invalid")
    expected = (owner.strip(), known)
    existing = read_personal_data_binding(root)
    if existing is not None:
        if existing != expected:
            raise ValueError("personal data handoff binding conflicts with the existing binding")
        return existing[0]
    path = Path(root).expanduser().resolve() / "state" / "personal_data_owner.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_config_json(path, {
            "version":1, "data_owner_id":expected[0], "legacy_owner_known":expected[1],
        }, expected_revision=None)
    except ConfigConflictError:
        # Compare the winning binding instead of overwriting a concurrent owner.
        pass
    actual = read_personal_data_binding(root)
    if actual != expected:
        raise ValueError("personal data handoff binding conflicts with the existing binding")
    return expected[0]


def resolve_personal_data_owner(
    root: Path | str,
    global_config: Mapping[str, Any],
    legacy_telegram_owner: int | None,
    *,
    establish: bool = False,
) -> str:
    existing = read_personal_data_owner(root)
    if existing is not None:
        return existing
    candidate = resolve_owner_scope_id(global_config, legacy_telegram_owner)
    if not establish:
        return candidate
    path = Path(root).expanduser().resolve() / "state" / "personal_data_owner.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_owner_known = bool(
        str(global_config.get("owner_scope_id") or "").strip() or legacy_telegram_owner
    )
    try:
        write_config_json(
            path,
            {"version": 1, "data_owner_id": candidate, "legacy_owner_known": legacy_owner_known},
            expected_revision=None,
        )
    except ConfigConflictError:
        # Another bootstrap established the binding; use its published value.
        pass
    published = read_personal_data_owner(root)
    if published is None:
        raise ValueError("personal data owner binding could not be established")
    return published
