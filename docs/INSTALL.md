# HASHI — Installation Guide (Windows / macOS / Linux)

> This is the **single source of truth** for installing and running HASHI.
> 
> Scope: developer/local installs (Git clone), multi-instance friendly.

---

## Contents

- [Windows](#windows)
- [macOS](#macos)
- [Linux (native) / WSL2](#linux-native--wsl2)
- [Multi-instance ports](#multi-instance-ports)
- [Hashi Remote](#hashi-remote)

---

## Windows

### Prerequisites
- Windows 10/11
- Python (project-supported version)
- Node.js + npm

### Install
1) Clone repo
2) Create venv and install Python deps (if required by your workflow)
3) Install workbench deps

### Run
- Preferred: use the unified launcher `bin/bridge-u.bat`.
- Workbench:
  - API server (Node): `http://127.0.0.1:3001`
  - UI (Vite dev): `http://127.0.0.1:5173`

---

## macOS

> macOS support has been tested.

### Prerequisites
- macOS 12.0+ (Monterey) recommended
- Homebrew

### Install
1) Install Homebrew
2) Install Python + Node
3) Clone repo
4) Install dependencies

---

## Linux (native) / WSL2

### Prerequisites
- Ubuntu 22.04+ recommended
- Python3 + venv
- Node.js + npm

### Run
- Preferred: `./bin/bridge-u.sh --resume-last --workbench`

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

New installs should run Hashi Remote as a default-on sidecar. `/remote on`
still works as a development fallback, but rescue-grade installs should use the
OS helper so Remote can survive HASHI core shutdown.

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
bin/hashi-remote-ctl.sh install
bin/hashi-remote-ctl.sh start
bin/hashi-remote-ctl.sh status
```

For manual development fallback:

```bash
python -m remote --hashi-root "$(pwd)" --no-tls --discovery lan
```

### Windows

```powershell
.\bin\hashi_remote_ctl.ps1 install
.\bin\hashi_remote_ctl.ps1 start
.\bin\hashi_remote_ctl.ps1 status
.\bin\hashi_remote_ctl.ps1 doctor
```

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

## nagare-viz

```bash
cd nagare-viz
npm ci
npm run build
```

The current release gate for `nagare-viz` is a clean production build.
