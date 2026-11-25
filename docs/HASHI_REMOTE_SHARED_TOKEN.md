# HASHI Remote Shared Token

The shared token is the HMAC credential HASHI Remote and Workbench use to
authenticate each other. It is stored in the instance `secrets.json` as
`hashi_remote_shared_token`, and `HASHI_REMOTE_SHARED_TOKEN` overrides it when
set for the process.

## Owner command

`/remote token` (owner only) manages the credential without ever echoing it.

| Command | Effect |
| --- | --- |
| `/remote token` or `/remote token status` | Source, mask, fingerprint, file revision and the Remote service's loaded credential generation |
| `/remote token verify` | Same report, used after a change to confirm Remote has reloaded |
| `/remote token set <value>` | Validate, atomically write `secrets.json`, report the mask and the new revision |
| `/remote token set` | Same, but asks for the value in a reply prompt (5 minute expiry, `/cancel` aborts, the plaintext message is deleted when the client allows it) |
| `/remote token clear` | Remove the token and persist a disabled marker |

Validation: 16-512 characters, no whitespace or control characters. Surrounding
whitespace from a paste is trimmed before saving.

## Resolution order

1. `HASHI_REMOTE_SHARED_TOKEN`
2. `secrets.json` in the instance root
3. nothing (Remote requires pairing)

While the environment variable is set, `set` and `clear` refuse with
`remote_token_environment_locked` instead of appearing to succeed.

## Clearing is durable

Startup provisioning creates a token when none exists. `clear` writes
`hashi_remote_shared_token_disabled: true`, and provisioning honours that marker
so a cleared credential is not silently recreated on the next start. Setting a
token removes the marker.

## Reload evidence

Remote reloads the credential on its discovery maintenance cycle. `status` and
`verify` read `GET http://127.0.0.1:<remote-port>/health`, which needs no
credential, and report `credential.generation`, `last_reload_at` and whether the
loaded credential presence matches the file. A mismatched report means the
reload is pending, not that the save failed.

## Workbench side

Workbench System Settings stores its own copy in the instance-private state
directory and resolves `HASHI_REMOTE_SHARED_TOKEN` first, then that private
value, then inherited HASHI `secrets.json` files. Saving triggers a real probe of
every enabled connection and distinguishes "authenticated", "reached but
rejected the token" and "unreachable".

One Workbench value applies to every HASHI connection it manages; per-connection
overrides are not part of this change.
