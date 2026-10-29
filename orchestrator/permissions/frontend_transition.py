"""Shared FC side of Agent-only level adoption, never an execution-host port."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator.permissions.ipc_server import PermissionEndpoint
from orchestrator.permissions.level_transition import FrontendLevelTransitions
from orchestrator.permissions.windows_ipc import WindowsPipeListener, PipePeerPolicy, current_process_user_sid
from orchestrator.permissions.windows_security import require_private_path
from orchestrator.permissions.wire import PermissionServiceRouter


def build_frontend_transition(api):
    raw = getattr(api.global_config, "permission_frontend", None)
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != {"endpoint", "private_key_path", "private_root", "service_sid"}:
        raise ValueError("invalid private permission FC installation")
    sid = current_process_user_sid()
    key_path, private_root = Path(raw["private_key_path"]), Path(raw["private_root"])
    require_private_path(key_path, trusted_sids=(sid,))
    require_private_path(private_root, trusted_sids=(sid,))
    key = Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())
    owner = FrontendLevelTransitions(api, private_root=private_root, trusted_sid=sid)
    return PermissionEndpoint(
        WindowsPipeListener(raw["endpoint"], service_sid=sid, peer_policy=PipePeerPolicy(raw["service_sid"])),
        PermissionServiceRouter(owner, methods=owner.METHODS), signing_key=key,
        service_generation=uuid4().hex,
    )
