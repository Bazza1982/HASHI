from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters import hashi_xai_oauth as oauth
from adapters.claw_cli import build_claw_env


def test_hashi_xai_oauth_module_has_no_hermes_imports():
    source = Path(oauth.__file__).read_text(encoding="utf-8")
    assert "import hermes" not in source
    assert "hermes_cli" not in source or source.count("hermes_cli") <= 2
    assert "from adapters.xai_oauth_credentials" not in source
    assert "from adapters.xai_api" not in source


def test_resolve_client_id_prefers_env(monkeypatch):
    monkeypatch.setenv("HASHI_XAI_OAUTH_CLIENT_ID", "env-client")
    cfg = SimpleNamespace(xai_oauth={"client_id": "cfg-client"}, claw_providers={})
    assert oauth.resolve_client_id(global_config=cfg) == "env-client"


def test_resolve_client_id_uses_agents_config(monkeypatch):
    monkeypatch.delenv("HASHI_XAI_OAUTH_CLIENT_ID", raising=False)
    cfg = SimpleNamespace(xai_oauth={"client_id": "cfg-client"}, claw_providers={})
    assert oauth.resolve_client_id(global_config=cfg) == "cfg-client"


def test_persist_and_resolve_refresh(tmp_path: Path, monkeypatch):
    store = tmp_path / "auth" / "xai_oauth.json"
    client_id = "hashi-client-1"
    token_endpoint = "https://auth.x.ai/oauth2/token"

    oauth._persist_tokens(
        store,
        access_token="old-access",
        refresh_token="refresh-1",
        expires_at=0.0,
        client_id=client_id,
        scopes="openid offline_access api:access",
        token_endpoint=token_endpoint,
    )

    def _fake_refresh(*, refresh_token, token_endpoint, client_id, timeout_seconds=20.0):
        assert refresh_token == "refresh-1"
        assert token_endpoint == "https://auth.x.ai/oauth2/token"
        assert client_id == "hashi-client-1"
        return {
            "access_token": "new-access",
            "refresh_token": "refresh-2",
            "expires_at": 9_999_999_999.0,
        }

    monkeypatch.setattr(oauth, "_refresh_access_token", _fake_refresh)
    monkeypatch.delenv("HASHI_XAI_OAUTH_CLIENT_ID", raising=False)

    cfg = SimpleNamespace(
        bridge_home=tmp_path,
        xai_oauth={"client_id": client_id, "auth_store": "auth/xai_oauth.json"},
        claw_providers={},
        xai_api_base_url="https://api.x.ai/v1",
    )
    creds = oauth.resolve_hashi_xai_credentials(global_config=cfg, force_refresh=True)
    assert creds.access_token == "new-access"
    assert creds.source.startswith("hashi_oauth_refresh:")

    saved = json.loads(store.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == "new-access"
    assert saved["tokens"]["refresh_token"] == "refresh-2"
    assert saved["relogin_required"] is False


def test_oauth_status_not_logged_in(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HASHI_XAI_OAUTH_CLIENT_ID", raising=False)
    cfg = SimpleNamespace(
        bridge_home=tmp_path,
        xai_oauth={"client_id": "c1", "auth_store": "auth/xai_oauth.json"},
        claw_providers={},
    )
    status = oauth.oauth_status(global_config=cfg)
    assert status["logged_in"] is False
    assert status["client_id_configured"] is True


def test_clear_tokens(tmp_path: Path):
    store = tmp_path / "auth" / "xai_oauth.json"
    store.parent.mkdir(parents=True)
    store.write_text("{}", encoding="utf-8")
    cfg = SimpleNamespace(bridge_home=tmp_path, xai_oauth={"auth_store": "auth/xai_oauth.json"}, claw_providers={})
    path = oauth.clear_tokens(global_config=cfg)
    assert path == store.resolve()
    assert not store.exists()


def test_build_claw_env_passes_xai_keys():
    env = build_claw_env(
        {
            "XAI_API_KEY": "token-abc",
            "XAI_BASE_URL": "https://api.x.ai/v1",
            "OPENAI_API_KEY": "should-also-pass",
            "HOME": "/tmp",
            "SECRET_SHOULD_DROP": "nope",
        }
    )
    assert env["XAI_API_KEY"] == "token-abc"
    assert env["XAI_BASE_URL"] == "https://api.x.ai/v1"
    assert env["OPENAI_API_KEY"] == "should-also-pass"
    assert "SECRET_SHOULD_DROP" not in env


def test_claw_provider_hashi_oauth_injection(tmp_path: Path, monkeypatch):
    from adapters.claw_cli import ClawCLIAdapter

    store = tmp_path / "auth" / "xai_oauth.json"
    oauth._persist_tokens(
        store,
        access_token="fresh-access",
        refresh_token="refresh-keep",
        expires_at=9_999_999_999.0,
        client_id="hashi-client",
        scopes="openid offline_access api:access",
        token_endpoint="https://auth.x.ai/oauth2/token",
    )

    agent = SimpleNamespace(
        name="xishi",
        model="grok-4.5",
        workspace_dir=tmp_path / "ws",
        system_md=tmp_path / "system.md",
        extra={"provider": "xai", "model": "grok-4.5"},
        project_root=tmp_path,
    )
    agent.workspace_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "system.md").write_text("sys", encoding="utf-8")

    global_cfg = SimpleNamespace(
        bridge_home=tmp_path,
        project_root=tmp_path,
        xai_oauth={"client_id": "hashi-client", "auth_store": "auth/xai_oauth.json"},
        claw_providers={
            "providers": {
                "xai": {
                    "auth_mode": "hashi_oauth",
                    "base_url": "https://api.x.ai/v1",
                    "status": "provisional",
                    "env_api_key": "XAI_API_KEY",
                    "env_base_url": "XAI_BASE_URL",
                }
            }
        },
        xai_api_base_url="https://api.x.ai/v1",
    )

    adapter = ClawCLIAdapter(agent, global_cfg, api_key=None)
    env = adapter._env_from_provider("xai")
    assert env["XAI_API_KEY"] == "fresh-access"
    assert env["XAI_BASE_URL"] == "https://api.x.ai/v1"
    assert "OPENAI_API_KEY" not in env or env.get("OPENAI_API_KEY") != "fresh-access"


def test_select_backend_prefers_xai_for_grok_model():
    from orchestrator.flexible_backend_manager import FlexibleBackendManager

    class Dummy:
        pass

    mgr = Dummy()
    mgr.config = SimpleNamespace(
        allowed_backends=[
            {"engine": "claw-cli", "provider": "openrouter", "model": "deepseek/deepseek-v4-flash"},
            {"engine": "claw-cli", "provider": "xai", "model": "grok-4.5"},
        ]
    )
    selected = FlexibleBackendManager._select_backend_cfg(mgr, "claw-cli", target_model="grok-4.5")
    assert selected["provider"] == "xai"
    assert selected["model"] == "grok-4.5"


def test_device_login_poll_success(tmp_path: Path, monkeypatch):
    session = oauth.DeviceCodeSession(
        device_code="dev",
        user_code="USER",
        verification_uri="https://auth.x.ai/device",
        verification_uri_complete="https://auth.x.ai/device?user_code=USER",
        interval=0.01,
        expires_in=30,
        token_endpoint="https://auth.x.ai/oauth2/token",
        client_id="hashi-client",
        scopes="openid offline_access api:access",
    )

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    calls = {"n": 0}

    def fake_post(url, headers=None, data=None, timeout=20.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Resp(400, {"error": "authorization_pending"})
        return _Resp(
            200,
            {
                "access_token": "access-z",
                "refresh_token": "refresh-z",
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(oauth.httpx, "post", fake_post)
    cfg = SimpleNamespace(
        bridge_home=tmp_path,
        xai_oauth={"client_id": "hashi-client", "auth_store": "auth/xai_oauth.json"},
        claw_providers={},
        xai_api_base_url="https://api.x.ai/v1",
    )
    creds = oauth.poll_device_login(session, global_config=cfg, sleep_fn=lambda _s: None)
    assert creds.access_token == "access-z"
    store = tmp_path / "auth" / "xai_oauth.json"
    assert store.is_file()
    saved = json.loads(store.read_text(encoding="utf-8"))
    assert saved["tokens"]["refresh_token"] == "refresh-z"
