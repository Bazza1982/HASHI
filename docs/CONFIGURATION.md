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

## Credentials and authorization

Enter API credentials and optional Telegram credentials in the masked local
connection page. Telegram configuration uses the agent's telegram_token_key
reference and the configured authorized user ID. Do not paste credentials
into ordinary model chat.

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
