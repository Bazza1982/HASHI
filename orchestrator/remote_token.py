"""Owner-facing management of the HASHI Remote shared token.

The Shared Token is the HMAC credential HASHI Remote and Workbench use to
authenticate each other. Before this module it could only be set by editing
secrets.json or exporting HASHI_REMOTE_SHARED_TOKEN, and an explicit clear was
impossible because startup provisioning recreated it.

The public surface never returns the credential. It reports a masked form, a
short SHA-256 fingerprint and a non-secret file revision. Whether the running
Remote service actually accepts the credential is answered by one real signed
request to an existing protected endpoint, never by comparing configuration
flags.
"""

from __future__ import annotations

import hashlib
import os
import ssl
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
from remote.security.shared_token import build_auth_headers, load_shared_token_snapshot

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
    verification: dict = field(default_factory=dict)

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


VERIFY_PATH = "/workbench/v1/status"


def verify_credential(
    token: str,
    *,
    port: int | None,
    use_tls: bool = False,
    from_instance: str = "HASHI",
    host: str = "127.0.0.1",
    timeout: float = 2.5,
) -> dict:
    """Proves a credential against the Remote service with one real signed request.

    The signature is the same HMAC the Remote service already enforces on its
    protected Workbench endpoint, so a success means the running service is
    actually accepting this credential - not that two configuration flags agree.
    """
    value = str(token or "").strip()
    if not value:
        return {"outcome": "missing", "status": 0, "path": VERIFY_PATH}
    if not port:
        return {"outcome": "unavailable", "status": 0, "path": VERIFY_PATH, "reason": "port_unknown"}
    scheme = "https" if use_tls else "http"
    url = f"{scheme}://{host}:{int(port)}{VERIFY_PATH}"
    try:
        headers = build_auth_headers(
            shared_token=value,
            method="GET",
            path=VERIFY_PATH,
            from_instance=from_instance or "HASHI",
            body_bytes=b"",
        )
    except Exception:  # pragma: no cover - defensive: signing must not leak the value
        return {"outcome": "failed", "status": 0, "path": VERIFY_PATH, "reason": "signing_failed"}
    request = urllib.request.Request(url, method="GET", headers=headers)
    context = None
    if use_tls:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310 - loopback only
            status = int(getattr(response, "status", 0) or 0)
    except urllib.error.HTTPError as exc:
        status = int(exc.code or 0)
    except (urllib.error.URLError, OSError, ssl.SSLError):
        return {"outcome": "unavailable", "status": 0, "path": VERIFY_PATH, "reason": "transport_failure"}
    if 200 <= status < 300:
        return {"outcome": "verified", "status": status, "path": VERIFY_PATH}
    if status in (401, 403):
        return {"outcome": "rejected", "status": status, "path": VERIFY_PATH}
    return {"outcome": "failed", "status": status, "path": VERIFY_PATH}


def _resolve(root: Path, secrets: Path, env: dict | None):
    """Resolve the effective credential. The value is returned, never rendered."""
    snapshot = load_shared_token_snapshot(root)
    environment = env if env is not None else os.environ
    env_token = str(environment.get("HASHI_REMOTE_SHARED_TOKEN", "") or "").strip()

    disabled = False
    try:
        document = read_config_json(secrets)
        disabled = bool(document.get(REMOTE_SHARED_TOKEN_DISABLED_KEY))
    except (OSError, ValueError, TypeError):
        pass

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
    return snapshot, disabled, source, token


def effective_token(
    hashi_root: Path | str,
    *,
    secrets_path: Path | str | None = None,
    env: dict | None = None,
) -> tuple[str, str]:
    """(source, value) of the credential that is actually in force.

    The value is only ever handed to the HMAC signer; it must not be rendered,
    logged or persisted anywhere else.
    """
    root = Path(hashi_root)
    secrets = Path(secrets_path) if secrets_path else root / "secrets.json"
    _snapshot, _disabled, source, token = _resolve(root, secrets, env)
    return source, token


def load_status(
    hashi_root: Path | str,
    *,
    secrets_path: Path | str | None = None,
    env: dict | None = None,
    verification: dict | None = None,
) -> RemoteTokenStatus:
    root = Path(hashi_root)
    secrets = Path(secrets_path) if secrets_path else root / "secrets.json"
    snapshot, disabled, source, token = _resolve(root, secrets, env)

    return RemoteTokenStatus(
        configured=bool(token),
        source=source,
        masked=mask_token(token),
        fingerprint=token_fingerprint(token) if token else "",
        revision=_display_revision(snapshot.revision),
        disabled=disabled,
        environment_locked=source == "environment",
        error=str(snapshot.error or ""),
        verification=dict(verification or {}),
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
