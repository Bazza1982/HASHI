# HASHI Portable Windows x64

This builder produces an allowlisted, self-contained Windows installation
bundle for a USB drive whose total capacity is 957,000,000 bytes. It does not
write to or format a USB device.

The USB is installation and transfer media, not the runtime disk. Its visible
root launchers and end-user guide use clear Simplified Chinese names. The user
runs `安装_HASHI_到本机.bat`, approves Windows administrator access, and
the installer copies and verifies the complete bundle—including program,
private Python/Node runtimes, configuration, secrets, and current user data—to:

```text
C:\HASHI-Portable\
```

Copying uses a unique staging directory on `C:`. The installer verifies the
static program image against `SHA256SUMS.txt`, verifies mutable `data` files
against their live USB sources, writes an identity-bound ownership marker, and
only then atomically moves the staging directory into place. It never
overwrites an existing directory. If a valid matching installation already
exists, no files are copied; the desktop shortcuts are repaired and the local
installation is launched. An invalid, incomplete, linked, or differently owned
destination fails closed.

The installer creates three Chinese Windows shortcuts on the invoking user's
desktop: `启动 HASHI（聊天界面）`, `启动 HASHI（工作台）`, and `停止 HASHI`.
Each shortcut targets the corresponding batch launcher inside the local
installation, so it cannot become stale after the USB is removed. Legacy
English shortcuts are removed during repair and uninstall. First installation
reports real copy and verification percentages. After success, the same
elevated visible window shows “Press any key to launch HASHI / 按任意键启动
HASHI” and opens the default TUI without another menu. HASHI and all child
runtime processes inherit the administrator token. Installation success and
subsequent startup failure are reported independently.

Local API discovery is strict. HASHI always binds its Backend API to
`127.0.0.1`, prefers the configured port, and asks Windows for a free port if
that port is occupied. Once health and instance identity are verified, the
launcher atomically writes PID, process start time, actual API port, Portable
identity, HASHI instance identity, build identity, and a random launch nonce to
`data\state\local-endpoint.json`. TUI, Workbench, Diagnose, and Stop use that
record; they do not scan adapters, guess WSL gateways, or fall back to a
`172.x` address. Workbench's browser UI port is selected the same way and added
to the record. Logs are always resolved from the local copy at
`data\logs\bridge.log`.

`从本机卸载_HASHI.bat` must be run from the original matching USB.
It requests confirmation, stops only processes whose executable or dedicated
browser profile belongs to the validated local installation, removes the three
known shortcuts, and deletes only the exact marked local directory. A marker,
Portable identity, build identity, target path, or reparse-point mismatch stops
the uninstall without deletion. Uninstall permanently deletes the local
conversations, settings, logs, and plaintext API keys; it does not alter the
USB bundle.

The image contains HER v2, official DeepSeek defaults, configurable Qwen via
the official DashScope OpenAI-compatible endpoint, TUI, the complete browser
Workbench, Remote/LAN/HChat, Scheduler, Nagare, Superloops, browser/desktop
tools, Tesseract OCR, FFmpeg, Edge TTS, and one Chinese Piper voice. It uses the
host Edge or Chrome and does not bundle Electron or Chromium. There is no CLI
Engine, local LLM, semantic vector runtime, system Python/Node installation,
global PATH change, Windows service, registry installation, or ProgramData
runtime cache.

The launched runtime is Windows-native-only: its isolated process environment
includes the system Windows PowerShell directory explicitly, while Bash/WSL
shell selection and direct WSL launcher execution are blocked. A missing native
Windows executor therefore fails visibly instead of crossing into WSL.

Remote discovery remains visible on the LAN. Pairing is approved in one click,
then protected operations require the issued bearer token, which expires after
seven days; the LAN itself is not treated as authenticated.

Build from the HASHI repository root:

```bash
python3 packaging/portable_windows/build.py
```

The source `secrets.json` must contain `deepseek_api_key`. Only the DeepSeek,
optional DashScope/OpenRouter, and Remote shared credentials are copied. Secret
values are never printed. The generated Workbench admin token and 128-bit
Portable identity are unique to the image.

The build fails closed if the result exceeds 957,000,000 bytes or if a CLI
Engine adaptor/package manager is present. Every downloaded asset is pinned by
SHA-256, Python dependencies are hash locked, and `SHA256SUMS.txt` covers the
fresh image. Because `data` is intentionally mutable after publication, the PC
installer uses static manifest hashes for non-data files and live source/dest
hash comparison for `data`. Capacity is checked conservatively using 32 KiB
allocation units; NTFS with its default 4 KiB allocation unit is recommended.

Only Git-tracked files from the explicit source allowlist enter `app/hashi`.
Untracked development state, logs, local secrets, and generated workflow runs
cannot leak into a newly built image. Tracked HASHI and Workbench inputs must be
clean, and both revisions are checked again before publication.
