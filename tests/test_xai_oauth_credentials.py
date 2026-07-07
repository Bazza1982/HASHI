import json
from pathlib import Path

import httpx
import pytest

from adapters import xai_oauth_credentials as creds


@pytest.fixture(autouse=True)
def _reset_cache():
    creds._cache["access_token"] = ""
    creds._cache["expires_at"] = 0.0
    creds._cache["source"] = ""
    creds._cache["cache_key"] = ""
    yield


def test_find_hermes_auth_path_prefers_explicit_home(tmp_path: Path):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {}}}}), encoding="utf-8")
    found = creds.find_hermes_auth_path(str(tmp_path))
    assert found == auth


def test_resolve_xai_credentials_uses_static_api_key_without_hermes():
    resolved = creds.resolve_xai_credentials(static_api_key="test-key")
    assert resolved.provider == "xai"
    assert resolved.api_key == "test-key"
    assert resolved.source == "static_api_key"


def test_resolve_xai_credentials_refreshes_from_hermes_auth(tmp_path: Path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {"refresh_token": "refresh-abc"},
                        "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    def _fake_refresh(refresh_token, token_endpoint, timeout_seconds=20.0):
        assert refresh_token == "refresh-abc"
        assert token_endpoint == "https://auth.x.ai/oauth/token"
        return {
            "access_token": "fresh-access",
            "expires_at": creds.time.time() + 3600,
            "refresh_token": "refresh-abc",
        }

    monkeypatch.setattr(creds, "_refresh_access_token", _fake_refresh)
    monkeypatch.setattr(creds, "_resolve_via_hermes_python", lambda **kwargs: None)
    resolved = creds.resolve_xai_credentials(hermes_home=str(tmp_path))
    assert resolved.provider == "xai-oauth"
    assert resolved.api_key == "fresh-access"
    assert "hermes_oauth" in resolved.source


def test_resolve_xai_credentials_uses_cache_on_second_call(tmp_path: Path, monkeypatch):
    calls = {"count": 0}

    def _fake_refresh(refresh_token, token_endpoint, timeout_seconds=20.0):
        calls["count"] += 1
        return {
            "access_token": f"token-{calls['count']}",
            "expires_at": creds.time.time() + 3600,
            "refresh_token": "refresh-abc",
        }

    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {"refresh_token": "refresh-abc"},
                        "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(creds, "_refresh_access_token", _fake_refresh)
    monkeypatch.setattr(creds, "_resolve_via_hermes_python", lambda **kwargs: None)

    first = creds.resolve_xai_credentials(hermes_home=str(tmp_path))
    second = creds.resolve_xai_credentials(hermes_home=str(tmp_path))
    assert first.api_key == second.api_key == "token-1"
    assert calls["count"] == 1


def test_resolve_xai_credentials_uses_secrets_refresh_token(monkeypatch):
    calls = {"count": 0}

    def _fake_refresh(refresh_token, token_endpoint, timeout_seconds=20.0):
        calls["count"] += 1
        assert refresh_token == "secret-refresh"
        return {
            "access_token": "secret-access",
            "expires_at": creds.time.time() + 3600,
            "refresh_token": "secret-refresh",
        }

    monkeypatch.setattr(creds, "_discover_token_endpoint", lambda timeout_seconds=15.0: "https://auth.x.ai/oauth/token")
    monkeypatch.setattr(creds, "_refresh_access_token", _fake_refresh)

    resolved = creds.resolve_xai_credentials(oauth_refresh_token="secret-refresh")
    assert resolved.provider == "xai-oauth"
    assert resolved.api_key == "secret-access"
    assert resolved.source == "secrets_oauth_refresh_token"


def test_resolve_xai_credentials_prefers_hermes_resolver(tmp_path: Path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "providers": {"xai-oauth": {"tokens": {}}},
                "credential_pool": {
                    "xai-oauth": [
                        {
                            "access_token": "pool-access",
                            "refresh_token": "pool-refresh",
                            "last_status": "ok",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    def _fake_hermes(**kwargs):
        assert kwargs["auth_path"] == auth
        assert kwargs["force_refresh"] is False
        return creds.XaiCredentials(
            provider="xai-oauth",
            api_key="hermes-access",
            base_url="https://api.x.ai/v1",
            source="hermes_resolver:test",
        )

    monkeypatch.setattr(creds, "_resolve_via_hermes_python", _fake_hermes)
    monkeypatch.setattr(
        creds,
        "_refresh_access_token",
        lambda *args, **kwargs: pytest.fail("should not refresh outside Hermes"),
    )

    resolved = creds.resolve_xai_credentials(hermes_home=str(tmp_path))
    assert resolved.api_key == "hermes-access"
    assert resolved.source == "hermes_resolver:test"


def test_resolve_xai_credentials_uses_pool_access_without_refresh(tmp_path: Path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "providers": {"xai-oauth": {"tokens": {}}},
                "credential_pool": {
                    "xai-oauth": [
                        {
                            "access_token": "pool-access",
                            "refresh_token": "pool-refresh",
                            "last_status": "ok",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(creds, "_resolve_via_hermes_python", lambda **kwargs: None)
    monkeypatch.setattr(
        creds,
        "_refresh_access_token",
        lambda *args, **kwargs: pytest.fail("should not refresh while pool access is available"),
    )

    resolved = creds.resolve_xai_credentials(hermes_home=str(tmp_path))
    assert resolved.api_key == "pool-access"
    assert resolved.source == f"hermes_oauth:{auth}"


def test_resolve_xai_credentials_cache_is_scoped_by_home(tmp_path: Path, monkeypatch):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    for home, token in ((home_a, "access-a"), (home_b, "access-b")):
        (home / "auth.json").write_text(
            json.dumps(
                {
                    "providers": {"xai-oauth": {"tokens": {}}},
                    "credential_pool": {
                        "xai-oauth": [
                            {
                                "access_token": token,
                                "refresh_token": f"refresh-{token}",
                                "last_status": "ok",
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(creds, "_resolve_via_hermes_python", lambda **kwargs: None)

    first = creds.resolve_xai_credentials(hermes_home=str(home_a))
    second = creds.resolve_xai_credentials(hermes_home=str(home_b))
    assert first.api_key == "access-a"
    assert second.api_key == "access-b"


def test_refresh_from_hermes_auth_persists_rotated_tokens(tmp_path: Path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "providers": {
                    "xai-oauth": {
                        "tokens": {"refresh_token": "old-refresh"},
                        "discovery": {"token_endpoint": "https://auth.x.ai/oauth/token"},
                    }
                },
                "credential_pool": {
                    "xai-oauth": [
                        {
                            "access_token": "old-access",
                            "refresh_token": "old-refresh",
                            "last_status": "ok",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(creds, "_resolve_via_hermes_python", lambda **kwargs: None)
    monkeypatch.setattr(
        creds,
        "_refresh_access_token",
        lambda *args, **kwargs: {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": creds.time.time() + 3600,
        },
    )

    resolved = creds.resolve_xai_credentials(hermes_home=str(tmp_path), force_refresh=True)
    assert resolved.api_key == "new-access"

    saved = json.loads(auth.read_text(encoding="utf-8"))
    provider_tokens = saved["providers"]["xai-oauth"]["tokens"]
    pool_entry = saved["credential_pool"]["xai-oauth"][0]
    assert provider_tokens["access_token"] == "new-access"
    assert provider_tokens["refresh_token"] == "new-refresh"
    assert pool_entry["access_token"] == "new-access"
    assert pool_entry["refresh_token"] == "new-refresh"


def test_secrets_oauth_refresh_available():
    assert creds.secrets_oauth_refresh_available({"xai_oauth_refresh_token": "abc"}) is True
    assert creds.secrets_oauth_refresh_available({}) is False


def test_xai_api_credentials_available_with_refresh_token_only():
    assert creds.xai_api_credentials_available(secrets={"xai_oauth_refresh_token": "abc"}) is True


def test_refresh_access_token_raises_on_http_error():
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "bad"})

    transport = httpx.MockTransport(_handler)
    client = httpx.Client(transport=transport)

    original_post = httpx.post

    def _post(url, **kwargs):
        if url == "https://auth.x.ai/oauth/token":
            response = client.post(url, **kwargs)
            return response
        return original_post(url, **kwargs)

    with pytest.raises(creds.XaiOAuthCredentialError):
        creds._refresh_access_token("refresh", "https://auth.x.ai/oauth/token")
