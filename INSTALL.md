# HASHI Installation Guide

HASHI is designed to run as a **local application**, not as a Python package installed to site-packages. This guide covers the recommended installation methods.

---

## Recommended: Git Clone Installation

### Step 1: Clone the Repository

```bash
git clone https://github.com/Bazza1982/HASHI.git
cd hashi
```

### Step 2: Install Python Dependencies

**Using pip:**
```bash
pip install -r requirements.txt
```

**Or create a virtual environment (recommended):**
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Step 3: Run Onboarding

```bash
python onboarding/onboarding_main.py
```

The onboarding program will:
- Detect your installed CLI backends (Gemini, Claude, Codex)
- Guide you through creating your first agent
- Set up Telegram/WhatsApp connections (optional)
- Launch the Workbench UI

### Step 4: Launch HASHI

**Linux** (macOS untested):
```bash
./bin/bridge-u.sh
```

**Windows:**
```cmd
bin\bridge-u.bat
```

Or from the repository root on any platform:
```bash
python main.py
```

---

## Alternative: Download Release

1. Download the latest release from [GitHub Releases](https://github.com/Bazza1982/HASHI/releases)
2. Extract the archive
3. Follow Steps 2-4 above

---

## npm Global Installation (Experimental)

You can install HASHI as a global npm package:

```bash
npm install -g hashi-bridge
```

This will:
- Install the `hashi` and `hashi-onboard` commands globally
- Check for Python 3.10+ and pip
- Prompt you to install Python dependencies

**Note:** The npm package is a lightweight wrapper that requires Python to be installed separately.

### Usage after npm install:

```bash
hashi-onboard        # Run onboarding
hashi                # Launch HASHI
hashi --help         # Show help
```

---

## Prerequisites

### Required
- **Python 3.10 or higher**
- **pip** (Python package installer)

### AI Backend (at least one)
- [Gemini CLI](https://github.com/google/generative-ai-js) — `gemini`
- [Claude Code](https://github.com/anthropics/claude-code) — `claude`
- [Codex CLI](https://github.com/openai/codex-cli) — `codex`
- **Or** an [OpenRouter](https://openrouter.ai/) API key

### Optional
- **Node.js 18+** (for Workbench UI)
- **Telegram Bot Token** (for Telegram transport)
- **WhatsApp** (for WhatsApp transport via whatsapp-web.js)

---

## Verifying Installation

After installation, verify that HASHI is set up correctly:

```bash
# Check Python version
python --version  # Should be 3.10 or higher

# Check if dependencies are installed
pip show python-telegram-bot httpx aiohttp pillow

# Check if CLI backends are available
gemini --version    # If using Gemini
claude -v           # If using Claude
codex --version     # If using Codex
```

---

## Troubleshooting

### "Python not found"
- Install Python from [python.org](https://www.python.org/downloads/)
- Ensure Python is in your PATH

### "pip not found"
- Install pip: [pip installation guide](https://pip.pypa.io/en/stable/installation/)

### "No CLI backends detected"
- Install at least one: Gemini CLI, Claude CLI, or Codex CLI
- Or prepare an OpenRouter API key

### "Permission denied" (Linux)
Make launch scripts executable:
```bash
chmod +x bridge-u.sh
chmod +x cli.js
chmod +x onboard-cli.js
```

---

## Uninstallation

### If installed via git clone:
Simply delete the `hashi/` directory.

### If installed via npm:
```bash
npm uninstall -g hashi-bridge
```

---

## Next Steps

After installation:
1. Run `hashi-onboard` or `python onboarding/onboarding_main.py`
2. Follow the guided setup
3. Read the [README](README.md) for usage instructions
4. Join the community: [GitHub Discussions](https://github.com/Bazza1982/HASHI/discussions)

---

**Need Help?**  
Open an issue: [GitHub Issues](https://github.com/Bazza1982/HASHI/issues)
