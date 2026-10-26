"""FC loopback credential bootstrap for an already authenticated Remote bridge."""
from __future__ import annotations

import os
import secrets

from orchestrator.config_json import ConfigDocument, write_config_json


def ensure_local_api_credential(document: ConfigDocument, *, profile: str) -> ConfigDocument:
    """Retain existing credentials; publish a missing personal loopback key once.

    Remote already reads this canonical key before each loopback request.  A
    private, revision-checked publication lets that existing hop authenticate
    without accepting anonymous question reads or exposing the network key.
    """
    if profile != "personal":
        return document
    current = document.get("workbench_admin_token")
    if current is not None and not isinstance(current, str):
        raise ValueError("workbench_admin_token must be a string")
    if isinstance(current, str) and current.strip():
        return document
    network = document.get("hashi_remote_shared_token")
    if not ((isinstance(network, str) and network.strip())
            or os.environ.get("HASHI_REMOTE_SHARED_TOKEN", "").strip()):
        return document
    updated = document.copy()
    updated["workbench_admin_token"] = secrets.token_urlsafe(48)
    updated.revision = write_config_json(updated.source, updated)
    return updated
