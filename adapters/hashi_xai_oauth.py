"""HASHI-native xAI OAuth (no Hermes, no xai-api engine).

Owns device-code login, refresh, and on-disk token storage under bridge_home.
Claw receives a fresh access_token via XAI_API_KEY at task start.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

logger = logging.getLogger("Backend.HashiXaiOAuth")

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
DEFAULT_SCOPES = "openid offline_access api:access"
DEFAULT_AUTH_RELATIVE = "auth/xai_oauth.json"
REFRESH_SKEW_SECONDS = 120
DEVICE_POLL_DEFAULT_INTERVAL = 5.0
DEVICE_POLL_MAX_SECONDS = 600.0

# Never import or call Hermes from this module.
_FORBIDDEN_IMPORT_MARKERS = ("hermes_cli", "hermes_agent", "xai_oauth_credentials")


class HashiXaiOAuthError(RuntimeError):
    """Raised when HASHI-native xAI OAuth cannot proceed."""


@dataclass(frozen=True)
class HashiXaiCredentials:
    access_token: str
    base_url: str
    source: str
    expires_at: float
    relogin_required: bool = False


@dataclass(frozen=True)
class DeviceCodeSession:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: float
    expires_in: float
    token_endpoint: str
    client_id: str
    scopes: str


_cache_lock = threading.Lock()
_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()
_memory_cache: dict[str, Any] = {
    "access_token": "",
    "expires_at": 0.0,
    "source": "",
    "cache_key": "",
}


def _file_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve()) if path.exists() or path.parent.exists() else str(path)
    with _file_locks_guard:
        lock = _file_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _file_locks[key] = lock
        return lock


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def resolve_client_id(
    *,
    explicit: str | None = None,
    global_config: Any | None = None,
) -> str:
    """Resolve HASHI's own OAuth client_id (strategy 1: configurable HASHI client)."""
    candidates = [
        str(explicit or "").strip(),
        str(os.environ.get("HASHI_XAI_OAUTH_CLIENT_ID") or "").strip(),
    ]
    xai_oauth = _as_mapping(getattr(global_config, "xai_oauth", None) if global_config is not None else None)
    if not xai_oauth and global_config is not None:
        # Allow dict-shaped global config (tests / raw agents.json global).
        raw = getattr(global_config, "__dict__", None)
        if isinstance(raw, dict):
            xai_oauth = _as_mapping(raw.get("xai_oauth"))
        if hasattr(global_config, "get"):
            try:
                xai_oauth = _as_mapping(global_config.get("xai_oauth"))  # type: ignore[union-attr]
            except Exception:
                pass
    candidates.append(str(xai_oauth.get("client_id") or "").strip())

    claw = _as_mapping(getattr(global_config, "claw_providers", None) if global_config is not None else None)
    providers = _as_mapping(claw.get("providers"))
    xai_provider = _as_mapping(providers.get("xai"))
    candidates.append(str(xai_provider.get("client_id") or "").strip())

    for value in candidates:
        if value:
            return value
    return ""


def resolve_scopes(global_config: Any | None = None) -> str:
    xai_oauth = _as_mapping(getattr(global_config, "xai_oauth", None) if global_config is not None else None)
    scopes = str(xai_oauth.get("scopes") or "").strip()
    if scopes:
        return scopes
    env_scopes = str(os.environ.get("HASHI_XAI_OAUTH_SCOPES") or "").strip()
    return env_scopes or DEFAULT_SCOPES


def resolve_base_url(global_config: Any | None = None, provider_cfg: Mapping[str, Any] | None = None) -> str:
    if provider_cfg:
        configured = str(provider_cfg.get("base_url") or "").strip().rstrip("/")
        if configured:
            return configured
    xai_oauth = _as_mapping(getattr(global_config, "xai_oauth", None) if global_config is not None else None)
    configured = str(xai_oauth.get("base_url") or "").strip().rstrip("/")
    if configured:
        return configured
    global_base = str(getattr(global_config, "xai_api_base_url", "") or "").strip().rstrip("/")
    return global_base or DEFAULT_XAI_BASE_URL


