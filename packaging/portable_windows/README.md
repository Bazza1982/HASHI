# HASHI Portable Windows x64

This builder produces an allowlisted, self-contained Windows installation
bundle for a USB drive whose total capacity is 957,000,000 bytes. It does not
write to or format a USB device.

The USB is installation and transfer media, not the runtime disk. The user
runs `Install_HASHI_On_This_PC.bat`, approves Windows administrator access, and
the installer copies and verifies the complete bundle—including program,
private Python runtime, configuration, secrets, and current user data—to:

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

The installer creates two Windows shortcuts on the invoking user's desktop:
`Start HASHI` and `Stop HASHI`. Each shortcut targets
the corresponding batch launcher inside the local installation, so it cannot
become stale after the USB is removed. First installation reports real copy and
verification percentages. After success, the same elevated visible window
shows “Press any key to launch HASHI / 按任意键启动 HASHI” and opens the default
TUI without another menu. HASHI and all child runtime processes inherit the
administrator token. Installation success and subsequent startup failure are
reported independently.

Local API discovery is strict. HASHI always binds its Backend API to
`127.0.0.1`, prefers the configured port, and asks Windows for a free port if
that port is occupied. Once health and instance identity are verified, the
launcher atomically writes PID, process start time, actual API port, Portable
identity, HASHI instance identity, build identity, and a random launch nonce to
`data\state\local-endpoint.json`. TUI, Diagnose, and Stop use that
record; they do not scan adapters, guess WSL gateways, or fall back to a
`172.x` address. Logs are always resolved from the local copy at
`data\logs\bridge.log`.

`Uninstall_HASHI_From_This_PC.bat` must be run from the original matching USB.
It requests confirmation, stops only processes whose executable or dedicated
browser profile belongs to the validated local installation, removes the two
known shortcuts, and deletes only the exact marked local directory. A marker,
Portable identity, build identity, target path, or reparse-point mismatch stops
the uninstall without deletion. Uninstall permanently deletes the local
conversations, settings, logs, and plaintext API keys; it does not alter the
USB bundle.

The image contains HER v2, official DeepSeek defaults, configurable Qwen via
the official DashScope OpenAI-compatible endpoint, TUI, the HASHI Backend API,
Remote/LAN/HChat, Scheduler, Nagare, Superloops, browser/desktop
tools, Tesseract OCR, FFmpeg, Edge TTS, and one Chinese Piper voice. It uses the
host Edge or Chrome and does not bundle Electron or Chromium. There is no CLI
Engine, local LLM, semantic vector runtime, system Python or Node installation,
global PATH change, Windows service, registry installation, or ProgramData
runtime cache. The retired Workbench frontend and Node server are not included.

The launched runtime is Windows-native-only: its isolated process environment
includes the system Windows PowerShell directory explicitly, while Bash/WSL
shell selection and direct WSL launcher execution are blocked. A missing native
Windows executor therefore fails visibly instead of crossing into WSL.

Remote discovery remains visible on the LAN. Pairing is approved in one click,
then protected operations require the issued bearer token, which expires after
seven days; the LAN itself is not treated as authenticated.

Build from the HASHI repository root:

```bash
python3 packaging/portable_windows/build.py \
  --expected-revision <full-commit-id> \
  --expected-tree <full-tree-id>
```

The expected Git identities are optional for local development and required
for a release build. Before downloading or staging an image, the builder
checks them against the clean source worktree and verifies that the Portable
hash lock contains every dependency from the runtime policy's standard lock
at the exact approved version. It checks the source identity again before the
staging directory is published.

The source `secrets.json` must contain `deepseek_api_key`. Only the DeepSeek,
optional DashScope/OpenRouter, and Remote shared credentials are copied. Secret
values are never printed. The generated Backend API admin token (stored under
its compatibility key) and 128-bit Portable identity are unique to the image.

The build fails closed if the result exceeds 957,000,000 bytes or if a CLI
Engine adaptor/package manager is present. Every downloaded asset is pinned by
SHA-256, Python dependencies are hash locked, and `SHA256SUMS.txt` covers the
fresh image. Because `data` is intentionally mutable after publication, the PC
installer uses static manifest hashes for non-data files and live source/dest
hash comparison for `data`. Capacity is checked conservatively using 32 KiB
allocation units; NTFS with its default 4 KiB allocation unit is recommended.

Only Git-tracked files from the explicit source allowlist enter `app/hashi`.
The allowlist includes `__main__.py`, `pyproject.toml`, the standard dependency
lock named by `[tool.hashi.runtime]`, and every protected Core source required
to recompute the runtime fingerprint. The bundled Python executes the shared
runtime-contract checker after dependencies are installed and before the image
can be published.
Untracked development state, logs, local secrets, project-private workflows,
instance-local Skills, and generated workflow runs cannot leak into a newly
built image. The tracked HASHI input must be clean, and its revision is checked
again before publication.
