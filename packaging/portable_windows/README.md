# HASHI Portable Windows x64

This builder produces an allowlisted, self-contained Windows directory image
for a USB drive whose total capacity is 957,000,000 bytes. It does not write to
or format a USB device.

The generated image also provides a machine-level local acceleration layer.
The first TUI or Workbench start requests administrator approval, expands a
compact small-file payload, streams the remaining large files, precompiles
Python bytecode, verifies every installed source file, and atomically activates
the cache under Windows ProgramData. There is no fixed ten-minute installation
cutoff. The private Python and Node runtimes are never installed globally and
the system PATH is not changed.

Only immutable program/runtime files enter the local cache. API keys, Sessions,
Workspace, configuration, browser profile, and all other user data remain on
the USB. A complete expanded program copy remains on the USB as fallback when
elevation is cancelled or installation fails. The generated root includes
explicit install/uninstall launchers, and the cache also registers a removable
entry in Windows Installed Apps.

The image contains HER v2, official DeepSeek defaults, configurable Qwen via
the official DashScope OpenAI-compatible endpoint, TUI, the complete browser
Workbench, Remote/LAN/HChat, Scheduler, Nagare, Superloops, browser/desktop
tools, Tesseract OCR, FFmpeg, Edge TTS, and one Chinese Piper voice. It uses the
host Edge or Chrome and does not bundle Electron or Chromium.

Remote discovery remains visible on the LAN. Pairing is approved in one click,
then protected operations require the issued bearer token, which expires after
seven days; the LAN itself is not treated as authenticated.

Build from the HASHI repository root:

```bash
python3 packaging/portable_windows/build.py
```

The source `secrets.json` must contain `deepseek_api_key`. Only the DeepSeek,
optional DashScope/OpenRouter, and Remote shared credentials are copied. Secret
values are never printed. The generated Workbench admin token is unique to the
image.

The build fails closed if the result exceeds 957,000,000 bytes or if a CLI
Engine adaptor/package manager is present. Every downloaded asset is pinned by
SHA-256, Python dependencies are hash locked, and `SHA256SUMS.txt` covers the
expanded image. Capacity is checked conservatively using 32 KiB allocation
units (including directory overhead); NTFS with its default 4 KiB allocation
unit is recommended for the target drive.
