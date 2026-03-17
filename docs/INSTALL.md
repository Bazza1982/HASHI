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
