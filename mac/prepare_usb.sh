#!/bin/bash
# ============================================================
# HASHI9 USB Packager for macOS
# Builds a fully self-contained HASHI9 on a USB drive.
# Run this ONCE on the host Mac before distributing the USB.
#
# Usage:
#   bash prepare_usb.sh                    # auto-detect USB
#   bash prepare_usb.sh /Volumes/MyUSB    # specify mount point
#
# Requirements: internet connection (downloads Python + packages)
# ============================================================

set -euo pipefail

SOURCE="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_VERSION="3.13.2"
PBS_DATE="20250317"

# ── Detect target ────────────────────────────────────────────
if [ -n "${1:-}" ]; then
    TARGET="$1/HASHI9"
else
    echo "Looking for USB drives under /Volumes ..."
    VOLUMES=()
    while IFS= read -r vol; do
        # Skip system volumes
        case "$vol" in
            /Volumes/Macintosh\ HD*|/Volumes/Recovery*|/Volumes/VM*|/Volumes/Preboot*) continue ;;
        esac
        VOLUMES+=("$vol")
    done < <(ls -d /Volumes/*/ 2>/dev/null || true)

    if [ ${#VOLUMES[@]} -eq 0 ]; then
        echo "ERROR: No external volumes found. Insert USB drive or specify path:"
        echo "  bash prepare_usb.sh /Volumes/MyUSB"
        exit 1
    elif [ ${#VOLUMES[@]} -eq 1 ]; then
        TARGET="${VOLUMES[0]%/}/HASHI9"
        echo "Found: ${VOLUMES[0]}"
    else
        echo "Multiple volumes found. Choose one:"
        select vol in "${VOLUMES[@]}"; do
            TARGET="${vol%/}/HASHI9"
            break
        done
    fi
fi

echo ""
echo "============================================================"
echo "  HASHI9 USB Packager for macOS"
echo "  Target: $TARGET"
echo "============================================================"
echo ""
read -p "Type YES to continue: " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
    echo "Cancelled."
    exit 0
fi

PYTHON_DIR="$TARGET/python"

# ── Step 1: Copy project files ───────────────────────────────
echo ""
echo "[1/5] Copying project files..."
mkdir -p "$TARGET"

rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='*.pyo' --exclude='build' \
    --exclude='dist' --exclude='*.spec' --exclude='logs' \
    --exclude='node_modules' --exclude='.idea' --exclude='.vscode' \
    --exclude='windows-packaging-smoke-home' \
    "$SOURCE/" "$TARGET/"

echo "   Done."

# ── Step 2: Download Python ──────────────────────────────────
echo ""
echo "[2/5] Downloading Python $PYTHON_VERSION (portable build)..."

if [ -f "$PYTHON_DIR/bin/python3" ]; then
    echo "   Python already present, skipping download."
else
    ARCH="$(uname -m)"  # arm64 or x86_64
    PBS_FILE="cpython-${PYTHON_VERSION}+${PBS_DATE}-${ARCH}-apple-darwin-install_only_stripped.tar.gz"
    PBS_URL="https://github.com/indygreg/python-build-standalone/releases/download/${PBS_DATE}/${PBS_FILE}"

    TMP_DIR="$(mktemp -d)"
    echo "   Downloading $PBS_FILE ..."
    curl -L --progress-bar "$PBS_URL" -o "$TMP_DIR/$PBS_FILE"

    echo "   Extracting..."
    mkdir -p "$PYTHON_DIR"
    tar -xzf "$TMP_DIR/$PBS_FILE" -C "$PYTHON_DIR" --strip-components=1
    rm -rf "$TMP_DIR"
    echo "   Done."
fi

PYTHON_EXE="$PYTHON_DIR/bin/python3"

# ── Step 3: Upgrade pip ──────────────────────────────────────
echo ""
echo "[3/5] Upgrading pip..."
"$PYTHON_EXE" -m pip install --upgrade pip --quiet
echo "   Done."

# ── Step 4: Install packages ─────────────────────────────────
echo ""
echo "[4/5] Installing Python packages (this may take a few minutes)..."
"$PYTHON_EXE" -m pip install \
    "python-telegram-bot>=20.0" \
    "httpx>=0.24.0" \
    "aiohttp>=3.8.0" \
    "pillow>=9.0.0" \
    "rich>=13.0.0" \
    "textual>=0.50.0" \
    "edge-tts>=6.0.0" \
    "psutil>=5.9.0" \
    --quiet

echo "   Done."

# ── Step 5: Finalise ─────────────────────────────────────────
echo ""
echo "[5/5] Finalising..."

# Clear runtime data that may have been copied
rm -rf "$TARGET/logs"
rm -f "$TARGET/workspaces/hashiko/bridge_memory.sqlite"
rm -f "$TARGET/workspaces/hashiko/transcript.jsonl"
rm -f "$TARGET/workspaces/hashiko/recent_context.jsonl"
: > "$TARGET/workspaces/onboarding_agent/conversation_log.jsonl" 2>/dev/null || true

# Create fresh logs dir
mkdir -p "$TARGET/logs"

# Make all .command and .sh scripts executable
find "$TARGET/mac" -name "*.command" -o -name "*.sh" | xargs chmod +x 2>/dev/null || true

echo "   Done."

echo ""
echo "============================================================"
echo "  USB package built successfully at $TARGET"
echo ""
echo "  To start HASHI9 on any Mac:"
echo "    Double-click:  HASHI9/mac/start_tui.command"
echo ""
echo "  NOTE: First run may need macOS Gatekeeper approval."
echo "  Right-click the .command file → Open → Open anyway."
echo "============================================================"
echo ""
