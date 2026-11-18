# HASHI Configuration

[Install](INSTALL.md) · [User guide](USER_GUIDE.md) ·
[Integrations](INTEGRATIONS.md) · [Troubleshooting](TROUBLESHOOTING.md)

Prefer onboarding and the live settings menus for normal setup. They validate
available choices and preserve the existing instance identity. This guide
explains where configuration belongs.

## Program and instance data

The program directory contains shipped code and assets. Each instance has a
separate bridge home containing identity, credentials, workspaces, and state.
A source checkout may use its repository as its bridge home; npm-managed
instances keep data outside the installed program.

| Resource | Role |
|---|---|
| agents.json | Instance identity, ports, agent definitions, and runtime choices |
| secrets.json | Referenced API keys, Bot Tokens, and other local credentials |
| tasks.json | Scheduler definitions |
| workspaces/ | Agent identity and continuity material |
| state/ | Runtime-owned persisted control state |
| logs/ | Local execution and diagnostic records |

Paths are relative to the selected instance's bridge home unless a field
explicitly defines another base. See the
[registry locations](INSTALL.md#npm-command-install-and-named-instances).

Use the supplied
[agent configuration sample](https://github.com/Bazza1982/HASHI/blob/main/agents.json.sample)
and [secrets sample](https://github.com/Bazza1982/HASHI/blob/main/secrets.json.sample)
as schema examples. Onboarding generates a local configuration; do not copy
another operator's identities, paths, account IDs, tokens, or ports.

## Session message age

Session API messages normally proceed under their existing authentication and
idempotency rules even when their sender timestamp is old or unavailable. The
timestamp and observed delay are informational and are not authorization.

An instance may explicitly select `global.session_message_age_policy` in its
configuration. `{"mode": "expire", "max_age_seconds": 60}` rejects a
message if it exceeds the selected deadline or its timestamp is unknown;
`{"mode": "confirm", "max_age_seconds": 60}` requests confirmation for
old messages. The default is `observe` and does not add an age gate. This
setting currently applies to the Session API; a verified local TUI is exempt
from the confirmation mode, but not the explicit expiry mode. Other connectors
retain their own existing behavior. Shared-token authentication and replay checks are
separate and are not changed by this setting.
An expired retry of an already accepted Session API request can retrieve the
original receipt after its stored request identity is checked; this lookup
cannot create a new Run. A different request using the same key is a conflict.

`global.hchat_request_age_policy` uses the same `observe`, `confirm`, and
`expire` shape for ordinary HChat requests received by LAN protocol,
Workbench HChat paths, and Exchange. It defaults to `observe`; already recorded
confirmation decisions remain authoritative after the setting changes. Exchange
stores the selected policy at acceptance, uses the same decision on recovery,
and applies an explicit expiry before accepting a new request. A matching
already-accepted request is not expired retroactively by that age policy.
A newly authenticated exact retry can retrieve its already committed receipt
after the Exchange transport deadline; an expired new message cannot be
accepted, and a changed payload conflicts with the saved identity.
The request's observed age is retained as message context when available.

A terminal reply with a verified outbound route and recorded source
conversation is shown once in that conversation, with its observed age, without
a display confirmation. This includes newly bound public-address Exchange
drafts; the signed Exchange delivery deadline still applies at acceptance.
Legacy replies without a source conversation binding retain their existing
confirmation handling.

`global.telegram_message_age_policy` has the same optional shape and defaults
to `observe`, so ordinary Telegram messages do not require another decision
because of their age. An explicitly selected `confirm` or `expire` policy uses
its configured threshold. Earlier pending, discarded, or expired decisions
continue to apply after a policy change. Normal text Runs carry a stable
Telegram update identity and preserve their observed message age. Media and
batch replay across restarts still require separate end-to-end qualification.

## Telegram user access

An authenticated administrator can stage a selected Telegram primary without
a recent Bot conversation or an existing private fallback file. An optional
observed-account proof uses its selected expiry only when that mode is chosen.
The administrative workflow finalizes after successful adoption. Finalization
creates or updates the private fallback with revision checks; a concurrent change
is preserved. The helper itself does not verify that a runtime adopted the change.

Telegram user settings can be inspected and updated while an Agent is offline.
The persisted settings are the desired policy. The access view reports each
Worker's observed policy revision; an offline, older, or unreachable Worker is
shown as pending rather than reported as having adopted the update. The shared
Telegram ingress checks the current persisted grant before forwarding a known
sender to a Function Worker. A message already being handled by an older Worker
or an already-started reply may need that Worker to converge before the change
is fully effective; saving the setting does not restart an Agent.

`global.telegram_users_per_scope_max` is the configurable maximum number of
users per global or Agent list, defaulting to 10,000. The access API advertises
the selected value. Existing larger lists remain readable and users can still
be removed; further additions require a sufficiently large configured value.
The Telegram access management API optionally pages each list independently:
`page_size` selects 1–500 entries, `global_page` and `agent_page` are zero-based,
and `revision` on subsequent reads detects concurrent changes. Older clients
that omit paging continue to receive complete lists. The page size does not
change the membership capacity.

## Agent document editor budget

`global.agent_document_max_bytes` sets the maximum UTF-8 document size that the
Agent document editor may read or replace. It must be a positive integer; the
default is 8 MiB. The document API reports the actual limit to clients and
continues to require the document revision when saving. Workbench separately
reports its transport budget, which may be lower after JSON encoding. Change
both budgets when supporting larger files; neither setting changes the Agent's
existing workspace or document ownership.

## Agent transfer receipt retention

The transfer finalizer records each operation in its instance-local SQLite
journal. Existing JSON state is imported on first access; the old file remains
as a migration snapshot. The journal is authoritative after migration. A
rollback or a mixed-version deployment must reconcile the current journal
first: the snapshot does not contain subsequent progress. If an older process
changes the snapshot after migration, the newer process reports the conflict
instead of silently discarding either copy.

There is no automatic receipt pruning by default. Operators may explicitly set
`HASHI_AGENT_MOVE_DELIVERED_RECEIPT_RETENTION` to a non-negative count to keep
that many most recent completed or cancelled receipts whose notifications were
sent or not requested. Active operations and undelivered or unconfirmed results
are never pruned by this policy. An invalid value is logged and ignored so it
cannot prevent recovery. All processes serving the same instance should use
the same setting.

`HASHI_AGENT_MOVE_EXECUTION_MAX_ATTEMPTS` and
`HASHI_AGENT_MOVE_DELIVERY_MAX_ATTEMPTS` optionally select non-negative retry
budgets; zero or an unset value means no fixed attempt limit. Recoverable
failures use a bounded backoff and retain the same transfer or publication ID.
An execution that reaches its configured budget can be explicitly resumed; a
completed transfer's notice can be resumed only when its last attempt was
proved unsent. Completed moves are not rerun to deliver notices, and an
unknown transport outcome is kept for reconciliation rather than sent again
without proof.

## Engine and model choices

Agent definitions explicitly use the supported Flex runtime type.
The runtime type and the Fixed/Flex working mode are different settings.

/backend selects an allowed Engine. /provider and /model configure HERV3's one
active Model Provider/model target. OpenRouter and DeepSeek are provider
adapters inside HERV3; their compatibility entries do not make them
selectable top-level engines.

Instance model and effort opt-ins belong in allowed_backends and its
supported model_efforts fields. Effective options resolve through the
runtime's existing capability resolver. Adding an instance model does not
require changing the shared catalogue.

The shipped Codex CLI default is `gpt-5.6-sol` with explicit `medium`
reasoning. Its shared catalogue contains only `gpt-5.5`, the Sol/Terra/Luna
GPT-5.6 family, and `gpt-6-astra`. OpenRouter is available only as a HERV3
Model Provider and exposes the approved DeepSeek V3.2 Exp, V4 Flash, V4 Pro,
and Gemini 3.8 Flash models. The official DeepSeek API exposes
`deepseek-flash` (V4.1 Flash with native vision) and `deepseek-v4-pro`;
temporary retired Flash aliases are accepted by the adapter but are not shown
as current model choices.

HERV3 has no HER execution modes or stage routes. `/effort` is a
Provider/model capability and selects only a supported reasoning level.
Legacy Direct/Strategic/Planned values and Quick/Pro route matrices are
compatibility inputs, not current selectable behavior.

Detailed contracts:
[working modes](https://github.com/Bazza1982/HASHI/blob/main/docs/FIXED_FLEX_WORKING_MODES.md),
[HERV3 upgrade](https://github.com/Bazza1982/HASHI/blob/main/docs/HERV3_UPGRADE.md),
and [runtime configuration boundaries](https://github.com/Bazza1982/HASHI/blob/main/docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md).

## Privacy configuration

Each Agent starts at Privacy Level 1 unless its own saved state selects another
level. Use `/privacy` to inspect or change the effective level; do not edit
Agent state files by hand. Level 2 is available only with HERV3 and its
qualified official DeepSeek Model Provider. Its local detector must be ready,
and the user must explicitly accept the residual PII miss risk before the
setting is saved. Installing the detector alone does not activate Level 2.

The detector runs in a separate Python environment, outside the HASHI Core
interpreter. Source, npm, enterprise, and Portable Windows installation paths
have different preparation steps; see [installation](INSTALL.md) and
[dependency profiles](DEPENDENCIES.md#isolated-privacy-level-2-detector).
Advanced deployments may select another local interpreter with
`HASHI_PRIVACY_FILTER_PYTHON` or a trusted replacement detector with
`HASHI_PRIVACY_FILTER_SCRIPT`; the replacement must obey the same local span
contract. Level 2 still cannot guarantee that all PII is found.

## Credentials and authorization

Enter API credentials and optional Telegram credentials in the masked local
connection page. Telegram configuration uses the agent's telegram_token_key
reference and the configured authorized user ID. Do not paste credentials
into ordinary model chat.

Workbench can replace an individual Agent's Bot token from **Manage Agents →
Manage → Telegram**. HASHI returns only configured status and an opaque
revision, validates a submitted token with Telegram `getMe`, and publishes it
with revision-checked private-file replacement. Shared credential references
and missing authorized-user configuration fail closed. When the Agent is running,
HASHI attempts an Agent-scoped activation after saving. If the Agent is busy or
activation is unavailable, the saved credential stays pending. A connected
status requires an authorized Telegram user and both live transport directions;
it does not establish that a real user's round trip has succeeded.

Replacing the human Telegram account is a separate instance lifecycle. An
administrator can select the new account, or explicitly choose an observed
private-chat proof. Unrelated delegate grants are preserved by default; an
explicit option can revoke them. The previous private fallback remains until
the new Function generation is adopted. Finalization revision-checks and
replaces that fallback without exposing Bot tokens. Historical channel bindings
are inert audit evidence, not authorization or delivery targets.

During migration, a legacy `owner_scope_id` still locates existing local data.
It is not a credential or a cross-instance permission, and its spelling or
length does not block configuration loading. A personal instance establishes a
local data binding from its existing lookup at Function bootstrap or the first
verified history-bearing inbound transfer. Read-only configuration inspection
does not create the binding. Later Telegram account changes do not change that
local data key. The binding does not discover or adopt unidentified old history;
a fallback with unknown older history remains explicitly reviewable.

The local binding is instance state, not a network credential or data to copy
between instances. When missing, a compatibility projection of the same data
key is written for older binaries; it does not grant access. An existing different
value is preserved and reported, never overwritten to manufacture agreement.
An invalid Telegram configuration can still prevent an older binary from
starting. Downgrade must be tested with the actual previous binary and current
configuration before claiming continuity. The broader migration to
connector-neutral ownership remains unfinished.

Keep secrets and OAuth stores private and out of source control. CLI engines
manage their own authentication; HERV3 uses configured provider profiles and
secret references.

Review permission_mode, access_scope, and tools.allowed for each engine
entry. Broad defaults are not a sandbox guarantee. Workzones, tool allowlists,
provider policy, and engine-level permissions each have their own role.
Configure access for the work that the agent is meant to perform.

## Ports and multiple instances

Use generated or explicitly configured per-instance ports. Port numbers and
instance names in older examples are not assignments for new installations.
The compatibility field global.workbench_port names the Backend API;
global.api_gateway_port names the optional model Gateway.

The registry chooses an instance; that instance's local configuration remains
the owner of its identity and ports. An existing Git registration reads those
facts without rebuilding them. Use status/doctor and the configured service
address to verify ownership.

## Instructions, skills, and scheduled work

Use /sys for personal or instance-global instruction slots. Use /skill for
installed instruction packages and /jobs for scheduled work. These have
different owners and persistence; a skill body is not a replacement scheduler.

Local command extensions live outside the tracked program in the user-scoped
HASHI private_commands directory. A module can export COMMANDS or
get_commands(); inline controls can export CALLBACKS or get_callbacks().
Picker metadata and implementation stay in that local module. Apply updates
through the scoped Agent Function adoption path.

## Backups and upgrades

Back up the complete selected bridge home, including credentials and
workspace/state files, before a migration. Treat backups as private.

An npm uninstall removes program entry points, not managed instance data.
Source-checkout deletion can remove its instance data when both share a
directory; locate and back up that data first. Portable uninstall has its
own contract and may permanently remove the installed data tree.

Use the [installation guide](INSTALL.md#stop-remove-upgrade-and-uninstall-safety)
for managed remove/restore and explicit program adoption. Never repair an
instance by replacing its configuration with a sample or deleting a lock
file while its process may still be running.


### Local connection setup without a live probe

The local connection page offers an explicit Save unverified action. It stores
the selected configuration without contacting the model provider or reloading
the running Agent, and records the selection as unverified. Connect remains an
optional, explicitly consented model check; a passing check is recorded for the
exact selected engine and model, without asserting runtime readiness. The page
accepts a custom model ID for adapters that support one. Settings and credentials
use revision-checked private writes; a stale page must be reopened before saving.
A different existing credential still requires explicit replacement.


### Fixed CLI tool-gateway capacity

Fixed CLI invocations wait in a per-adapter FIFO when all tool-gateway slots
are in use. The default is 16 active invocations and 128 waiting invocations.
`HASHI_MCP_MAX_ACTIVE_INVOCATIONS` selects a positive active-slot count;
`HASHI_MCP_MAX_QUEUED_INVOCATIONS` selects a nonnegative queue count, where
zero requests immediate backpressure instead of waiting. Invalid settings fall
back to their defaults. These limits are read when the adapter's admission queue
is created.

Cancelling a waiter removes it before dispatch. A full queue returns a
retryable capacity result with the current counts; it does not claim that the
request executed. Before a queued request starts, its current Run binding and
attachment grants are checked again. Switching the backend while it waits
returns a retryable not-started result. Existing Run fencing and owner tool
permissions continue to apply.
