# HASHI-native xAI OAuth

HASHI retains a backend-neutral device-login and token-store utility for a
future HASHI-issued xAI OAuth client.

## Current boundary

- `python hashi.py auth xai status|login|logout` manages the HASHI-owned token
  store.
- `/xaiauth status` reports that store from an Agent session.
- Login requires `global.xai_oauth.client_id` or
  `HASHI_XAI_OAUTH_CLIENT_ID`.
- No active backend automatically consumes this token store. HER v1's native
  environment injection was retired with that backend.
- The `xai-api` adapter keeps its existing, separately configured API-key or
  Hermes credential path.

This separation prevents a credential-management command from silently
changing backend routing.

## Configuration

```json
{
  "global": {
    "xai_oauth": {
      "client_id": "<HASHI-issued public OAuth client id>",
      "scopes": "openid offline_access api:access",
      "auth_store": "auth/xai_oauth.json",
      "base_url": "https://api.x.ai/v1"
    }
  }
}
```

The token store is instance-local. Do not commit it to Git or copy it into an
Agent package.
