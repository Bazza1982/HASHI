# HASHI Portable Windows x64

This builder produces an allowlisted, self-contained Windows directory image
for a USB drive whose total capacity is 957,000,000 bytes. It does not write to
or format a USB device.

The generated image also provides a machine-level local runtime layer.
The first TUI or Workbench start requests administrator approval, expands a
compact small-file payload, streams the remaining large files, precompiles
Python bytecode, verifies every installed source file, and atomically activates
the runtime under Windows ProgramData. The user sees bilingual English/zh-CN
guidance, four plain-language stages, real percentage/byte/file progress, and a
short notice when a stage may take several minutes. The private Python and Node
runtimes are never installed globally and the system PATH is not changed.

Only immutable program/runtime files enter the local runtime directory. API
keys, Sessions, Workspace, configuration, browser profile, and all other user
data remain on the USB. If setup fails, HASHI does not launch from an incomplete
installation; the original non-elevated launcher offers Retry or Exit and points
to the setup log. A complete expanded program copy remains available for
explicitly selected USB execution and backward-compatible images. The generated
root includes explicit install/uninstall launchers, and the runtime also
registers a removable entry in Windows Installed Apps. Every generated USB has
a random 128-bit portable-instance identity. Its ProgramData directory,
ownership marker, running processes, and Installed Apps key are all scoped to
that identity. The uninstaller always requires matching identity and ownership
markers, cross-checks bundle and Windows registration metadata when present,
and fails closed on any mismatch; it never recursively deletes the shared
product root or another portable instance's directory. During an upgrade from
the earlier shared-cache layout, setup removes the obsolete cache and its
unsafe shared uninstall entry only after its marker, bundle, source volume,
required files, directory shape, and Windows registration all match the current
USB. An unknown or ambiguous legacy layout stops setup without deleting it.
`Stop_HASHI.bat` closes this instance's runtime, TUI launcher, and dedicated
browser-profile processes, and reports safe ejection only after they are gone.

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
