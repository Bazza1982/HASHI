#!/bin/bash
# Build a self-contained, integrity-checked HASHI image on a macOS volume.
# Run this from a clean Git checkout on a connected macOS build machine.

set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_VERSION="3.12.13"
PBS_DATE="20260303"
PYTHON_SHA256_AARCH64="377234f346fce41b6d3112b5ead89cb6af2d5596244f9edc1a739065770dde1f"

BUILD_ROOT=""
DOWNLOAD_ROOT=""

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

cleanup() {
    result=$?
    if [ -n "$BUILD_ROOT" ] && [ -d "$BUILD_ROOT" ]; then
        rm -rf -- "$BUILD_ROOT"
    fi
    if [ -n "$DOWNLOAD_ROOT" ] && [ -d "$DOWNLOAD_ROOT" ]; then
        rm -rf -- "$DOWNLOAD_ROOT"
    fi
    return "$result"
}

trap cleanup EXIT
trap 'exit 130' HUP INT TERM

for tool in curl git shasum tar; do
    command -v "$tool" >/dev/null 2>&1 || fail "Required build tool is missing: $tool"
done

repo_root="$(git -C "$SOURCE_ROOT" rev-parse --show-toplevel 2>/dev/null)" || \
    fail "The source directory is not a Git checkout."
[ "$repo_root" = "$SOURCE_ROOT" ] || fail "Run the builder from the HASHI repository root."

dirty="$(git -C "$SOURCE_ROOT" status --porcelain=v1 --untracked-files=normal)"
[ -z "$dirty" ] || fail "The source checkout has uncommitted files. Commit or remove them before packaging."
revision="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"

if [ -n "${1:-}" ]; then
    VOLUME_ROOT="${1%/}"
else
    echo "Looking for writable volumes under /Volumes ..."
    VOLUMES=()
    while IFS= read -r volume; do
        case "$volume" in
            /Volumes/Macintosh\ HD*|/Volumes/Recovery*|/Volumes/VM*|/Volumes/Preboot*)
                continue
                ;;
        esac
        [ -d "$volume" ] && [ -w "$volume" ] && VOLUMES+=("${volume%/}")
    done < <(ls -d /Volumes/*/ 2>/dev/null || true)

    if [ "${#VOLUMES[@]}" -eq 0 ]; then
        fail "No writable external volume found. Supply one explicitly: bash mac/prepare_usb.sh /Volumes/MyUSB"
    elif [ "${#VOLUMES[@]}" -eq 1 ]; then
        VOLUME_ROOT="${VOLUMES[0]}"
    else
        echo "Multiple writable volumes found. Choose one:"
        select volume in "${VOLUMES[@]}"; do
            [ -n "$volume" ] || continue
            VOLUME_ROOT="$volume"
            break
        done
    fi
fi

[ -d "$VOLUME_ROOT" ] || fail "Destination volume does not exist: $VOLUME_ROOT"
[ -w "$VOLUME_ROOT" ] || fail "Destination volume is not writable: $VOLUME_ROOT"

TARGET="$VOLUME_ROOT/HASHI"
[ ! -e "$TARGET" ] || fail "Destination already exists and will not be overwritten: $TARGET"

[ "$(uname -s)" = "Darwin" ] || fail "This portable builder must run on macOS."
machine_arch="$(uname -m)"
case "$machine_arch" in
    arm64)
        pbs_arch="aarch64"
        expected_sha256="$PYTHON_SHA256_AARCH64"
        ;;
    x86_64)
        fail "The portable image currently supports Apple Silicon only; use the source-install path on Intel Macs."
        ;;
    *)
        fail "Unsupported macOS architecture: $machine_arch"
        ;;
esac

python_asset="cpython-${PYTHON_VERSION}+${PBS_DATE}-${pbs_arch}-apple-darwin-install_only_stripped.tar.gz"
python_url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_DATE}/${python_asset}"

echo ""
echo "============================================================"
echo "  HASHI verified portable builder for macOS"
echo "  Source revision: $revision"
echo "  Architecture:    $machine_arch"
echo "  Destination:     $TARGET"
echo "============================================================"
echo ""
read -r -p "Type YES to build a new image: " confirmation
[ "$confirmation" = "YES" ] || { echo "Cancelled."; exit 0; }

BUILD_ROOT="$VOLUME_ROOT/.HASHI-build-$$"
[ ! -e "$BUILD_ROOT" ] || fail "Temporary build path already exists: $BUILD_ROOT"
mkdir -m 700 "$BUILD_ROOT"
DOWNLOAD_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/hashi-macos-download.XXXXXX")"

echo "[1/5] Copying the committed source tree..."
git -C "$SOURCE_ROOT" archive --format=tar HEAD | tar -xf - -C "$BUILD_ROOT"

echo "[2/5] Downloading the pinned Python $PYTHON_VERSION runtime..."
python_archive="$DOWNLOAD_ROOT/$python_asset"
curl --fail --location --proto '=https' --tlsv1.2 \
    --retry 3 --output "$python_archive" "$python_url"
actual_sha256="$(shasum -a 256 "$python_archive" | awk '{print $1}')"
[ "$actual_sha256" = "$expected_sha256" ] || \
    fail "Python archive checksum mismatch: expected $expected_sha256, got $actual_sha256"

while IFS= read -r member; do
    case "$member" in
        python/*) ;;
        *) fail "Python archive contains an unexpected path: $member" ;;
    esac
    case "/$member/" in
        */../*) fail "Python archive contains an unsafe path: $member" ;;
    esac
done < <(tar -tzf "$python_archive")

echo "[3/5] Extracting the verified runtime..."
tar -xzf "$python_archive" -C "$BUILD_ROOT"
PYTHON_EXE="$BUILD_ROOT/python/bin/python3"
[ -x "$PYTHON_EXE" ] || fail "The verified archive did not contain python/bin/python3"
"$PYTHON_EXE" -c \
    "import sys; raise SystemExit(0 if sys.version_info[:3] == (3, 12, 13) and sys.platform == 'darwin' else 1)" || \
    fail "The extracted interpreter does not match the approved macOS runtime."

echo "[4/5] Installing the pinned standard dependency generation..."
"$PYTHON_EXE" -m pip install --disable-pip-version-check --only-binary=:all: \
    -r "$BUILD_ROOT/constraints/standard-py312.lock"
"$PYTHON_EXE" "$BUILD_ROOT/scripts/check_runtime_contract.py" --code-root "$BUILD_ROOT"

echo "[5/5] Finalising the image..."
mkdir -p "$BUILD_ROOT/logs"
find "$BUILD_ROOT/mac" -type f \( -name '*.command' -o -name '*.sh' \) \
    -exec chmod +x {} +
printf '%s\n' \
    "HASHI portable macOS image" \
    "Source revision: $revision" \
    "Python asset: $python_asset" \
    "Python SHA-256: $expected_sha256" \
    > "$BUILD_ROOT/PORTABLE_BUILD_INFO.txt"

mv "$BUILD_ROOT" "$TARGET"
BUILD_ROOT=""

echo ""
echo "============================================================"
echo "  Portable image built successfully: $TARGET"
echo "  Start: $TARGET/mac/start_tui.command"
echo "  No ignored or untracked source files were copied."
echo "============================================================"
