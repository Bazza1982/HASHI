#!/usr/bin/env bash
set -euo pipefail

# WSLg display env
export DISPLAY=:0

# Create private XDG runtime dir (dbus requires 0700, not world-writable)
export XDG_RUNTIME_DIR="$HOME/.xdg-runtime"
mkdir -p "$XDG_RUNTIME_DIR/pulse"
chmod 700 "$XDG_RUNTIME_DIR"

# Bridge WSLg sockets into private runtime dir
ln -snf /mnt/wslg/runtime-dir/wayland-0 "$XDG_RUNTIME_DIR/wayland-0"
ln -snf /mnt/wslg/runtime-dir/pulse/native "$XDG_RUNTIME_DIR/pulse/native"

export WAYLAND_DISPLAY=wayland-0
export PULSE_SERVER="unix:$XDG_RUNTIME_DIR/pulse/native"

mkdir -p /tmp/chrome-wsl-profile

exec dbus-run-session -- google-chrome \
  --no-sandbox \
  --ozone-platform=wayland \
  --enable-features=UseOzonePlatform \
  --disable-gpu \
  --disable-software-rasterizer \
  --user-data-dir=/tmp/chrome-wsl-profile \
  "$@"
