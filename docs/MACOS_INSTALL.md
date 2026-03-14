# HASHI on macOS — Installation Guide

## Minimum Requirements
- macOS 12.0 (Monterey) or later
- Apple Silicon (M1/M2/M3) or Intel Mac
- 4 GB RAM minimum, 8 GB recommended

## Step 1: Install Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

After install, follow the instructions to add brew to your PATH
(Apple Silicon: `eval "$(/opt/homebrew/bin/brew shellenv)"`)

## Step 2: Install System Dependencies

```bash
brew install python@3.11 node ffmpeg
```

Optional for WhatsApp support:
```bash
brew install go
```

## Step 3: Clone HASHI

```bash
git clone https://github.com/Bazza1982/hashi2.git
cd hashi2
```

## Step 4: Run Onboarding

```bash
python3 onboarding/onboarding_main.py
```

This will:
- Create a Python virtual environment
- Install Python dependencies
- Guide you through agent and backend setup

## Step 5: Start HASHI

```bash
./bin/bridge-u.sh
```

## TTS / Voice on macOS

HASHI will automatically use macOS native TTS (`say` command).
No extra installation required.

For higher quality cloud TTS:
```bash
pip install edge-tts
```
Then in HASHI settings, change voice provider to `edge`.

## Troubleshooting

### `say` command not found
This should never happen on macOS. If it does:
```bash
which say  # should return /usr/bin/say
```

### `node` not found after install
```bash
# Apple Silicon
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
source ~/.zprofile

# Intel
echo 'export PATH="/usr/local/bin:$PATH"' >> ~/.zprofile
source ~/.zprofile
```

### Workbench won't open in Safari
Use Chrome or Firefox. Safari on macOS 12 has known WebSocket issues with Vite HMR.