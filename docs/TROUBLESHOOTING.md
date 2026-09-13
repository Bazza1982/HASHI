# HASHI Troubleshooting

[Install](INSTALL.md) · [User guide](USER_GUIDE.md) ·
[Configuration](CONFIGURATION.md) · [Releases](RELEASES.md)

## Identify what is installed

For the current npm command surface:

~~~bash
hashi version
hashi help
hashi status --all
hashi doctor
hashi logs --lines 100
~~~

If help does not list these commands, first inspect the installed package:

~~~bash
npm list --global hashi-bridge --depth=0
npm view hashi-bridge dist-tags --json
~~~

A legacy npm install can coexist with a newer Git checkout. Updating source
files does not replace the global command, and replacing the global command
does not prove an existing running instance adopted it.

On Windows use Get-Command hashi -All in PowerShell or where hashi in CMD.
On Linux/macOS/WSL use command -v hashi. Keep native Windows and WSL
installations in their intended OS environment.

## Runtime setup is incomplete

The current runtime requires the approved CPython patch and locked dependency
set. A successful npm download is not sufficient if post-install reported
incomplete runtime setup.

From a source checkout, use the approved interpreter to inspect the contract:

~~~bash
python --version
python scripts/check_runtime_contract.py --json
~~~

Follow the reported missing/runtime mismatch details. Prepare an environment
for the intended program version; do not upgrade dependencies underneath a
running Core. See [dependency profiles](DEPENDENCIES.md).

## Engine or provider cannot connect

Use /connect in the local TUI, or hashi onboard. Choose an installed CLI or
configure a HER v2 provider in the masked local page. Confirm the chosen
provider's minimal connection check before expecting model tasks to work.

For CLI engines, verify the executable and authentication from the same OS
and user environment as HASHI. For API providers, check the configured
endpoint, credential reference, and model permission. Credentials entered
into ordinary chat do not configure a connection.

## Telegram does not respond

Check the configured agent's telegram_token_key and corresponding private
secret, the authorized numeric user ID, and whether the agent is enabled.
Each polling Bot Token should be used by only one running poller.
Telegram is optional; a skipped Telegram connection does not prevent local
TUI use once an engine is connected.

## An instance is already running or a port is occupied

Use status/doctor to identify the selected instance, its configured home,
and live process. Multiple instances may run on one machine, but each needs
its own identity/data scope and non-conflicting ports.

Do not infer ownership from a directory name, delete a live lock file, or
kill every Python/HASHI process. A duplicate launch of the same instance and
a different process occupying its port are different problems.

## A task is stalled

Use /status and the relevant /queue or /bg status view. /stop cancels work;
/steer changes direction; /resend repeats prior output; /retry executes the
last retryable request again. Review possible side effects before retrying
a task that may already have changed files or external state.

A restart is an operational action, not a diagnostic prerequisite. Function
adoption, shared replacement, and Core migration have different scopes.

## A Remote peer appears offline

Check Remote health, its configured port, the peer handshake, and the
capability required for the operation. Discovery-only is not a completed
trusted connection. An available Remote peer does not imply that its Backend
API, agents, or browser workers are ready.

See [Remote setup](INSTALL.md#hashi-remote) and
[TUI switching](https://github.com/Bazza1982/HASHI/blob/main/docs/TUI_INSTANCE_SWITCHING.md).

## Logs and bug reports

hashi logs reads the selected instance's runtime logs. Source instances
normally keep logs under their configured bridge home. Gateway transport
and observability logs can contain request/response material; review and
redact them before sharing.

Report issues at [GitHub Issues](https://github.com/Bazza1982/HASHI/issues).
Include the program/package version, installation method, OS, Python version,
selected engine/model, reproduction steps, and relevant redacted errors.
Never attach secrets.json, OAuth profiles, Bot Tokens, or full private
conversation/workspace dumps.
