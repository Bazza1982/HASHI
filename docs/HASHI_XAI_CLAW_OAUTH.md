# HASHI-native xAI OAuth → Claw

**Status:** Implemented (hashi1)  
**Date:** 2026-07-19

## Goal

Use Grok models through **Claw** without:

- `grok-cli`
- Hermes `auth.json` / `hermes_cli`
- HASHI `xai-api` engine

HASHI performs its own OAuth device-code login, stores tokens under the bridge home, refreshes access tokens, and injects `XAI_API_KEY` into the Claw subprocess.

## Architecture

```text
python hashi.py auth xai login
  → device code at auth.x.ai
  → bridge_home/auth/xai_oauth.json  (0600)

agent: engine=claw-cli provider=xai model=grok-4.5
  → resolve_hashi_xai_credentials()
  → Claw env: XAI_API_KEY + XAI_BASE_URL
  → claw binary → api.x.ai
```

## Configuration

`agents.json` global:

```json
{
  "xai_oauth": {
    "client_id": "<HASHI registered public OAuth client id>",
    "scopes": "openid offline_access api:access",
    "auth_store": "auth/xai_oauth.json",
    "base_url": "https://api.x.ai/v1"
  },
  "claw_providers": {
    "providers": {
      "xai": {
        "auth_mode": "hashi_oauth",
        "base_url": "https://api.x.ai/v1",
        "status": "provisional",
        "env_api_key": "XAI_API_KEY",
        "env_base_url": "XAI_BASE_URL"
      }
    }
  }
}
```

Environment override:

```text
HASHI_XAI_OAUTH_CLIENT_ID=<client id>
HASHI_XAI_OAUTH_SCOPES=openid offline_access api:access
```

Client strategy: **HASHI's own OAuth client** (not Hermes client id).

## Commands

```bash
python hashi.py auth xai status
python hashi.py auth xai login
python hashi.py auth xai logout
```

Telegram (status only): `/xaiauth`

Device-code login must complete on the host shell (browser on the operator machine).

## Trial agent

**Xishi** has an additional allowed backend (existing backends untouched):

```json
{
  "engine": "claw-cli",
  "provider": "xai",
  "model": "grok-4.5",
  "permission_mode": "workspace-write",
  "allowed_tools": ["read", "glob", "grep"]
}
```

After login:

```text
/backend claw-cli grok-4.5
```

Backend selection prefers the `provider=xai` row when the model is a Grok id.

## Non-goals (this feature)

- Do not delete or disable `grok-cli`, Hermes paths, or `xai-api`
- Do not read Hermes credentials
- Do not route through `XaiApiAdapter`
- Multi-account credential pool (future)

## Files

| Path | Role |
| --- | --- |
| `adapters/hashi_xai_oauth.py` | Login, refresh, store |
| `adapters/claw_cli.py` | `XAI_*` env allowlist + `auth_mode=hashi_oauth` |
| `hashi.py` | `auth xai` CLI |
| `orchestrator/commands/xai_auth.py` | `/xaiauth` |
| `tests/test_hashi_xai_oauth.py` | Unit tests |
