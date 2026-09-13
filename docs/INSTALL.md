# HASHI — Installation Guide (Windows / macOS / Linux)

> This is the **single source of truth** for installing and running HASHI.
>
> Scope: current source-candidate installation, npm command packaging, and
> isolated instances. Published packages may lag behind this source.

---

## Contents

- [npm command install and named instances](#npm-command-install-and-named-instances)
- [Source install](#source-install)
- [Windows](#windows)
- [macOS](#macos)
- [Linux (native) / WSL2](#linux-native--wsl2)
- [Python dependency profiles](#python-dependency-profiles)
- [Multi-instance ports](#multi-instance-ports)
- [Hashi Remote](#hashi-remote)
- [Nagare developer install](#nagare-core-developer-install)

Check [release and package selection](RELEASES.md) before choosing an archive
or npm version. At the 2026-09-11 documentation review, npm latest was still
1.0.1 and GitHub stable Latest was v2.0.0; neither represented the current
v4.0.0-alpha.2 source candidate. Beta publication remains a separate step.

---

## npm command install and named instances

The npm package name is **`hashi-bridge`**. The unscoped package name `hashi`
belongs to an unrelated third party and must not be used for this project. A
current-generation `hashi-bridge` package installs the `hashi` and
`hashi-onboard` commands. Inspect what is published:

```bash
npm view hashi-bridge dist-tags --json
npm view hashi-bridge versions --json
```

Replace the placeholder below with an actually published version. A bare
install selects npm's `latest` tag and may install a legacy package.
Pre-release channels such as `beta` are usable only after publication:

```bash
npm install --global hashi-bridge@<published-version>
hashi help
```

Use Node.js 18+ and npm 9+ as declared in the package metadata, plus the
approved CPython 3.12.13 interpreter in the same OS environment.
For the unreleased source candidate, follow [Source install](#source-install).

Post-install prepares a versioned, user-scoped Python 3.12.13 virtual
environment from `constraints/standard-py312.lock`. It prints “runtime is
ready” only after the complete runtime contract passes. If Python, network, or
dependency preparation fails, npm leaves the program entry installed but
reports **runtime setup is incomplete**; the failed build is not adopted and
no instance data is touched.

One program installation serves multiple isolated named instances in the same
OS environment. Windows, each WSL distribution, native Linux, and macOS keep
separate registries and never follow another environment's PATH or runtime.
The default locations are:

| Environment | Registry | Instance data |
|---|---|---|
| Windows | `%LOCALAPPDATA%\HASHI\instances.json` | `%LOCALAPPDATA%\HASHI\instances\<name>` |
| Linux / WSL | `${XDG_CONFIG_HOME:-~/.config}/hashi/instances.json` | `${XDG_DATA_HOME:-~/.local/share}/hashi/instances/<name>` |
| macOS | `${XDG_CONFIG_HOME:-~/.config}/hashi/instances.json` | `${XDG_DATA_HOME:-~/.local/share}/hashi/instances/<name>` |

`HASHI_REGISTRY_ROOT` and `HASHI_DATA_ROOT` are supported explicit overrides,
primarily for managed deployments and tests. They do not make a Windows
registry visible to WSL or vice versa. Both roots must remain outside the npm
program directory so a later package uninstall cannot remove instance data.

### Commands

```text
hashi
hashi start
hashi tui
hashi status
hashi stop
hashi ui
hashi --instance <name> ...
hashi instance create <name>
hashi instance list
hashi instance default <name>
hashi instance bind <name> [directory]
hashi instance remove <name>
hashi instance restore <name>
hashi instance adopt <name>
```

`hashi` starts or attaches the selected instance and opens the TUI. `start`
runs it in the background, while `status`, `stop`, and `tui` all use the same
registry, bridge home, configured Backend API port, instance lock, and runtime
identity. Instance selection is deterministic:

1. explicit `--instance`;
2. the longest matching current-directory binding;
3. the user default;
4. the only registered instance;
5. a terminal prompt when interactive, or an explicit ambiguity error for a
   script/non-interactive caller.

The first interactive `hashi` run creates a minimal `default` instance and
enters onboarding. A scripted first run fails with instructions instead of
creating an identity implicitly. To create another isolated instance:

```bash
hashi instance create research --default
hashi instance bind research /path/to/research-project
hashi --instance research onboard
hashi --instance research start
```

Creation writes only a new instance ID, an unused adjacent Backend/API Gateway
port pair, and an empty Agent list. Credentials are created only through that
instance's onboarding. Onboarding writes workspaces, language, wakeup state,
configuration, secrets, and crash logs beneath the selected bridge home—not
the shared npm program directory.

An existing Git instance can be registered without reconstructing or editing
its identity:

```bash
hashi instance create hashi2 --from /home/me/projects/hashi2
hashi instance create split-home --from /opt/hashi --home /srv/hashi-data
```

Registration requires an existing HASHI `main.py`, runtime contract, and
`agents.json`; it reads the configured instance ID/ports and binds the code and
data roots. It does not copy or rewrite the checkout, credentials, workspaces,
or Agent PCMs. External/Git data can be unregistered but is never recursively
purged by this command.

### Stop, remove, upgrade, and uninstall safety

`hashi stop` first verifies the exact local API identity plus every Agent's
live generation/queue/transfer state and all non-terminal background jobs. If
work exists—or either activity endpoint cannot be verified—it sends no shutdown
request and never falls back to a force kill.
When idle, it requests the existing authenticated graceful-shutdown API and
waits for the instance lock to release.

`hashi instance remove <name>` requires the instance to be stopped. Managed
data moves atomically into the user-scoped HASHI recovery area and can be
returned with `instance restore`; Git/external data remains where it was.
Permanent deletion is available only for marker-verified managed data and
requires both `--purge` and an exact `--confirm <name>` (or exact interactive
entry):

```bash
hashi instance remove research --purge --confirm research
```

Updating the npm program never restarts a running instance and never rewrites
its identity or data. A stopped managed instance whose recorded version differs
from the installed version reports `update-pending`; adopt it explicitly, then
start it:

```bash
npm install --global hashi-bridge@<version>
hashi --instance research status
hashi instance adopt research
hashi --instance research start
```

`npm uninstall --global hashi-bridge` removes program entry points only. It has
no uninstall lifecycle script and leaves the registry, versioned runtime,
recoverable removals, identities, credentials, workspaces, transcripts, and
logs intact.

Workbench remains retired. `hashi ui` only starts an independently installed
`hashi-ui` executable (or the exact path in `HASHI_UI_EXECUTABLE`) against the
selected instance's Backend API; no Workbench implementation is bundled or
silently restored.

---

## Source install

Install Git and the approved CPython **3.12.13** interpreter first. Clone the
repository; the directory name below matches Git's case-sensitive default:

```bash
git clone https://github.com/Bazza1982/HASHI.git
cd HASHI
python --version
```

`python --version` must report 3.12.13 before continuing. Use `python3.12` or
the Windows `py -3.12` launcher instead if needed, after verifying its exact
patch version.

On Linux/macOS, create and activate a new virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

On WSL, use `.venv-wsl` so a shared checkout does not reuse a Windows venv:

```bash
python -m venv .venv-wsl
source .venv-wsl/bin/activate
```

On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked by local shell policy, invoke the environment's
Python directly; do not change machine-wide execution policy for this guide.
Install and check the standard runtime using that environment:

```bash
python -m pip install -r constraints/standard-py312.lock
python scripts/check_runtime_contract.py --json
```

Open the local connection page from the repository root:

```bash
python -m onboarding.onboarding_main
```

Choose a detected CLI engine or a HER v2 Model Provider, enter any required
credentials in the masked local page, and confirm the minimal connection
check. Telegram is optional. After setup, launch with the platform helper
below, or run `python main.py` from the activated environment.

This runs the checked-out source. A GitHub source archive uses the same setup
steps after extraction; it is not a portable installation bundle.

## Windows

### Prerequisites
- Windows 10/11
- CPython 3.12.13 (approved Core; source compatibility is 3.12)
- Node.js + npm for npm installation or Nagare visual editor development;
  the direct Python source runtime does not require them

### Install
Follow [Source install](#source-install) or the explicit npm version path.

For a self-contained Windows handoff, use the allowlisted Portable Windows
builder described in the [Portable Windows guide](https://github.com/Bazza1982/HASHI/blob/main/packaging/portable_windows/README.md).
The resulting USB is verified installation media; HASHI runs from the local
copy installed on the recipient PC.

### Run
- Preferred: use the unified launcher `bin/bridge-u.bat`.
- HASHI exposes its Python Backend API on the configured `workbench_port`.
  Workbench has retired and is no longer installed or launched by HASHI.

---

## macOS

> The source launchers target macOS 12+ on supported Python environments. The
> portable builder currently targets Apple Silicon only and has deterministic
> source-copy and asset-integrity contracts; each release still requires a
> real macOS smoke run before claiming platform qualification.

### Prerequisites
- macOS 12.0+ (Monterey) recommended
- A way to install the approved Python runtime (Homebrew is optional)

### Install
Follow [Source install](#source-install). Homebrew alone does not guarantee
the exact approved Python patch. Install Node.js/npm when using the npm
command path or working on the Nagare editor.

For an offline handoff image, start from a clean checkout on a connected Mac:

```bash
bash mac/prepare_usb.sh /Volumes/MyUSB
```

The builder copies committed files only, verifies the Apple Silicon CPython
archive by its pinned SHA-256 digest, and refuses to overwrite an existing
`/Volumes/MyUSB/HASHI` image. Intel Macs must use the source-install path: the
current locked dependency generation does not provide a complete Intel wheel
set, so the portable builder fails closed instead of compiling unreviewed
native dependencies.

---

## Linux (native) / WSL2

### Prerequisites
- Ubuntu 22.04+ recommended
- CPython 3.12.13 + venv
- Node.js + npm for npm installation or Nagare editor work

Follow [Source install](#source-install) before launching.

### Run
- Preferred: `./bin/bridge-u.sh --resume-last`

---

## Python dependency profiles

For a normal local HASHI installation, use the standard source profile:

```bash
python -m pip install -r constraints/standard-py312.lock
```

`requirements.txt` is the human-maintained dependency input; launch and
deployment use the generated lock above. The standard profile includes the
core plus media handling, Hashi Remote, and the terminal UI. Test packages are
deliberately separate:

```bash
python -m pip install -r requirements-dev.txt
```

Every launcher checks the interpreter against `[tool.hashi.runtime]` before
importing HASHI. Linux/WSL uses a separate `.venv-wsl` when the checkout also
contains a native Windows `.venv`. Do not install or upgrade packages in a
running Core environment; rebuild the environment and perform a planned Core
migration instead.

HASHI sets `[tool.uv] managed = false` because the repository-local environment
may belong to a running Core. Run diagnostics with that environment's Python
directly. For disposable helpers, use `uv run --no-project` or an explicitly
isolated environment; project-aware `uv run`, `uv lock`, and `uv sync` must not
modify the runtime environment implicitly.

Minimal or specialised environments can install from `pyproject.toml` instead:

```bash
python -m pip install -e .
python -m pip install -e ".[media,remote]"
python -m pip install -e ".[browser,voice]"
python -m pip install -e ".[all]"
```

The last command installs every declared optional integration and can be very
large. See [Dependency profiles](DEPENDENCIES.md) before choosing it.

---

## Multi-instance ports

HASHI supports running multiple instances simultaneously.

- Each instance should have its own `bridge_home` directory.
- Each instance should use a unique `workbench_port`.

Managed creation allocates ports and stores them in instance configuration.
Use the selected instance's status/doctor output and configuration to discover
them. Older named-instance port examples are not defaults for new installs.

---

## Hashi Remote

Hashi Remote is included and enabled by default. On supported platforms HASHI
registers and enables its per-instance OS supervisor on first startup. If OS
supervision is unavailable, HASHI uses bundled child mode so Remote remains
available for the current session. `/remote on|off` remains the operator control.

### Shared token

Configure the same shared token on each trusted HASHI instance:

```bash
export HASHI_REMOTE_SHARED_TOKEN="<long random token>"
```

or add this key to each instance's `secrets.json`:

```json
{
  "hashi_remote_shared_token": "<long random token>"
}
```

Without a token, Remote starts in `discovery-only` mode. Peers can discover one
another, but trusted protocol messaging, full peer detail, file transfer, and
rescue controls are unavailable.

### Linux / WSL

```bash
bin/hashi-remote-ctl.sh enable
bin/hashi-remote-ctl.sh status
```

The helper derives a distinct unit name from each checkout's configured
`global.instance_id` (for example, `hashi-remote-hashi1.service`). This lets
multiple HASHI instances on the same Linux/WSL user session keep independent
Remote supervisors. Use `bin/hashi-remote-ctl.sh service-name` to print the
resolved unit name.

For manual development fallback:

```bash
python -m remote --hashi-root "$(pwd)" --no-tls --discovery lan
```

### Windows

```powershell
.\bin\hashi_remote_ctl.ps1 enable
.\bin\hashi_remote_ctl.ps1 status
.\bin\hashi_remote_ctl.ps1 doctor
```

The default scheduled-task name is also per instance (for example,
`HashiRemote-hashi1`). Pass `-TaskName` only when an explicit override is
required.

Keep the default `security.max_terminal_level: "L2_WRITE"` unless you
intentionally want remote HASHI rescue start. To enable rescue start before a
core outage, set:

```yaml
security:
  max_terminal_level: "L3_RESTART"
```

Only use `L3_RESTART` on trusted LAN/Tailscale machines.

### Opt out

Existing installs can opt out of default-on Remote:

```yaml
lifecycle:
  remote_enabled: false
```

Operators can also run `/remote off`, which writes
`state/remote_disabled.json` under the configured Remote lifecycle root and prevents supervised restart until
`/remote on` clears it.

---

## Nagare Core (Developer Install)

Use this path when working on the extracted workflow engine directly.

### Python package

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

### Smoke verification

```bash
python -m nagare.cli --help
python -m nagare.cli run tests/fixtures/smoke_test.yaml --yes --silent --smoke-handler
```

The `--smoke-handler` flag is for packaging and CI validation. It avoids external model CLIs and writes deterministic artifacts locally.
The separately selected Nagare contract gate is documented in the
[Nagare release checklist](https://github.com/Bazza1982/HASHI/blob/main/docs/NAGARE_RELEASE_CHECKLIST.md);
the repository's entire contract directory is not a Nagare-only smoke test.
Run state defaults to `flow/runs/` below the current directory. Pass
`--runs-root <directory>` to relocate it. Pass `--repo-root <directory>` when
relative `agent_md` paths should resolve against a different trusted checkout.

## nagare-viz

```bash
cd nagare-viz
npm ci
npm run build
```

The current release gate for `nagare-viz` is a clean production build.

## Terminal and local connection

Run `hashi help`, `hashi help tui` or `hashi help instance create` for syntax.
Use `hashi status --all`, `hashi status --check`, `hashi doctor`,
`hashi logs --lines 100`, and `hashi version` for inspection. `hashi tui
--attach-only` connects without starting or creating an instance. `start` and
`stop` accept `--timeout 1..300`; a timeout never force-kills a process.
`hashi instance default` reads the current default; `hashi instance unbind`
removes only the current directory binding. Completion scripts are printed by
`hashi completion powershell|bash|zsh|fish` and are not installed automatically. Completions query the command parser for the current subcommand, option and value; they do not offer unrelated destructive flags.

For first connection, interactive `hashi` asks for one detected CLI or one API
provider. Confirm a minimal adapter call, then enter Hashiko's TUI conversation.
API credentials use masked local input and configure HER v2 internally.
`/connect` opens the same local repair page when a model is unavailable.
Telegram can be skipped; its optional local page requires a Bot Token and your
numeric user ID. Do not paste credentials into ordinary chat.

Before replacing a legacy npm 1.0.1 entry, back up the installed program and
verify where its instance data resides. Installation, stopped-instance program
adoption, and actual running generations remain separate. Replacing the npm
entry does not authorize stopping or migrating a live instance.
