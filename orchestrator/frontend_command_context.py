"""Carry an already-authenticated frontend principal through native handlers.

The Backend API and its private Worker transport establish the owner. This
short-lived context carries that existing decision; it does not authenticate
clients or change Telegram ingress authorization.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


@dataclass
class _CommandPrincipal:
    runtime: Any
    owner_id: str
    active: bool = True


_current: ContextVar[_CommandPrincipal | None] = ContextVar(
    "hashi_frontend_command_principal", default=None
)


@contextmanager
def bind_frontend_command_principal(
    runtime: Any, owner_id: str | None
) -> Iterator[None]:
    owner = str(owner_id or "").strip()
    if not owner:
        yield
        return
    principal = _CommandPrincipal(runtime, owner)
    token = _current.set(principal)
    try:
        yield
    finally:
        # A task spawned by a handler may inherit the context; it must not
        # retain the command's authority after the handler has returned.
        principal.active = False
        _current.reset(token)


def is_frontend_command_principal(runtime: Any, actor_id: Any) -> bool:
    principal = _current.get()
    return bool(
        principal is not None
        and principal.active
        and principal.runtime is runtime
        and isinstance(actor_id, str)
        and actor_id == principal.owner_id
    )


def frontend_command_owner(runtime: Any) -> str | None:
    """Return the current trusted command owner while dispatch is active."""
    principal = _current.get()
    if principal is None or not principal.active or principal.runtime is not runtime:
        return None
    return principal.owner_id


def command_owner_for_instance(root: Path) -> str | None:
    """Return the live authenticated command owner for this exact instance.

    The Backend/Worker boundary establishes this context. Governed callers
    carry the canonical enterprise user selected by that boundary; this
    function does not grant any role or transfer permission.
    """
    principal = _current.get()
    if principal is None or not principal.active:
        return None
    config = getattr(principal.runtime, "global_config", None)
    if config is None:
        return None
    profile = str(getattr(config, "deployment_profile", "personal") or "personal")
    if profile != "personal" and (
        profile not in {"team", "enterprise"}
        or not principal.owner_id.startswith("enterprise:")
        or not principal.owner_id.removeprefix("enterprise:").strip()
    ):
        return None
    home = getattr(config, "bridge_home", None)
    config_path = getattr(config, "config_path", None)
    instance_root = home or (Path(config_path).parent if config_path else getattr(config, "project_root", None))
    if instance_root is None or Path(instance_root).resolve() != Path(root).resolve():
        return None
    return principal.owner_id


def transfer_command_owner_for_instance(
    root: Path, *, allow_expired: bool = False,
) -> str | None:
    """Select a live principal without falling back across instances or lifetimes."""
    principal = _current.get()
    if principal is None:
        return None
    if not principal.active:
        if allow_expired:
            return None
        raise ValueError("frontend command context has expired")
    owner = command_owner_for_instance(root)
    if owner is None:
        raise ValueError("frontend command belongs to a different instance or has an invalid principal")
    return owner


def personal_command_owner_for_instance(root: Path) -> str | None:
    """Compatibility helper limited to a live personal command principal."""
    principal = _current.get()
    if principal is None or not principal.active:
        return None
    config = getattr(principal.runtime, "global_config", None)
    if config is None or str(getattr(config, "deployment_profile", "personal") or "personal") != "personal":
        return None
    return command_owner_for_instance(root)


def frontend_command_policy_context(runtime: Any) -> dict[str, str]:
    """Identify the authenticated governed user to the existing policy owner.

    The owner is established by the Backend API, not the command payload. A
    stale context inherited by a spawned task has no authority after dispatch.
    """
    principal = _current.get()
    if principal is None or not principal.active or principal.runtime is not runtime:
        return {}
    if not principal.owner_id.startswith("enterprise:"):
        return {}
    user_id = principal.owner_id.removeprefix("enterprise:").strip()
    if not user_id:
        return {}
    return {"actor_id": user_id, "user_id": user_id}
