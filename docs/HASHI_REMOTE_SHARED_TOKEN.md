# HASHI Remote Shared Token

The shared token is the HMAC credential HASHI Remote and Workbench use to
authenticate each other. It is stored in the instance `secrets.json` as
`hashi_remote_shared_token`, and `HASHI_REMOTE_SHARED_TOKEN` overrides it when
set for the process.

## Owner command

`/remote token` is a subcommand of the existing `/remote` command (owner only).
It manages the credential without ever echoing it.

| Command | Effect |
| --- | --- |
| `/remote token` or `/remote token status` | Source, mask, fingerprint and file revision. No network request, no reload claim |
| `/remote token verify` | Signs one request with the credential in force and reports whether the running Remote service **accepted**, **rejected** or did not answer |
| `/remote token set` | Asks for the value in a reply prompt (5 minute expiry) |
| `/remote token clear` | Removes the token and persists a disabled marker |
| `/remote token cancel` | Abandons a pending prompt |

Validation: 16-512 characters, no whitespace or control characters. Surrounding
whitespace from a paste is trimmed before saving.

### Why the value is never typed inline

A token written into the command line is copied into command auditing, the
session projection and the chat history. `/remote token set <value>` is therefore
refused with a pointer to the reply prompt. The prompt reply is consumed by the
message handler before history, queue or agent dispatch, the user's message is
deleted when the client allows it, and the value is handed straight to the
signer and the secrets writer.

### Cancelling

Reply `cancel` (or `取消`) to the prompt, or send `/remote token cancel`. Both
reach the same state and leave the stored credential untouched.

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

## Verification evidence

`verify` (and the report at the end of `set` and `clear`) sends one HMAC-signed
`GET /workbench/v1/status` to the Remote service on its loopback port, signed
with the credential that is in force. This is the same endpoint and the same
signature the service already enforces for Workbench, so the result is evidence
about the running process, not about configuration:

| Result | Meaning |
| --- | --- |
| `verified` | The running service accepted this credential (2xx) |
| `rejected` | The service answered but refused this credential (401/403); it is still using another value and picks up the new one on its next reload |
| `unavailable` | No answer on either scheme; there is no evidence either way |
| `missing` | No credential is in force, so no request was sent |

Remote reloads the credential on its discovery maintenance cycle. A `rejected`
result right after `set` therefore means "saved, reload pending", never "save
failed".

## Workbench side

Workbench System Settings stores its own copy in a dedicated private
`shared-token` subdirectory of the Workbench state directory; the ACL hardening
is scoped to that subdirectory and never touches the rest of the state
directory. It resolves `HASHI_REMOTE_SHARED_TOKEN` first, then that private
value, then inherited HASHI `secrets.json` files.

Saving triggers a real probe of every enabled connection and distinguishes
"authenticated", "reached but rejected the token", "could not be reached" and
"no credential, so no request was attempted". Clearing the private copy reports
whether an inherited credential is still in force.

One Workbench value applies to every HASHI connection it manages; per-connection
overrides are not part of this change.
