# HASHI — Installation Guide (Windows / macOS / Linux)

> This is the **single source of truth** for installing and running HASHI.
> 
> Scope: developer/local installs (Git clone), multi-instance friendly.

---

## Contents

- [Windows](#windows)
- [macOS](#macos)
- [Linux (native) / WSL2](#linux-native--wsl2)
- [Python dependency profiles](#python-dependency-profiles)
- [Multi-instance ports](#multi-instance-ports)
- [Hashi Remote](#hashi-remote)

---

## Windows

### Prerequisites
- Windows 10/11
- CPython 3.12.13 (approved Core; source compatibility is 3.12)
- Node.js + npm only when developing the optional Nagare visual editor

### Install
1) Clone repo
2) Create venv and install Python deps (if required by your workflow)

For a self-contained Windows handoff, use the allowlisted Portable Windows
builder described in [`packaging/portable_windows/README.md`](../packaging/portable_windows/README.md).
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
- Homebrew

### Install
1) Install Homebrew
2) Install approved CPython 3.12.13; install Node.js only for Nagare editor work
3) Clone repo
4) Install dependencies

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
- Node.js + npm only for Nagare editor work

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

Example (conceptual):
- HASHI2: `workbench_port=18802`
- HASHI9: `workbench_port=18819`

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
`<HASHI_ROOT>/state/remote_disabled.json` and prevents supervised restart until
`/remote on` clears it.

---

## Nagare Core (Developer Install)

Use this path when working on the extracted workflow engine directly.

### Python package

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .[test]
```

### Smoke verification

```bash
pytest -q tests/contract
python -m nagare.cli run tests/fixtures/smoke_test.yaml --yes --silent --smoke-handler
```

The `--smoke-handler` flag is for packaging and CI validation. It avoids external model CLIs and writes deterministic artifacts locally.
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
