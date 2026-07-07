from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("Backend.XaiOAuth")

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
REFRESH_SKEW_SECONDS = 120


@dataclass
class XaiCredentials:
    provider: str
    api_key: str
    base_url: str
    source: str


class XaiOAuthCredentialError(RuntimeError):
    pass


_cache_lock = threading.Lock()
_cache: dict[str, Any] = {
    "access_token": "",
    "expires_at": 0.0,
    "source": "",
    "cache_key": "",
}


def _hermes_home_candidates(explicit: str | None = None) -> list[Path]:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit).expanduser())
    env_home = os.environ.get("HERMES_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser())
    profile = os.environ.get("USERPROFILE", "").strip()
    if profile:
        candidates.append(Path(profile) / "AppData" / "Local" / "hermes")
    wsl_user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if wsl_user:
        candidates.append(Path(f"/mnt/c/Users/{wsl_user}/AppData/Local/hermes"))
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def find_hermes_auth_path(hermes_home: str | None = None) -> Path | None:
    for home in _hermes_home_candidates(hermes_home):
        auth_path = home / "auth.json"
        if auth_path.is_file():
            return auth_path
    return None


def _load_hermes_oauth_state(auth_path: Path) -> dict[str, Any]:
    data = json.loads(auth_path.read_text(encoding="utf-8"))
    provider = (data.get("providers") or {}).get("xai-oauth") or {}
    if not isinstance(provider, dict):
        provider = {}
    tokens = dict(provider.get("tokens") or {})
    pool = (data.get("credential_pool") or {}).get("xai-oauth") or []
    access_token = str(tokens.get("access_token") or "").strip()
    refresh_token = str(tokens.get("refresh_token") or "").strip()
    pool_index = None
    pool_entry: dict[str, Any] = {}
    if not refresh_token and isinstance(pool, list) and pool:
        for idx, entry in enumerate(pool):
            if not isinstance(entry, dict):
                continue
            entry_access = str(entry.get("access_token") or "").strip()
            entry_refresh = str(entry.get("refresh_token") or "").strip()
            last_status = str(entry.get("last_status") or "").strip().lower()
            if entry_refresh and (last_status in {"", "ok"} or entry_access):
                access_token = access_token or entry_access
                refresh_token = entry_refresh
                pool_index = idx
                pool_entry = dict(entry)
                break
    discovery = dict(provider.get("discovery") or {})
    return {
        "auth_path": auth_path,
        "access_token": access_token,
        "tokens": tokens,
        "refresh_token": refresh_token,
        "pool_index": pool_index,
        "pool_entry": pool_entry,
        "discovery": discovery,
    }


def _hermes_agent_path_candidates(auth_path: Path) -> list[Path]:
    home = auth_path.parent
    candidates = [
        home / "hermes-agent",
        home.parent / "hermes-agent",
        home.parent.parent / "hermes-agent",
    ]
    env_agent = os.environ.get("HERMES_AGENT_HOME", "").strip()
    if env_agent:
        candidates.insert(0, Path(env_agent).expanduser())
    seen: set[str] = set()
    unique: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _resolve_via_hermes_python(
    *,
    auth_path: Path,
    force_refresh: bool,
    fallback_base_url: str,
) -> XaiCredentials | None:
    """Use Hermes' own resolver when its Python package is locally available."""
    agent_path = next(
        (path for path in _hermes_agent_path_candidates(auth_path) if (path / "hermes_cli").is_dir()),
        None,
    )
    if agent_path is None:
        return None

    inserted = False
    agent_path_str = str(agent_path)
    old_home = os.environ.get("HERMES_HOME")
    try:
        if agent_path_str not in sys.path:
            sys.path.insert(0, agent_path_str)
            inserted = True
        os.environ["HERMES_HOME"] = str(auth_path.parent)

        from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

        creds = resolve_xai_oauth_runtime_credentials(force_refresh=force_refresh)
        access_token = str(creds.get("api_key") or "").strip()
        if not access_token:
            return None
        base_url = str(creds.get("base_url") or fallback_base_url).strip().rstrip("/")
        return XaiCredentials(
            provider="xai-oauth",
            api_key=access_token,
            base_url=base_url or fallback_base_url,
            source=f"hermes_resolver:{auth_path}",
        )
    except Exception as exc:
        logger.debug("Hermes xAI resolver unavailable for %s: %s", auth_path, exc)
        return None
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home
        if inserted:
            try:
                sys.path.remove(agent_path_str)
            except ValueError:
                pass


def _save_hermes_refreshed_token(auth_path: Path, refreshed: dict[str, Any]) -> None:
    """Best-effort fallback persistence for legacy Hermes stores."""
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        provider = data.setdefault("providers", {}).setdefault("xai-oauth", {})
        if not isinstance(provider, dict):
            return
        tokens = provider.setdefault("tokens", {})
        if not isinstance(tokens, dict):
            tokens = {}
            provider["tokens"] = tokens
        tokens["access_token"] = refreshed["access_token"]
        tokens["refresh_token"] = refreshed["refresh_token"]
        tokens.setdefault("token_type", "Bearer")
        last_refresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        provider["last_refresh"] = last_refresh

        pool = data.setdefault("credential_pool", {}).get("xai-oauth")
        if isinstance(pool, list):
            for entry in pool:
                if not isinstance(entry, dict):
                    continue
                if entry.get("refresh_token") or entry.get("access_token"):
                    entry["access_token"] = refreshed["access_token"]
                    entry["refresh_token"] = refreshed["refresh_token"]
                    entry["last_status"] = "ok"
                    entry["last_error_code"] = None
                    entry["last_error_reason"] = None
                    entry["last_error_message"] = None
                    entry["last_refresh"] = last_refresh
                    break
        auth_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("Could not persist refreshed xAI OAuth token to %s: %s", auth_path, exc)


def _discover_token_endpoint(timeout_seconds: float = 15.0) -> str:
    response = httpx.get(
        XAI_OAUTH_DISCOVERY_URL,
        headers={"Accept": "application/json"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    endpoint = str(payload.get("token_endpoint") or "").strip()
    if not endpoint:
        raise XaiOAuthCredentialError("xAI OIDC discovery missing token_endpoint")
    return endpoint


def _refresh_access_token(refresh_token: str, token_endpoint: str, timeout_seconds: float = 20.0) -> dict[str, Any]:
    if not refresh_token:
        raise XaiOAuthCredentialError(
            "xAI OAuth refresh_token missing. Run `hermes auth add xai-oauth --type oauth`."
        )
    response = httpx.post(
        token_endpoint,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={
            "grant_type": "refresh_token",
            "client_id": XAI_OAUTH_CLIENT_ID,
            "refresh_token": refresh_token,
        },
        timeout=timeout_seconds,
    )
    if response.status_code != 200:
        detail = response.text.strip()[:300]
        raise XaiOAuthCredentialError(
            f"xAI OAuth refresh failed ({response.status_code}): {detail}"
        )
    payload = response.json()
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise XaiOAuthCredentialError("xAI OAuth refresh response missing access_token")
    expires_in = payload.get("expires_in")
    expires_at = time.time() + float(expires_in or 3600)
    return {
        "access_token": access_token,
        "expires_at": expires_at,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
    }


def _cache_valid(cache_key: str) -> bool:
    token = str(_cache.get("access_token") or "").strip()
    expires_at = float(_cache.get("expires_at") or 0.0)
    cached_key = str(_cache.get("cache_key") or "")
    return bool(token) and cached_key == cache_key and time.time() < (expires_at - REFRESH_SKEW_SECONDS)


def _store_cache(access_token: str, expires_at: float, source: str, cache_key: str) -> None:
    _cache["access_token"] = access_token
    _cache["expires_at"] = expires_at
    _cache["source"] = source
    _cache["cache_key"] = cache_key


def _resolve_oauth_refresh_token(
    *,
    oauth_refresh_token: str | None,
    hermes_home: str | None,
) -> tuple[str, str, str, Path | None]:
    """Return (refresh_token, token_endpoint, source_label, auth_path)."""
    explicit = str(oauth_refresh_token or "").strip()
    if explicit:
        return explicit, "", "secrets_oauth_refresh_token", None

    auth_path = find_hermes_auth_path(hermes_home)
    if auth_path is None:
        return "", "", "", None

    state = _load_hermes_oauth_state(auth_path)
    refresh_token = str(state.get("refresh_token") or "").strip()
    discovery = state.get("discovery") or {}
    token_endpoint = str(discovery.get("token_endpoint") or "").strip()
    return refresh_token, token_endpoint, f"hermes_oauth:{auth_path}", auth_path


def resolve_xai_credentials(
    *,
    static_api_key: str | None = None,
    oauth_refresh_token: str | None = None,
    hermes_home: str | None = None,
    base_url: str | None = None,
    force_refresh: bool = False,
) -> XaiCredentials:
    resolved_base = (base_url or DEFAULT_XAI_BASE_URL).strip().rstrip("/") or DEFAULT_XAI_BASE_URL
    cache_key = "|".join(
        [
            str(hermes_home or ""),
            str(oauth_refresh_token or ""),
            str(static_api_key or os.environ.get("XAI_API_KEY") or ""),
            resolved_base,
        ]
    )

    static_key = str(static_api_key or os.environ.get("XAI_API_KEY") or "").strip()
    if static_key and not force_refresh and not oauth_refresh_token:
        return XaiCredentials(
            provider="xai",
            api_key=static_key,
            base_url=resolved_base,
            source="static_api_key",
        )

    with _cache_lock:
        if not force_refresh and _cache_valid(cache_key):
            return XaiCredentials(
                provider="xai-oauth",
                api_key=str(_cache["access_token"]),
                base_url=resolved_base,
                source=str(_cache.get("source") or "oauth_cache"),
            )

        refresh_token, token_endpoint, source_label, auth_path = _resolve_oauth_refresh_token(
            oauth_refresh_token=oauth_refresh_token,
            hermes_home=hermes_home,
        )

        if auth_path is not None and not oauth_refresh_token:
            hermes_creds = _resolve_via_hermes_python(
                auth_path=auth_path,
                force_refresh=force_refresh,
                fallback_base_url=resolved_base,
            )
            if hermes_creds is not None:
                _store_cache(
                    hermes_creds.api_key,
                    time.time() + 900,
                    hermes_creds.source,
                    cache_key,
                )
                return hermes_creds

            if not force_refresh:
                state = _load_hermes_oauth_state(auth_path)
                access_token = str(state.get("access_token") or "").strip()
                if access_token and refresh_token:
                    _store_cache(
                        access_token,
                        time.time() + 300,
                        source_label,
                        cache_key,
                    )
                    return XaiCredentials(
                        provider="xai-oauth",
                        api_key=access_token,
                        base_url=resolved_base,
                        source=source_label,
                    )

        if not refresh_token:
            if static_key:
                return XaiCredentials(
                    provider="xai",
                    api_key=static_key,
                    base_url=resolved_base,
                    source="static_api_key",
                )
            raise XaiOAuthCredentialError(
                "No xAI OAuth refresh token (Hermes or secrets.json) and no xai_api_key configured."
            )

        if not token_endpoint:
            token_endpoint = _discover_token_endpoint()

        refreshed = _refresh_access_token(refresh_token, token_endpoint)
        if auth_path is not None:
            _save_hermes_refreshed_token(auth_path, refreshed)
        cache_source = source_label or "oauth_refresh"
        _store_cache(
            refreshed["access_token"],
            float(refreshed["expires_at"]),
            cache_source,
            cache_key,
        )
        return XaiCredentials(
            provider="xai-oauth",
            api_key=refreshed["access_token"],
            base_url=resolved_base,
            source=cache_source,
        )


def hermes_oauth_available(hermes_home: str | None = None) -> bool:
    auth_path = find_hermes_auth_path(hermes_home)
    if auth_path is None:
        return False
    try:
        state = _load_hermes_oauth_state(auth_path)
        return bool(str(state.get("refresh_token") or "").strip())
    except Exception:
        return False


def secrets_oauth_refresh_available(secrets: dict | None) -> bool:
    if not secrets:
        return False
    return bool(str(secrets.get("xai_oauth_refresh_token") or "").strip())


def xai_api_credentials_available(
    *,
    hermes_home: str | None = None,
    secrets: dict | None = None,
) -> bool:
    if hermes_oauth_available(hermes_home):
        return True
    if not secrets:
        return False
    for key in ("xai_api_key", "XAI_API_KEY", "xai_oauth_refresh_token"):
        if str(secrets.get(key) or "").strip():
            return True
    return False
