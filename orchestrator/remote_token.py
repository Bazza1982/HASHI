"""Owner-facing management of the HASHI Remote shared token.

The Shared Token is the HMAC credential HASHI Remote and Workbench use to
authenticate each other. Before this module it could only be set by editing
secrets.json or exporting HASHI_REMOTE_SHARED_TOKEN, and an explicit clear was
impossible because startup provisioning recreated it.

The public surface never returns the credential. It reports a masked form, a
short SHA-256 fingerprint, a non-secret file revision, and the Remote service's
currently loaded credential generation read from the unauthenticated loopback
health endpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from orchestrator.config_json import (
    ConfigConflictError,
    read_config_json,
    write_config_json,
)
from orchestrator.workbench_auth import (
    REMOTE_SHARED_TOKEN_DISABLED_KEY,
    resolve_remote_runtime_sids,
)
from remote.security.shared_token import load_shared_token_snapshot

TOKEN_KEY = "hashi_remote_shared_token"
TOKEN_MIN_LENGTH = 16
TOKEN_MAX_LENGTH = 512
FINGERPRINT_LENGTH = 12
REVISION_DISPLAY_LENGTH = 12


class RemoteTokenError(RuntimeError):
    """A failure the command layer can render as one readable class."""

    def __init__(self, message: str, *, error_code: str = "remote_token_error", detail: str = "") -> None:
        super().__init__(message)
        self.error_code = error_code
        self.detail = detail


@dataclass(frozen=True)
class RemoteTokenStatus:
    configured: bool
    source: str
    masked: str = ""
    fingerprint: str = ""
    revision: str = ""
    disabled: bool = False
    environment_locked: bool = False
    error: str = ""
    remote: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def token_issue(value: object) -> str:
    """Returns '' when usable, otherwise a stable reason code."""
    if not isinstance(value, str):
        return "missing"
    token = value.strip()
    if not token:
        return "missing"
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in token):
        return "contains_whitespace_or_control"
    if len(token) < TOKEN_MIN_LENGTH:
        return "too_short"
    if len(token) > TOKEN_MAX_LENGTH:
        return "too_long"
    return ""


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def mask_token(token: str) -> str:
    value = str(token or "")
    return f"••••{value[-4:]}" if value else ""


def _display_revision(revision: str) -> str:
    return str(revision or "")[:REVISION_DISPLAY_LENGTH]


def fetch_remote_credential(port: int, *, host: str = "127.0.0.1", timeout: float = 1.5) -> dict:
    """Read the Remote service's loaded credential generation over loopback.

    /health needs no credential, so this cannot leak the token and cannot fail
    because of the token itself. A missing service is reported, never guessed.
    """
    if not port:
        return {"reachable": False, "reason": "port_unknown"}
    url = f"http://{host}:{int(port)}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return {"reachable": False, "reason": "unreachable"}
    credential = payload.get("credential") if isinstance(payload, dict) else None
    if not isinstance(credential, dict):
        return {"reachable": True, "reason": "credential_missing"}
    return {
        "reachable": True,
        "configured": bool(credential.get("configured")),
        "generation": int(credential.get("generation") or 0),
        "last_reload_at": int(credential.get("last_reload_at") or 0),
        "configuration_state": str(credential.get("configuration_state") or ""),
        "configuration_error": str(credential.get("configuration_error") or ""),
    }


def load_status(
    hashi_root: Path | str,
    *,
    secrets_path: Path | str | None = None,
    env: dict | None = None,
    remote_credential: dict | None = None,
) -> RemoteTokenStatus:
    root = Path(hashi_root)
    secrets = Path(secrets_path) if secrets_path else root / "secrets.json"
    snapshot = load_shared_token_snapshot(root)
    environment = env if env is not None else os.environ
    env_token = str(environment.get("HASHI_REMOTE_SHARED_TOKEN", "") or "").strip()

    disabled = False
    try:
        document = read_config_json(secrets)
        disabled = bool(document.get(REMOTE_SHARED_TOKEN_DISABLED_KEY))
    except (OSError, ValueError, TypeError):
        document = None

    if env_token:
        source, token = "environment", env_token
    elif snapshot.token:
        source, token = "secrets", snapshot.token
    elif disabled:
        source, token = "disabled", ""
    elif snapshot.state == "invalid":
        source, token = "invalid", ""
    else:
        source, token = "absent", ""

    return RemoteTokenStatus(
        configured=bool(token),
        source=source,
        masked=mask_token(token),
        fingerprint=token_fingerprint(token) if token else "",
        revision=_display_revision(snapshot.revision),
        disabled=disabled,
        environment_locked=bool(env_token),
        error=str(snapshot.error or ""),
        remote=dict(remote_credential or {}),
    )


def _private_sids(root: Path, private_sids) -> tuple[str, ...]:
    if private_sids is not None:
        return tuple(private_sids)
    return tuple(resolve_remote_runtime_sids(root))


def _publish(secrets: Path, payload, revision, root: Path, private_sids) -> str:
    try:
        return write_config_json(
            secrets,
            payload,
            expected_revision=revision,
            private_full_control_sids=_private_sids(root, private_sids),
        )
    except ConfigConflictError as exc:
        raise RemoteTokenError(
            "The secrets file changed while the new token was being prepared.",
            error_code="remote_token_revision_conflict",
        ) from exc
    except (OSError, ValueError) as exc:
        raise RemoteTokenError(
            "The secrets file could not be written; nothing was changed.",
            error_code="remote_token_write_failed",
        ) from exc


def replace_token(
    hashi_root: Path | str,
    token: str,
    *,
    secrets_path: Path | str | None = None,
    expected_revision: str | None = None,
    private_sids: tuple[str, ...] | None = None,
) -> RemoteTokenStatus:
    issue = token_issue(token)
    if issue:
        raise RemoteTokenError(
            "The Shared Token is not valid.", error_code="remote_token_invalid", detail=issue
        )
    if str(os.environ.get("HASHI_REMOTE_SHARED_TOKEN", "") or "").strip():
        raise RemoteTokenError(
            "HASHI_REMOTE_SHARED_TOKEN is set; the environment value wins and cannot be overridden here.",
            error_code="remote_token_environment_locked",
        )
    root = Path(hashi_root)
    secrets = Path(secrets_path) if secrets_path else root / "secrets.json"
    document = read_config_json(secrets) if secrets.exists() else None
    if expected_revision is not None and document is not None and document.revision != expected_revision:
        raise RemoteTokenError(
            "The secrets file changed since the token was read.",
            error_code="remote_token_revision_conflict",
        )
    payload = dict(document or {})
    payload[TOKEN_KEY] = token.strip()
    payload.pop(REMOTE_SHARED_TOKEN_DISABLED_KEY, None)
    _publish(secrets, payload, document.revision if document is not None else None, root, private_sids)
    return load_status(root, secrets_path=secrets)


def clear_token(
    hashi_root: Path | str,
    *,
    secrets_path: Path | str | None = None,
    expected_revision: str | None = None,
    private_sids: tuple[str, ...] | None = None,
) -> RemoteTokenStatus:
    if str(os.environ.get("HASHI_REMOTE_SHARED_TOKEN", "") or "").strip():
        raise RemoteTokenError(
            "HASHI_REMOTE_SHARED_TOKEN is set; remove the environment value first.",
            error_code="remote_token_environment_locked",
        )
    root = Path(hashi_root)
    secrets = Path(secrets_path) if secrets_path else root / "secrets.json"
    document = read_config_json(secrets) if secrets.exists() else None
    if expected_revision is not None and document is not None and document.revision != expected_revision:
        raise RemoteTokenError(
            "The secrets file changed since the token was read.",
            error_code="remote_token_revision_conflict",
        )
    payload = dict(document or {})
    payload.pop(TOKEN_KEY, None)
    payload[REMOTE_SHARED_TOKEN_DISABLED_KEY] = True
    _publish(secrets, payload, document.revision if document is not None else None, root, private_sids)
    return load_status(root, secrets_path=secrets)
