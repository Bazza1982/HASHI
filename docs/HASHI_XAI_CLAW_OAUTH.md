# HASHI-native xAI OAuth → Claw

**Status:** Coming soon (code landed; live OAuth blocked on HASHI's own `client_id`)  
**Date:** 2026-07-19  
**Commit:** `bc77fe6` — Add HASHI-native xAI OAuth for Claw Grok

## Coming soon

**Direct Grok OAuth** (HASHI login → Claw → Grok, no `grok-cli`, no Hermes) is **coming soon**.

| Layer | State |
| --- | --- |
| Device-code OAuth module | Implemented |
| Token store under `bridge_home` | Implemented |
| Claw `XAI_API_KEY` injection | Implemented |
| CLI / Telegram status commands | Implemented |
| Unit tests | Implemented (`tests/test_hashi_xai_oauth.py`) |
| HASHI-registered OAuth `client_id` from xAI | **Pending operator application** |
| Live Xishi smoke (login + one turn) | **Blocked until `client_id` is configured** |

xAI does not currently offer public self-service OAuth app registration. Third-party products (Hermes, Grok CLI) ship with a pre-issued client id. HASHI uses **strategy 1: its own configurable client** (`global.xai_oauth.client_id` or `HASHI_XAI_OAUTH_CLIENT_ID`). Until xAI issues that id, device login cannot complete against production `auth.x.ai`.

Until then:

- Existing backends stay unchanged (`grok-cli`, Hermes paths, `xai-api`, openrouter Claw, …).
- Operators may continue using **Grok CLI** for Grok models.
- Optional interim path (not the OAuth goal): Console API key as `XAI_API_KEY` for Claw — document only if product decides to enable it.

## Goal

Use Grok models through **Claw** without:

- `grok-cli`
- Hermes `auth.json` / `hermes_cli`
- HASHI `xai-api` engine

When live:

```text
python hashi.py auth xai login
  → device code at auth.x.ai
  → bridge_home/auth/xai_oauth.json  (0600)

agent: engine=claw-cli provider=xai model=grok-4.5
  → resolve_hashi_xai_credentials()
  → Claw env: XAI_API_KEY + XAI_BASE_URL
  → claw binary → api.x.ai
```

## What is already in the tree

| Path | Role |
| --- | --- |
| `adapters/hashi_xai_oauth.py` | Login, refresh, store (no Hermes imports) |
| `adapters/claw_cli.py` | `XAI_*` env allowlist + `auth_mode=hashi_oauth` |
| `orchestrator/flexible_backend_manager.py` | Prefer matching claw-cli row by model/provider |
| `hashi.py` | `auth xai status\|login\|logout` |
| `orchestrator/commands/xai_auth.py` | `/xaiauth` status |
| `tests/test_hashi_xai_oauth.py` | Unit tests |
| `docs/examples/xishi_claw_xai_backend.json` | Trial backend snippet for Xishi |

## Configuration (ready; `client_id` required for live login)

`agents.json` global (instance-local / often gitignored):

```json
{
  "xai_oauth": {
    "client_id": "<HASHI registered public OAuth client id — required>",
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
HASHI_XAI_OAUTH_CLIENT_ID=<client id issued to HASHI by xAI>
HASHI_XAI_OAUTH_SCOPES=openid offline_access api:access
```

Client strategy: **HASHI's own OAuth client** (not Hermes' embedded client id).

### How to obtain a client id

1. Apply to xAI (Console / API support / enterprise contact) for an OAuth **public** client for product **HASHI**.
2. Request Device Code flow against `https://auth.x.ai`, scopes at least `openid offline_access api:access`.
3. Place the issued id in `global.xai_oauth.client_id` or `HASHI_XAI_OAUTH_CLIENT_ID`.
4. Run `python hashi.py auth xai login`, then trial on **Xishi**: `/backend claw-cli grok-4.5`.

There is no public self-service “Create OAuth App” page for third parties as of 2026-07-19.

## Commands (available now; login needs client id)

```bash
python hashi.py auth xai status
python hashi.py auth xai login    # requires configured client_id
python hashi.py auth xai logout
```

Telegram (status only): `/xaiauth`

## Trial agent

**Xishi** is the designated trial agent (existing backends untouched). Example backend entry:

See [examples/xishi_claw_xai_backend.json](examples/xishi_claw_xai_backend.json).

After a successful login:

```text
/backend claw-cli grok-4.5
```

## Non-goals

- Do not delete or disable `grok-cli`, Hermes paths, or `xai-api`
- Do not read Hermes credentials for this path
- Do not route this path through `XaiApiAdapter`
- Multi-account credential pool (future)

## Rollout checklist (after client id arrives)

1. Configure `HASHI_XAI_OAUTH_CLIENT_ID` or `global.xai_oauth.client_id`
2. `python hashi.py auth xai login`
3. On Xishi: `/backend claw-cli grok-4.5`
4. One short prompt; confirm no `grok` CLI process
5. Mark this doc status **Live** and record smoke evidence