def resolve_auth_store_path(
    *,
    bridge_home: str | Path | None = None,
    global_config: Any | None = None,
    explicit: str | Path | None = None,
) -> Path:
    if explicit:
        return Path(explicit).expanduser()

    xai_oauth = _as_mapping(getattr(global_config, "xai_oauth", None) if global_config is not None else None)
    relative = str(xai_oauth.get("auth_store") or DEFAULT_AUTH_RELATIVE).strip() or DEFAULT_AUTH_RELATIVE
    if Path(relative).is_absolute():
        return Path(relative)

    home: Path | None = None
    if bridge_home:
        home = Path(bridge_home).expanduser()
    elif global_config is not None:
        raw_home = getattr(global_config, "bridge_home", None)
        if raw_home:
            home = Path(raw_home).expanduser()
        else:
            project_root = getattr(global_config, "project_root", None)
            if project_root:
                home = Path(project_root).expanduser()
    if home is None:
        env_home = str(os.environ.get("HASHI_BRIDGE_HOME") or "").strip()
        home = Path(env_home).expanduser() if env_home else Path.cwd()
    return (home / relative).resolve()


def discover_oauth_metadata(timeout_seconds: float = 15.0) -> dict[str, Any]:
    response = httpx.get(
        XAI_OAUTH_DISCOVERY_URL,
        headers={"Accept": "application/json"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise HashiXaiOAuthError("xAI OIDC discovery returned a non-object payload")
    return payload


def _require_client_id(client_id: str) -> str:
    value = str(client_id or "").strip()
    if not value:
        raise HashiXaiOAuthError(
            "HASHI xAI OAuth client_id is not configured. "
            "Set global.xai_oauth.client_id in agents.json or HASHI_XAI_OAUTH_CLIENT_ID."
        )
    return value


def _load_store(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HashiXaiOAuthError(f"Could not read HASHI xAI OAuth store {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _atomic_write_store(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_token_state(
    *,
    bridge_home: str | Path | None = None,
    global_config: Any | None = None,
    auth_store: str | Path | None = None,
) -> dict[str, Any]:
    path = resolve_auth_store_path(
        bridge_home=bridge_home,
        global_config=global_config,
        explicit=auth_store,
    )
    with _file_lock_for(path):
        data = _load_store(path)
    data["_path"] = str(path)
    return data


def clear_tokens(
    *,
    bridge_home: str | Path | None = None,
    global_config: Any | None = None,
    auth_store: str | Path | None = None,
) -> Path:
    path = resolve_auth_store_path(
        bridge_home=bridge_home,
        global_config=global_config,
        explicit=auth_store,
    )
    with _file_lock_for(path):
        if path.is_file():
            path.unlink()
    with _cache_lock:
        _memory_cache["access_token"] = ""
        _memory_cache["expires_at"] = 0.0
        _memory_cache["source"] = ""
        _memory_cache["cache_key"] = ""
    return path


def oauth_status(
    *,
    bridge_home: str | Path | None = None,
    global_config: Any | None = None,
    auth_store: str | Path | None = None,
) -> dict[str, Any]:
    path = resolve_auth_store_path(
        bridge_home=bridge_home,
        global_config=global_config,
        explicit=auth_store,
    )
    client_id = resolve_client_id(global_config=global_config)
    if not path.is_file():
        return {
            "logged_in": False,
            "relogin_required": False,
            "has_refresh_token": False,
            "has_access_token": False,
            "client_id_configured": bool(client_id),
            "auth_store": str(path),
            "source": "hashi_xai_oauth",
            "message": "Not logged in. Run: python hashi.py auth xai login",
        }
    state = load_token_state(
        bridge_home=bridge_home,
        global_config=global_config,
        auth_store=auth_store,
    )
    tokens = _as_mapping(state.get("tokens"))
    has_refresh = bool(str(tokens.get("refresh_token") or "").strip())
    has_access = bool(str(tokens.get("access_token") or "").strip())
    relogin = bool(state.get("relogin_required"))
    return {
        "logged_in": has_refresh and not relogin,
        "relogin_required": relogin,
        "has_refresh_token": has_refresh,
        "has_access_token": has_access,
        "client_id_configured": bool(client_id),
        "auth_store": str(path),
        "source": "hashi_xai_oauth",
        "last_error": state.get("last_error") if isinstance(state.get("last_error"), dict) else {},
        "expires_at": tokens.get("expires_at"),
        "message": (
            "relogin required"
            if relogin
            else ("logged in" if has_refresh else "store present but refresh_token missing")
        ),
    }


def hashi_oauth_available(
    *,
    bridge_home: str | Path | None = None,
    global_config: Any | None = None,
    auth_store: str | Path | None = None,
) -> bool:
    status = oauth_status(
        bridge_home=bridge_home,
        global_config=global_config,
        auth_store=auth_store,
    )
    return bool(status.get("logged_in"))


def start_device_login(
    *,
    client_id: str | None = None,
    global_config: Any | None = None,
    scopes: str | None = None,
    timeout_seconds: float = 20.0,
) -> DeviceCodeSession:
    resolved_client = _require_client_id(client_id or resolve_client_id(global_config=global_config))
    resolved_scopes = (scopes or resolve_scopes(global_config)).strip() or DEFAULT_SCOPES
    metadata = discover_oauth_metadata(timeout_seconds=timeout_seconds)
    device_endpoint = str(metadata.get("device_authorization_endpoint") or "").strip()
    token_endpoint = str(metadata.get("token_endpoint") or "").strip()
    if not device_endpoint or not token_endpoint:
        raise HashiXaiOAuthError("xAI OIDC discovery missing device_authorization_endpoint or token_endpoint")

    response = httpx.post(
        device_endpoint,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "client_id": resolved_client,
            "scope": resolved_scopes,
        },
        timeout=timeout_seconds,
    )
    if response.status_code != 200:
        detail = response.text.strip()[:400]
        raise HashiXaiOAuthError(f"Device code request failed ({response.status_code}): {detail}")
    payload = response.json()
    device_code = str(payload.get("device_code") or "").strip()
    user_code = str(payload.get("user_code") or "").strip()
    verification_uri = str(
        payload.get("verification_uri") or payload.get("verification_uri_complete") or ""
    ).strip()
    verification_uri_complete = str(payload.get("verification_uri_complete") or verification_uri).strip()
    if not device_code or not user_code or not verification_uri:
        raise HashiXaiOAuthError("Device code response missing device_code/user_code/verification_uri")
    interval = float(payload.get("interval") or DEVICE_POLL_DEFAULT_INTERVAL)
    expires_in = float(payload.get("expires_in") or 600)
    return DeviceCodeSession(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=verification_uri_complete,
        interval=max(1.0, interval),
        expires_in=expires_in,
        token_endpoint=token_endpoint,
        client_id=resolved_client,
        scopes=resolved_scopes,
    )


def _persist_tokens(
    path: Path,
    *,
    access_token: str,
    refresh_token: str,
    expires_at: float,
    client_id: str,
    scopes: str,
    token_endpoint: str,
) -> None:
    data = {
        "version": 1,
        "provider": "hashi-xai-oauth",
        "client_id": client_id,
        "scopes": scopes,
        "token_endpoint": token_endpoint,
        "relogin_required": False,
        "last_error": None,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_at": expires_at,
        },
    }
    with _file_lock_for(path):
        _atomic_write_store(path, data)


def _mark_relogin_required(path: Path, *, code: str, message: str) -> None:
    with _file_lock_for(path):
        data = _load_store(path)
        data["relogin_required"] = True
        data["last_error"] = {
            "code": code,
            "message": message,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if path.parent.exists() or data:
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_store(path, data)


def poll_device_login(
    session: DeviceCodeSession,
    *,
    bridge_home: str | Path | None = None,
    global_config: Any | None = None,
    auth_store: str | Path | None = None,
    max_wait_seconds: float = DEVICE_POLL_MAX_SECONDS,
    sleep_fn=time.sleep,
) -> HashiXaiCredentials:
    path = resolve_auth_store_path(
        bridge_home=bridge_home,
        global_config=global_config,
        explicit=auth_store,
    )
    deadline = time.time() + min(float(session.expires_in), float(max_wait_seconds))
    interval = float(session.interval)
    while time.time() < deadline:
        response = httpx.post(
            session.token_endpoint,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": session.device_code,
                "client_id": session.client_id,
            },
            timeout=20.0,
        )
        if response.status_code == 200:
            payload = response.json()
            access_token = str(payload.get("access_token") or "").strip()
            refresh_token = str(payload.get("refresh_token") or "").strip()
            if not access_token or not refresh_token:
                raise HashiXaiOAuthError("Device login succeeded but tokens were incomplete")
            expires_in = float(payload.get("expires_in") or 3600)
            expires_at = time.time() + expires_in
            _persist_tokens(
                path,
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                client_id=session.client_id,
                scopes=session.scopes,
                token_endpoint=session.token_endpoint,
            )
            cache_key = str(path)
            with _cache_lock:
                _memory_cache.update(
                    {
                        "access_token": access_token,
                        "expires_at": expires_at,
                        "source": f"hashi_device_login:{path}",
                        "cache_key": cache_key,
                    }
                )
            return HashiXaiCredentials(
                access_token=access_token,
                base_url=resolve_base_url(global_config),
                source=f"hashi_device_login:{path}",
                expires_at=expires_at,
            )

        try:
            err = response.json()
        except Exception:
            err = {"error": response.text.strip()[:200]}
        error_code = str(err.get("error") or "").strip()
        if error_code in {"authorization_pending", "slow_down"}:
            if error_code == "slow_down":
                interval += 5.0
            sleep_fn(interval)
            continue
        if error_code in {"expired_token", "access_denied"}:
            raise HashiXaiOAuthError(f"Device login failed: {error_code}")
        detail = str(err.get("error_description") or err.get("error") or response.text)[:300]
        raise HashiXaiOAuthError(f"Device login token poll failed ({response.status_code}): {detail}")

    raise HashiXaiOAuthError("Device login timed out waiting for user authorization")


def login_with_device_code(
    *,
    client_id: str | None = None,
    global_config: Any | None = None,
    bridge_home: str | Path | None = None,
    auth_store: str | Path | None = None,
    scopes: str | None = None,
    max_wait_seconds: float = DEVICE_POLL_MAX_SECONDS,
    on_user_code=None,
    sleep_fn=time.sleep,
) -> HashiXaiCredentials:
    session = start_device_login(
        client_id=client_id,
        global_config=global_config,
        scopes=scopes,
    )
    if callable(on_user_code):
        on_user_code(session)
    else:
        print("HASHI xAI OAuth device login")
        print(f"  Open: {session.verification_uri}")
        print(f"  Code: {session.user_code}")
        if session.verification_uri_complete and session.verification_uri_complete != session.verification_uri:
            print(f"  Direct: {session.verification_uri_complete}")
    return poll_device_login(
        session,
        bridge_home=bridge_home,
        global_config=global_config,
        auth_store=auth_store,
        max_wait_seconds=max_wait_seconds,
        sleep_fn=sleep_fn,
    )


def _refresh_access_token(
    *,
    refresh_token: str,
    token_endpoint: str,
    client_id: str,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    if not refresh_token:
        raise HashiXaiOAuthError("Missing refresh_token; run: python hashi.py auth xai login")
    response = httpx.post(
        token_endpoint,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        timeout=timeout_seconds,
    )
    if response.status_code != 200:
        detail = response.text.strip()[:400]
        raise HashiXaiOAuthError(f"xAI OAuth refresh failed ({response.status_code}): {detail}")
    payload = response.json()
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise HashiXaiOAuthError("xAI OAuth refresh response missing access_token")
    expires_in = float(payload.get("expires_in") or 3600)
    return {
        "access_token": access_token,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "expires_at": time.time() + expires_in,
    }


def resolve_hashi_xai_credentials(
    *,
    bridge_home: str | Path | None = None,
    global_config: Any | None = None,
    auth_store: str | Path | None = None,
    client_id: str | None = None,
    base_url: str | None = None,
    force_refresh: bool = False,
    provider_cfg: Mapping[str, Any] | None = None,
) -> HashiXaiCredentials:
    """Return a usable Bearer access_token owned by HASHI (never Hermes)."""
    path = resolve_auth_store_path(
        bridge_home=bridge_home,
        global_config=global_config,
        explicit=auth_store,
    )
    resolved_base = (base_url or resolve_base_url(global_config, provider_cfg)).rstrip("/")
    resolved_client = resolve_client_id(explicit=client_id, global_config=global_config)
    cache_key = f"{path}|{resolved_client}|{resolved_base}"

    with _cache_lock:
        cached_token = str(_memory_cache.get("access_token") or "").strip()
        cached_exp = float(_memory_cache.get("expires_at") or 0.0)
        if (
            not force_refresh
            and cached_token
            and str(_memory_cache.get("cache_key") or "") == cache_key
            and time.time() < (cached_exp - REFRESH_SKEW_SECONDS)
        ):
            return HashiXaiCredentials(
                access_token=cached_token,
                base_url=resolved_base,
                source=str(_memory_cache.get("source") or "hashi_oauth_cache"),
                expires_at=cached_exp,
            )

    state = load_token_state(
        bridge_home=bridge_home,
        global_config=global_config,
        auth_store=path,
    )
    if bool(state.get("relogin_required")) and not force_refresh:
        err = _as_mapping(state.get("last_error"))
        message = str(err.get("message") or "relogin required")
        raise HashiXaiOAuthError(
            f"HASHI xAI OAuth requires re-login ({message}). Run: python hashi.py auth xai login"
        )

    tokens = _as_mapping(state.get("tokens"))
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    access_token = str(tokens.get("access_token") or "").strip()
    expires_at = float(tokens.get("expires_at") or 0.0)
    store_client = str(state.get("client_id") or "").strip()
    effective_client = _require_client_id(resolved_client or store_client)
    token_endpoint = str(state.get("token_endpoint") or "").strip()
    if not token_endpoint:
        token_endpoint = str(discover_oauth_metadata().get("token_endpoint") or "").strip()
    if not token_endpoint:
        raise HashiXaiOAuthError("Missing token_endpoint for HASHI xAI OAuth refresh")

    needs_refresh = force_refresh or (not access_token) or (time.time() >= (expires_at - REFRESH_SKEW_SECONDS))
    if not needs_refresh:
        with _cache_lock:
            _memory_cache.update(
                {
                    "access_token": access_token,
                    "expires_at": expires_at,
                    "source": f"hashi_oauth_store:{path}",
                    "cache_key": cache_key,
                }
            )
        return HashiXaiCredentials(
            access_token=access_token,
            base_url=resolved_base,
            source=f"hashi_oauth_store:{path}",
            expires_at=expires_at,
        )

    if not refresh_token:
        raise HashiXaiOAuthError(
            "No HASHI xAI refresh_token on disk. Run: python hashi.py auth xai login"
        )

    try:
        refreshed = _refresh_access_token(
            refresh_token=refresh_token,
            token_endpoint=token_endpoint,
            client_id=effective_client,
        )
    except HashiXaiOAuthError as exc:
        text = str(exc).lower()
        if "invalid_grant" in text or "revoked" in text or "401" in text or "400" in text:
            _mark_relogin_required(path, code="refresh_failed", message=str(exc))
        raise

    _persist_tokens(
        path,
        access_token=refreshed["access_token"],
        refresh_token=refreshed["refresh_token"],
        expires_at=float(refreshed["expires_at"]),
        client_id=effective_client,
        scopes=str(state.get("scopes") or resolve_scopes(global_config)),
        token_endpoint=token_endpoint,
    )
    with _cache_lock:
        _memory_cache.update(
            {
                "access_token": refreshed["access_token"],
                "expires_at": float(refreshed["expires_at"]),
                "source": f"hashi_oauth_refresh:{path}",
                "cache_key": cache_key,
            }
        )
    return HashiXaiCredentials(
        access_token=refreshed["access_token"],
        base_url=resolved_base,
        source=f"hashi_oauth_refresh:{path}",
        expires_at=float(refreshed["expires_at"]),
    )


def assert_no_hermes_coupling() -> None:
    """Test helper: ensure this module stays Hermes-free."""
    source = Path(__file__).read_text(encoding="utf-8")
    for marker in _FORBIDDEN_IMPORT_MARKERS:
        if marker in source:
            # Allow the string only in this constant / docstring context via explicit check.
            if marker == "xai_oauth_credentials" and "xai_oauth_credentials" in _FORBIDDEN_IMPORT_MARKERS:
                # Present only as forbidden marker list value is OK.
                if source.count(marker) > 1:
                    raise AssertionError(f"hashi_xai_oauth must not couple to {marker}")
                continue
            if marker in ("hermes_cli", "hermes_agent") and source.count(marker) > 2:
                raise AssertionError(f"hashi_xai_oauth must not couple to {marker}")
